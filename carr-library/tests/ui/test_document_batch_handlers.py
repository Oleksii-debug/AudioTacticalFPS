"""Real batch closures and conversion services, with widget/scheduler stand-ins."""
from pathlib import Path
import runpy
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.conversion.batches import DocumentBatches
from carr_library.ui.document_batch import METHODS, create_document_batch_dialog


_fixtures = runpy.run_path(str(Path(__file__).resolve().parents[1] /
    'speech' / 'test_pronunciation_handlers.py'))


class Widget(_fixtures['Widget']):
    forbidden = []
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hidden = False
    def native(self):
        if self.destroyed:
            self.forbidden.append(self.name)
        return super().native()
    def SetEscapeId(self, value):
        self.native(); self.escape = value
    def EndModal(self, value):
        self.native(); self.modal_result = value


class Sizer:
    def __init__(self, *args, **kwargs): pass
    def Add(self, *args, **kwargs): pass


class Frame:
    def __init__(self, root):
        self.library = SimpleNamespace(root=root)
        self.cancel_event, self.working, self._closing = Event(), False, False
        self.queue, self.operations, self.progress = [], [], []
        self.defer = False
        self.on_progress = None

    def run_job(self, title, operation, done, failed, *, with_progress):
        assert with_progress is True
        self.working = True; self.cancel_event.clear(); self.operations.append(title)
        self.queue.append((operation, done, failed))
        if not self.defer:
            self.drain()

    def drain(self):
        operation, done, failed = self.queue.pop(0)
        def progress(*args, **kwargs):
            self.progress.append(kwargs)
            if self.on_progress:
                self.on_progress(kwargs.get('phase', ''))
        try:
            result = operation(progress)
        except Exception as exc:
            self.working = False; failed(exc)
        else:
            self.working = False; done(result)


@pytest.fixture
def desktop(tmp_path):
    Widget.forbidden.clear()
    library = tmp_path / 'library'; library.mkdir()
    output = tmp_path / 'output'; output.mkdir()
    source = tmp_path / 'джерело.txt'; source.write_bytes('😀 First\r\n\r\nДругий\r\n'.encode())
    service = DocumentBatches(library)
    identity = service.create([source])
    frame = Frame(library)
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND',
             'TE_MULTILINE', 'TE_READONLY', 'EVT_BUTTON', 'EVT_CLOSE', 'EVT_CHOICE', 'EVT_TEXT',
             'ID_CANCEL', 'ID_OK', 'FD_OPEN', 'FD_FILE_MUST_EXIST', 'FD_MULTIPLE', 'EVT_WINDOW_DESTROY')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(names)})
    for name in ('Dialog', 'StaticText', 'TextCtrl', 'Choice', 'Button', 'ScrolledWindow'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    dialog = create_document_batch_dialog(frame, service=service, wx_module=wx)
    return frame, dialog, service, identity, output, source, wx


def open_job(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    dialog.reload()
    assert dialog.job_choice.GetSelection() == -1
    dialog.job_choice.SetSelection(0); dialog.select_job()
    assert dialog.identity == identity
    return dialog


def preview(desktop):
    dialog = open_job(desktop)
    dialog.prepare()
    assert 'prepared' in dialog.status.GetValue()
    dialog.destination.ChangeValue(str(desktop[4]))
    dialog.method.SetSelection(next(i for i, pair in enumerate(METHODS) if pair[0] == 'blank-lines'))
    dialog.on_preview()
    assert dialog.preview is not None, dialog.status.GetValue()
    return dialog


def test_open_does_not_capture_prepare_or_start_speech_and_requires_selection(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    assert frame.operations == [] and service.get(identity)['sources'][0]['prepared'] is None
    dialog.prepare(); dialog.on_preview(); dialog.run_saved()
    assert frame.operations == [] and not list(output.iterdir())
    assert 'select' in dialog.status.GetValue()


def test_real_handler_review_approval_run_and_restart_preserve_exact_text(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    dialog = preview(desktop)
    assert len(dialog.preview.outputs) == 2
    assert 'SHA256' in dialog.review.GetValue() and '[0, ' in dialog.review.GetValue()
    assert not list(output.iterdir()) and service.get(identity)['review'] is None
    dialog.approve()
    assert service.get(identity)['status'] == 'reviewed' and not list(output.iterdir())
    dialog.run_saved()
    assert service.get(identity)['status'] == 'complete'
    raw = b''.join((output / row['name']).read_bytes() for row in service.get(identity)['outputs'])
    assert raw == source.read_bytes()
    old = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in output.iterdir()}
    dialog.close(); assert dialog.hidden
    fresh = create_document_batch_dialog(frame, wx_module=wx)
    fresh.reload(); fresh.job_choice.SetSelection(0); fresh.select_job()
    assert fresh.approved_scope is not None and 'Actual new output names:' in fresh.review.GetValue()
    fresh.run_saved()
    assert 'complete:' in fresh.status.GetValue()
    assert {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in output.iterdir()} == old


def test_silent_control_change_cannot_approve_preview_or_reuse_saved_approval(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    dialog = preview(desktop)
    dialog.template.ChangeValue('changed-{part}')  # no EVT_TEXT was emitted
    dialog.approve()
    assert service.get(identity)['review'] is None
    assert 'controls changed' in dialog.status.GetValue()
    dialog.on_preview(); dialog.approve()
    assert service.get(identity)['review'] is not None
    dialog.destination.ChangeValue(str(output.parent))
    count = len(frame.operations); dialog.run_saved()
    assert len(frame.operations) == count and not list(output.iterdir())
    assert 'current controls' in dialog.status.GetValue()


def test_queued_preview_rejects_silent_edits_on_return(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    dialog = preview(desktop)
    frame.defer = True; dialog.on_preview()
    assert dialog.pending and not dialog.template.IsEnabled()
    dialog.marker.ChangeValue('late unannounced edit')
    frame.drain()
    assert dialog.preview is None and 'controls changed' in dialog.status.GetValue()


def test_queued_library_object_change_prevents_converter_entry(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    open_job(desktop)
    frame.defer = True; dialog.prepare()
    frame.library = SimpleNamespace(root=service.library)  # even the same path is a different owner
    frame.drain()
    assert service.get(identity)['sources'][0]['prepared'] is None
    assert 'library changed' in dialog.status.GetValue()
    assert not dialog.pending


def test_close_waits_for_owned_operation_and_suppresses_late_result(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    open_job(desktop)
    frame.defer = True; dialog.prepare()
    dialog.close()
    assert dialog.pending and dialog.closing and dialog.alive and not dialog.hidden
    assert dialog.cancelled.is_set() and not frame.cancel_event.is_set()
    frame.drain()
    assert not dialog.pending and not dialog.alive and dialog.hidden
    assert service.get(identity)['sources'][0]['prepared'] is None


def test_pause_stops_real_batch_then_resumes_without_rewriting_completed_part(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    dialog = preview(desktop); dialog.approve()
    frame.on_progress = lambda message: dialog.pause() if message.startswith('Verified output 1 ') else None
    dialog.run_saved()
    state = service.get(identity)
    assert state['status'] == 'paused' and len(state['outputs']) == 1
    first = output / state['outputs'][0]['name']; before = first.stat()
    frame.on_progress = None; dialog.run_saved()
    assert service.get(identity)['status'] == 'complete'
    assert (first.stat().st_ino, first.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize('control', ['method', 'ignore_case', 'combine', 'action', 'format'])
def test_unselected_choice_does_not_alias_the_last_option(desktop, control):
    frame, dialog, service, identity, output, source, wx = desktop
    dialog = preview(desktop)
    getattr(dialog, control).SetSelection(-1)
    count = len(frame.operations); dialog.on_preview()
    assert len(frame.operations) == count and not list(output.iterdir())
    assert 'Choose every required' in dialog.status.GetValue()


def test_occupied_output_blocks_native_approval_and_never_overwrites(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    dialog = preview(desktop)
    target = output / dialog.preview.outputs[0]; target.write_text('foreign')
    dialog.on_preview()
    assert dialog.preview.conflicts and not dialog.buttons['approve'].IsEnabled()
    dialog.approve()
    assert service.get(identity)['review'] is None and target.read_text() == 'foreign'


def test_folder_capture_reports_actual_source_count_and_preserves_prior_job_control(desktop, tmp_path):
    frame, dialog, service, identity, output, source, wx = desktop
    open_job(desktop)
    folder = tmp_path / 'chosen'; folder.mkdir()
    (folder / 'one.txt').write_text('one'); (folder / 'two.txt').write_text('two')
    class Picker:
        def __init__(self, *args, **kwargs): pass
        def ShowModal(self): return wx.ID_OK
        def GetPath(self): return str(folder)
        def Destroy(self): pass
    wx.DirDialog = Picker
    frame.defer = True; dialog.capture('folder')
    assert dialog.active_identity is None
    dialog.pause(); frame.drain()
    assert service._control(identity) == 'run'
    frame.defer = False; dialog.capture('folder')
    assert dialog.identity != identity
    assert len(service.get(dialog.identity)['sources']) == 2
    assert dialog.jobs[-1]['files'] == 2


def test_restored_record_requires_new_preview_even_when_controls_are_unchanged(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    dialog = preview(desktop); dialog.approve()
    record = service.get(identity)
    record['library_root'] = str(service.library.with_name('former-library'))
    service._save(record)
    dialog.loaded(service.get(identity)); dialog.enable()
    assert dialog.approved_scope is None and not dialog.buttons['run'].IsEnabled()
    count = len(frame.operations); dialog.run_saved()
    assert len(frame.operations) == count and not list(output.iterdir())


@pytest.mark.parametrize('ending', ['direct', 'parent'])
def test_native_owner_deletion_drops_held_completion_and_never_cancels_another_job(desktop, ending):
    frame, dialog, service, identity, output, source, wx = desktop
    open_job(desktop)
    frame.defer = True
    dialog.prepare()
    owner_id = dialog.GetId()
    if ending == 'direct':
        dialog.Destroy()
    else:
        Widget.Destroy(dialog)
        dialog.on_destroy(SimpleNamespace(GetId=lambda: owner_id, Skip=lambda: None))
    assert not dialog and not dialog.alive and dialog.cancelled.is_set()
    assert not frame.cancel_event.is_set()
    frame.drain()
    assert not dialog.pending
    before = service.store.path(identity).read_bytes()
    for name in ('reload', 'select_job', 'prepare', 'on_preview', 'approve', 'run_saved',
                 'pause', 'stop', 'close', 'changed', 'choose_destination', 'load_voices', 'fill_jobs', 'fill_voices'):
        getattr(dialog, name)()
    assert dialog.Destroy() is False
    assert service.store.path(identity).read_bytes() == before
    assert not Widget.forbidden and not frame.cancel_event.is_set() and not list(output.iterdir())


@pytest.mark.parametrize('picker_kind', ['files', 'folder', 'reading', 'destination'])
def test_nested_picker_parent_deletion_never_reads_or_destroys_a_dead_picker(desktop, picker_kind):
    frame, dialog, service, identity, output, source, wx = desktop
    class Picker(Widget):
        def ShowModal(self):
            owner_id = dialog.GetId()
            Widget.Destroy(dialog)
            dialog.on_destroy(SimpleNamespace(GetId=lambda: owner_id, Skip=lambda: None))
            return wx.ID_OK
        def GetPath(self):
            self.native(); return str(output if picker_kind == 'destination' else source.parent)
        def GetPaths(self):
            self.native(); return [str(source)]
    wx.DirDialog = wx.FileDialog = Picker
    if picker_kind == 'destination':
        dialog.choose_destination()
    else:
        dialog.capture(picker_kind)
    assert not dialog and not dialog.alive and not frame.operations
    assert not Widget.forbidden and not frame.cancel_event.is_set()


def test_completion_callback_that_destroys_owner_cannot_focus_dead_controls(desktop):
    frame, dialog, service, identity, output, source, wx = desktop
    def completed(value):
        assert value == 'actual worker value'
        dialog.Destroy()
    assert dialog.begin('Nested completion', lambda stopping, progress: 'actual worker value', completed)
    assert not dialog and not dialog.alive and not Widget.forbidden
    assert not frame.cancel_event.is_set()


@pytest.mark.parametrize('picker_kind', ['source', 'destination'])
def test_existing_conversion_modal_parent_deletion_never_reads_dead_picker(
        tmp_path, monkeypatch, picker_kind):
    from carr_library.ui import document_conversion
    import sys
    calls = []
    class Owner:
        _closing = False
        library = SimpleNamespace(root=tmp_path)
        def __bool__(self):
            return not self._closing
        def report(self, message):
            calls.append(('report', message))
        def run_job(self, *args, **kwargs):
            calls.append(('job', args))
    frame = Owner()
    source = tmp_path / 'source.txt'
    source.write_text('Exact source.', encoding='utf-8')
    class Picker:
        def __init__(self, kind):
            self.kind, self.deleted = kind, False
        def __bool__(self):
            return not self.deleted
        def ShowModal(self):
            if self.kind == picker_kind:
                self.deleted = True
                frame._closing = True
            return 1
        def GetPaths(self):
            if self.deleted:
                calls.append(('forbidden', 'paths'))
                raise RuntimeError('Deleted picker')
            return [str(source)]
        def GetPath(self):
            if self.deleted:
                calls.append(('forbidden', 'path'))
                raise RuntimeError('Deleted picker')
            return str(tmp_path)
        def Destroy(self):
            if self.deleted:
                calls.append(('forbidden', 'destroy'))
                raise RuntimeError('Deleted picker')
            self.deleted = True
    wx = SimpleNamespace(ID_OK=1, FD_OPEN=2, FD_FILE_MUST_EXIST=4, FD_MULTIPLE=8,
                         FileDialog=lambda *args, **kwargs: Picker('source'),
                         DirDialog=lambda *args, **kwargs: Picker('destination'))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    document_conversion.convert_selected_documents(frame)
    assert calls == []
    assert source.read_text(encoding='utf-8') == 'Exact source.'
