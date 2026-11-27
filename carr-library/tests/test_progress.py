"""Real counters, PCM, filesystem preferences and bounded worker acceptance."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
from threading import Event, get_ident
import time
import wave

import pytest

from carr_library.progress import (FILENAME, MAX_BYTES, PCM_DURATION, PCM_RATE,
    TONE_FREQUENCIES, FeedbackPreferences, ProgressFeedback, ProgressHub,
    ProgressPreferenceStore, WindowsFeedbackAdapter, FeedbackUnavailable,
    encode_preferences, progress_wave, validate_preferences_bytes)


def test_known_units_wait_for_verified_finish_and_never_report_false_100():
    hub = ProgressHub()
    handle = hub.start('copy', 'Back up library', 'Copying bytes', total=200)
    assert hub.snapshot()[0].percent == 0
    assert hub.update(handle, completed=67).percent == 33
    pending = hub.update(handle, completed=200)
    assert pending.percent is None and pending.awaiting_verification
    assert 'final verification pending' in pending.describe()
    with pytest.raises(ValueError, match='explicit verified'):
        hub.finish(handle)
    final = hub.finish(handle, verified=True)
    assert final.percent == 100 and final.state == 'completed' and final.verified
    assert '100%; verified complete' in final.describe()
    assert hub.update(handle, completed=0) is None


def test_verified_finish_cannot_skip_unprocessed_known_units():
    hub = ProgressHub()
    handle = hub.start('hash', 'Checking bundle', total=10)
    hub.update(handle, completed=7)
    with pytest.raises(ValueError, match='All known units'):
        hub.finish(handle, verified=True)
    assert hub.snapshot()[0].completed == 7


@pytest.mark.parametrize('total', [None, 0])
def test_unknown_or_empty_work_is_not_a_fabricated_percentage(total):
    hub = ProgressHub()
    handle = hub.start('scan', 'Scan input', total=total)
    assert hub.snapshot()[0].percent is None
    if total is None:
        assert hub.update(handle, completed=18).percent is None
        assert '18 processed; total unknown' in hub.snapshot()[0].describe()
    assert hub.finish(handle, verified=True).percent == 100


def test_pause_resume_and_real_cancel_fail_are_distinct():
    for ending in ('cancelled', 'failed'):
        hub = ProgressHub()
        handle = hub.start(ending, 'Job', 'Reading', total=4)
        hub.update(handle, completed=1)
        assert hub.pause(handle).state == 'paused'
        assert hub.snapshot()[0].percent == 25
        assert hub.resume(handle).state == 'resumed'
        final = hub.finish(handle, state=ending)
        assert final.state == ending and final.percent == 25 and not final.verified
        assert ending.capitalize() in final.describe()
        assert hub.finish(handle, verified=True) is None


def test_count_regression_only_allowed_after_explicit_phase_or_total_change():
    hub = ProgressHub()
    handle = hub.start('import', 'Import', 'Copying', total=20)
    hub.update(handle, completed=15)
    for count in (14, None):
        with pytest.raises(ValueError, match='backwards'):
            hub.update(handle, completed=count)
    assert hub.update(handle, phase='Hashing', completed=0).percent == 0
    hub.update(handle, completed=12)
    assert hub.update(handle, total=5, completed=2).percent == 40


@pytest.mark.parametrize('field,value', [('completed', -1), ('total', -1),
    ('total', True), ('completed', '1'), ('completed', float('nan')),
    ('total', float('inf')), ('total', 10**19), ('total', 10**1000), ('completed', 3)])
def test_invalid_counts_never_replace_last_valid_event(field, value):
    hub = ProgressHub()
    handle = hub.start('work', 'Work', total=2)
    previous = hub.snapshot()
    with pytest.raises(ValueError):
        hub.update(handle, **{field: value})
    assert hub.snapshot() == previous


def test_task_generation_close_and_foreign_handles_withdraw_late_callbacks():
    hub = ProgressHub()
    old = hub.start('task', 'First attempt')
    hub.finish(old, state='cancelled')
    current = hub.start('task', 'Second attempt')
    assert old != current
    assert hub.update(old, completed=8) is None
    other = ProgressHub().start('task', 'Different owner')
    assert hub.finish(other, verified=True) is None
    assert hub.snapshot()[0].handle == current
    hub.close()
    assert hub.snapshot() == hub.drain() == ()
    assert hub.start('task', 'Too late') is None
    assert hub.update(current, completed=1) is None


def test_many_concurrent_updates_coalesce_per_task_with_bounded_inventory():
    hub = ProgressHub(max_tasks=4)
    handles = [hub.start(str(index), f'Task {index}', total=1000) for index in range(4)]
    def run(handle):
        for count in range(1, 1001):
            hub.update(handle, completed=count)
        hub.finish(handle, verified=True)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(run, handles))
    latest = hub.drain()
    assert len(latest) == 4 and all(item.percent == 100 for item in latest)
    assert hub.drain() == ()
    assert len({item.sequence for item in latest}) == 4
    hub.start('next', 'Another task')
    assert len(hub.snapshot()) == 4
    for index in range(3):
        hub.start(f'next-{index}', 'Active task')
    before = hub.snapshot()
    with pytest.raises(ValueError, match='concurrent'):
        hub.start('too-many', 'Overflow')
    assert hub.snapshot() == before


def test_active_task_identity_cannot_silently_replace_a_running_producer():
    hub = ProgressHub()
    handle = hub.start('task', 'Active')
    with pytest.raises(ValueError, match='already active'):
        hub.start('task', 'New')
    assert hub.snapshot()[0].handle == handle


def decode_wave(raw):
    with wave.open(io.BytesIO(raw), 'rb') as stream:
        assert stream.getnchannels() == 1 and stream.getsampwidth() == 2
        assert stream.getframerate() == PCM_RATE
        assert stream.getcomptype() == 'NONE'
        count = stream.getnframes()
        samples = struct.unpack(f'<{count}h', stream.readframes(count))
    return samples


def measured_frequency(samples):
    # Measure actual PCM zero crossings well inside attack/release regions.
    points = [index for index in range(1, len(samples)) if samples[index - 1] <= 0 < samples[index]]
    assert len(points) >= 6
    return PCM_RATE * (len(points) - 1) / (points[-1] - points[0])


@pytest.mark.parametrize('percent', [0, 25, 50, 75, 100])
def test_actual_pcm_milestones_rise_in_pitch_and_are_bounded(percent):
    samples = decode_wave(progress_wave(percent, volume=60, state='completed' if percent == 100 else 'running'))
    assert len(samples) / PCM_RATE == PCM_DURATION
    assert samples[0] == samples[-1] == 0
    frequency = measured_frequency(samples[500:-500])
    assert frequency == pytest.approx(TONE_FREQUENCIES[percent], abs=1.5)
    assert 0 < max(abs(value) for value in samples) <= math.ceil(32767 * 0.45 * 0.6)


def test_real_pcm_unknown_pause_cancel_error_have_distinct_audible_patterns():
    unknown = decode_wave(progress_wave(None, volume=50))
    middle = len(unknown) // 2
    assert measured_frequency(unknown[300:middle-300]) == pytest.approx(330, abs=2)
    assert measured_frequency(unknown[middle+300:-300]) == pytest.approx(440, abs=2)
    patterns = [progress_wave(None, state=state) for state in ('running', 'paused', 'cancelled', 'failed')]
    assert len(set(patterns)) == 4


def test_pcm_volume_controls_actual_amplitude_and_zero_is_silence():
    quiet = decode_wave(progress_wave(50, volume=20))
    louder = decode_wave(progress_wave(50, volume=40))
    assert max(map(abs, louder)) / max(map(abs, quiet)) == pytest.approx(2, abs=0.001)
    assert set(decode_wave(progress_wave(50, volume=0))) == {0}


def test_preferences_default_off_round_trip_real_atomic_file(tmp_path):
    store = ProgressPreferenceStore(tmp_path)
    assert store.load() == FeedbackPreferences() and not store.path.exists()
    desired = FeedbackPreferences('Beep+Speech', 2, 32, True, False)
    saved = store.save(desired)
    assert store.load() == desired == validate_preferences_bytes(saved)
    assert store.path.name == FILENAME
    assert not tuple(tmp_path.glob('.progress-*.tmp'))


@pytest.mark.parametrize('mutation', [
    {'version': True}, {'kind': 'another-file'}, {'extra': 1}, {'mode': 'Run script'},
    {'volume': True}, {'volume': 101}, {'volume': -1}, {'volume': 2.5},
    {'interval': 0}, {'interval': 121}, {'interval': float('nan')},
    {'muted': 1}, {'background': 'yes'},
])
def test_tampered_preferences_are_rejected_without_touching_bytes(tmp_path, mutation):
    value = json.loads(encode_preferences(FeedbackPreferences()))
    value.update(mutation)
    raw = json.dumps(value).encode()
    path = tmp_path / FILENAME
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        ProgressPreferenceStore(tmp_path).load()
    assert path.read_bytes() == raw


@pytest.mark.parametrize('raw', [b'', b'x' * (MAX_BYTES + 1), b'\xff',
    b'{"version":1,"version":1}', b'[]', b'null', b'{}'])
def test_invalid_preference_encodings_rejected(raw):
    with pytest.raises(ValueError):
        validate_preferences_bytes(raw)


def test_preference_write_failure_preserves_previous_content(tmp_path, monkeypatch):
    store = ProgressPreferenceStore(tmp_path)
    old = store.save(FeedbackPreferences('Beep'))
    monkeypatch.setattr('carr_library.progress.os.replace', lambda *_: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        store.save(FeedbackPreferences('Speech'))
    assert store.path.read_bytes() == old
    assert not tuple(tmp_path.glob('.progress-*.tmp'))


def test_linked_preferences_rejected_without_writing_target(tmp_path):
    target = tmp_path / 'real.json'
    target.write_bytes(encode_preferences(FeedbackPreferences('Off')))
    import os
    os.link(target, tmp_path / FILENAME)
    store = ProgressPreferenceStore(tmp_path)
    with pytest.raises(ValueError):
        store.load()
    with pytest.raises(ValueError):
        store.save(FeedbackPreferences('Beep'))
    assert validate_preferences_bytes(target.read_bytes()).mode == 'Off'


class AudioRecorder:
    def __init__(self, *, block=False):
        self.calls = []
        self.entered = Event()
        self.release = Event()
        if not block:
            self.release.set()
    def play(self, raw, *, cancelled):
        self.entered.set()
        while not self.release.wait(0.005):
            if cancelled():
                return
        if not cancelled():
            self.calls.append(('beep', raw, get_ident()))
    def speak(self, text, *, volume, cancelled):
        if not cancelled():
            self.calls.append(('speech', text, volume, get_ident()))


def event_at(percent=25):
    hub = ProgressHub()
    handle = hub.start('operation', 'Import', 'Copying', total=100)
    return hub.update(handle, completed=percent)


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def test_default_off_does_not_start_worker_or_audio_adapter():
    adapter = AudioRecorder()
    feedback = ProgressFeedback(adapter=adapter)
    assert not feedback.submit(event_at())
    assert feedback._thread is None and adapter.calls == []
    feedback.close()


def test_audio_runs_off_ui_thread_with_actual_pcm_and_optional_speech():
    adapter = AudioRecorder()
    feedback = ProgressFeedback(FeedbackPreferences('Beep+Speech'), adapter=adapter)
    try:
        assert feedback.submit(event_at())
        wait_for(lambda: len(adapter.calls) == 2)
        assert decode_wave(adapter.calls[0][1])
        assert '25%' in adapter.calls[1][1]
        assert all(call[-1] != get_ident() for call in adapter.calls)
    finally:
        feedback.close()
    feedback._thread.join(timeout=1)
    assert not feedback._thread.is_alive()


def test_audio_queue_coalesces_latest_event_and_close_discards_pending():
    adapter = AudioRecorder(block=True)
    feedback = ProgressFeedback(FeedbackPreferences('Beep', interval=120), adapter=adapter, max_tasks=2)
    assert feedback.submit(event_at(1))
    assert adapter.entered.wait(1)
    hub = ProgressHub()
    handles = [hub.start(str(index), f'Job {index}', total=100) for index in range(3)]
    for index in range(1, 100):
        for handle in handles:
            feedback.submit(hub.update(handle, completed=index))
    with feedback._condition:
        assert len(feedback._pending) == 2
        assert all(item[0].completed == 99 for item in feedback._pending.values())
    feedback.close()
    adapter.release.set()
    feedback._thread.join(timeout=1)
    assert adapter.calls == [] and not feedback._thread.is_alive()


@pytest.mark.parametrize('reason', ['mute', 'off', 'zero', 'background', 'playback', 'recording', 'external'])
def test_sound_suppressed_and_inflight_callback_cancelled_for_media_or_quiet_settings(reason):
    adapter = AudioRecorder(block=True)
    external = Event()
    feedback = ProgressFeedback(FeedbackPreferences('Beep+Speech'), adapter=adapter, media_busy=external.is_set)
    assert feedback.submit(event_at()) and adapter.entered.wait(1)
    if reason == 'mute':
        feedback.configure(FeedbackPreferences('Beep+Speech', muted=True))
    elif reason == 'off':
        feedback.configure(FeedbackPreferences())
    elif reason == 'zero':
        feedback.configure(FeedbackPreferences('Beep+Speech', volume=0))
    elif reason == 'background':
        feedback.set_background(True)
    elif reason == 'external':
        external.set()
    else:
        feedback.set_media_state(**{reason: True})
    adapter.release.set()
    assert not feedback.submit(event_at(50))
    feedback.close()
    feedback._thread.join(timeout=1)
    assert adapter.calls == []


def test_interval_is_global_for_concurrent_tasks_manual_query_can_request_current():
    now = [100.0]
    adapter = AudioRecorder()
    feedback = ProgressFeedback(FeedbackPreferences('Speech', interval=5), adapter=adapter, clock=lambda: now[0])
    try:
        feedback.submit(event_at(1))
        wait_for(lambda: len(adapter.calls) == 1)
        feedback.submit(event_at(25))
        time.sleep(0.03)
        assert len(adapter.calls) == 1
        now[0] = 105
        with feedback._condition:
            feedback._condition.notify_all()
        wait_for(lambda: len(adapter.calls) == 2)
        feedback.submit(event_at(50), manual=True)
        wait_for(lambda: len(adapter.calls) == 3)
    finally:
        feedback.close()


def test_unavailable_real_platform_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr('carr_library.progress.os.name', 'posix')
    adapter = WindowsFeedbackAdapter()
    with pytest.raises(FeedbackUnavailable, match='Windows'):
        adapter.speak('Test current progress', volume=20, cancelled=lambda: False)
    assert adapter.speech_available is False
    with pytest.raises(FeedbackUnavailable):
        adapter.play(progress_wave(25), cancelled=lambda: False)


def test_external_adapter_error_does_not_leak_raw_diagnostic_or_break_visual_event():
    class Unavailable:
        def speak(self, *args, **kwargs):
            raise RuntimeError('private path or secret diagnostic')
    feedback = ProgressFeedback(FeedbackPreferences('Speech'), adapter=Unavailable())
    current = event_at()
    try:
        feedback.submit(current)
        wait_for(lambda: bool(feedback.notice))
        assert 'private' not in feedback.notice and 'visual progress remains active' in feedback.notice
        assert current.percent == 25
    finally:
        feedback.close()


def test_actual_notification_child_receives_pcm_only_via_stdin():
    from carr_library.progress import _run_notification
    raw = progress_wave(75)
    program = ('import io,sys,wave; raw=sys.stdin.buffer.read(); '
               'w=wave.open(io.BytesIO(raw)); '
               'assert w.getnframes()==3840 and w.getframerate()==24000')
    assert _run_notification([sys.executable, '-I', '-c', program], raw,
                             cancelled=lambda: False, timeout=2)


def test_actual_cancel_kills_only_owned_notification_process(tmp_path, monkeypatch):
    from carr_library.progress import _run_notification
    started = tmp_path / 'child-started'
    children = []
    original = subprocess.Popen
    def record_child(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr('carr_library.progress.subprocess.Popen', record_child)
    program = ('import pathlib,sys,time; '
               'pathlib.Path(sys.argv[1]).write_text("started"); time.sleep(30)')
    assert not _run_notification([sys.executable, '-I', '-c', program, str(started)], b'',
                                 cancelled=started.exists, timeout=2)
    assert started.is_file() and len(children) == 1 and children[0].poll() is not None


def test_actual_notification_timeout_is_bounded_and_reaps_child(monkeypatch):
    from carr_library.progress import _run_notification
    children = []
    original = subprocess.Popen
    def record_child(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr('carr_library.progress.subprocess.Popen', record_child)
    started = time.monotonic()
    with pytest.raises(FeedbackUnavailable, match='notification limit'):
        _run_notification([sys.executable, '-I', '-c', 'import time; time.sleep(30)'], b'',
                          cancelled=lambda: False, timeout=0.05)
    assert time.monotonic() - started < 2 and children[0].poll() is not None


def test_media_handoff_waits_for_the_actual_notification_child_to_exit(tmp_path, monkeypatch):
    from carr_library.progress import _run_notification
    started = tmp_path / 'notification-started'
    children, original = [], subprocess.Popen
    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr('carr_library.progress.subprocess.Popen', spawn)
    class ChildAdapter:
        def play(self, raw, *, cancelled):
            program = ('import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("started"); '
                       'time.sleep(30)')
            _run_notification([sys.executable, '-I', '-c', program, str(started)], raw,
                              cancelled=cancelled, timeout=2)
    feedback = ProgressFeedback(FeedbackPreferences('Beep'), adapter=ChildAdapter())
    try:
        assert feedback.submit(event_at())
        wait_for(started.exists)
        assert not feedback.wait_quiet(0)
        feedback.set_media_state(playback=True)
        assert feedback.wait_quiet(2)
        assert children and all(child.poll() is not None for child in children)
        assert not feedback.submit(event_at(50), manual=True)
    finally:
        feedback.close()


def test_uncooperative_notification_cannot_approve_media_start():
    class SlowAdapter:
        entered, release = Event(), Event()
        def play(self, raw, *, cancelled):
            self.entered.set()
            self.release.wait(2)  # Simulate output that has not acknowledged cancellation.
    adapter = SlowAdapter()
    feedback = ProgressFeedback(FeedbackPreferences('Beep'), adapter=adapter)
    try:
        assert feedback.submit(event_at()) and adapter.entered.wait(1)
        feedback.set_media_state(recording=True)
        assert not feedback.wait_quiet(0.01)
        adapter.release.set()
        assert feedback.wait_quiet(1)
    finally:
        adapter.release.set()
        feedback.close()


def test_full_stdin_pipe_cannot_block_cancellation_or_real_child_reaping(tmp_path, monkeypatch):
    """Exercise Windows communicate's blocking-write hazard on every OS."""
    from carr_library.progress import _run_notification
    started = tmp_path / 'does-not-read-stdin'
    children, original = [], subprocess.Popen
    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr('carr_library.progress.subprocess.Popen', spawn)
    program = ('import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("started"); '
               'time.sleep(30)')
    began = time.monotonic()
    assert not _run_notification([sys.executable, '-I', '-c', program, str(started)],
                                 b'x' * (1024 * 1024), cancelled=started.exists, timeout=5)
    assert time.monotonic() - began < 4
    assert children and all(child.poll() is not None for child in children)


def test_unconfirmed_child_cleanup_retains_quiet_guard_until_actual_exit():
    from carr_library.progress import _NotificationCleanupError
    child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'])
    class PendingAdapter:
        def play(self, raw, *, cancelled):
            raise _NotificationCleanupError(child)
    feedback = ProgressFeedback(FeedbackPreferences('Beep'), adapter=PendingAdapter())
    try:
        assert feedback.submit(event_at())
        wait_for(lambda: bool(feedback.notice))
        feedback.set_media_state(playback=True)
        assert not feedback.wait_quiet(0.01)
        assert child.poll() is None
        child.kill()
        child.wait(timeout=2)
        assert feedback.wait_quiet(1)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        feedback.close()


def test_rejected_notification_writer_thread_reaps_its_actual_child(monkeypatch):
    from carr_library.progress import _run_notification
    children, original = [], subprocess.Popen
    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    def reject(thread):
        raise RuntimeError('No notification writer available')
    monkeypatch.setattr('carr_library.progress.subprocess.Popen', spawn)
    monkeypatch.setattr('carr_library.progress.threading.Thread.start', reject)
    with pytest.raises(RuntimeError, match='No notification writer'):
        _run_notification([sys.executable, '-I', '-c', 'import time; time.sleep(30)'], b'input',
                          cancelled=lambda: False, timeout=2)
    assert children and all(child.poll() is not None and child.stdin.closed for child in children)
