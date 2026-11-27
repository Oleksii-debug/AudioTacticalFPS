"""Real book-dialog closures with scheduler/widgets stand-ins, not native evidence."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.studio.book_audio import BookAudioService
from carr_library.studio.render import ProductionService
from carr_library.ui import book_audio_dialog as ui


@pytest.fixture
def handlers(tmp_path, monkeypatch):
    state = {'widgets': [], 'queue': [], 'approvals': [], 'reports': [], 'end': []}
    ui_thread = threading.get_ident()

    class Widget:
        def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
            self.value, self.label, self.name = value, label, ''
            self.choices, self.selection = list(choices), -1
            self.bindings, self.enabled, self.focused = {}, True, False
            state['widgets'].append(self)
        def SetName(self, name): self.name = name
        def GetValue(self):
            assert threading.get_ident() == ui_thread, 'Worker read a live native control.'
            return self.value
        def ChangeValue(self, value): self.value = value
        def GetSelection(self):
            assert threading.get_ident() == ui_thread, 'Worker read live voice/split selection.'
            return self.selection
        def SetSelection(self, value): self.selection = value
        def SetItems(self, values): self.choices, self.selection = list(values), -1
        def Bind(self, event, handler): self.bindings[event] = handler
        def SetFocus(self): self.focused = True
        def SetSizer(self, sizer): pass
        def Enable(self, value=True): self.enabled = value
        def EndModal(self, code): state['end'].append(code)
        def Destroy(self): pass

    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): pass
        def AddGrowableCol(self, *args): pass

    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'TE_READONLY', 'TE_MULTILINE',
             'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND', 'ID_ANY', 'ID_CANCEL',
             'ID_OK', 'YES_NO', 'NO_DEFAULT', 'YES', 'EVT_CHOICE', 'EVT_TEXT',
             'EVT_BUTTON', 'EVT_CLOSE', 'EVT_MENU')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(names)})
    for name in ('Dialog', 'StaticText', 'TextCtrl', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = wx.FlexGridSizer = Sizer
    wx.CallAfter = lambda callback, *args: callback(*args)
    def approve(*args, **kwargs):
        state['approvals'].append(args[0])
        return wx.YES
    wx.MessageBox = approve
    monkeypatch.setitem(sys.modules, 'wx', wx)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Book handler source')
    imported = app.add_text(workspace, 'UI source', 'First literal part.\n\nSecond literal part.')
    voices = SimpleNamespace(voices=lambda: (SimpleNamespace(
        id='synthetic-listed-voice', name='Synthetic listed voice', language='en'),))
    production = ProductionService(app.root / 'state' / 'studio', voices=voices)
    service = BookAudioService(app, workspace, production=production)
    panel = SimpleNamespace(ready=True, workspace=workspace,
                            rows={imported['edition']: object()},
                            _selection=lambda: (None, None, imported['edition']))
    frame = SimpleNamespace(library=app, active=(workspace, None),
        book=SimpleNamespace(GetPage=lambda index: panel), working=False, _closing=False,
        cancel_event=threading.Event(), report=state['reports'].append)
    def run_job(title, operation, done=None, failed=None):
        assert not frame.working
        frame.working = True
        state['queue'].append((title, operation, done, failed))
        return True
    frame.run_job = run_job
    output = tmp_path / 'outputs'
    output.mkdir()
    def find(name):
        return next(widget for widget in state['widgets'] if widget.name == name)
    def press(name):
        widget = find(name)
        assert widget.enabled, name
        widget.bindings[wx.EVT_BUTTON](None)
    def complete():
        title, operation, done, failed = state['queue'].pop(0)
        results = []
        def work():
            try: results.append((done, operation()))
            except Exception as exc: results.append((failed, exc))
        thread = threading.Thread(target=work)
        thread.start()
        thread.join(20)
        assert not thread.is_alive() and len(results) == 1, title
        frame.working = False
        callback, result = results[0]
        if callback:
            callback(result)
        elif isinstance(result, Exception):
            raise result
        return result
    dialog, close = ui.create_book_audio_dialog(frame, service=service, wx_module=wx)
    yield SimpleNamespace(state=state, wx=wx, frame=frame, service=service, production=production,
        find=find, press=press, complete=complete, close=close, output=output,
        app=app, workspace=workspace, imported=imported, ui_thread=ui_thread)
    close()
    service.close()
    production.close()


def preview(e):
    e.press('Load selected book')
    assert not e.service.saved()
    e.complete()
    e.press('Discover local voices')
    e.complete()
    e.find('Split mode').SetSelection(6)
    e.find('Reviewed output folder').ChangeValue(str(e.output))
    e.press('Preview split and names')
    assert e.state['queue']
    e.complete()


def test_preview_and_prepare_validate_source_on_worker_and_never_start_speech(handlers, monkeypatch):
    import carr_library.studio.book_audio as book_audio
    e = handlers
    original, calls = book_audio.validate_source, []
    def checked(*args, **kwargs):
        calls.append(threading.get_ident())
        assert calls[-1] != e.ui_thread
        return original(*args, **kwargs)
    monkeypatch.setattr(book_audio, 'validate_source', checked)
    preview(e)
    assert calls and not e.production.jobs()
    status = e.find('Book split preview and saved job status')
    assert 'UI source' in status.value and 'Revision:' in status.value and status.focused
    assert '0001-UI-source' in status.value and '0002-UI-source' in status.value
    e.press('Prepare saved items')
    assert len(e.state['approvals']) == 1 and not e.production.jobs()
    assert e.state['queue'] and not e.find('Split mode').enabled
    e.complete()
    assert len(e.production.jobs()) == 2 and not list(e.output.iterdir())
    assert e.find('Run or resume saved items').enabled
    assert 'PREPARED' in status.value
    assert all(thread != e.ui_thread for thread in calls)


@pytest.mark.parametrize('field,value', [
    ('Part name template: index, book, part', '{index:04d} - Changed name'),
    ('Reviewed output folder', 'different-output'),
    ('First reading position; blank for whole source', '2'),
])
def test_changed_fields_cannot_reuse_preview_approval(handlers, field, value):
    e = handlers
    preview(e)
    e.find(field).ChangeValue(value)
    e.press('Prepare saved items')
    assert not e.state['queue'] and not e.state['approvals'] and not e.production.jobs()
    assert 'Preview this exact' in e.find('Book split preview and saved job status').value


def test_run_gets_only_the_explicit_frozen_review(handlers, monkeypatch):
    e = handlers
    preview(e)
    e.press('Prepare saved items')
    e.complete()
    requests = []
    def run(plan_id, destination, **options):
        assert threading.get_ident() != e.ui_thread
        requests.append((plan_id, destination, options['approval']))
        options['guard']()
        assert not options['cancelled']()
        return tuple({'index': i, 'status': 'complete', 'destination': state['destination']}
                     for i, state in enumerate(e.production.jobs(), 1))
    monkeypatch.setattr(e.service, 'run', run)
    e.press('Run or resume saved items')
    assert len(e.state['approvals']) == 2 and requests == []
    e.complete()
    assert len(requests) == 1 and requests[0][1] == str(e.output)
    saved, = e.service.saved()
    review = e.service.review(saved, e.output)
    assert requests[0] == (saved.id, str(e.output), review.fingerprint)
    assert 'Verified saved items' in e.find('Book split preview and saved job status').value


def test_closing_before_queued_source_work_retains_borrowed_service_and_no_plan(handlers):
    e = handlers
    e.press('Load selected book')
    e.close()
    result = e.complete()
    assert isinstance(result, InterruptedError)
    assert not e.service.saved() and not e.production.jobs()
    assert not e.service._closed.is_set()
    assert e.state['end'] == [e.wx.ID_CANCEL]


def test_scope_change_after_preview_never_prepares_jobs(handlers):
    e = handlers
    preview(e)
    e.frame.active = (e.workspace, 'different-topic')
    e.press('Prepare saved items')
    assert not e.state['queue'] and not e.production.jobs()
    assert 'active library or topic changed' in e.find('Book split preview and saved job status').value


def test_preview_uses_new_values_even_if_widget_change_emits_no_event(handlers):
    e = handlers
    preview(e)
    old = e.find('Book split preview and saved job status').value
    e.find('Part name template: index, book, part').ChangeValue('{index:04d} - Explicit new name')
    e.press('Preview split and names')
    e.complete()
    assert 'Explicit-new-name' in e.find('Book split preview and saved job status').value
    assert e.find('Book split preview and saved job status').value != old
    assert not e.production.jobs()


def test_owned_cleanup_is_queued_after_active_work_and_never_runs_on_wx(handlers, monkeypatch):
    e = handlers
    e.close()
    # Reopen with this explicit service returned as the owned constructor.
    e.state['widgets'].clear()
    monkeypatch.setattr(ui, 'BookAudioService', lambda *args: e.service)
    closes = []
    original = e.service.close
    def close_service():
        closes.append(threading.get_ident())
        assert closes[-1] != e.ui_thread
        original()
    monkeypatch.setattr(e.service, 'close', close_service)
    _, close = ui.create_book_audio_dialog(e.frame, wx_module=e.wx)
    e.press('Load selected book')
    close()
    assert closes == []
    e.complete()  # Cancel the existing source operation, then queue owner cleanup.
    assert closes == [] and len(e.state['queue']) == 1
    e.complete()
    assert len(closes) == 1 and e.service._closed.is_set()
    monkeypatch.setattr(e.service, 'close', original)


def test_saved_plan_controls_display_locked_voice_and_split_without_automatic_resume(handlers, monkeypatch):
    e = handlers
    preview(e)
    e.press('Prepare saved items')
    e.complete()
    class Picker:
        def __init__(self, *args): pass
        def ShowModal(self): return e.wx.ID_OK
        def GetSelection(self): return 0
        def Destroy(self): pass
    monkeypatch.setattr(e.wx, 'SingleChoiceDialog', Picker, raising=False)
    e.press('Saved book plans')
    e.complete()
    assert e.find('Split mode').GetSelection() == 6 and not e.find('Split mode').enabled
    assert e.find('Installed local voice').choices == ['Saved local voice: synthetic-listed-voice']
    assert not e.find('Run or resume saved items').enabled and not e.state['queue']
    assert len(e.production.jobs()) == 2


def test_pause_before_queued_run_preserves_prepared_items_without_synthesis(handlers):
    e = handlers
    preview(e)
    e.press('Prepare saved items')
    e.complete()
    before = {row['id']: (e.production.jobs_root / row['id'] / 'job.json').read_bytes()
              for row in e.production.jobs()}
    e.press('Run or resume saved items')
    e.press('Pause')
    result = e.complete()
    assert isinstance(result, InterruptedError)
    assert not list(e.output.iterdir())
    assert before == {identity: (e.production.jobs_root / identity / 'job.json').read_bytes()
                      for identity in before}
