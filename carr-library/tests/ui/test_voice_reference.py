"""Real borrowed thread plus faithful modal lifetime; no audio/model claim here."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import pytest

from carr_library.audio.voice_reference import (ReferenceDescription, ReferenceReview,
    ReferencePackageResult, VoiceReferenceService)
from carr_library.audio.voice_packages import VoicePackageManager
from carr_library.runtime_settings import load_settings, save_settings
from carr_library.ui.voice_reference import VoiceReferenceDialog


class Host:
    def __init__(self, root):
        self.root, self.main = root, threading.get_ident()
        self.dead = self.running = self.revoked = self.stopped = False
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = self.callbacks = None
        self.calls, self.saved, self.commit_flags = [], [], []
        save_settings(root / 'state', load_settings(root / 'state'))
        self.service = VoiceReferenceService(root)
        self.service.review = self.review
        self.service.create = self.create

    def gui(self):
        assert threading.get_ident() == self.main

    def guard(self):
        self.gui()
        if self.dead or self.revoked:
            raise ValueError('The current parent library scope was revoked.')

    def submit(self, work, done, *, on_error, committed=False):
        self.gui()
        if self.dead or self.running:
            return False
        self.running, self.stopped = True, False
        self.callbacks = done, on_error
        self.commit_flags.append(committed)
        self.future = self.pool.submit(work)
        return True

    def finish(self):
        self.gui()
        done, failed = self.callbacks
        self.callbacks = None
        self.running = False
        try:
            value = self.future.result(timeout=5)
        except Exception as error:
            failed(error)
        else:
            done(value)

    def review(self, source, description, *, consent, cancelled):
        assert threading.get_ident() != self.main
        self.calls.append(('review', consent))
        if cancelled():
            raise InterruptedError('Cancelled controlled review.')
        doc = {'files': {'reference.txt': {'sha256': sha256(
            description.reference_text.encode()).hexdigest()}}}
        pcm = dict(frames=1, sample_rate=16000, channels=1, seconds=1 / 16000,
            samples=1, peak=100, rms_dbfs=-50, peak_dbfs=-50,
            full_scale_samples=0, zero_samples=0)
        return ReferenceReview(str(Path(source).absolute()), (1, 2, 3, 4, 5), 'a' * 64, 3,
            description, (1, 2), 'b' * 64, 1, '{}', json.dumps(pcm), json.dumps(doc))

    def create(self, review, target, *, consent, quality_reviewed, cancelled):
        assert threading.get_ident() != self.main
        self.calls.append(('create', consent, quality_reviewed))
        if cancelled():
            raise InterruptedError('Cancelled controlled package creation.')
        target = Path(target)
        target.write_bytes(b'actual controlled UI receipt bytes, not a voice package')
        return ReferencePackageResult(str(target), sha256(target.read_bytes()).hexdigest(),
            target.stat().st_size, review.fingerprint, review.description.profile_id,
            review.description.version)


@pytest.fixture
def view(tmp_path, monkeypatch):
    host = Host(tmp_path)
    wx = SimpleNamespace(on_show=lambda window: None, on_picker=lambda: None,
        picker_answer=5100, picker_path=str(tmp_path / 'selected.dordiai-voice'))
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
                 'TE_MULTILINE', 'TE_READONLY', 'EXPAND', 'ALL', 'LEFT', 'TOP',
                 'ALIGN_CENTER_VERTICAL', 'FD_OPEN', 'FD_FILE_MUST_EXIST', 'FD_SAVE',
                 'TAB_TRAVERSAL', 'VSCROLL')
    for index, name in enumerate(constants):
        setattr(wx, name, 1 << index)
    wx.ID_ANY, wx.ID_CANCEL, wx.ID_OK = -1, 5101, 5100
    wx.EVT_TEXT, wx.EVT_BUTTON, wx.EVT_CHECKBOX = 'text', 'button', 'checkbox'
    wx.EVT_CLOSE, wx.EVT_WINDOW_DESTROY = 'close', 'destroy'
    ids = iter(range(100, 10000))

    class Widget:
        def __init__(self, parent=None, *args, value='', label='', **kwargs):
            self.parent, self.value, self.label = parent, value, label
            self.id, self.dead = next(ids), False
            self.bindings, self.enabled, self.destroy_count = {}, True, 0

        def live(self):
            host.gui()
            node = self
            while isinstance(node, Widget):
                assert not node.dead, 'Deleted native wrapper was touched.'
                node = node.parent

        def SetName(self, name): self.live(); self.name = name
        def GetLabel(self): self.live(); return self.label
        def GetId(self): self.live(); return self.id
        def SetValue(self, value): self.live(); self.value = value
        def ChangeValue(self, value): self.SetValue(value)
        def GetValue(self): self.live(); return self.value
        def SetFocus(self): self.live()
        def SetInsertionPoint(self, value): self.live()
        def Enable(self, value=True): self.live(); self.enabled = value
        def SetSizer(self, sizer): self.live()
        def SetMinSize(self, size): self.live()
        def SetScrollRate(self, *args): self.live()
        def FitInside(self): self.live()
        def SetEscapeId(self, value): self.live()
        def Bind(self, event, callback): self.live(); self.bindings[event] = callback
        def IsModal(self): self.live(); return getattr(self, 'modal', False)
        def Hide(self): self.live(); self.hidden = True
        def EndModal(self, value):
            self.live()
            assert self.IsModal(), 'Only the running owned modal can end.'
            self.modal_result, self.modal = value, False
        def ShowModal(self):
            self.live()
            self.modal = True
            try:
                wx.on_show(self)
                return wx.ID_CANCEL
            finally:
                self.modal = False
        def Destroy(self):
            self.live()
            self.destroy_count += 1
            self.dead = True  # SIP may remove the original wrapper before the event.
            event = SimpleNamespace(GetId=lambda: self.id, Skip=lambda: None,
                GetEventObject=lambda: pytest.fail('Do not dereference a dying native owner.'))
            self.bindings[wx.EVT_WINDOW_DESTROY](event)

    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): host.gui()
        def AddGrowableCol(self, *args, **kwargs): host.gui()

    class Picker:
        def __init__(self, *args, **kwargs): host.gui()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def ShowModal(self): wx.on_picker(); return wx.picker_answer
        def GetPath(self): return wx.picker_path

    for name in ('Dialog', 'ScrolledWindow', 'TextCtrl', 'StaticText', 'CheckBox', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = wx.WrapSizer = wx.FlexGridSizer = Sizer
    wx.FileDialog = Picker
    monkeypatch.setitem(sys.modules, 'wx', wx)
    controller = VoiceReferenceDialog(None, service=host.service, submit=host.submit,
        alive=lambda: not host.dead, busy=lambda: host.running, cancelled=lambda: host.stopped,
        guard=host.guard, saved=host.saved.append)
    try:
        yield host, controller, wx
    finally:
        controller.cancelled.set()
        if not controller.destroyed:
            controller.dialog.Destroy()
        host.pool.shutdown(wait=True, cancel_futures=True)


def fill(host, view):
    fields = dict(source=str(host.root / 'selected.wav'), profile_id='fixture', version='1.0.0',
        name='Synthetic UI reference', language='uk', provenance_source='UI fixture only',
        rights_notice='No real voice assets')
    for name, value in fields.items():
        view.fields[name].ChangeValue(value)
    view.transcript.ChangeValue('Exact controlled transcript.')
    view.changed()
    view.consent.SetValue(True)
    view.consent_changed()


def ready(host, view):
    fill(host, view)
    assert view.review_input()
    host.finish()
    assert view.review is not None
    view.quality.SetValue(True)
    view.sync()


def test_no_implicit_permission_or_quality_confirmation(view):
    host, controller, _ = view
    assert not controller.consent.GetValue() and not controller.quality.GetValue()
    assert not controller.quality.enabled and not controller.create_button.enabled
    assert controller.review_input() is False and host.future is None


def test_actual_borrowed_worker_review_then_explicit_local_handoff(view):
    host, controller, wx = view
    ready(host, controller)
    assert host.calls == [('review', True)]
    assert controller.create_button.enabled and not Path(wx.picker_path).exists()
    assert controller.create_package()
    host.finish()
    assert host.calls == [('review', True), ('create', True, True)]
    assert controller.result.path == wx.picker_path
    assert host.saved == [Path(wx.picker_path)]
    assert 'UNENCRYPTED' in controller.status.GetValue()
    assert 'separate installation' in controller.status.GetValue()
    assert controller.review is None and not controller.quality.GetValue()


def test_input_edits_remove_prior_consent_and_review(view):
    host, controller, _ = view
    ready(host, controller)
    controller.transcript.ChangeValue('Different text.')
    controller.changed()
    assert controller.review is None and not controller.consent.GetValue()
    assert not controller.quality.GetValue() and not controller.create_button.enabled
    assert controller.create_package() is False
    assert host.calls == [('review', True)]


def test_relative_selected_path_is_normalized_only_from_the_worker_result(view):
    host, controller, _ = view
    fill(host, controller)
    controller.fields['source'].ChangeValue('selected.wav')
    assert controller.review_input()
    host.finish()
    assert controller.fields['source'].GetValue() == str(Path('selected.wav').absolute())
    assert controller.review.source == controller.fields['source'].GetValue()


@pytest.mark.parametrize('reason', ['cancel', 'inputs', 'scope', 'destroy'])
def test_queued_review_cannot_become_current_after_invalidation(view, reason):
    host, controller, _ = view
    fill(host, controller)
    assert controller.review_input()
    host.future.result(timeout=5)  # Hold the real GUI completion, not the work.
    if reason == 'cancel':
        controller.cancel()
    elif reason == 'inputs':
        controller.fields['name'].ChangeValue('New name')
        controller.changed()
    elif reason == 'scope':
        host.revoked = True
    else:
        controller.dialog.Destroy()
    host.finish()
    assert controller.review is None and host.saved == []
    if reason != 'destroy':
        assert not controller.create_button.enabled


@pytest.mark.parametrize('answer', [5100, 5101], ids=['accept-picker', 'cancel-picker'])
def test_forced_close_inside_destination_picker_never_touches_destroyed_controls(view, answer):
    host, controller, wx = view
    ready(host, controller)
    wx.picker_answer = answer
    wx.on_picker = controller.dialog.Destroy
    assert controller.create_package() is False
    assert host.calls == [('review', True)]
    assert controller.destroyed and not Path(wx.picker_path).exists()


@pytest.mark.parametrize('reason', ['cancel', 'scope', 'field'])
def test_destination_picker_rechecks_authority_before_second_dispatch(view, reason):
    host, controller, wx = view
    ready(host, controller)
    def change():
        if reason == 'cancel':
            host.stopped = True
        elif reason == 'scope':
            host.revoked = True
        else:
            controller.transcript.ChangeValue('Changed in picker')
            controller.changed()
    wx.on_picker = change
    assert controller.create_package() is False
    assert host.calls == [('review', True)] and not Path(wx.picker_path).exists()


def test_late_cancel_after_actual_controlled_commit_is_reported_as_saved(view):
    host, controller, wx = view
    ready(host, controller)
    assert controller.create_package()
    result = host.future.result(timeout=5)
    assert Path(result.path).is_file()
    controller.cancel()
    host.finish()
    assert controller.result is result and host.saved == [Path(result.path)]
    assert 'Saved local UNENCRYPTED' in controller.status.GetValue()
    assert sha256(Path(result.path).read_bytes()).hexdigest() == result.sha256


def test_failed_parent_handoff_does_not_turn_a_saved_file_into_a_failed_operation(view):
    host, controller, _ = view
    ready(host, controller)
    controller.saved = lambda path: (_ for _ in ()).throw(RuntimeError('Parent handoff unavailable.'))
    assert controller.create_package()
    host.finish()
    assert controller.result is not None and Path(controller.result.path).is_file()
    assert 'Saved local UNENCRYPTED' in controller.status.GetValue()
    assert 'handoff is unavailable' in controller.status.GetValue()


def test_direct_destroy_during_show_skips_a_second_destroy(view):
    host, controller, wx = view
    wx.on_show = lambda window: window.Destroy()
    assert controller.show() is None
    assert controller.destroyed and controller.dialog.destroy_count == 1


def test_child_destroy_does_not_revoke_the_owner(view):
    _, controller, _ = view
    event = SimpleNamespace(GetId=lambda: controller._window_id + 1, Skip=lambda: None,
        GetEventObject=lambda: pytest.fail('No native owner dereference.'))
    controller._destroyed(event)
    assert not controller.closed and not controller.destroyed


def test_existing_parent_work_is_not_replaced_or_given_another_worker(view):
    host, controller, _ = view
    fill(host, controller)
    host.running = True
    assert controller.review_input() is False
    assert host.future is None and not controller.busy


def test_recorder_is_explicit_and_only_selects_a_new_unapproved_input(view):
    host, controller, _ = view
    ready(host, controller)
    called = []
    def record(window, selected):
        called.append(window)
        selected(host.root / 'verified-recording.wav')
    controller.record = record
    assert called == []
    controller.open_recorder()
    assert called == [controller.dialog]
    assert controller.fields['source'].GetValue() == str(host.root / 'verified-recording.wav')
    assert controller.review is None
    assert not controller.consent.GetValue() and not controller.quality.GetValue()
    assert host.calls == [('review', True)]  # No recording, review or inference is launched implicitly.


@pytest.mark.parametrize('change', ['close', 'fields', 'scope', 'busy'])
def test_delayed_recorder_selection_cannot_cross_reference_scope(view, change):
    host, controller, _ = view
    fill(host, controller)
    pending = []
    controller.record = lambda window, selected: pending.append(selected)
    controller.open_recorder()
    before = controller.fields['source'].GetValue()
    if change == 'close':
        controller.dialog.Destroy()
    elif change == 'fields':
        controller.changed()
    elif change == 'scope':
        host.revoked = True
    else:
        controller.busy = True
    pending[0](host.root / 'late-recording.wav')
    if change != 'close':
        assert controller.fields['source'].GetValue() == before
    assert host.calls == []


def _package_controller(root, frame=None):
    calls = []
    if frame is None:
        frame = SimpleNamespace(library=SimpleNamespace(root=root), _closing=False,
                                run_job=lambda *args, **kwargs: None)
    library = frame.library
    original = root.stat()
    save_settings(root / 'state', load_settings(root / 'state'))
    manager = VoicePackageManager(root)
    manager.inspect = lambda path, **kwargs: calls.append(('inspect', path)) or 'reviewed'
    control = SimpleNamespace(closed=False, busy=False, manager=manager,
        frame=frame, library=library, root=root, cancelled=threading.Event(), dialog=object(),
        invalidate=lambda: calls.append(('invalidate',)),
        reviewed=lambda value: calls.append(('reviewed', value)))
    def guard():
        info = root.stat()
        if (control.closed or frame._closing or frame.library is not library
                or library.root != root or manager.store.root != root
                or (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino)):
            raise InterruptedError('The current library changed; reopen reference review.')
    def current():
        try:
            guard()
        except (OSError, ValueError, RuntimeError, InterruptedError):
            return False
        return True
    control._guard, control._current = guard, current
    control._cancelled = control.cancelled.is_set
    def start(work, complete, *, on_error=None, committed=False):
        calls.append(('start',))
        try:
            complete(work())
        except Exception as exc:
            if on_error is None:
                raise
            on_error(exc)
        return True
    control.start = start
    return control, calls

def test_package_adapter_reuses_exact_nested_desktop_recorder_and_captured_scope(tmp_path, monkeypatch):
    from carr_library.ui import voice_reference as module
    library = SimpleNamespace(root=tmp_path)
    desktop = SimpleNamespace(library=library, _closing=False, run_job=lambda *a, **k: None)
    other_root = SimpleNamespace(library=SimpleNamespace(root=tmp_path / 'other'),
        run_job=lambda *a, **k: pytest.fail('Do not select another library frame.'),
        GetParent=lambda: desktop)
    middle = SimpleNamespace(GetParent=lambda: other_root)
    controller, calls = _package_controller(tmp_path, desktop)
    controller.dialog = SimpleNamespace(GetParent=lambda: pytest.fail('Do not infer authority from a native parent.'))
    received, recorder_calls = [], []
    reference_dialog = object()
    def recorder(frame, *, on_selected):
        assert frame is desktop
        recorder_calls.append(frame)
        on_selected({'id': 'verified-record', 'path': str(tmp_path / 'recorded.wav')})
    monkeypatch.setitem(sys.modules, 'carr_library.ui.recording_dialog',
        SimpleNamespace(show_recording=recorder))
    def show(parent, **kwargs):
        assert parent is controller.dialog and calls == [] and recorder_calls == []
        # The service guard must work on the existing worker without touching wx.
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(kwargs['service'].scope_guard).result(timeout=5)
        kwargs['record'](reference_dialog, received.append)
        desktop.library = SimpleNamespace(root=tmp_path)
        with pytest.raises(InterruptedError, match='library changed'):
            kwargs['guard']()
        return None
    monkeypatch.setattr(module, 'show_voice_reference', show)
    assert module.show_reference_from_packages(controller) is None
    assert received == [str(tmp_path / 'recorded.wav')] and recorder_calls == [desktop]
    assert calls == []


def test_package_adapter_without_explicit_desktop_cannot_create_a_fallback_dispatcher(tmp_path, monkeypatch):
    from carr_library.ui import voice_reference as module
    controller, calls = _package_controller(tmp_path)
    del controller.frame
    monkeypatch.setattr(module, 'show_voice_reference',
        lambda *args, **kwargs: pytest.fail('No current host was captured.'))
    with pytest.raises(TypeError, match='captured desktop job owner'):
        module.show_reference_from_packages(controller)
    assert calls == []


def test_package_handoff_uses_read_only_parent_inspection_and_never_applies(tmp_path, monkeypatch):
    from carr_library.ui import voice_reference as module
    controller, calls = _package_controller(tmp_path, None)
    result = ReferencePackageResult(str(tmp_path / 'saved.zip'), 'a' * 64, 100,
        'b' * 64, 'fixture', '1.0.0')
    monkeypatch.setattr(module, 'show_voice_reference', lambda *a, **k: result)
    assert module.show_reference_from_packages(controller) == result
    assert calls == [('invalidate',), ('start',), ('inspect', result.path), ('reviewed', 'reviewed')]


def test_parent_root_change_during_reference_dialog_preserves_saved_result_without_handoff(tmp_path, monkeypatch):
    from carr_library.ui import voice_reference as module
    controller, calls = _package_controller(tmp_path, None)
    result = ReferencePackageResult(str(tmp_path / 'saved.zip'), 'a' * 64, 100,
        'b' * 64, 'fixture', '1.0.0')
    def show(*args, **kwargs):
        controller.manager.store.root = tmp_path / 'replacement'
        return result
    monkeypatch.setattr(module, 'show_voice_reference', show)
    assert module.show_reference_from_packages(controller) == result
    assert calls == []


def test_reference_create_preserves_committed_flag_through_borrowed_dispatcher(view):
    host, controller, _ = view
    ready(host, controller)
    assert host.commit_flags == [False]
    assert controller.create_package()
    host.finish()
    assert host.commit_flags == [False, True]
    assert controller.result is not None and Path(controller.result.path).is_file()


def test_package_adapter_propagates_actual_host_refusal_to_reference_child(tmp_path, monkeypatch):
    from carr_library.ui import voice_reference as module
    controller, calls = _package_controller(tmp_path)
    received = []
    def refused(*args, **kwargs):
        received.append(kwargs.get('committed'))
        return False
    controller.start = refused
    def show(parent, **kwargs):
        assert kwargs['submit'](lambda: pytest.fail('Refused work ran.'), lambda result: None,
                                on_error=lambda error: None, committed=True) is False
        return None
    monkeypatch.setattr(module, 'show_voice_reference', show)
    assert module.show_reference_from_packages(controller) is None
    assert received == [True] and calls == []


def test_adapter_root_replacement_cannot_admit_recorder_selection_or_worker(tmp_path, monkeypatch):
    from carr_library.ui import voice_reference as module
    root = tmp_path / 'library'
    root.mkdir()
    controller, calls = _package_controller(root)
    moved = tmp_path / 'retained-library'
    recorder_calls = []
    monkeypatch.setitem(sys.modules, 'carr_library.ui.recording_dialog',
        SimpleNamespace(show_recording=lambda *args, **kwargs: recorder_calls.append(True)))
    def show(parent, **kwargs):
        root.rename(moved)
        root.mkdir()
        canary = root / 'canary.txt'
        canary.write_text('untouched', encoding='utf-8')
        assert kwargs['alive']() is False
        with pytest.raises(InterruptedError, match='library changed'):
            kwargs['record'](object(), lambda path: pytest.fail('A stale reference was selected.'))
        with pytest.raises(InterruptedError, match='library changed'):
            kwargs['submit'](lambda: pytest.fail('A stale worker started.'), lambda value: None,
                             on_error=lambda error: None)
        assert list(root.iterdir()) == [canary] and canary.read_text(encoding='utf-8') == 'untouched'
        return None
    monkeypatch.setattr(module, 'show_voice_reference', show)
    try:
        assert module.show_reference_from_packages(controller) is None
        assert calls == [] and recorder_calls == []
    finally:
        if moved.exists():
            if (root / 'canary.txt').exists():
                (root / 'canary.txt').unlink()
            root.rmdir()
            moved.rename(root)


@pytest.mark.parametrize('modal', [False, True])
def test_repeated_reference_close_revokes_once_and_never_ends_returned_modal(view, modal):
    host, controller, wx = view
    controller.dialog.modal = modal
    before = controller.generation
    controller.close()
    assert controller.closed and controller.cancelled.is_set() and controller.review is None
    assert controller.generation == before + 1
    if modal:
        assert controller.dialog.modal_result == wx.ID_CANCEL and not controller.dialog.modal
    else:
        assert controller.dialog.hidden and not hasattr(controller.dialog, 'modal_result')
    controller.close()
    assert controller.generation == before + 1
    controller.dialog.Destroy()
    controller.close()  # No native access after the destroy callback either.
    assert controller.destroyed and host.calls == []
