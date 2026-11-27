"""Actual dialog callbacks with explicit control doubles; native peers are tested separately."""
from hashlib import sha256
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.models import AudioCancelled
from carr_library.transcription import TranscriptionService
from carr_library.ui.transcription_dialog import show_transcription


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Transcription lifetime')
    service = app.transcription(workspace)
    models = tmp_path / 'models'
    models.mkdir()
    worker = tmp_path / 'original-worker.py'
    worker.write_text('# Explicit synthetic source; no inference.\n', encoding='utf-8')
    replacement = tmp_path / 'selected-worker.py'
    replacement.write_text('# Another explicitly selected synthetic source.\n', encoding='utf-8')
    service.configure(Path(sys.executable).resolve(), worker, models)
    original = tmp_path / 'original.wav'
    original.write_bytes(b'Synthetic retained input, not a user recording.')
    identity = service.submit([original])
    state = SimpleNamespace(controls=[], pickers=[], jobs=[], callbacks=[], main=None,
                            scenario=lambda: None, picker_scenario=None, ended=[])
    class Control:
        def __init__(self, parent=None, *args, **kwargs):
            self.parent, self.identity = parent, 100 + len(state.controls)
            self.children, self.handlers = [], {}
            self.destroyed = self.pending = False
            self.destroy_calls, self.reads = 0, 0
            self.name = kwargs.get('label', '')
            self.title = kwargs.get('title', args[0] if args else '')
            self.value = kwargs.get('value', '')
            self.items, self.selection = list(kwargs.get('choices', ())), -1
            self.enabled = True
            if parent is not None:
                parent.children.append(self)
            state.controls.append(self)
        def check(self):
            peer = self
            while peer is not None:
                assert not peer.destroyed and not peer.pending, 'Accessed a dead or pending native control'
                peer = peer.parent
        def __bool__(self):
            return not self.destroyed
        def GetId(self):
            self.check()
            return self.identity
        def IsBeingDeleted(self):
            assert not self.destroyed, 'Inspected a deleted native wrapper'
            return self.pending
        def Bind(self, event, handler, *args):
            self.handlers.setdefault(event, []).append(handler)
        def Unbind(self, event, *, handler):
            self.handlers[event].remove(handler)
        def emit(self, event, identity=None):
            identity = self.identity if identity is None else identity
            value = SimpleNamespace(GetId=lambda: identity, Skip=lambda: None,
                                    CanVeto=lambda: True, Veto=lambda: None)
            for handler in tuple(self.handlers.get(event, ())):
                handler(value)
        def native_destroy(self, *, pending=False):
            if self.destroyed:
                return
            self.pending = True
            for child in self.children:
                child.native_destroy(pending=pending)
            if pending:
                return
            self.emit('destroy')
            parent = self.parent
            while parent is not None:
                parent.emit('destroy', self.identity)
                parent = parent.parent
            self.destroyed, self.pending = True, False
        def Destroy(self):
            self.check()
            self.destroy_calls += 1
            self.native_destroy()
        def SetName(self, value): self.check(); self.name = value
        def SetValue(self, value): self.check(); self.value = value
        def ChangeValue(self, value): self.check(); self.value = value
        def GetValue(self): self.check(); self.reads += 1; return self.value
        def SetSelection(self, value): self.check(); self.selection = value
        def GetSelection(self): self.check(); self.reads += 1; return self.selection
        def SetItems(self, values): self.check(); self.items, self.selection = list(values), -1
        def Enable(self, value=True): self.check(); self.enabled = value
        def SetFocus(self): self.check()
        def SetSizer(self, value): self.check()
        def EndModal(self, value): self.check(); state.ended.append(value)
    class Dialog(Control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state.main = self
        def ShowModal(self):
            state.scenario()
            return 2
    class Picker(Control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.selection = 0
            state.pickers.append(self)
        def ShowModal(self):
            if state.picker_scenario is not None:
                return state.picker_scenario(self)
            return 1
        def GetPath(self):
            self.check()
            self.reads += 1
            if 'Python локального' in self.title:
                return str(Path(sys.executable).resolve())
            if 'worker.py' in self.title:
                return str(replacement)
            if 'моделей' in self.title:
                return str(models)
            return str(tmp_path)
        def GetPaths(self):
            self.check()
            self.reads += 1
            return [str(original)]
        def GetValue(self):
            self.check()
            self.reads += 1
            return 'explicit-export'
    class Layout:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): pass
    frame = Control()
    frame.library, frame.active = app, (workspace,)
    frame.working = frame._closing = False
    frame.cancel_event = threading.Event()
    frame.report = lambda value: state.ended.append(str(value))
    def run_job(label, operation, *, done, failed):
        state.jobs.append(SimpleNamespace(label=label, operation=operation, done=done, failed=failed))
        return True
    frame.run_job = run_job
    wx = SimpleNamespace(Dialog=Dialog, TextCtrl=Control, Choice=Control, StaticText=Control,
        CheckBox=Control, Button=Control, BoxSizer=Layout, GridSizer=Layout,
        SingleChoiceDialog=Picker, FileDialog=Picker, DirDialog=Picker, TextEntryDialog=Picker,
        MessageBox=lambda *args, **kwargs: 1,
        CallAfter=lambda call, *args: state.callbacks.append(lambda: call(*args)),
        EVT_BUTTON='button', EVT_CHOICE='choice', EVT_TEXT='text', EVT_CLOSE='close',
        EVT_WINDOW_DESTROY='destroy')
    for name in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'ALL', 'EXPAND',
                 'TE_MULTILINE', 'TE_READONLY', 'YES_NO', 'NO_DEFAULT', 'FD_OPEN',
                 'FD_FILE_MUST_EXIST', 'DD_DIR_MUST_EXIST', 'FD_MULTIPLE'):
        setattr(wx, name, 0)
    wx.ID_OK, wx.YES, wx.ID_CLOSE = 1, 1, 2
    monkeypatch.setitem(sys.modules, 'wx', wx)
    state.app, state.service, state.identity = app, service, identity
    state.frame, state.wx, state.worker, state.replacement = frame, wx, worker, replacement
    state.original, state.Control = original, Control
    def find(name):
        return next(control for control in state.controls if control.name == name)
    def click(name):
        find(name).emit('button')
    def evidence():
        return {path.relative_to(app.root).as_posix(): sha256(path.read_bytes()).hexdigest()
                for path in (app.root / 'state' / 'transcription').rglob('*') if path.is_file()}
    state.find, state.click, state.evidence = find, click, evidence
    yield state


@pytest.mark.parametrize('target', ['owner', 'parent'])
@pytest.mark.parametrize('pending', [False, True], ids=['destroyed', 'pending-deletion'])
def test_queued_runtime_selection_cannot_execute_after_native_destruction(window, target, pending):
    before = window.evidence()
    def scenario():
        window.click('Підключити Whisper')
        assert len(window.jobs) == 1
        owner = window.main if target == 'owner' else window.frame
        owner.native_destroy(pending=pending)
    window.scenario = scenario
    show_transcription(window.frame)
    with pytest.raises(AudioCancelled, match='closed before'):
        window.jobs[0].operation()
    window.jobs[0].done({'protocol': 'receipts-v1'})
    window.jobs[0].failed(RuntimeError('late callback'))
    assert window.evidence() == before
    assert window.service.configuration()['worker'] == str(window.worker)
    assert window.main.destroy_calls == 0


@pytest.mark.parametrize('target', ['owner', 'parent'])
def test_late_progress_and_completed_callback_do_not_touch_destroyed_controls(window, monkeypatch, target):
    before, observed = window.evidence(), []
    actual_get = TranscriptionService.get
    def operation(service, identity, *, cancelled, progress):
        observed.append(cancelled)
        progress(None, None, 'running')
        return actual_get(service, identity)  # No fabricated result or receipt is committed.
    monkeypatch.setattr(TranscriptionService, 'run', operation)
    def scenario():
        window.click('Запустити / продовжити')
        pending = window.jobs[-1]
        result = pending.operation()
        assert not observed[0]() and window.callbacks
        (window.main if target == 'owner' else window.frame).native_destroy()
        assert observed[0]()
        for callback in window.callbacks:
            callback()
        pending.done(result)
        pending.failed(RuntimeError('late observer error'))
    window.scenario = scenario
    show_transcription(window.frame)
    assert len(observed) == 1 and window.evidence() == before
    assert window.main.destroy_calls == 0


@pytest.mark.parametrize('action,index', [
    ('Підключити Whisper', 1), ('Підключити Whisper', 2),
    ('Підключити Whisper', 3), ('Підключити Whisper', 4),
    ('Додати записи', 1), ('Додати папку з підпапками', 1),
    ('Експорт', 1), ('Експорт', 2),
])
@pytest.mark.parametrize('target,pending', [
    ('picker', False), ('picker', True), ('parent', False), ('parent', True),
])
def test_every_modal_answer_is_inert_after_picker_or_owner_destruction(
        window, action, index, target, pending):
    before, reached = window.evidence(), []
    def answer(picker):
        if len(window.pickers) == index:
            reached.append(picker)
            (picker if target == 'picker' else window.frame).native_destroy(pending=pending)
        return window.wx.ID_OK  # A stale OK is not permission to read a dead peer.
    window.picker_scenario = answer
    window.scenario = lambda: window.click(action)
    show_transcription(window.frame)
    assert len(reached) == 1
    assert reached[0].reads == reached[0].destroy_calls == 0
    assert len(window.pickers) == index and not window.jobs
    assert window.evidence() == before


def test_child_destroy_event_does_not_close_live_transcription_owner(window):
    before = window.evidence()
    def scenario():
        child = window.Control(window.main)
        child.native_destroy()
        window.click('Підключити Whisper')
        assert len(window.jobs) == 1
        result = window.jobs[0].operation()
        window.jobs[0].done(result)
        assert window.service.configuration()['worker'] == str(window.replacement)
        window.click('Закрити')
    window.scenario = scenario
    show_transcription(window.frame)
    assert window.main.destroy_calls == 1
    assert window.service.get(window.identity)['status'] == 'ready'
    assert window.evidence() != before  # Only the explicitly selected runtime was changed.
    assert window.original.read_bytes() == b'Synthetic retained input, not a user recording.'
