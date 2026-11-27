"""Native-handler composition with controlled shared jobs and wx test doubles.

Actual wx controls and keyboard behavior are exercised by the installed native
probe separately. This suite checks callbacks, stale scope and worker threads.
"""
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace as N

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.profiles import ProfileStore, ProviderProfile
from carr_library.assistant.translation import ENDPOINT
from carr_library.ui.translation_dialog import create_translation_dialog


@pytest.fixture
def form(tmp_path):
    owner = threading.get_ident()
    controls, messages = [], []
    class Control:
        def __init__(self, *args, **kwargs):
            self.value = kwargs.get('value', '')
            self.label = kwargs.get('label', '')
            self.title = kwargs.get('title', '')
            self.choices = list(kwargs.get('choices', ()))
            self.selection = -1
            self.enabled = self.editable = True
            self.focused = self.destroyed = self.hidden = False
            self.handlers = {}
            controls.append(self)
        def native(self): assert threading.get_ident() == owner
        def GetValue(self): self.native(); return self.value.replace('\r\n', '\n')
        def ChangeValue(self, value): self.native(); self.value = value
        def SetValue(self, value): self.ChangeValue(value)
        def SetName(self, name): self.native(); self.name = name
        def SetSelection(self, index): self.native(); self.selection = index
        def GetSelection(self): self.native(); return self.selection
        def GetSelections(self): self.native(); return [0]
        def Enable(self, enabled): self.native(); self.enabled = enabled
        def Disable(self): self.Enable(False)
        def SetEditable(self, editable): self.native(); self.editable = editable
        def SetFocus(self): self.native(); self.focused = True
        def Add(self, *args): self.native()
        def SetSizer(self, *args): self.native()
        def Bind(self, event, handler): self.native(); self.handlers[event] = handler
        def IsModal(self): self.native(); return False
        def EndModal(self, result): self.native()
        def Hide(self): self.native(); self.hidden = True
        def Destroy(self): self.native(); self.destroyed = True
    class Picker(Control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.selection = 0
        def ShowModal(self): self.native(); return 1
        def GetPath(self): self.native(); return str(tmp_path)
    class Entry(Picker):
        def GetValue(self): self.native(); return '1-2'
    choice_callback = [lambda: None]
    def message(*args):
        assert threading.get_ident() == owner
        messages.append(args)
        choice_callback[0]()
        return 2
    wx = N(Dialog=Control, TextCtrl=Control, Choice=Control, Button=Control, StaticText=Control,
        BoxSizer=Control, SingleChoiceDialog=Picker, MultiChoiceDialog=Picker,
        TextEntryDialog=Entry, DirDialog=Picker, MessageBox=message,
        ID_OK=1, YES=2, ID_CLOSE=3, EVT_BUTTON='button', EVT_TEXT='text', EVT_CHOICE='choice',
        EVT_CLOSE='close', CallAfter=lambda callback, *args: None)
    for name in ('ALL', 'EXPAND', 'VERTICAL', 'HORIZONTAL', 'DEFAULT_DIALOG_STYLE',
                 'RESIZE_BORDER', 'TE_MULTILINE', 'TE_READONLY', 'YES_NO', 'NO_DEFAULT'):
        setattr(wx, name, 0)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Native selection tests')
    book = app.add_text(workspace, 'Exact source book', 'Original CRLF.\r\nSecond line.\fNext section.')
    p = ProviderProfile('ui_google', 'UI test account', 'google-translation-v2', ENDPOINT, 'nmt',
        capabilities=('translation.text', 'translation.languages'), enabled=True,
        credential_ref='carr/provider/ui_google/' + 'c' * 32)
    profiles = ProfileStore(app.root / 'state/providers')
    profiles.save(p)
    job = {'id': 'e' * 32, 'title': 'Saved exact selection', 'status': 'complete',
        'source_text': 'Saved original.\r\nSecond line.', 'source_language': 'en', 'target_language': 'uk',
        'profile': p.to_dict(), 'authorization': {'budget': '1.00'}, 'chunks': ['Saved'],
        'receipts': [{'text': 'Translation'}]}
    class Service:
        def __init__(self):
            self.calls = []
            self.scope_guard = lambda: None
            self.gateway = None
        def record(self, operation, *args):
            assert threading.get_ident() != owner, 'Domain work ran on the native UI thread.'
            self.scope_guard()
            self.calls.append((operation, args))
        def preview(self, profile, value, source, target, **kwargs):
            self.record('preview', profile, value, source, target, kwargs)
            return {'estimated_cost': None, 'characters': len(value), 'calls': 1}
        def submit(self, *args, **kwargs):
            self.record('submit', args, kwargs)
            return job['id']
        def resume(self, identity, **kwargs):
            self.record('resume', identity, kwargs)
            return {'id': identity, 'status': 'complete'}
        def get(self, identity, **kwargs):
            self.record('get', identity)
            assert identity == job['id']
            return deepcopy(job)
        def list(self):
            self.record('list')
            return (deepcopy(job),)
        def export(self, identity, destination, **kwargs):
            self.record('export', identity, destination, kwargs)
            return {'path': str(destination)}
    service = Service()
    class Frame:
        library = app
        active = (workspace, None)
        _closing = False
        working = False
        cancel_event = threading.Event()
        def __init__(self):
            self.calls = []
        def run_job(self, title, operation, done, failed):
            assert threading.get_ident() == owner and not self.working
            self.working = True
            self.calls.append((operation, done, failed))
            return True
        def finish(self, index):
            operation, done, failed = self.calls[index]
            outcome = []
            def run():
                try: outcome.append((True, operation()))
                except BaseException as exc: outcome.append((False, exc))
            worker = threading.Thread(target=run)
            worker.start()
            worker.join(10)
            assert not worker.is_alive(), 'Bounded handler job did not finish.'
            self.working = False
            success, result = outcome[0]
            (done if success else failed)(result)
    frame = Frame()
    dialog = create_translation_dialog(frame, wx_module=wx, service=service)
    return N(dialog=dialog, controls=dialog.translation_controls, frame=frame, service=service,
        app=app, workspace=workspace, book=book, job=job, profile=p, profiles=profiles,
        wx=wx, messages=messages, choose=choice_callback)


def fill(form):
    c = form.controls
    c['selected'].ChangeValue('Fresh selected text.')
    c['source'].ChangeValue('en')
    c['target'].ChangeValue('uk')
    c['budget'].ChangeValue('1')


def choose_saved(form):
    form.dialog.on_jobs()
    form.frame.finish(0)
    form.frame.finish(1)
    assert form.controls['selected'].GetValue() == 'Saved original.\nSecond line.'


def test_preview_uses_shared_worker_and_never_native_controls_on_worker(form):
    fill(form)
    form.dialog.on_preview()
    assert form.service.calls == [] and not form.controls['preview'].enabled
    form.dialog.on_run()
    assert len(form.frame.calls) == 1
    form.frame.finish(0)
    assert form.service.calls[0][0] == 'preview'
    assert form.controls['preview'].enabled
    assert 'No request sent' in form.controls['status'].GetValue()


def test_changed_options_discard_pending_preview_without_approval(form):
    fill(form)
    form.dialog.on_preview()
    form.controls['budget'].ChangeValue('2')
    form.frame.finish(0)
    assert 'changed' in form.controls['status'].GetValue()
    form.dialog.on_run()
    assert form.messages == [] and len(form.frame.calls) == 1


def test_close_cancels_pending_preview_and_suppresses_late_native_updates(form):
    fill(form)
    form.dialog.on_preview()
    before = form.controls['status'].GetValue()
    form.dialog.on_close()
    form.frame.finish(0)
    assert form.dialog.translation_cancel.is_set()
    assert form.controls['status'].GetValue() == before
    assert form.messages == []


@pytest.mark.parametrize('change', ['text', 'budget', 'provider'])
def test_saved_resume_rejects_changed_visible_scope_before_domain_resume(form, change):
    choose_saved(form)
    c = form.controls
    if change == 'text': c['selected'].ChangeValue('Different source')
    elif change == 'budget': c['budget'].ChangeValue('2')
    else: c['provider'].SetSelection(-1)
    form.dialog.on_run()
    if len(form.frame.calls) > 2:
        form.frame.finish(2)
    assert not any(name in ('submit', 'resume') for name, _ in form.service.calls)
    assert c['status'].focused
    assert 'differ' in c['status'].GetValue() or 'account' in c['status'].GetValue()


def test_saved_selection_retains_exact_crlf_despite_native_display_normalization(form):
    choose_saved(form)
    form.dialog.on_run()
    form.frame.finish(2)
    assert any(name == 'resume' for name, _ in form.service.calls)
    assert 'complete' in form.controls['status'].GetValue()


@pytest.mark.parametrize('change', ['budget', 'scope'])
def test_approval_dialog_cannot_launch_stale_scope(form, change):
    fill(form)
    form.dialog.on_preview()
    form.frame.finish(0)
    if change == 'budget':
        form.choose[0] = lambda: form.controls['budget'].ChangeValue('3')
    else:
        form.choose[0] = lambda: setattr(form.frame, 'active', ('foreign-library', None))
    form.dialog.on_run()
    assert len(form.messages) == 1 and len(form.frame.calls) == 1
    assert not any(name == 'submit' for name, _ in form.service.calls)


def test_saved_export_preflight_is_async_and_close_never_launches_writer(form):
    choose_saved(form)
    form.dialog.on_export()
    assert len(form.frame.calls) == 3 and not any(name == 'export' for name, _ in form.service.calls)
    form.dialog.on_close()
    form.frame.finish(2)
    assert not any(name == 'export' for name, _ in form.service.calls)
    assert len(form.frame.calls) == 3


def test_whole_document_capture_is_async_readonly_and_crlf_exact(form):
    c = form.controls
    c['input_scope'].SetSelection(2)
    form.dialog.on_scope_changed()
    form.dialog.on_sources()
    form.frame.finish(0)
    form.frame.finish(1)
    assert not c['selected'].editable
    assert c['selected'].GetValue() == 'Original CRLF.\nSecond line.\n\nNext section.'
    c['budget'].ChangeValue('1')
    form.dialog.on_preview()
    form.frame.finish(2)
    name, args = form.service.calls[-1]
    assert name == 'preview' and args[1] == 'Original CRLF.\r\nSecond line.\n\nNext section.'
    binding = args[4]['source_binding']
    assert binding['sources'][0]['anchor']['edition'] == form.book['edition']


def test_late_source_capture_from_changed_library_cannot_fill_native_text(form):
    form.controls['selected'].ChangeValue('Retain my existing manual text.')
    form.controls['input_scope'].SetSelection(2)
    form.dialog.on_scope_changed()
    form.dialog.on_sources()
    form.frame.finish(0)
    form.frame.active = ('changed-library', None)
    form.frame.finish(1)
    assert form.controls['selected'].GetValue() == 'Retain my existing manual text.'
    assert 'changed' in form.controls['status'].GetValue()


def test_cancelled_source_capture_keeps_prior_selection(form):
    form.controls['selected'].ChangeValue('Prior manual text')
    form.controls['input_scope'].SetSelection(2)
    form.dialog.on_scope_changed()
    form.dialog.on_sources()
    form.frame.finish(0)
    form.dialog.translation_cancel.set()
    form.frame.finish(1)
    assert form.controls['selected'].GetValue() == 'Prior manual text'
    assert not any(name in ('submit', 'resume') for name, _ in form.service.calls)
