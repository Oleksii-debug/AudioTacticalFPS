"""Truthful progress, bounded synthetic cues and safe local preferences."""
from dataclasses import replace
from io import BytesIO
import json
import math
import os
from pathlib import Path
import struct
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library import progress as p


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_real_units_never_claim_completion_before_result_is_accepted():
    tracker = p.ProgressTracker('Import', total=4, unit='files')
    assert tracker.snapshot().percent == 0
    for done, expected in ((1, 25), (2, 50), (3, 75), (4, 99)):
        tracker.update(phase='Validating retained source', completed=done, total=4)
        snapshot = tracker.snapshot()
        assert snapshot.percent == expected
        assert f'{done} of 4 files' in snapshot.description
    with pytest.raises(ValueError, match='verified'):
        tracker.finish()
    assert tracker.snapshot().state == 'running'
    tracker.finish(verified=True)
    assert tracker.snapshot().percent == 100
    assert 'result was accepted' in tracker.snapshot().description
    assert not tracker.update(completed=0, total=4)
    assert not tracker.finish('failed')


@pytest.mark.parametrize('terminal', ['failed', 'cancelled'])
def test_partial_or_unknown_failure_never_reports_100(terminal):
    for total in (None, 4):
        tracker = p.ProgressTracker('Render or import', total=total)
        if total is not None:
            tracker.update(completed=4, total=4)
        tracker.finish(terminal, phase='Validation incomplete')
        assert tracker.snapshot().percent != 100
        assert tracker.snapshot().verified is False
        assert 'Validation incomplete' in tracker.snapshot().description


def test_unknown_duration_stays_indeterminate_until_verified_success():
    clock = Clock()
    tracker = p.ProgressTracker('Render', clock=clock)
    for phase in ('Prepare voices', 'Reuse 500 cached chunks', 'Mix 9 tracks', 'Verify output'):
        clock.now += 100
        tracker.update(phase=phase)
        assert tracker.snapshot().percent is None
        assert tracker.snapshot().eta_seconds is None
        assert 'indeterminate' in tracker.snapshot().description
    tracker.finish(verified=True)
    assert tracker.snapshot().percent == 100


def test_eta_uses_new_measured_work_and_resets_after_changed_total_or_resume():
    clock = Clock()
    tracker = p.ProgressTracker('Resume import', total=10, completed=6, clock=clock)
    clock.now = 20
    assert tracker.snapshot().eta_seconds is None  # Six cached units have no measured rate.
    tracker.update(completed=7, total=10)
    assert tracker.snapshot().eta_seconds == 60
    tracker.pause()
    clock.now = 200
    assert tracker.snapshot().eta_seconds is None
    tracker.resume()
    assert tracker.snapshot().eta_seconds is None
    clock.now = 205
    tracker.update(completed=8, total=10)
    assert tracker.snapshot().eta_seconds == 10
    tracker.update(completed=8, total=20)
    assert tracker.snapshot().eta_seconds is None
    assert 'Total work changed from 10 to 20' in tracker.snapshot().description


@pytest.mark.parametrize('change', [
    {'completed': -1, 'total': 4}, {'completed': True, 'total': 4},
    {'completed': 3, 'total': 2}, {'completed': 1, 'total': 4},
    {'completed': 2, 'total': 10**12 + 1}, {'completed': 2},
    {'completed': 3, 'total': 8, 'unit': []}, {'phase': []},
], ids=['negative', 'boolean', 'above-total', 'decrease', 'budget', 'missing-total', 'bad-unit', 'bad-phase'])
def test_invalid_update_is_atomic(change):
    clock = Clock()
    tracker = p.ProgressTracker('Work', total=4, completed=2, clock=clock)
    before = tracker.snapshot()
    with pytest.raises(ValueError):
        tracker.update(**change)
    assert tracker.snapshot() == before
    with pytest.raises(ValueError):
        tracker.finish(verified=True, phase=[])
    assert tracker.snapshot() == before


def test_preferences_default_off_roundtrip_and_allowlisted_snapshot(tmp_path):
    store = p.ProgressSettingsStore(tmp_path)
    assert store.load() == p.ProgressSettings()
    assert not (tmp_path / p.FILENAME).exists()
    selected = p.ProgressSettings('beep', 37, 9, True, True)
    assert store.save(selected) == selected
    assert store.load() == selected
    assert p.snapshot_progress_settings(tmp_path) == {p.FILENAME: p.encode_progress_settings(selected)}


@pytest.mark.parametrize('key,value', [
    ('version', True), ('version', 3), ('mode', 'speech'), ('mode', []),
    ('volume', True), ('volume', 101), ('interval_seconds', 0),
    ('interval_seconds', 61), ('muted', 1), ('background', 'yes'), ('secret', 'unused'),
])
def test_invalid_preferences_are_rejected_without_replacing_existing_file(tmp_path, key, value):
    store = p.ProgressSettingsStore(tmp_path)
    store.save(p.ProgressSettings())
    before = (tmp_path / p.FILENAME).read_bytes()
    payload = json.loads(before)
    payload[key] = value
    with pytest.raises(ValueError):
        p.validate_progress_settings_payload(json.dumps(payload).encode())
    assert (tmp_path / p.FILENAME).read_bytes() == before


@pytest.mark.parametrize('payload', [b'', b'[]', b'{"kind":1,"kind":2}', b'NaN', b'\xff', b' ' * 8193],
                         ids=['empty', 'list', 'duplicate', 'nonfinite', 'encoding', 'budget'])
def test_malformed_preferences_rejected(payload):
    with pytest.raises(ValueError):
        p.validate_progress_settings_payload(payload)


def test_failed_staging_keeps_old_preferences_and_removes_tempfile(tmp_path, monkeypatch):
    store = p.ProgressSettingsStore(tmp_path)
    store.save(p.ProgressSettings())
    original = (tmp_path / p.FILENAME).read_bytes()
    monkeypatch.setattr(p.os, 'fsync', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        store.save(p.ProgressSettings(mode='beep'))
    assert (tmp_path / p.FILENAME).read_bytes() == original
    assert list(tmp_path.glob('.progress-*.tmp')) == []


def test_hardlinked_preferences_are_not_read_or_overwritten(tmp_path):
    other = tmp_path / 'other.json'
    other.write_bytes(p.encode_progress_settings(p.ProgressSettings()))
    os.link(other, tmp_path / p.FILENAME)
    store = p.ProgressSettingsStore(tmp_path)
    with pytest.raises(ValueError):
        store.load()
    with pytest.raises(ValueError):
        store.save(p.ProgressSettings(mode='beep'))
    assert other.read_bytes() == p.encode_progress_settings(p.ProgressSettings())


def pcm(raw):
    with wave.open(BytesIO(raw), 'rb') as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        return struct.unpack('<' + 'h' * audio.getnframes(), audio.readframes(audio.getnframes()))


def test_actual_pcm_pitch_rises_and_volume_scales_without_clipping():
    tracker = p.ProgressTracker('Known work', total=4)
    frequencies = []
    for done in range(4):
        tracker.update(completed=done, total=4)
        cue = p.tone_segments(tracker.snapshot())
        samples = pcm(p.tone_wave(cue, 50))
        # Count full periods away from the small ramps. This checks generated
        # PCM, rather than merely trusting the requested frequency number.
        start, end = 160, 960
        crossings = sum(samples[i - 1] < 0 <= samples[i] for i in range(start + 1, end))
        measured = crossings * 16000 / (end - start)
        assert abs(measured - cue[0][0]) <= 25
        assert len(samples) == 1120 and samples[0] == samples[-1] == 0
        assert max(map(abs, samples)) <= 6000
        frequencies.append(measured)
    assert frequencies == sorted(set(frequencies))
    cue = ((440, 100),)
    low, high = (pcm(p.tone_wave(cue, volume)) for volume in (20, 80))
    rms = lambda data: math.sqrt(sum(value * value for value in data) / len(data))
    assert 3.99 < rms(high) / rms(low) < 4.01
    assert set(pcm(p.tone_wave(cue, 0))) == {0}
    transitions = [p.tone_segments(replace(tracker.snapshot(), state=state))
                   for state in ('completed', 'failed', 'cancelled', 'paused')]
    assert len(set(transitions)) == 4
    assert all(len(p.tone_wave(cue, 100)) < 30000 for cue in transitions)


@pytest.mark.parametrize('segments', [(), ((99, 70),), ((440, 201),), ((True, 70),), (None,), ((440,),)],
                         ids=['empty', 'frequency', 'duration', 'boolean', 'bad-segment', 'short-segment'])
def test_tones_have_a_strict_small_resource_budget(segments):
    with pytest.raises(ValueError):
        p.tone_wave(segments, 20)


def test_sound_policy_respects_default_off_interval_mute_background_and_terminal_once():
    clock = Clock()
    policy = p.ProgressTonePolicy(clock=clock)
    tracker = p.ProgressTracker('Work', total=4, clock=clock)
    settings = p.ProgressSettings(mode='beep', interval_seconds=5)
    assert policy.choose(tracker.snapshot(), p.ProgressSettings()) is None
    assert policy.choose(tracker.snapshot(), settings, active=False) is None
    assert policy.choose(tracker.snapshot(), settings, suppressed=True) is None
    assert policy.choose(tracker.snapshot(), replace(settings, muted=True)) is None
    assert policy.choose(tracker.snapshot(), settings) == ((220, 70),)
    tracker.update(completed=1, total=4)
    clock.now = 4.9
    assert policy.choose(tracker.snapshot(), settings) is None
    clock.now = 5
    assert policy.choose(tracker.snapshot(), settings)[0][0] > 220
    tracker.pause()
    assert policy.choose(tracker.snapshot(), settings) == ((350, 80),)
    clock.now = 100
    assert policy.choose(tracker.snapshot(), settings) is None
    tracker.resume()
    assert policy.choose(tracker.snapshot(), settings) == ((350, 60), (440, 60))
    tracker.finish(verified=True)
    assert policy.choose(tracker.snapshot(), settings) == ((880, 90), (1100, 90))
    clock.now = 200
    assert policy.choose(tracker.snapshot(), settings) is None


def test_native_backend_uses_async_flags_and_at_most_two_small_files():
    sound = SimpleNamespace(PlaySound=Mock(), SND_FILENAME=1, SND_ASYNC=2, SND_NODEFAULT=4)
    backend = p.WindowsToneBackend(sound)
    directory = Path(backend.directory.name)
    try:
        for index in range(6):
            selected = backend.prepare(p.tone_wave(((220 + index, 70),), 20))
            backend.play(selected)
        assert len(list(directory.iterdir())) == 2
        assert all(path.stat().st_size < 3000 for path in directory.iterdir())
        assert all(call.args[1] == 7 for call in sound.PlaySound.call_args_list)
        backend.stop()
        assert sound.PlaySound.call_args.args == (None, 0)
    finally:
        backend.close()
    assert not directory.exists()


def test_dispatcher_coalesces_bursts_and_preview_gate_blocks_queued_tone(monkeypatch):
    started, release, prepared, closed = Event(), Event(), Event(), Event()
    seen = []

    class Backend:
        count = 0

        def prepare(self, raw):
            self.count += 1
            if self.count == 1:
                started.set()
                assert release.wait(5)
            else:
                prepared.set()
            return raw

        def play(self, raw):
            seen.append(raw)

        def stop(self):
            pass

        def close(self):
            closed.set()

    dispatcher = p.ToneDispatcher(backend_factory=Backend)
    monkeypatch.setattr(p, '_dispatcher', dispatcher)
    owner = object()
    try:
        dispatcher.request(((220, 70),), 10)
        assert started.wait(5)
        for frequency in range(300, 400):
            assert dispatcher.request(((frequency, 70),), 20)
        assert dispatcher.mailbox.qsize() == 1
        p.set_media_active(owner, True)
        release.set()
        assert prepared.wait(5)
        dispatcher.close()
        dispatcher.thread.join(5)
        assert not dispatcher.thread.is_alive()
        assert seen == [] and closed.is_set()
        assert not dispatcher.request(((440, 70),), 20)
    finally:
        release.set()
        p.set_media_active(owner, False)
        dispatcher.close()
        dispatcher.thread.join(5)
