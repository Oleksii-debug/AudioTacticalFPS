"""Measured native presentation boundaries; no fake audio-device acceptance."""
from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory

from carr_library.progress import (ProgressHub, ProgressPreferences, decode_preferences,
    encode_preferences, finish_result, load_preferences)
from carr_library.progress_audio import (LocalNotificationOutput, NotificationWorker,
                                        voice_fingerprint)
from carr_library.ui.operation_progress import OperationProgress


class Control:
    def __init__(self):
        self.value, self.label, self.name, self.pulses = 0, '', '', 0
    def SetName(self, value):
        self.name = value
    def SetRange(self, value):
        self.range = value
    def SetValue(self, value):
        self.value = value
    def SetLabel(self, value):
        self.label = value
    def Pulse(self):
        self.pulses += 1


class Notifications:
    def __init__(self):
        self.preferences, self.events = ProgressPreferences(), ()
        self.quiescent, self.error, self.closed = True, '', False
        self.configured, self.clears, self.registry = [], 0, None
    def configure(self, value):
        self.preferences = value
        self.configured.append(value)
    def clear(self):
        self.events = ()
        self.clears += 1
    def set_registry(self, value):
        self.registry = value
    def environment(self, **values):
        self.environment_values = values
    def offer(self, events):
        self.events = events
    def close(self):
        self.closed = True


class Later:
    def __init__(self, action):
        self.action, self.stopped = action, False
    def Stop(self):
        self.stopped = True
    def run(self):
        if not self.stopped:
            self.stopped = True
            self.action()


class NativeBoundary:
    """Only a deterministic event queue; native wx acceptance is separate."""
    def __init__(self):
        self.active, self.later, self.posted = None, [], []
    def GetActiveWindow(self):
        return self.active
    def CallLater(self, delay, action):
        value = Later(action)
        self.later.append(value)
        return value
    def CallAfter(self, action):
        self.posted.append(action)


class PresenterTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.frame = SimpleNamespace(library=SimpleNamespace(root=self.root), messages=[])
        self.frame.report = self.frame.messages.append
        self.wx, self.gauge, self.label = NativeBoundary(), Control(), Control()
        self.wx.active = self.frame
        self.audio = Notifications()
        self.now = [0.]
        self.presenter = OperationProgress(self.frame, self.gauge, self.label,
            wx_module=self.wx, notifications=self.audio, clock=lambda: self.now[0], start_timer=False)
        self.addCleanup(self.presenter.close)

    def test_unknown_total_is_indeterminate_and_does_not_take_focus(self):
        self.presenter.start('Hashing source')
        self.presenter.tick()
        self.assertEqual(self.gauge.pulses, 1)
        self.assertIn('total unknown', self.label.label)
        self.assertIn('measured', self.gauge.name)
        self.assertEqual(self.frame.messages, [])
        self.presenter.read_status()
        self.assertIn('total unknown', self.frame.messages[-1])

    def test_known_gauge_never_reaches_100_before_actual_completion(self):
        event = self.presenter.start('Import', total=4, unit='files')
        self.presenter.update(2, token=event.token)
        self.presenter.tick()
        self.assertEqual(self.gauge.value, 50)
        self.assertIn('2 of 4 files', self.label.label)
        self.presenter.update(4, token=event.token)
        self.presenter.tick()
        self.assertEqual(self.gauge.value, 99)
        self.presenter.finish_result([{'state': 'complete'}] * 4, token=event.token)
        self.presenter.tick()
        self.assertEqual(self.gauge.value, 100)
        self.assertTrue(self.presenter.event.verified)

    def test_cancel_request_is_not_acknowledgement_or_hidden_failure(self):
        event = self.presenter.start('Write')
        self.presenter.request_cancel()
        self.assertEqual(self.presenter.event.state, 'running')
        self.presenter.finish_result([{'state': 'failed'}], cancelled=True, token=event.token)
        self.assertEqual(self.presenter.event.state, 'failed')
        self.assertNotEqual(self.presenter.event.percent, 100)

    def test_timeout_and_nested_unknown_states_require_review(self):
        for result, expected in (({'rows': [{'state': 'timed_out'}]}, 'failed'),
                                 ([{'results': [{'status': 'new-unknown'}]}], 'paused'),
                                 ([{'state': 'cancelled'}], 'cancelled')):
            with self.subTest(expected=expected):
                event = self.presenter.start('Import')
                self.presenter.finish_result(result, token=event.token)
                self.assertEqual(self.presenter.event.state, expected)

    def test_late_cancel_does_not_erase_a_committed_typed_result(self):
        event = self.presenter.start('Export')
        self.presenter.finish_result({'status': 'complete'}, cancelled=True, token=event.token)
        self.assertEqual(self.presenter.event.percent, 100)

    def test_old_operation_callbacks_and_notices_cannot_change_new_work(self):
        old = self.presenter.start('Old')
        self.presenter.succeed(token=old.token)
        new = self.presenter.start('Current', total=3)
        self.assertIsNone(self.presenter.update(99, token=old.token))
        self.assertIsNone(self.presenter.fail('late failure', token=old.token))
        self.presenter.tick()
        self.assertEqual(self.presenter.event.token, new.token)
        self.assertEqual([event.token for event in self.audio.events], [new.token])

    def test_worker_binding_updates_the_existing_hub(self):
        event = self.presenter.start('Read')
        from carr_library.progress import report_progress
        with self.presenter.bind(event.token):
            report_progress(completed=2, total=5, phase='Reading verified units')
        self.presenter.tick()
        self.assertEqual(self.gauge.value, 40)
        self.assertEqual(len(self.presenter.hub.snapshot()), 1)

    def test_library_change_discards_voices_old_callbacks_and_final_notices(self):
        event = self.presenter.start('Old')
        self.audio.registry = object()
        newroot = self.root / 'another'
        newroot.mkdir()
        self.frame.library = SimpleNamespace(root=newroot)
        self.assertIsNone(self.presenter.update(1, token=event.token))
        self.presenter.tick()
        self.assertIsNone(self.presenter.event)
        self.assertIsNone(self.audio.registry)
        self.assertEqual(self.audio.events, ())
        self.assertEqual(self.gauge.value, 0)

    def test_legacy_preferences_decode_and_selected_voice_roundtrip(self):
        raw = json.loads(encode_preferences(ProgressPreferences()))
        raw['version'] = 1
        for name in ('voice_id', 'voice_language', 'voice_fingerprint'):
            del raw['preferences'][name]
        self.assertEqual(decode_preferences(json.dumps(raw).encode()).mode, 'off')
        selected = ProgressPreferences(mode='speech', quiet=True, voice_id='local:chosen',
            voice_language='uk', voice_fingerprint='a' * 64)
        self.presenter.configure(selected)
        self.assertEqual(load_preferences(self.root), selected)
        with self.assertRaisesRegex(ValueError, 'explicit local voice'):
            self.presenter.configure(ProgressPreferences(mode='speech'))

    def test_session_quiet_does_not_change_saved_choice(self):
        selected = ProgressPreferences(mode='beep')
        self.presenter.configure(selected)
        self.presenter.set_session_quiet(True)
        self.assertTrue(self.audio.preferences.quiet)
        self.assertEqual(load_preferences(self.root), selected)
        self.presenter.set_session_quiet(False)
        self.assertFalse(self.audio.preferences.quiet)

    def test_content_waits_for_our_actual_output_release(self):
        self.presenter.configure(ProgressPreferences(mode='beep', suppress_during_media=False))
        self.audio.quiescent = False
        calls = []
        request = self.presenter.media_action(lambda: calls.append('started'))
        self.assertTrue(self.audio.preferences.quiet)
        self.assertEqual(calls, [])
        self.assertFalse(request.closed)
        self.audio.quiescent = True
        self.wx.later.pop(0).run()
        self.assertEqual(calls, ['started'])
        self.assertTrue(request.closed)
        self.assertFalse(self.audio.preferences.quiet)

    def test_failed_release_and_stale_library_never_start_content(self):
        self.audio.quiescent = False
        calls, failures = [], []
        self.presenter.media_action(lambda: calls.append('started'), failed=failures.append, timeout=2)
        self.now[0] = 3
        self.wx.later.pop(0).run()
        self.assertEqual(calls, [])
        self.assertEqual(len(failures), 1)
        self.presenter.media_action(lambda: calls.append('stale'))
        self.frame.library = SimpleNamespace(root=self.root / 'other')
        self.audio.quiescent = True
        self.wx.later.pop(0).run()
        self.assertEqual(calls, [])

    def test_cancelled_or_closed_content_request_ignores_late_native_timer(self):
        self.audio.quiescent = False
        calls = []
        request = self.presenter.media_action(lambda: calls.append('started'))
        timer = self.wx.later[-1]
        request.close()
        self.audio.quiescent = True
        timer.action()
        self.assertEqual(calls, [])
        self.presenter.start('Closing')
        self.presenter.close()
        self.presenter.tick()
        self.assertTrue(self.audio.closed)

    def test_observed_player_state_suppresses_media_and_paused_is_not_playing(self):
        class Player:
            state = 'playing'
        player = Player()
        remove = self.presenter.register_player(player)
        self.assertTrue(self.audio.environment_values['media_active'])
        player.state = 'paused'
        self.presenter.tick()
        self.assertFalse(self.audio.environment_values['media_active'])
        player.state = 'starting'
        self.presenter.tick()
        self.assertTrue(self.audio.environment_values['media_active'])
        remove()
        self.assertFalse(self.audio.environment_values['media_active'])


class VoiceBindingTests(unittest.TestCase):
    def registry(self, identity='model-a'):
        voice = SimpleNamespace(id='local:chosen', name='Chosen', available=True,
            languages=('uk-UA',), controls=('volume',), engine='test-local', revision='1')
        return SimpleNamespace(voices=lambda **kwargs: (voice,),
                               cache_identity=lambda *args, **kwargs: identity)

    def test_voice_runtime_and_language_are_bound(self):
        registry = self.registry()
        original = voice_fingerprint(registry, 'local:chosen', 'uk')
        self.assertEqual(len(original), 64)
        self.assertNotEqual(original, voice_fingerprint(self.registry('model-b'), 'local:chosen', 'uk'))
        with self.assertRaises(ValueError):
            voice_fingerprint(registry, 'other:voice', 'uk')
        with self.assertRaises(ValueError):
            voice_fingerprint(registry, 'local:chosen', 'en')

    def test_no_automatic_voice_fallback_or_runtime_launch_without_selection(self):
        calls = []
        output = LocalNotificationOutput(registry_factory=lambda: calls.append('discovery'))
        with self.assertRaisesRegex(RuntimeError, 'Choose a local progress voice'):
            output.speak('synthetic notice', volume=.1, cancelled=lambda: False)
        self.assertEqual(calls, [])

    def test_changed_runtime_cannot_synthesize_the_old_selection(self):
        registry = self.registry('changed')
        calls = []
        registry.synthesize = lambda *args, **kwargs: calls.append('synthesized')
        output = LocalNotificationOutput(registry_factory=lambda: registry)
        with self.assertRaisesRegex(RuntimeError, 'runtime changed'):
            output.speak('synthetic notice', volume=.1, cancelled=lambda: False,
                selection=('local:chosen', 'uk', 'f' * 64))
        self.assertEqual(calls, [])

    def test_expected_quiet_preemption_does_not_disable_the_selected_voice(self):
        entered, finished = threading.Event(), threading.Event()
        class Output:
            quiescent = True
            def speak(self, text, *, cancelled, **kwargs):
                entered.set()
                if not finished.wait(2):
                    raise AssertionError('The synthetic output was not released.')
                if cancelled():
                    raise InterruptedError('quiet acknowledged')
            def beep(self, *args, **kwargs):
                raise AssertionError('Speech mode did not request a beep.')
        worker = NotificationWorker(ProgressPreferences(mode='speech',
            voice_id='local:chosen', voice_language='uk', voice_fingerprint='a' * 64), output=Output())
        try:
            reporter = ProgressHub().start('Synthetic')
            worker.offer((reporter.event,))
            self.assertTrue(entered.wait(2))
            worker.configure(replace(worker._preferences, quiet=True))
            finished.set()
            worker._thread.join(.3)
            self.assertEqual(worker.error, '')
            self.assertEqual(worker._preferences.mode, 'speech')
            self.assertTrue(worker._preferences.quiet)
        finally:
            finished.set()
            worker.close()
            if worker._thread is not None:
                worker._thread.join(2)


def test_selected_voice_and_quiet_preferences_survive_complete_backup(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    from carr_library.progress import save_preferences
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic voice preferences backup')
    selected = ProgressPreferences(mode='speech', quiet=True, voice_id='local:chosen',
        voice_language='sk', voice_fingerprint='b' * 64)
    save_preferences(app.root, selected)
    target = tmp_path / 'backup'
    digest = create_backup(app, target)
    verify_backup(target, digest)
    restored = restore_backup(target, tmp_path / 'restored', digest)
    assert load_preferences(restored) == selected


if __name__ == '__main__':
    unittest.main()
