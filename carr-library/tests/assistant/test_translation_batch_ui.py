"""Actual batch handlers and real worker calls; stand-ins are not native proof."""
from dataclasses import replace
import json
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace

import pytest

from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Gateway
from carr_library.ui import translation_batch_dialog as ui

_fixture = runpy.run_path(str(Path(__file__).with_name('translation_batch_fixtures.py')))
transport, source_library, batch_env = (_fixture[name] for name in ('transport', 'source_library', 'batch_env'))


@pytest.fixture
def view(batch_env, monkeypatch, tmp_path):
    e = batch_env
    ui_thread = threading.get_ident()
    state = {'widgets': [], 'queue': [], 'deferred': [], 'approvals': [], 'allow': True,
             'selection': 0, 'selections': [0, 1], 'paths': [], 'errors': [], 'forbidden': [],
             'modal': None, 'admission': None}
    class Widget:
        serial = 100
        def __init__(self, parent=None, *args, value='', label='', choices=(), **options):
            Widget.serial += 1
            self.id, self.parent, self.children = Widget.serial, parent, []
            self.live, self.value, self.label, self.name = True, value, label, ''
            self.choices, self.selection = list(choices), -1
            self.enabled, self.bindings, self.focused = True, {}, False
            if isinstance(parent, Widget):
                parent.children.append(self)
            state['widgets'].append(self)
        def __bool__(self): return self.live
        def check(self, operation):
            if not self.live:
                state['forbidden'].append(operation)
                raise RuntimeError('Deleted native peer was accessed: ' + operation)
            assert threading.get_ident() == ui_thread, 'Worker touched a native control.'
        def GetId(self): self.check('GetId'); return self.id
        def SetName(self, name): self.check('SetName'); self.name = name
        def GetValue(self): self.check('GetValue'); return self.value
        def ChangeValue(self, value): self.check('ChangeValue'); self.value = value
        def GetSelection(self): self.check('GetSelection'); return self.selection
        def SetSelection(self, value): self.check('SetSelection'); self.selection = value
        def SetItems(self, values): self.check('SetItems'); self.choices, self.selection = list(values), -1
        def Enable(self, value=True): self.check('Enable'); self.enabled = value
        def SetFocus(self): self.check('SetFocus'); self.focused = True
        def Bind(self, event, callback): self.check('Bind'); self.bindings[event] = callback
        def SetSizer(self, value): self.check('SetSizer')
        def SetEscapeId(self, value): self.check('SetEscapeId')
        def IsModal(self): self.check('IsModal'); return False
        def Hide(self): self.check('Hide')
        def EndModal(self, value): self.check('EndModal')
        def Destroy(self):
            if not self.live:
                state['forbidden'].append('Destroy twice')
                return False
            if wx.EVT_WINDOW_DESTROY in self.bindings:
                self.bindings[wx.EVT_WINDOW_DESTROY](SimpleNamespace(
                    GetId=lambda: self.id, GetEventObject=lambda: object(), Skip=lambda: None))
            for child in tuple(self.children):
                if child:
                    Widget.Destroy(child)
            self.live = False
            return True
    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): pass
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND',
        'TE_MULTILINE', 'TE_READONLY', 'ID_ANY', 'ID_CANCEL', 'ID_OK', 'ID_YES', 'ID_NO',
        'YES_NO', 'NO_DEFAULT', 'EVT_BUTTON', 'EVT_CLOSE', 'EVT_WINDOW_DESTROY',
        'FD_OPEN', 'FD_FILE_MUST_EXIST', 'FD_MULTIPLE', 'FD_SAVE')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog', 'StaticText', 'TextCtrl', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = wx.WrapSizer = Sizer
    wx.CallAfter = lambda callback, *args: state['deferred'].append((callback, args))
    output = tmp_path / 'reviewed-package.zip'
    class Picker(Widget):
        confirmation = False
        def __init__(self, parent, *args, **kwargs):
            super().__init__(parent, *args, **kwargs)
            if self.confirmation:
                state['approvals'].append(str(args[0]) if args else '')
        def ShowModal(self):
            self.check('ShowModal')
            callback, state['modal'] = state['modal'], None
            if callback is not None:
                callback()
            return wx.ID_YES if self.confirmation and state['allow'] else (
                wx.ID_NO if self.confirmation else wx.ID_OK)
        def GetSelection(self): self.check('picker GetSelection'); return state['selection']
        def GetSelections(self): self.check('GetSelections'); return state['selections']
        def GetPath(self): self.check('GetPath'); return str(output)
        def GetPaths(self): self.check('GetPaths'); return list(state['paths'])
    class Confirm(Picker):
        confirmation = True
    wx.FileDialog = wx.SingleChoiceDialog = wx.MultiChoiceDialog = Picker
    wx.MessageDialog = Confirm
    frame = SimpleNamespace(library=e.app, active=(e.workspace, e.topic), working=False,
                            _closing=False, cancel_event=threading.Event())
    def run_job(title, operation, done=None, failed=None, *, progress_total=None,
                progress_unit='units', with_progress=False):
        if state['admission'] is not None:
            raise state['admission']('Queue admission failed before accepting work.')
        assert not frame.working and with_progress is False
        frame.working = True
        frame.cancel_event.clear()
        state['queue'].append((operation, done, failed))
    frame.run_job = run_job
    monkeypatch.setattr(ui, 'TranslationService', lambda *args, **kwargs: e.service)
    monkeypatch.setattr(ui, 'Gateway', lambda directory, **options: Gateway(directory, vault=e.vault, **options))
    dialog, close, draft = ui.create_translation_batch_dialog(frame, items=e.items, wx_module=wx)
    def find(name):
        return next(widget for widget in state['widgets'] if widget.name == name)
    def press(name):
        widget = find(name)
        assert widget.enabled, name
        return widget.bindings[wx.EVT_BUTTON](None)
    def execute():
        operation, done, failed = state['queue'].pop(0)
        outcome = []
        def work():
            try:
                outcome.append((done, operation()))
            except BaseException as error:
                outcome.append((failed, error))
        thread = threading.Thread(target=work)
        thread.start(); thread.join(45)
        assert not thread.is_alive() and len(outcome) == 1
        return outcome[0]
    def deliver(outcome):
        frame.working = False
        while state['deferred']:
            callback, arguments = state['deferred'].pop(0)
            callback(*arguments)
        callback, value = outcome
        if isinstance(value, BaseException):
            state['errors'].append(value)
        callback(value)
        return value
    def complete():
        return deliver(execute())
    def destroy(kind):
        if kind == 'direct':
            dialog.Destroy()
        else:
            Widget.Destroy(dialog)
    e.frame, e.wx, e.dialog, e.state, e.find, e.press = frame, wx, dialog, state, find, press
    e.complete, e.execute, e.deliver, e.destroy = complete, execute, deliver, destroy
    e.close, e.draft, e.output, e.ui_thread = close, draft, output, ui_thread
    try:
        yield e
    finally:
        close()
        while state['queue']:
            complete()
        if dialog:
            dialog.Destroy()
        assert not state['forbidden']


def review(e):
    e.press('Review entire translation batch')
    assert e.frame.working and not e.service.jobs.identities()
    e.complete()
    assert not e.state['errors']
    assert 'REVIEW ONLY' in e.find('Translation batch review and existing checkpoints').value


def prepare(e):
    review(e)
    e.press('Prepare approved batch')
    assert e.state['queue'] and not e.service.jobs.identities()
    e.complete()
    assert not e.state['errors']
    row, = e.batches.list()
    return row['id']


def test_real_thread_review_preparation_and_current_child_approvals(view, transport, monkeypatch):
    e = view
    source = e.batches.service._source_context
    reads = []
    def observed(*args, **kwargs):
        reads.append(threading.get_ident())
        assert reads[-1] != e.ui_thread
        return source(*args, **kwargs)
    monkeypatch.setattr(e.service, '_source_context', observed)
    count = len(transport['calls'])
    identity = prepare(e)
    assert reads and len(transport['calls']) == count
    assert e.batches.get(identity)['status'] == 'queued'
    assert len(e.service.list()) == 2
    assert len(e.state['approvals']) == 1 and '2 USD' in e.state['approvals'][0]
    assert e.p.endpoint in e.state['approvals'][0]
    assert all(job['kind'] == 'translation/2' for job in e.service.list())


def test_changed_review_order_requires_fresh_explicit_approval(view, transport):
    e = view
    e.press('Review entire translation batch')
    e.find('Translation batch title').ChangeValue('Changed after work was queued')
    e.complete()
    e.press('Prepare approved batch')
    assert not e.state['queue'] and not e.service.jobs.identities() and not e.state['approvals']
    e.find('Translation batch title').ChangeValue('Translation batch')
    e.find('Ordered translation documents').SetSelection(1)
    e.press('Move document up')
    assert not e.find('Prepare approved batch').enabled
    identity = prepare(e)
    manifest = e.batches._load(identity)
    assert manifest['members'][0]['job']['source_text'] == e.selected[1]['text']


def test_explicit_multiple_files_capture_original_crlf_with_per_row_settings(view, tmp_path):
    e = view
    e.press('New batch draft')
    e.find('Per-document USD reservation; explicit, no default').ChangeValue('1')
    files = [tmp_path / 'first.txt', tmp_path / 'second.txt']
    contents = ['First exact\r\nУкраїна', 'Second exact\n🙂']
    for path, value in zip(files, contents):
        path.write_bytes(value.encode('utf-8'))
    e.state['paths'] = [str(path) for path in files]
    e.press('Add UTF-8 files...')
    e.complete()
    assert [row['text'] for row in e.draft()] == contents
    assert all('source_anchor' not in row for row in e.draft())
    e.find('Ordered translation documents').SetSelection(1)
    e.find('Target language code').ChangeValue('fr')
    e.find('Per-document USD reservation; explicit, no default').ChangeValue('2')
    e.press('Apply settings to selected document')
    assert [row['budget'] for row in e.draft()] == ['1', '2']
    assert [row['target'] for row in e.draft()] == ['uk', 'fr']


def test_explicit_multiple_books_use_current_authorized_source_positions(view):
    e = view
    e.press('New batch draft')
    e.find('Per-document USD reservation; explicit, no default').ChangeValue('1')
    e.press('Add library books...')
    e.complete()  # Actual browse then explicit native multi-selection.
    assert e.state['queue']
    e.complete()  # Actual ReadingDesk / read_source_range.
    rows = e.draft()
    assert len(rows) == 2
    assert {row['source_anchor']['edition'] for row in rows} == {e.imported['edition'], e.second['edition']}
    assert all(row['source_topic'] == e.topic and row['source_anchor']['quotation_authority'] is False for row in rows)


@pytest.mark.parametrize('error', [OSError, RuntimeError, KeyboardInterrupt])
def test_actual_queue_admission_failure_clears_pending_and_allows_retry(view, error):
    e = view
    e.state['admission'] = error
    e.press('Review entire translation batch')
    assert not e.frame.working and not e.state['queue']
    assert e.find('Review entire translation batch').enabled
    assert not e.service.jobs.identities()
    e.state['admission'] = None
    review(e)


@pytest.mark.parametrize('kind', ['direct', 'parent'])
@pytest.mark.parametrize('completed', [False, True])
def test_deleted_native_owner_rejects_actual_queued_or_completed_review(view, kind, completed):
    e = view
    e.press('Review entire translation batch')
    result = e.execute() if completed else None
    e.destroy(kind)
    if result is None:
        result = e.execute()
    e.deliver(result)
    assert not e.dialog and not e.frame.working
    assert not e.state['forbidden'] and not e.service.jobs.identities()
    # Actual previously bound handlers remain inert after native disposal.
    for widget in e.state['widgets']:
        callback = widget.bindings.get(e.wx.EVT_BUTTON)
        if callback is not None:
            callback(None)
    assert not e.state['forbidden'] and not e.state['queue']


@pytest.mark.parametrize('kind', ['direct', 'parent'])
def test_completed_preparation_retains_only_real_saved_children_after_owner_deletion(view, kind):
    e = view
    review(e)
    e.press('Prepare approved batch')
    result = e.execute()
    assert len(e.service.jobs.identities()) == 2
    e.destroy(kind)
    e.deliver(result)
    assert e.batches.list()[0]['status'] == 'queued'
    assert not e.state['forbidden']


@pytest.mark.parametrize('action', ['files', 'books', 'prepare', 'saved', 'languages', 'export'])
def test_parent_death_inside_real_bound_modal_never_reads_poisoned_peer(view, action):
    e = view
    if action in ('saved', 'export'):
        identity = prepare(e)
        if action == 'export':
            e.batches.resume(identity)
    elif action == 'prepare':
        review(e)
    else:
        e.find('Per-document USD reservation; explicit, no default').ChangeValue('1')
    e.state['modal'] = lambda: e.destroy('parent')
    names = {'files': 'Add UTF-8 files...', 'books': 'Add library books...',
             'prepare': 'Prepare approved batch', 'saved': 'Saved translation batches',
             'languages': 'Fetch account languages...', 'export': 'Export completed ZIP...'}
    e.press(names[action])
    if action in ('books', 'saved'):
        e.complete()
    assert not e.dialog and not e.state['queue'] and not e.state['forbidden']
    assert not e.output.exists()


def test_saved_resume_rechecks_account_after_gateway_construction(view, transport, monkeypatch):
    e = view
    identity = prepare(e)
    count = len(transport['calls'])
    def revoked(directory, **options):
        value = Gateway(directory, vault=e.vault, **options)
        ProfileStore(e.app.root / 'state' / 'providers').save(replace(e.p, enabled=False))
        return value
    monkeypatch.setattr(ui, 'Gateway', revoked)
    e.press('Run or resume batch')
    e.complete()
    assert isinstance(e.state['errors'][-1], PermissionError)
    assert len(transport['calls']) == count
    assert e.batches.get(identity)['completed_chunks'] == 0


def test_actual_ui_export_uses_captured_destination_before_queued_work(view, tmp_path):
    e = view
    identity = prepare(e)
    e.batches.resume(identity)
    e.press('Export completed ZIP...')
    assert e.state['queue'] and not e.output.exists()
    occupied = e.output
    occupied.write_bytes(b'Foreign file appeared while operation was queued.')
    e.complete()
    assert isinstance(e.state['errors'][-1], (ValueError, FileExistsError))
    assert occupied.read_bytes() == b'Foreign file appeared while operation was queued.'
    assert e.batches.get(identity)['status'] == 'complete'


@pytest.mark.parametrize('kind', ['direct', 'parent'])
@pytest.mark.parametrize('stage', ['submit', 'prepare'])
def test_native_death_after_actual_saved_preparation_keeps_success(view, transport, monkeypatch, kind, stage):
    e = view
    if stage == 'prepare':
        identity = prepare(e)
    else:
        review(e)
        identity = None
    entered, release = threading.Event(), threading.Event()
    original = getattr(ui.TranslationBatchService, stage)
    def held(service, *args, **kwargs):
        result = original(service, *args, **kwargs)
        entered.set()
        assert release.wait(30), 'The test did not release the real saved result.'
        return result
    monkeypatch.setattr(ui.TranslationBatchService, stage, held)
    before_calls = len(transport['calls'])
    e.press('Prepare approved batch')
    operation, done, failed = e.state['queue'].pop(0)
    outcome = []
    def work():
        try:
            outcome.append((done, operation()))
        except BaseException as error:
            outcome.append((failed, error))
    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert entered.wait(30), 'The actual preparation never saved its result.'
        row, = e.batches.list()
        identity = identity or row['id']
        manifest = e.batches.jobs.path(identity).read_bytes()
        children = {job: e.service.jobs.path(job).read_bytes() for job in e.service.jobs.identities()}
        assert len(children) == 2 and row['status'] == 'queued'
        e.destroy(kind)
    finally:
        release.set()
        thread.join(30)
    assert not thread.is_alive() and len(outcome) == 1
    assert not isinstance(outcome[0][1], BaseException), outcome[0][1]
    e.deliver(outcome[0])
    assert not e.state['errors'] and not e.state['forbidden']
    assert e.batches.jobs.path(identity).read_bytes() == manifest
    assert {job: e.service.jobs.path(job).read_bytes() for job in e.service.jobs.identities()} == children
    assert e.batches.get(identity)['status'] == 'queued'
    assert len(transport['calls']) == before_calls
