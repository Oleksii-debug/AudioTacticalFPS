"""Real native handler closures and actual stores; native widgets are stand-ins."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from queue import Queue
import runpy
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService
from carr_library.assistant.templates import TemplateStore
from carr_library.conversion.planning import SplitOptions
from carr_library.ui import workflow_templates as ui

_helpers = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'speech' / 'test_pronunciation_handlers.py'))


class Widget(_helpers['Widget']):
    forbidden = []
    def native(self):
        if self.destroyed:
            self.forbidden.append(self.name)
        super().native()
    def SetEditable(self, value):
        self.native(); self.editable = value
    def SetEscapeId(self, value):
        self.native(); self.escape = value
    def EndModal(self, value):
        self.native(); self.modal_result = value


class Frame:
    """Actual threads and deferred UI callbacks, matching the shared run_job signature."""
    def __init__(self, app):
        self.library, self._closing, self.working = app, False, False
        self.cancel_event, self.gate = Event(), Event()
        self.gate.set()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.callbacks, self.future = Queue(), None
        self.reject = None
    def run_job(self, title, operation, done=None, failed=None, *, progress_total=None,
                progress_unit='units', with_progress=False):
        assert with_progress is False
        if self.reject:
            raise self.reject('actual admission seam')
        self.working = True
        def work():
            if not self.gate.wait(5):
                raise TimeoutError('owned test barrier')
            return operation()
        self.future = self.executor.submit(work)
        self.future.add_done_callback(lambda future: self.callbacks.put((future, done, failed)))
    def drain(self):
        future, done, failed = self.callbacks.get(timeout=5)
        self.working = False
        try:
            value = future.result(timeout=5)
        except BaseException as exc:
            failed(exc)
        else:
            done(value)
    def close(self):
        self.gate.set(); self.executor.shutdown(wait=True)
        while not self.callbacks.empty():
            self.drain()


@pytest.fixture
def view(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    frame = Frame(app); Widget.forbidden.clear()
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND',
             'TE_MULTILINE', 'EVT_BUTTON', 'EVT_CHOICE', 'EVT_CLOSE', 'EVT_WINDOW_DESTROY',
             'ID_CANCEL', 'ID_OK', 'FD_OPEN', 'FD_FILE_MUST_EXIST')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(names)})
    for name in ('Dialog', 'StaticText', 'TextCtrl', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = _helpers['Sizer']
    dialog = ui.create_workflow_templates_dialog(frame, initial=('assistant',
        {'steps': [{'tool': 'library.list', 'arguments': {}}]}), wx_module=wx)
    try:
        yield SimpleNamespace(app=app, frame=frame, dialog=dialog, wx=wx)
    finally:
        dialog.dispose(); frame.close()
        if dialog:
            dialog.Destroy()


def destroy(view, kind):
    dialog = view.dialog
    if kind == 'direct':
        dialog.Destroy()
    else:
        native_id = dialog._native_id
        event = SimpleNamespace(GetId=lambda: native_id, GetEventObject=lambda: object(), Skip=lambda: None)
        dialog.handlers[view.wx.EVT_WINDOW_DESTROY](event)
        Widget.Destroy(dialog)  # Parent-driven deletion bypasses Python override.


def test_actual_save_and_reopen_never_submits_a_job_or_inherits_scope(view):
    e = view
    e.dialog.name.ChangeValue('Named macro')
    assert e.dialog.save()
    e.frame.drain()
    saved, = TemplateStore(e.app.root).list()
    assert e.dialog.previous.id == saved.id and not e.dialog.pending
    assert AssistantService(e.app).jobs() == ()
    e.dialog.new()
    e.dialog.reload(selected_id=saved.id); e.frame.drain()
    assert e.dialog.previous == saved and e.dialog.values.GetValue() == '{}'


@pytest.mark.parametrize('error', [OSError, RuntimeError, KeyboardInterrupt])
def test_admission_exception_releases_pending_state_and_allows_retry(view, error):
    e = view; e.frame.reject = error
    assert e.dialog.save() is False
    assert not e.dialog.pending and e.dialog.name.IsEnabled()
    assert e.dialog.store.list() == () and not e.frame.working
    e.frame.reject = None
    assert e.dialog.save()
    e.frame.drain()
    assert len(e.dialog.store.list()) == 1 and not e.dialog.pending


@pytest.mark.parametrize('kind', ['direct', 'parent'])
@pytest.mark.parametrize('completed', [False, True])
def test_actual_pending_or_completed_save_survives_only_as_data_after_native_detach(view, kind, completed):
    e = view
    e.dialog.name.ChangeValue('Saved only if publication happened')
    if not completed:
        e.frame.gate.clear()
    assert e.dialog.save()
    if completed:
        e.frame.future.result(timeout=5)
    destroy(e, kind)
    e.frame.gate.set(); e.frame.drain()
    assert not e.dialog.alive and not e.dialog.pending and not Widget.forbidden
    assert len(TemplateStore(e.app.root).list()) == int(completed)
    assert AssistantService(e.app).jobs() == ()


def test_current_request_is_handed_to_existing_assistant_without_execution(view, monkeypatch):
    from carr_library.ui import assistant_dialog
    e = view; calls = []
    monkeypatch.setattr(assistant_dialog, 'show_assistant',
        lambda frame, *, initial_instruction: calls.append((frame, initial_instruction)))
    e.dialog.review()
    assert len(calls) == 1 and calls[0][0] is e.frame
    import json
    request = json.loads(calls[0][1])
    assert request['schema'] == 'carr-workflow-review/1' and 'scope' not in request
    assert request['template']['definition']['steps'][0]['tool'] == 'library.list'
    assert AssistantService(e.app).jobs() == ()


def test_batch_preset_opens_current_review_with_no_old_job_or_output(view, monkeypatch):
    from carr_library.ui import document_batch
    e = view; calls = []
    settings = dict(options=asdict(SplitOptions(mode='blank-lines')), template='{source}-{part:03d}',
        combined=False, action='text', voice_id='', language='', format='wav')
    e.dialog.kind.SetSelection(2)
    import json
    e.dialog.definition.ChangeValue(json.dumps(settings))
    monkeypatch.setattr(document_batch, 'show_document_batches',
        lambda frame, *, initial_settings: calls.append(initial_settings))
    e.dialog.review()
    assert calls == [settings] and not e.dialog.store.list()
    assert not any(key in calls[0] for key in ('destination', 'sources', 'job_id', 'review'))


def test_parent_death_during_per_item_chooser_never_reads_or_opens_deleted_peer(view, monkeypatch):
    from carr_library.ui import document_batch
    e = view; calls = []
    settings = dict(options=asdict(SplitOptions()), template='{source}-{part:03d}',
        combined=False, action='text', voice_id='', language='', format='wav')
    import json
    e.dialog.kind.SetSelection(2); e.dialog.definition.ChangeValue(json.dumps(settings))
    e.dialog.items.ChangeValue('[{},{}]')
    class Chooser(Widget):
        def ShowModal(self):
            destroy(e, 'parent')
            return e.wx.ID_OK
    e.wx.SingleChoiceDialog = Chooser
    monkeypatch.setattr(document_batch, 'show_document_batches', lambda *a, **k: calls.append(True))
    e.dialog.review()
    assert not calls and not Widget.forbidden and not e.dialog.alive


def test_library_switch_discards_real_queued_store_result(view):
    e = view
    e.dialog.reload()
    e.frame.future.result(timeout=5)
    e.frame.library = LibraryApp(e.app.root.parent / 'different')
    e.frame.drain()
    assert 'library changed' in e.dialog.status.GetValue()
    assert not e.dialog.pending and e.dialog.rows == ()
