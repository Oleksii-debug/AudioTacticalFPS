"""The legacy producer vocabulary shares actual core snapshots and ownership."""
import math
from dataclasses import replace
import json
from pathlib import Path
import runpy
from threading import Event, Thread, current_thread
from time import monotonic

import pytest

from carr_library.progress import ProgressError, ProgressHub, ProgressState, bind_progress, current_progress
from carr_library.progress_feedback import ProgressFeedback
from carr_library.progress_preferences import (
    LegacyProgressPreferences, LibraryProgressPreferenceStore, ProgressPreferences,
    ProgressPreferenceStore,
)
from carr_library.ui.operation_progress import ProgressTracker


def eventually(predicate, seconds=3):
    deadline = monotonic() + seconds
    while not predicate():
        assert monotonic() < deadline, 'The owned progress worker did not finish.'
        Event().wait(.002)


def test_fractional_units_are_the_real_bound_snapshot_without_count_fabrication():
    clock = [0.]
    hub = ProgressHub(clock=lambda: clock[0])
    tracker = ProgressTracker(clock=lambda: clock[0], hub=hub)
    original = tracker.start('Measured playback seconds', total=2.5)
    tracker.update(.625, 2.5, token=original.token)
    snapshot = tracker.task.snapshot()
    assert (snapshot.completed, snapshot.total, snapshot.percent) == (.625, 2.5, 25)
    with bind_progress(tracker.task):
        assert current_progress() is tracker.task
        current_progress().advance(.625)
    assert (tracker.event.completed, tracker.event.total, tracker.event.percent) == (1.25, 2.5, 50)
    assert snapshot.completed == .625
    assert current_progress() is None


def test_ordinary_canonical_counts_and_durable_completion_remain_strict():
    hub = ProgressHub()
    with pytest.raises(ProgressError):
        hub.start('Integer units', total=1.5)
    task = hub.start('Integer units', total=2)
    with pytest.raises(ProgressError):
        task.update(completed=.5)
    with pytest.raises(ProgressError):
        task.succeed()
    assert task.snapshot().state is ProgressState.RUNNING
    assert task.snapshot().completed == 0


def test_core_completion_and_legacy_tokens_refer_to_the_same_task():
    tracker = ProgressTracker()
    first = tracker.start('First operation', 2)
    task = tracker.task
    task.update(completed=2)
    task.succeed()
    assert tracker.event.state == 'succeeded' and tracker.event.percent == 100
    second = tracker.start('Second operation')
    assert second.token != first.token
    assert tracker.update(1, 2, token=first.token) is None
    assert tracker.transition('failed', token=first.token) is None
    assert tracker.task.snapshot().state is ProgressState.RUNNING
    assert task.snapshot().state is ProgressState.SUCCEEDED


def test_superseded_legacy_observers_release_bounded_hub_history():
    hub = ProgressHub(max_tasks=2)
    tracker = ProgressTracker(hub=hub)
    for index in range(6):
        tracker.start('Measured operation ' + str(index))
    assert len(hub.snapshots()) == 2
    assert len(hub.snapshots(active_only=True)) == 1
    assert hub.snapshots()[0].state is ProgressState.UNAVAILABLE


def test_subnormal_measured_advance_preserves_units_without_infinite_eta():
    clock = [0.]
    hub = ProgressHub(clock=lambda: clock[0])
    task = hub.start_measured('Measured duration', total=1.)
    clock[0] = 2.
    measured = math.nextafter(0., 1.)
    snapshot = task.update(completed=measured)
    assert snapshot.completed == measured and snapshot.total == 1.
    assert snapshot.eta_seconds is None
    description = snapshot.describe()
    assert 'Estimated remaining' not in description
    assert snapshot.state is ProgressState.RUNNING and snapshot.percent == 0


def test_idle_feedback_drains_and_an_actual_second_update_starts_a_successor():
    clock, workers, cues = [0.], [], []
    first, second = Event(), Event()
    class Sink:
        def play(self, tone, *, cancelled):
            assert not cancelled()
            workers.append(current_thread())
            cues.append(tone)
            (first if len(cues) == 1 else second).set()
    feedback = ProgressFeedback(ProgressPreferences(mode='beep', interval_seconds=1),
                                tone_sink=Sink(), clock=lambda: clock[0], idle_exit=True)
    task = ProgressHub(clock=lambda: clock[0]).start('Two measured notifications', total=2)
    try:
        feedback.update(task.snapshot())
        assert first.wait(3)
        workers[0].join(3)
        assert not workers[0].is_alive() and feedback._worker is None
        clock[0] = 2.
        task.advance()
        feedback.update(task.snapshot())
        assert second.wait(3)
        workers[1].join(3)
        assert len(cues) == 2 and workers[1] is not workers[0]
        assert not workers[1].is_alive() and feedback.quiescent
    finally:
        feedback.close()
        for worker in tuple(workers):
            worker.join(3)
            assert not worker.is_alive()



def test_terminal_state_cancels_the_real_running_notice_before_announcing_completion():
    clock, spoken = [0.], []
    entered, release, completed = Event(), Event(), Event()
    class Voice:
        quiescent = True
        def speak_progress(self, text, *, volume, cancelled):
            self.quiescent = False
            try:
                if not entered.is_set():
                    entered.set()
                    assert release.wait(3)
                    assert cancelled()
                elif not cancelled():
                    spoken.append(text)
                    completed.set()
            finally:
                self.quiescent = True
    voice = Voice()
    feedback = ProgressFeedback(ProgressPreferences(mode='speech', interval_seconds=1),
                                speech_provider=voice, clock=lambda: clock[0])
    task = ProgressHub(clock=lambda: clock[0]).start('Real latest notification', total=2)
    try:
        feedback.update(task.snapshot())
        assert entered.wait(3)
        clock[0] = 1.
        task.update(completed=2)
        task.succeed()
        feedback.update(task.snapshot())
        release.set()
        assert completed.wait(3)
        eventually(lambda: feedback.quiescent)
        assert len(spoken) == 1 and 'Completed successfully' in spoken[0]
        assert feedback.last_error == ''
    finally:
        release.set()
        feedback.close()
        if feedback._worker is not None:
            feedback._worker.join(3)
            assert not feedback._worker.is_alive()


def legacy_file(root):
    path = root / 'state' / 'ui' / 'progress.json'
    path.parent.mkdir(parents=True)
    payload = {'schema': 1, 'preferences': {'mode': 'both', 'interval_seconds': 110.,
        'volume': .1234567, 'quiet': False, 'background': True, 'mute_during_audio': True}}
    raw = (json.dumps(payload) + '\n').encode()
    path.write_bytes(raw)
    return path, raw


def test_legacy_preference_precision_and_original_producer_survive_explicit_save(tmp_path):
    path, raw = legacy_file(tmp_path)
    store = LibraryProgressPreferenceStore(tmp_path)
    value = store.load()
    assert isinstance(value, LegacyProgressPreferences)
    assert value.volume == 12.34567 and value.interval_seconds == 110
    assert path.read_bytes() == raw and not store.canonical_path.exists()
    store.save(replace(value, muted=True))
    saved = json.loads(path.read_bytes())
    assert saved['schema'] == 1 and saved['preferences']['volume'] == .1234567
    assert saved['preferences']['quiet'] is True
    assert not store.canonical_path.exists()
    assert LibraryProgressPreferenceStore(tmp_path).load() == replace(value, muted=True)


def test_canonical_preference_takes_priority_without_rewriting_legacy(tmp_path):
    path, raw = legacy_file(tmp_path)
    canonical = ProgressPreferenceStore(tmp_path / 'progress-preferences.json')
    canonical.load()
    wanted = ProgressPreferences(interval_seconds=7, volume=23)
    canonical.save(wanted)
    store = LibraryProgressPreferenceStore(tmp_path)
    assert store.load() == wanted and not store.legacy
    store.save(replace(wanted, muted=True))
    assert path.read_bytes() == raw


def test_new_canonical_preferences_cannot_be_overwritten_through_stale_legacy_review(tmp_path):
    path, raw = legacy_file(tmp_path)
    store = LibraryProgressPreferenceStore(tmp_path)
    value = store.load()
    canonical = ProgressPreferenceStore(tmp_path / 'progress-preferences.json')
    canonical.load()
    canonical.save(ProgressPreferences(volume=81))
    newer = canonical.path.read_bytes()
    with pytest.raises(ValueError, match='[Cc]anonical|reload|Reload'):
        store.save(replace(value, muted=True))
    assert path.read_bytes() == raw and canonical.path.read_bytes() == newer


def test_stale_legacy_preferences_keep_the_other_windows_actual_bytes(tmp_path):
    path, raw = legacy_file(tmp_path)
    store = LibraryProgressPreferenceStore(tmp_path)
    value = store.load()
    newer = raw.replace(b'110.0', b'115.0')
    path.write_bytes(newer)
    with pytest.raises(ValueError, match='changed'):
        store.save(replace(value, muted=True))
    assert path.read_bytes() == newer


def test_strict_canonical_writer_rejects_historical_precision_without_a_file(tmp_path):
    store = ProgressPreferenceStore(tmp_path / 'progress-preferences.json')
    store.load()
    with pytest.raises(ValueError):
        store.save(LegacyProgressPreferences(interval_seconds=110, volume=12.34567))
    assert not store.path.exists()


def test_postcommit_temporary_unlink_failure_does_not_reverse_saved_preferences(tmp_path, monkeypatch):
    store = ProgressPreferenceStore(tmp_path / 'progress-preferences.json')
    store.load()
    wanted = ProgressPreferences(volume=71)
    unlink = Path.unlink
    def fail_temporary(path, *args, **kwargs):
        if path.name.startswith('.progress-preferences.json-') and path.name.endswith('.tmp'):
            raise OSError('Authored late temporary cleanup failure')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail_temporary)
        store.save(wanted)
    assert store.warning and ProgressPreferenceStore(store.path).load() == wanted


@pytest.fixture
def native_panel(tmp_path, monkeypatch):
    module = runpy.run_path(str(Path(__file__).with_name('test_progress_media_integration.py')))
    owner = module['panel'].__wrapped__(tmp_path, monkeypatch)
    panel = next(owner)
    try:
        yield panel
    finally:
        try:
            next(owner)
        except StopIteration:
            pass


def test_legacy_panel_alias_keeps_phase_only_units_and_stale_tokens(native_panel):
    panel = native_panel
    task = panel.begin('Actual canonical counts', total=4)
    task.update(completed=1)
    alias = panel.legacy
    token = alias.event.token
    alias.update(None, None, 'Still the same measured work', token=token)
    assert panel.active_task is task
    assert (task.snapshot().completed, task.snapshot().total) == (1, 4)
    replacement = panel.begin('Different operation', total=2)
    assert alias.update(2, 4, token=token) is None
    assert replacement.snapshot().completed == 0


def test_legacy_alias_tokens_do_not_recur_after_library_root_change(native_panel, tmp_path):
    panel = native_panel
    first = panel.legacy.start('First library')
    other = tmp_path / 'second-library'
    other.mkdir()
    panel.frame.library.root = other
    panel.change_root(other)
    second = panel.legacy.start('Second library')
    assert first != second
    assert panel.legacy.update(1, 2, token=first) is None
    assert panel.active_task.snapshot().completed == 0


def test_capture_admission_and_worker_release_survive_native_panel_close(native_panel):
    panel = native_panel
    active = [True]
    release = panel.frame.live_media_feedback.hold(lambda: active[0])
    capture = panel.hold_audio(recording=True)
    playback = panel.hold_audio()
    assert not capture.ready and not playback.ready
    active[0] = False
    assert capture.ready and not playback.ready
    panel.close()
    queued = tuple(panel.fixture_callbacks)
    worker = Thread(target=lambda: (release(), capture.close(), playback.close()))
    worker.start()
    worker.join(3)
    assert not worker.is_alive()
    assert tuple(panel.fixture_callbacks) == queued
    assert not panel.media.holds
