"""Shared event/state and notification tests; these are synthetic UI earcons.

No test below claims real speech, acoustic listening, native wx or NVDA.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import io
import json
import math
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
import wave

from carr_library.progress import (FILENAME, MAX_BYTES, ProgressHub, ProgressPreferences,
    batch_progress, current_progress, decode_preferences, encode_preferences,
    finish_result, load_preferences, report_progress, save_preferences)
from carr_library.progress_audio import (NotificationSchedule, NotificationWorker,
    cue_wav, progress_frequency, speech_text)


def pcm(event, volume=.25):
    with wave.open(io.BytesIO(cue_wav(event, volume=volume)), 'rb') as source:
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        rate, frames = source.getframerate(), source.getnframes()
        return rate, struct.unpack('<' + str(frames) + 'h', source.readframes(frames))


def measured_frequency(samples, rate):
    # Independent waveform measurement: interpolate rising zero crossings,
    # rather than consulting the production note list or frequency helper.
    crossings = [i - samples[i] / (samples[i + 1] - samples[i])
                 for i in range(len(samples) - 1)
                 if samples[i] <= 0 < samples[i + 1]]
    if len(crossings) < 3:
        raise AssertionError('The emitted cue has too few waveform cycles.')
    return (len(crossings) - 1) * rate / (crossings[-1] - crossings[0])


class ProgressStateTests(unittest.TestCase):
    def test_measured_percent_and_verified_completion(self):
        reporter = ProgressHub().start('Import', total=4, unit='files')
        self.assertEqual(reporter.event.percent, 0)
        for completed, expected in ((1, 25), (2, 50), (3, 75), (4, 99)):
            reporter.update(completed=completed)
            self.assertEqual(reporter.event.percent, expected)
        with self.assertRaises(ValueError):
            reporter.complete()
        reporter.complete(verified=True)
        self.assertEqual(reporter.event.percent, 100)
        with self.assertRaises(ValueError):
            reporter.update(completed=3)
        with self.assertRaises(FrozenInstanceError):
            reporter.event.completed = 0

    def test_known_work_cannot_be_skipped_or_regress(self):
        reporter = ProgressHub().start('Render', total=5)
        reporter.update(completed=2)
        for values in ({'completed': 1}, {'completed': -1},
                       {'completed': True}, {'completed': 6}, {'total': 10}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                reporter.update(**values)
        with self.assertRaises(ValueError):
            reporter.complete(verified=True)
        self.assertEqual(reporter.event.completed, 2)

    def test_changed_total_explained_and_eta_reset(self):
        clock = [0.]
        reporter = ProgressHub(clock=lambda: clock[0]).start('Index', total=10)
        self.assertIsNone(reporter.event.eta_seconds)
        clock[0] = 4.
        reporter.update(completed=2)
        self.assertEqual(reporter.event.eta_seconds, 16.)
        reporter.update(total=20, total_changed_reason='Ten additional selected files.')
        self.assertIsNone(reporter.event.eta_seconds)
        self.assertIn('Total changed', reporter.event.text)
        reporter.update(total=None, total_changed_reason='The converter cannot report its remaining units.')
        self.assertIsNone(reporter.event.percent)
        self.assertIsNone(reporter.event.eta_seconds)

    def test_unknown_pause_resume_failure_cancel_never_100(self):
        for terminal in ('fail', 'cancel'):
            reporter = ProgressHub().start('Unknown render duration')
            self.assertIsNone(reporter.event.percent)
            reporter.update(phase='Verifying completed chunks')
            reporter.pause('Checkpoint saved')
            self.assertEqual(reporter.event.transition, 'pause')
            with self.assertRaises(ValueError):
                reporter.complete(verified=True)
            reporter.resume()
            self.assertEqual(reporter.event.transition, 'resume')
            getattr(reporter, terminal)('Stopped')
            self.assertNotEqual(reporter.event.percent, 100)

    def test_known_total_zero_requires_completion_assertion(self):
        reporter = ProgressHub().start('Empty search', total=0)
        self.assertEqual(reporter.event.percent, 0)
        reporter.complete(verified=True)
        self.assertEqual(reporter.event.percent, 100)

    def test_separate_concurrent_subtasks_and_context(self):
        hub = ProgressHub()
        overall = hub.start('Assistant', total=2)
        child = overall.child('Audio chapter', total=4)
        with overall.bind():
            self.assertIs(current_progress(), overall)
            with child.bind():
                report_progress(completed=3, phase='Mixing')
            report_progress(completed=1)
        self.assertIsNone(current_progress())
        self.assertEqual(overall.event.percent, 50)
        self.assertEqual(child.event.percent, 75)
        self.assertEqual(child.event.parent_id, overall.operation_id)
        self.assertIsNone(report_progress(completed=100))
        def another():
            self.assertIsNone(current_progress())
            reporter = hub.start('Backup', total=1)
            reporter.update(completed=1)
            reporter.complete(verified=True)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(another).result()
        self.assertEqual(len(hub.snapshot()), 3)

    def test_bounded_snapshot_does_not_drop_active_work(self):
        hub = ProgressHub(maximum=2)
        first, second = hub.start('One'), hub.start('Two')
        for i in range(1000):
            first.update(message=f'Checkpoint {i}')
        self.assertEqual(len(hub.snapshot()), 2)
        with self.assertRaises(ValueError):
            hub.start('Three')
        second.cancel()
        hub.start('Three')
        self.assertIn(first.operation_id, {item.operation_id for item in hub.snapshot()})

    def test_media_activity_is_nested_and_exception_safe(self):
        hub = ProgressHub()
        with hub.media_activity():
            with hub.media_activity():
                self.assertTrue(hub.media_active)
            self.assertTrue(hub.media_active)
        self.assertFalse(hub.media_active)
        with self.assertRaises(RuntimeError):
            with hub.media_activity():
                raise RuntimeError('device gone')
        self.assertFalse(hub.media_active)

    def test_import_review_failure_and_cancel_are_not_complete(self):
        for counts, expected in (({'complete': 1, 'quarantined': 1}, 'paused'),
                                 ({'complete': 1, 'failed': 1}, 'failed'),
                                 ({'complete': 1, 'queued': 1}, 'paused')):
            reporter = ProgressHub().start('Import')
            batch = {'total': 2, 'counts': counts}
            batch_progress(reporter, batch)
            finish_result(reporter, batch)
            self.assertEqual(reporter.event.state, expected)
            self.assertNotEqual(reporter.event.percent, 100)
        for result, expected in (({'state': 'cancelled'}, 'cancelled'),
                                 ({'status': 'attention'}, 'paused'),
                                 ({'status': 'future-unknown-state'}, 'paused')):
            reporter = ProgressHub().start('Operation')
            finish_result(reporter, result)
            self.assertEqual(reporter.event.state, expected)

    def test_committed_typed_result_survives_late_cancellation_request(self):
        reporter = ProgressHub().start('Save')
        finish_result(reporter, {'state': 'complete'}, cancelled=True)
        self.assertEqual(reporter.event.percent, 100)
        reporter = ProgressHub().start('Untyped operation')
        finish_result(reporter, None, cancelled=True)
        self.assertEqual(reporter.event.state, 'cancelled')


class PreferencesTests(unittest.TestCase):
    def test_defaults_roundtrip_and_strict_structure(self):
        default = ProgressPreferences()
        self.assertEqual(default.mode, 'off')
        self.assertFalse(default.background)
        self.assertTrue(default.suppress_during_media)
        self.assertEqual(decode_preferences(encode_preferences(default)), default)
        good = json.loads(encode_preferences(default))
        variants = []
        for key, value in [('mode', 'network'), ('volume', True), ('volume', float('nan')),
                           ('volume', -1), ('interval_seconds', 0), ('muted', 1)]:
            changed = json.loads(encode_preferences(default))
            changed['preferences'][key] = value
            variants.append(json.dumps(changed).encode())
        variants.extend((b'{}', b'\xff', b' ' * (MAX_BYTES + 1),
                         encode_preferences(default).replace(b'"version": 2', b'"version": 2, "version": 2')))
        for raw in variants:
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                decode_preferences(raw)

    def test_atomic_settings_preserve_old_file_on_failed_replace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(load_preferences(root).mode, 'off')
            selected = ProgressPreferences(mode='beep', interval_seconds=3, volume=.2)
            save_preferences(root, selected)
            previous = (root / FILENAME).read_bytes()
            self.assertEqual(load_preferences(root), selected)
            with patch('carr_library.progress.os.replace', side_effect=OSError('blocked')):
                with self.assertRaises(OSError):
                    save_preferences(root, ProgressPreferences(mode='speech'))
            self.assertEqual((root / FILENAME).read_bytes(), previous)
            self.assertFalse(tuple(root.glob('.progress-*.tmp')))

    def test_malformed_preferences_are_retained(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / FILENAME).write_bytes(b'not JSON')
            with self.assertRaises(ValueError):
                load_preferences(root)
            self.assertEqual((root / FILENAME).read_bytes(), b'not JSON')

    @unittest.skipUnless(hasattr(os, 'link'), 'Hard links unavailable.')
    def test_hardlinked_preferences_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            original = root / 'other.json'
            original.write_bytes(encode_preferences(ProgressPreferences()))
            try:
                os.link(original, root / FILENAME)
            except OSError:
                self.skipTest('This filesystem does not support hard links.')
            with self.assertRaises(ValueError):
                load_preferences(root)
            with self.assertRaises(ValueError):
                save_preferences(root, ProgressPreferences(mode='beep'))


class EarconTests(unittest.TestCase):
    def test_actual_wave_pitch_at_0_25_50_75_100(self):
        measured = []
        for percent in (0, 25, 50, 75, 100):
            reporter = ProgressHub().start('Synthetic fixture', total=100)
            reporter.update(completed=percent)
            if percent == 100:
                reporter.complete(verified=True)
            rate, samples = pcm(reporter.event)
            tone = samples[-round(rate * .12):] if percent == 100 else samples
            measured.append(measured_frequency(tone[round(rate * .006):-round(rate * .006)], rate))
            self.assertAlmostEqual(measured[-1], 220 * 4 ** (percent / 100), delta=1.)
            self.assertLessEqual(max(abs(value) for value in samples), 4000)
            self.assertEqual(samples[0], 0)
            self.assertEqual(samples[-1], 0)
            expected = .225 if percent == 100 else .09
            self.assertAlmostEqual(len(samples) / rate, expected, delta=3 / rate)
        self.assertEqual(measured, sorted(measured))

    def test_volume_scaling_silence_and_distinct_state_cues(self):
        hub = ProgressHub()
        event = hub.start('Unknown total').event
        rate, loud = pcm(event, .5)
        _, quiet = pcm(event, .25)
        self.assertAlmostEqual(sum(x*x for x in quiet) / sum(x*x for x in loud), .25, delta=.001)
        self.assertGreater(sum(value == 0 for value in quiet), round(rate * .08))
        self.assertTrue(all(value == 0 for value in pcm(event, 0)[1]))
        cues = {cue_wav(event)}
        for state in ('fail', 'cancel', 'pause'):
            reporter = hub.start('Unknown total')
            getattr(reporter, state)()
            cues.add(cue_wav(reporter.event))
        reporter = hub.start('Unknown total')
        reporter.pause()
        reporter.resume()
        cues.add(cue_wav(reporter.event))
        self.assertEqual(len(cues), 5)
        self.assertIn('Total unknown', speech_text(event))
        for value in (-1, 101, True, float('nan')):
            with self.assertRaises(ValueError):
                progress_frequency(value)

    def test_schedule_throttles_busy_concurrency_and_avoids_nested_duplicate(self):
        hub = ProgressHub()
        one = hub.start('One')
        child = one.child('Subtask', total=4)
        child.update(completed=1)
        two = hub.start('Two', total=4)
        schedule = NotificationSchedule()
        prefs = ProgressPreferences(mode='beep', interval_seconds=5)
        self.assertEqual(schedule.choose(hub.snapshot(), prefs, now=0).operation_id, one.operation_id)
        self.assertIsNone(schedule.choose(hub.snapshot(), prefs, now=4.99))
        self.assertEqual(schedule.choose(hub.snapshot(), prefs, now=5).operation_id, two.operation_id)
        self.assertEqual(schedule.choose(hub.snapshot(), prefs, now=10).operation_id, one.operation_id)
        one.fail()
        self.assertEqual(schedule.choose(hub.snapshot(), prefs, now=10.25).state, 'failed')
        self.assertIsNone(schedule.choose(hub.snapshot(), prefs, now=10.5))

    def test_off_mute_quiet_background_media_suppress_and_do_not_replay_completion(self):
        reporter = ProgressHub().start('Job')
        event = reporter.event
        prefs = ProgressPreferences(mode='beep')
        for preferences, environment in (
                (ProgressPreferences(), {}),
                (replace(prefs, muted=True), {}),
                (replace(prefs, quiet=True), {}),
                (prefs, {'foreground': False}),
                (prefs, {'media_active': True})):
            self.assertIsNone(NotificationSchedule().choose((event,), preferences, now=0, **environment))
        self.assertIsNotNone(NotificationSchedule().choose((event,), replace(prefs, background=True),
                                                           now=0, foreground=False))
        reporter.complete(verified=True)
        schedule = NotificationSchedule()
        self.assertIsNone(schedule.choose((reporter.event,), prefs, now=0, media_active=True))
        self.assertIsNone(schedule.choose((reporter.event,), prefs, now=10))

    def test_worker_is_lazy_nonblocking_and_cancels_own_output(self):
        entered, stopped = threading.Event(), threading.Event()
        thread_ids = []
        class Output:
            def beep(self, raw, *, cancelled):
                thread_ids.append(threading.get_ident())
                entered.set()
                deadline = time.monotonic() + 4
                while not cancelled() and time.monotonic() < deadline:
                    stopped.wait(.01)
                stopped.set()
            def speak(self, *args, **kwargs):
                raise AssertionError('Speech was not selected.')
        hub = ProgressHub()
        reporter = hub.start('Job', total=1000)
        worker = NotificationWorker(output=Output())
        try:
            worker.offer(hub.snapshot())
            self.assertIsNone(worker._thread)
            worker.configure(ProgressPreferences(mode='beep'))
            worker.offer(hub.snapshot())
            self.assertTrue(entered.wait(3))
            self.assertNotEqual(thread_ids[0], threading.get_ident())
            for completed in range(100):
                reporter.update(completed=completed)
                worker.offer(hub.snapshot())
            self.assertFalse(stopped.is_set(), 'Offering updates must not wait for audio output.')
            self.assertEqual(len(worker._events), 1)
            worker.environment(foreground=True, media_active=True)
            self.assertTrue(stopped.wait(3), 'Media activity must cancel notification playback.')
        finally:
            worker.close()
        if worker._thread is not None:
            worker._thread.join(3)
            self.assertFalse(worker._thread.is_alive())


if __name__ == '__main__':
    unittest.main()
