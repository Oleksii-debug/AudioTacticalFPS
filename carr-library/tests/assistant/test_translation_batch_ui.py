"""Actual batch dialog closures and worker threads; stand-ins are not native proof."""
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from carr_library.assistant.providers import Gateway
from carr_library.ui import translation_batch_dialog as ui
from carr_library.ui import translation_dialog
from test_translation_batch import batch_env
from test_translation_source import source_library
from test_translation_v3 import transport, translation_handlers, prepare_handler


@pytest.fixture
def batch_ui(batch_env, monkeypatch, tmp_path):
    e = batch_env
    ui_thread = threading.get_ident()
    state = {'widgets': [], 'queue': [], 'deferred': [], 'approvals': [], 'allow': True,
             'selection': 0, 'errors': [], 'closed': False}
    class Widget:
        def __init__(self, parent=None, *args, value='', label='', choices=(), **options):
            self.value, self.label, self.name = value, label, ''
            self.choices, self.selection = list(choices), -1
            self.enabled, self.bindings, self.focused = True, {}, False
            state['widgets'].append(self)
        def SetName(self, name): self.name = name
        def GetValue(self):
            assert threading.get_ident() == ui_thread, 'A worker read a live UI field.'
            return self.value
        def ChangeValue(self, value):
            assert threading.get_ident() == ui_thread, 'A worker wrote a live UI field.'
            self.value = value
        def GetSelection(self):
            assert threading.get_ident() == ui_thread, 'A worker read a live native selection.'
            return self.selection
        def SetSelection(self, value): self.selection = value
        def SetItems(self, values): self.choices, self.selection = list(values), -1
        def Enable(self, value=True): self.enabled = value
        def SetFocus(self): self.focused = True
        def Bind(self, event, callback): self.bindings[event] = callback
        def SetSizer(self, sizer): pass
        def SetEscapeId(self, identity): self.escape = identity
        def IsModal(self): return not state['closed']
        def EndModal(self, value): state['closed'] = True
        def Destroy(self): pass
    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): pass
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND',
             'TE_MULTILINE', 'TE_READONLY', 'ID_ANY', 'ID_CANCEL', 'ID_OK', 'YES', 'NO',
             'YES_NO', 'NO_DEFAULT', 'EVT_BUTTON', 'EVT_CLOSE')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(names)})
    for name in ('Dialog', 'StaticText', 'TextCtrl', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    wx.CallAfter = lambda callback, *args: state['deferred'].append((callback, args))
    def confirm(message, *args):
        assert threading.get_ident() == ui_thread
        state['approvals'].append(message)
        return wx.YES if state['allow'] else wx.NO
    wx.MessageBox = confirm
    output = tmp_path / 'reviewed exports'
    output.mkdir()
    class Picker:
        def __init__(self, *args, **kwargs): pass
        def ShowModal(self): return wx.ID_OK
        def GetSelection(self): return state['selection']
        def GetPath(self): return str(output)
        def Destroy(self): pass
    wx.SingleChoiceDialog = wx.DirDialog = Picker
    frame = SimpleNamespace(library=e.app, active=(e.workspace, e.topic), working=False,
                            _closing=False, cancel_event=threading.Event())
    def run_job(title, operation, done, failed):
        assert not frame.working
        frame.working = True
        frame.cancel_event.clear()
        state['queue'].append((operation, done, failed))
        return True
    frame.run_job = run_job
    monkeypatch.setattr(ui, 'TranslationService', lambda *args, **kwargs: e.service)
    monkeypatch.setattr(ui, 'Gateway', lambda directory, **options: Gateway(directory, vault=e.vault, **options))
    dialog, close, draft = ui.create_translation_batch_dialog(frame, items=e.items, wx_module=wx)
    def find(name):
        return next(control for control in state['widgets'] if control.name == name)
    def press(name):
        control = find(name)
        assert control.enabled, name
        control.bindings[wx.EVT_BUTTON](None)
    def complete():
        operation, done, failed = state['queue'].pop(0)
        outcome = []
        def work():
            try:
                outcome.append((done, operation()))
            except Exception as error:
                outcome.append((failed, error))
        thread = threading.Thread(target=work)
        thread.start()
        thread.join(20)
        assert not thread.is_alive() and len(outcome) == 1
        frame.working = False
        while state['deferred']:
            callback, args = state['deferred'].pop(0)
            callback(*args)
        callback, value = outcome[0]
        if isinstance(value, Exception):
            state['errors'].append(value)
        if not frame._closing:
            callback(value)
        return value
    e.frame, e.ui, e.state, e.find, e.press = frame, wx, state, find, press
    e.complete, e.close, e.draft, e.output, e.ui_thread = complete, close, draft, output, ui_thread
    yield e
    close()


def review(e):
    e.press('Review entire translation batch')
    assert e.frame.working and e.state['queue'] and not e.service.jobs.identities()
    e.complete()
    assert not e.state['errors']
    assert 'REVIEW ONLY' in e.find('Translation batch review and existing checkpoints').value


def prepare(e):
    review(e)
    e.press('Prepare approved batch')
    assert e.state['queue'] and not e.service.jobs.identities()
    e.complete()
    assert not e.state['errors']
    saved, = e.batches.list()
    return saved['id']


def test_real_thread_review_and_prepare_never_read_widgets_or_send_text(batch_ui, transport, monkeypatch):
    e = batch_ui
    actual = e.service._validate_source
    reads = []
    def source(*args, **kwargs):
        reads.append(threading.get_ident())
        assert reads[-1] != e.ui_thread
        return actual(*args, **kwargs)
    monkeypatch.setattr(e.service, '_validate_source', source)
    before = len(transport['calls'])
    identity = prepare(e)
    assert reads and len(transport['calls']) == before
    assert len(e.service.list()) == 2 and e.batches.get(identity)['status'] == 'queued'
    assert len(e.state['approvals']) == 1
    assert e.p.endpoint in e.state['approvals'][0] and '2 USD' in e.state['approvals'][0]
    assert e.batches.get_review(identity)['items'][1]['glossary'] in e.state['approvals'][0]
    assert not e.find('Move document up').enabled and e.find('Run or resume batch').enabled
    assert 'PREPARED' in e.find('Translation batch review and existing checkpoints').value


def test_frozen_field_snapshot_and_order_require_fresh_review_before_approval(batch_ui, transport):
    e = batch_ui
    before = len(transport['calls'])
    e.press('Review entire translation batch')
    e.find('Translation batch title').ChangeValue('Changed while worker was queued')
    e.items[0]['text'] = 'Caller changed its own input after dialog construction.'
    e.complete()
    e.press('Prepare approved batch')
    assert not e.state['queue'] and not e.service.jobs.identities() and not e.state['approvals']
    assert 'Review this exact' in e.find('Translation batch review and existing checkpoints').value
    e.find('Translation batch title').ChangeValue('Translation batch')
    e.find('Ordered translation documents').SetSelection(1)
    e.press('Move document up')
    assert not e.find('Prepare approved batch').enabled
    e.press('Review entire translation batch')
    e.complete()
    e.press('Prepare approved batch')
    e.complete()
    identity = e.batches.list()[0]['id']
    manifest = e.service.jobs.load(identity)
    assert manifest['members'][0]['job']['source_text'] == e.selected[1]['text']
    assert manifest['members'][1]['job']['source_text'] == e.selected[0]['text']
    assert len(transport['calls']) == before


def test_native_confirmation_decline_creates_no_job_and_no_transfer(batch_ui, transport):
    e = batch_ui
    review(e)
    e.state['allow'] = False
    before = len(transport['calls'])
    e.press('Prepare approved batch')
    assert not e.state['queue'] and not e.service.jobs.identities()
    assert len(e.state['approvals']) == 1 and len(transport['calls']) == before


@pytest.mark.parametrize('change', ['scope', 'close', 'desktop_close'])
def test_queued_batch_action_stops_without_provider_or_new_approval_after_owner_change(batch_ui, transport, change):
    e = batch_ui
    identity = prepare(e)
    before = len(transport['calls'])
    original = {job['id']: e.service.jobs.path(job['id']).read_bytes() for job in e.service.list()}
    e.press('Run or resume batch')
    if change == 'scope':
        e.frame.active = (e.workspace, None)
    elif change == 'close':
        e.close()
    else:
        e.frame._closing = True
    e.complete()
    assert e.state['errors'] and len(transport['calls']) == before
    assert all(e.service.jobs.path(key).read_bytes() == raw for key, raw in original.items())
    assert len(e.state['approvals']) == 1


def test_saved_batch_requires_original_scope_and_never_infers_new_permission(batch_ui, transport):
    e = batch_ui
    identity = prepare(e)
    e.press('New batch draft')
    e.press('Saved translation batches')
    e.complete()  # list, native selection, then queue exact saved approval
    assert e.state['queue']
    e.frame.active = (e.workspace, None)
    before = len(transport['calls'])
    e.complete()
    assert isinstance(e.state['errors'][-1], PermissionError)
    assert not e.find('Run or resume batch').enabled and len(transport['calls']) == before
    assert e.batches.get(identity)['status'] == 'queued'


def test_explicit_run_reuses_jobs_and_export_preserves_original_bytes(batch_ui, transport):
    e = batch_ui
    identity = prepare(e)
    e.press('Run or resume batch')
    e.complete()
    assert not e.state['errors'] and e.batches.get(identity)['status'] == 'complete'
    before = len(transport['calls'])
    e.press('Run or resume batch')
    e.complete()
    assert len(transport['calls']) == before
    e.press('Export completed batch')
    e.complete()
    output, = e.output.glob('TranslationBatch-*')
    for index, source in enumerate(e.selected, 1):
        assert (output / f'{index:04d}-translation' / 'original.txt').read_bytes() == source['text'].encode('utf-8')
    assert 'Saved new batch files in:' in e.find('Translation batch review and existing checkpoints').value
    assert e.find('Translation batch review and existing checkpoints').focused


def test_existing_translation_workspace_collects_immutable_per_document_settings(translation_handlers, monkeypatch, transport):
    e = translation_handlers
    received = []
    def batch(frame, *, items):
        received.append(deepcopy(items))
        return items
    monkeypatch.setattr(ui, 'show_translation_batch', batch)
    def scenario():
        prepare_handler(e)
        e.press('Add current source to batch')
        e.find('Selected text; originals remain unchanged').ChangeValue('Second selected original.')
        e.find('Target language code').ChangeValue('fr')
        e.find('Maximum approved budget in USD; no default price').ChangeValue('2')
        e.press('Add current source to batch')
        before = len(transport['calls'])
        e.press('Document batches...')
        assert len(received) == 1 and len(received[0]) == 2
        assert received[0][0]['text'] == 'Handler reviewed original.'
        assert [row['target'] for row in received[0]] == ['uk', 'fr']
        assert [row['budget'] for row in received[0]] == ['1', '2']
        assert not e.service.jobs.identities() and len(transport['calls']) == before
    e.scenario(scenario)
