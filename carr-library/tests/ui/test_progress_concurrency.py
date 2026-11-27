"""Observed preference conflicts and the existing native-presenter boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace

import pytest

from carr_library import progress
from carr_library.ui import operation_progress as ui

BOUNDARY = runpy.run_path(str(Path(__file__).with_name('test_operation_progress.py')))


@contextmanager
def presenter(root):
    frame = SimpleNamespace(library=SimpleNamespace(root=root), messages=[])
    frame.report = frame.messages.append
    wx = BOUNDARY['NativeBoundary']()
    wx.active = frame
    audio = BOUNDARY['Notifications']()
    value = ui.OperationProgress(frame, BOUNDARY['Control'](), BOUNDARY['Control'](),
        wx_module=wx, notifications=audio, start_timer=False)
    try:
        yield value
    finally:
        value.close()


def write(root, selected):
    raw = progress.encode_preferences(selected)
    (root / progress.FILENAME).write_bytes(raw)
    return raw


def test_absent_snapshot_is_passive_and_has_a_distinct_absence_token(tmp_path):
    assert progress.preference_snapshot(tmp_path) == (None, None)
    assert progress.load_preferences(tmp_path) == progress.ProgressPreferences()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('raw', [
    progress.encode_preferences(progress.ProgressPreferences(mode='beep')),
    b'{"version":1,"preferences":{"mode":"beep","interval_seconds":5,"volume":35,'
    b'"muted":false,"allow_background":false,"quiet_during_media":true}}',
])
def test_snapshot_binds_exact_current_or_canonical_bytes_without_rewriting(tmp_path, raw):
    path = tmp_path / progress.FILENAME
    path.write_bytes(raw)
    before = path.stat().st_mtime_ns
    assert progress.preference_snapshot(tmp_path) == (raw, sha256(raw).hexdigest())
    assert progress.load_preferences(tmp_path).mode == 'beep'
    assert path.read_bytes() == raw and path.stat().st_mtime_ns == before


@pytest.mark.parametrize('change', ['created', 'replaced', 'deleted', 'malformed'])
def test_observed_save_refuses_foreign_state_including_absent_to_created(tmp_path, change):
    path = tmp_path / progress.FILENAME
    if change != 'created':
        write(tmp_path, progress.ProgressPreferences())
    _, observed = progress.preference_snapshot(tmp_path)
    if change == 'deleted':
        path.unlink()
        foreign = None
    elif change == 'malformed':
        foreign = b'externally changed invalid JSON'
        path.write_bytes(foreign)
    else:
        previous = path.stat() if path.exists() else None
        foreign = write(tmp_path, progress.ProgressPreferences(volume=.67))
        if previous is not None:
            os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    with pytest.raises(ValueError, match='changed in another window'):
        progress.save_preferences(tmp_path, progress.ProgressPreferences(mode='beep'),
                                  expected_sha256=observed)
    assert (path.read_bytes() if path.exists() else None) == foreign
    assert not list(tmp_path.glob('.progress-*.tmp'))


@pytest.mark.parametrize('explicit', [False, True])
def test_changed_bytes_after_stage_flush_refuse_publication_even_without_an_observed_token(tmp_path, monkeypatch, explicit):
    write(tmp_path, progress.ProgressPreferences())
    _, observed = progress.preference_snapshot(tmp_path)
    foreign = progress.encode_preferences(progress.ProgressPreferences(volume=.61))
    fsync = progress.os.fsync
    changed = []
    def inject(descriptor):
        fsync(descriptor)
        if not changed:
            changed.append(True)
            (tmp_path / progress.FILENAME).write_bytes(foreign)
    monkeypatch.setattr(progress.os, 'fsync', inject)
    kwargs = {'expected_sha256': observed} if explicit else {}
    with pytest.raises(ValueError, match='changed before saving'):
        progress.save_preferences(tmp_path, progress.ProgressPreferences(mode='beep'), **kwargs)
    assert changed and (tmp_path / progress.FILENAME).read_bytes() == foreign
    assert not list(tmp_path.glob('.progress-*.tmp'))


@pytest.mark.parametrize('boundary', ['stage-flush', 'replace'])
def test_precommit_failure_retains_old_selection_and_removes_only_owned_stage(tmp_path, monkeypatch, boundary):
    original = write(tmp_path, progress.ProgressPreferences(volume=.24))
    _, observed = progress.preference_snapshot(tmp_path)
    def fail(*args, **kwargs):
        raise OSError('synthetic precommit refusal')
    monkeypatch.setattr(progress.os, 'fsync' if boundary == 'stage-flush' else 'replace', fail)
    with pytest.raises(OSError, match='synthetic precommit refusal'):
        progress.save_preferences(tmp_path, progress.ProgressPreferences(mode='beep'),
                                  expected_sha256=observed)
    assert (tmp_path / progress.FILENAME).read_bytes() == original
    assert not list(tmp_path.glob('.progress-*.tmp'))


@pytest.mark.parametrize('boundary', ['directory-open', 'directory-flush'])
def test_postcommit_directory_flush_failure_remains_committed_success(tmp_path, monkeypatch, boundary):
    original = write(tmp_path, progress.ProgressPreferences())
    selected = progress.ProgressPreferences(mode='beep', volume=.42)
    sentinel = tmp_path / 'inert-directory-flush-descriptor'
    sentinel.write_bytes(b'fixture descriptor only')
    old_open, old_fsync = progress.os.open, progress.os.fsync
    directory_flag = getattr(progress.os, 'O_DIRECTORY', 0x10000000)
    monkeypatch.setattr(progress.os, 'O_DIRECTORY', directory_flag, raising=False)
    opened = []
    def open_directory(path, flags, *args, **kwargs):
        if Path(path) == tmp_path and flags & directory_flag:
            if boundary == 'directory-open':
                raise OSError('synthetic directory open failure after publication')
            descriptor = old_open(sentinel, os.O_RDONLY)
            opened.append(descriptor)
            return descriptor
        return old_open(path, flags, *args, **kwargs)
    def flush(descriptor):
        if descriptor in opened:
            raise OSError('synthetic directory flush failure after publication')
        return old_fsync(descriptor)
    monkeypatch.setattr(progress.os, 'open', open_directory)
    monkeypatch.setattr(progress.os, 'fsync', flush)
    assert progress.save_preferences(tmp_path, selected,
        expected_sha256=sha256(original).hexdigest()) == selected
    assert progress.load_preferences(tmp_path) == selected
    assert not list(tmp_path.glob('.progress-*.tmp'))
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_two_actual_writers_cannot_both_publish_from_the_same_observation(tmp_path):
    barrier = threading.Barrier(2)
    def attempt(volume):
        _, observed = progress.preference_snapshot(tmp_path)
        barrier.wait(timeout=10)
        try:
            progress.save_preferences(tmp_path, progress.ProgressPreferences(volume=volume),
                                      expected_sha256=observed)
            return 'saved', volume
        except (ValueError, RuntimeError) as error:
            return 'refused', str(error)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (.22, .77)))
    assert sorted(row[0] for row in results) == ['refused', 'saved']
    saved = next(row[1] for row in results if row[0] == 'saved')
    assert progress.load_preferences(tmp_path).volume == saved
    assert not list(tmp_path.glob('.progress-*.tmp'))


def test_presenter_conflict_preserves_its_observation_runtime_and_foreign_settings(tmp_path):
    with presenter(tmp_path) as first, presenter(tmp_path) as second:
        chosen = progress.ProgressPreferences(mode='beep', volume=.44)
        first.configure(chosen)
        foreign = (tmp_path / progress.FILENAME).read_bytes()
        before = (second.preferences, second._preferences_sha256, list(second.notifications.configured))
        with pytest.raises(ValueError, match='changed in another window'):
            second.configure(progress.ProgressPreferences(mode='beep', volume=.75))
        assert (second.preferences, second._preferences_sha256, second.notifications.configured) == before
        assert (tmp_path / progress.FILENAME).read_bytes() == foreign
        first.configure(replace(chosen, volume=.31))
        assert progress.load_preferences(tmp_path).volume == .31


def test_readable_invalid_settings_have_an_exact_explicit_repair_token(tmp_path):
    raw = b'old invalid preferences retained for explicit review'
    (tmp_path / progress.FILENAME).write_bytes(raw)
    with presenter(tmp_path) as view:
        assert view.load_notice and view.preferences.mode == 'off'
        assert view._preferences_sha256 == sha256(raw).hexdigest()
        view.configure(progress.ProgressPreferences(mode='beep'))
        assert progress.load_preferences(tmp_path).mode == 'beep'
        assert view._preferences_sha256 == sha256((tmp_path / progress.FILENAME).read_bytes()).hexdigest()


def test_unreadable_linked_settings_never_gain_a_replacement_token(tmp_path):
    raw = write(tmp_path, progress.ProgressPreferences(volume=.69))
    os.link(tmp_path / progress.FILENAME, tmp_path / 'external-link')
    with presenter(tmp_path) as view:
        assert view.load_notice and view.preferences.mode == 'off'
        with pytest.raises(ValueError, match='could not be observed'):
            view.configure(progress.ProgressPreferences(mode='beep'))
        assert (tmp_path / progress.FILENAME).read_bytes() == raw


def test_immediate_mute_survives_a_save_conflict_without_overwriting_the_other_window(tmp_path):
    with presenter(tmp_path) as view:
        observed = view._preferences_sha256
        foreign = write(tmp_path, progress.ProgressPreferences(mode='beep', volume=.72))
        view.toggle_mute()
        assert view.preferences.muted and view.notifications.preferences.muted
        assert 'Muted for this session' in view.notice
        assert view._preferences_sha256 == observed
        assert (tmp_path / progress.FILENAME).read_bytes() == foreign


class ModalBoundary:
    """Deterministic dialog decisions; actual wx qualification remains separate."""
    DEFAULT_DIALOG_STYLE = RESIZE_BORDER = VERTICAL = ALL = EXPAND = 1
    OK, CANCEL, ID_OK, ID_CANCEL = 1, 2, 5100, 5101
    EVT_BUTTON = object()

    def __init__(self, native, action):
        self.native, self.action, self.last = native, action, None

    def __getattr__(self, name):
        return getattr(self.native, name)

    def Dialog(self, frame, **kwargs):
        owner = self
        class Dialog:
            def __init__(self):
                self.controls, self.save, self.result = {}, None, owner.ID_CANCEL
            def SetSizer(self, value): pass
            def CreateButtonSizer(self, flags): return object()
            def Bind(self, event, action, *, id):
                assert id == owner.ID_OK
                self.save = action
            def EndModal(self, result): self.result = result
            def ShowModal(self):
                owner.action(self)
                return self.result
            def Destroy(self): self.destroyed = True
        self.last = Dialog()
        return self.last

    def BoxSizer(self, direction):
        return SimpleNamespace(Add=lambda *args, **kwargs: None)

    def control(self, parent, *, value='', label='', choices=None):
        class Field:
            def __init__(self):
                self.value, self.selection, self.label = value, 0, label
            def SetName(self, name): parent.controls[name] = self
            def SetSelection(self, selection): self.selection = selection
            def GetSelection(self): return self.selection
            def SetValue(self, selected): self.value = selected
            def GetValue(self): return self.value
            def SetLabel(self, selected): self.label = selected
            def SetFocus(self): pass
            def Bind(self, *args, **kwargs): pass
            def Enable(self, *args): pass
            def Disable(self): pass
        return Field()

    StaticText = TextCtrl = Choice = CheckBox = Button = control


def test_reopened_modal_displays_foreign_selection_but_cancel_keeps_runtime_and_old_token(tmp_path):
    with presenter(tmp_path) as view:
        before = (view.preferences, view._preferences_sha256, list(view.notifications.configured))
        foreign = write(tmp_path, progress.ProgressPreferences(mode='beep', volume=.68))
        def cancel(dialog):
            assert dialog.controls['Progress audio mode'].GetSelection() == 1
            assert float(dialog.controls['Notification volume, 0 to 1'].GetValue()) == .68
        view.wx = ModalBoundary(view.wx, cancel)
        assert ui.show_progress_settings(view) is False
        assert (view.preferences, view._preferences_sha256, view.notifications.configured) == before
        assert (tmp_path / progress.FILENAME).read_bytes() == foreign


def test_reopened_modal_ok_saves_only_the_newly_reviewed_observation(tmp_path):
    with presenter(tmp_path) as view:
        write(tmp_path, progress.ProgressPreferences(mode='beep', volume=.68))
        def save(dialog):
            dialog.controls['Notification volume, 0 to 1'].SetValue('.35')
            dialog.save(None)
        view.wx = ModalBoundary(view.wx, save)
        assert ui.show_progress_settings(view) is True
        assert view.preferences.volume == .35 and progress.load_preferences(tmp_path).volume == .35
        assert view._preferences_sha256 == sha256((tmp_path / progress.FILENAME).read_bytes()).hexdigest()


def test_modal_ok_rejects_a_change_after_the_displayed_observation(tmp_path):
    with presenter(tmp_path) as view:
        initial = (view.preferences, view._preferences_sha256)
        foreign = progress.encode_preferences(progress.ProgressPreferences(volume=.81))
        def changed(dialog):
            (tmp_path / progress.FILENAME).write_bytes(foreign)
            dialog.save(None)
            assert 'changed in another window' in dialog.controls['Progress settings status'].label
        view.wx = ModalBoundary(view.wx, changed)
        assert ui.show_progress_settings(view) is False
        assert (view.preferences, view._preferences_sha256) == initial
        assert (tmp_path / progress.FILENAME).read_bytes() == foreign


@pytest.mark.parametrize('token', [True, 0, '', 'a' * 40, 'A' * 64, 'g' * 64])
def test_invalid_observation_never_mutates_saved_settings(tmp_path, token):
    raw = write(tmp_path, progress.ProgressPreferences())
    with pytest.raises(ValueError, match='could not be observed'):
        progress.save_preferences(tmp_path, progress.ProgressPreferences(mode='beep'), expected_sha256=token)
    assert (tmp_path / progress.FILENAME).read_bytes() == raw
