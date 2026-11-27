from types import SimpleNamespace

import pytest

from carr_library.ui.translation_dialog import _matches_saved_scope


def account(identity='account-a', name='Account A'):
    return SimpleNamespace(to_dict=lambda: {'id': identity, 'name': name})


def job():
    return {'profile': account().to_dict(), 'source_text': 'Original A',
            'source_language': 'en', 'target_language': 'uk', 'authorization': {'budget': '1.00'}}


@pytest.mark.parametrize('index,changed', [(0, account('account-b')), (0, account(name='Changed')),
    (0, None), (1, 'Original B'), (2, 'auto'), (3, 'sk'), (4, '0')])
def test_changed_visible_scope_cannot_resume(index, changed):
    values = [account(), 'Original A', 'en', 'uk', '1.0']
    values[index] = changed
    assert not _matches_saved_scope(job(), *values)


def test_unchanged_scope_matches_numeric_budget():
    assert _matches_saved_scope(job(), account(), 'Original A', 'en', 'uk', '1')


def test_offline_export_does_not_need_available_account():
    assert _matches_saved_scope(job(), None, 'Original A', 'en', 'uk', '1', offline=True)
    assert not _matches_saved_scope(job(), None, 'Original B', 'en', 'uk', '1', offline=True)
    assert not _matches_saved_scope(job(), account('account-b'), 'Original A', 'en', 'uk', '1', offline=True)


from dataclasses import replace
from pathlib import Path
import runpy
import sys
import threading

from carr_library.ui import translation_dialog as workspace


@pytest.fixture
def saved_translation(tmp_path, monkeypatch):
    # Reuse the existing real Gateway/ledger seam, with authored synthetic HTTP.
    # No live network, credentials or source checkout are added to import paths.
    authored = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'assistant' / 'test_translation.py'))
    connection = authored['connection'].__wrapped__(monkeypatch)
    service, profile, vault = authored['ready'](tmp_path, connection)
    original = 'Selected source A; preserve the original and its approved scope.'
    approval, bound = workspace.translation_approval(profile, original, 'en', 'uk', '1')
    identity = service.submit(profile, original, 'en', 'uk', authorization=approval, per_call_cost=bound)
    assert service.resume(identity)['status'] == 'complete'
    return SimpleNamespace(service=service, profile=profile, vault=vault,
                           connection=connection, identity=identity, original=original)


def native_protocol_double(monkeypatch, tmp_path, saved, profiles):
    """Exercise actual bound UI handlers and stores; this is not native wx evidence."""
    ui = SimpleNamespace(fields={}, buttons={}, jobs=[], gateways=[], choice_labels=[],
                         choice_result=1, chooser_count=0, focus=None, scenario=None,
                         directory=tmp_path / 'exports', directory_hook=lambda: None,
                         choice_hook=lambda: None, peers=[], forbidden=[], destroyed_ids=[],
                         dialog=None, thread_id=threading.get_ident())
    ui.directory.mkdir()

    class Control:
        def __init__(self, *args, value='', choices=(), label='', **kwargs):
            self.value, self.choices, self.label = value, tuple(choices), label
            self.selection, self.enabled, self.handler = -1, True, None
            self.parent = args[0] if args and isinstance(args[0], Control) else None
            self.children, self.handlers, self.live = [], {}, True
            self.native_id = len(ui.peers) + 100
            ui.peers.append(self)
            if self.parent is not None:
                self.parent.children.append(self)

        def __bool__(self):
            return self.live

        def native(self, method):
            if threading.get_ident() != ui.thread_id:
                ui.forbidden.append((self.native_id, 'worker:' + method))
                raise RuntimeError('Translation native access left the UI thread.')
            if not self.live:
                ui.forbidden.append((self.native_id, method))
                raise RuntimeError('Native method on a deleted translation peer: ' + method)

        def GetId(self):
            self.native('GetId')
            return self.native_id

        def SetName(self, name):
            self.native('SetName')
            ui.fields[name] = self

        def GetValue(self):
            self.native('GetValue')
            return self.value

        def SetEditable(self, editable):
            self.native('SetEditable')
            self.editable = editable

        def ChangeValue(self, value):
            self.native('ChangeValue')
            self.value = value

        def GetSelection(self):
            self.native('GetSelection')
            return self.selection

        def SetSelection(self, index):
            self.native('SetSelection')
            self.selection = index

        def Enable(self, enabled=True):
            self.native('Enable')
            self.enabled = enabled

        def Disable(self):
            self.Enable(False)

        def SetFocus(self):
            self.native('SetFocus')
            ui.focus = self

        def Bind(self, event, handler):
            self.native('Bind')
            self.handlers[event] = handler
            if event == 'button':
                self.handler = handler

        def SetSizer(self, layout):
            self.native('SetSizer')

        def _native_destroy(self):
            """Native-parent deletion bypasses the Python Destroy override."""
            if not self.live:
                return False
            for child in tuple(self.children):
                child._native_destroy()
            self.live = False
            ui.destroyed_ids.append(self.native_id)
            event = SimpleNamespace(GetId=lambda: self.native_id, Skip=lambda: None,
                GetEventObject=lambda: SimpleNamespace(GetId=lambda: self.native_id))
            peer = self
            while peer is not None:
                handler = peer.handlers.get('destroy')
                if handler is not None:
                    handler(event)
                peer = peer.parent
            return True

        def Destroy(self):
            self.native('Destroy')
            return self._native_destroy()

    class Dialog(Control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.modal = False
            ui.dialog = self

        def ShowModal(self):
            self.native('ShowModal')
            self.modal = True
            ui.scenario(ui)
            self.modal = False
            return 2

        def IsModal(self):
            self.native('IsModal')
            return self.modal

        def EndModal(self, result):
            self.native('EndModal')
            self.modal = False

    class Button(Control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            ui.buttons[self.label.replace('&', '')] = self

        def click(self):
            self.native('click')
            assert self.enabled, 'The test attempted a disabled native action.'
            self.handler(None)

    class ChoiceDialog(Control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            ui.choice_labels = list(args[3])
            self.selection = 0

        def ShowModal(self):
            self.native('ShowModal')
            ui.choice_hook()
            return ui.choice_result

    class DirectoryDialog(Control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            ui.chooser_count += 1

        def ShowModal(self):
            self.native('ShowModal')
            ui.directory_hook()
            return 1

        def GetPath(self):
            self.native('GetPath')
            return str(ui.directory)

    wx = SimpleNamespace(Dialog=Dialog, TextCtrl=Control, Choice=Control, StaticText=Control,
        Button=Button, SingleChoiceDialog=ChoiceDialog, DirDialog=DirectoryDialog,
        BoxSizer=lambda *args: SimpleNamespace(Add=lambda *a, **k: None),
        CallAfter=lambda callback, *args: callback(*args), MessageBox=lambda *a, **k: 1,
        ID_OK=1, ID_CLOSE=2, YES=1, YES_NO=4, NO_DEFAULT=8,
        VERTICAL=1, HORIZONTAL=2, ALL=4, EXPAND=8, TE_MULTILINE=16, TE_READONLY=32,
        DEFAULT_DIALOG_STYLE=64, RESIZE_BORDER=128, EVT_BUTTON='button', EVT_CLOSE='close',
        EVT_WINDOW_DESTROY='destroy')
    ui.wx, ui.Control = wx, Control
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr(workspace, 'TranslationService', lambda root, **kwargs: saved.service)
    # Use the real current-account catalog: visible snapshots cannot stand in
    # for the profile reread performed by the queued remote operation.
    from carr_library.assistant.profiles import ProfileStore
    store = ProfileStore(tmp_path / 'library' / 'state' / 'providers')
    for profile in profiles:
        store.save(profile)

    def gateway(root, *, mode):
        ui.gateways.append((root, mode))
        saved.service.gateway.mode = mode
        return saved.service.gateway

    monkeypatch.setattr(workspace, 'Gateway', gateway)

    def run_job(title, operation, *, done, failed):
        ui.jobs.append(title)
        try:
            result = operation()
        except Exception as exc:
            failed(str(exc))
        else:
            done(result)
        return True

    ui.frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path / 'library'),
        working=False, _closing=False, cancel_event=threading.Event(), run_job=run_job)
    return ui

def visible(ui):
    return {key: ui.fields[name] for key, name in {
        'account': 'Google API account and edition',
        'source': 'Source language code; auto detects',
        'target': 'Target language code',
        'text': 'Selected text; originals remain unchanged',
        'budget': 'Maximum approved budget in USD; no default price',
        'rate': 'Optional price per million characters in USD; your current contract rate',
        'status': 'Preview, supported languages and job status',
    }.items()}


def test_saved_job_restores_visible_scope_and_resumes_without_rebilling(tmp_path, monkeypatch, saved_translation):
    saved = saved_translation
    ui = native_protocol_double(monkeypatch, tmp_path, saved, [saved.profile])
    before = len(saved.connection['calls'])

    def scenario(ui):
        fields = visible(ui)
        fields['text'].ChangeValue('Different previously displayed source.')
        fields['source'].ChangeValue('auto')
        fields['target'].ChangeValue('fr')
        fields['budget'].ChangeValue('99')
        fields['rate'].ChangeValue('123')
        ui.buttons['Saved jobs'].click()
        assert fields['account'].GetSelection() == 0
        assert fields['text'].GetValue() == saved.original
        assert fields['source'].GetValue() == 'en' and fields['target'].GetValue() == 'uk'
        assert fields['budget'].GetValue() == '1' and fields['rate'].GetValue() == ''
        assert saved.identity in ui.choice_labels[0] and saved.profile.id in ui.choice_labels[0]
        assert ui.focus is fields['status']
        ui.buttons['Translate / resume'].click()

    ui.scenario = scenario
    workspace.show_translation(ui.frame)
    assert ui.jobs == ['Reading saved translation jobs', 'Translating approved selected text']
    assert len(saved.connection['calls']) == before
    assert saved.service.get(saved.identity)['source_text'] == saved.original


@pytest.mark.parametrize('action', ['Translate / resume', 'Export'])
@pytest.mark.parametrize('change', ['account', 'text', 'source', 'target', 'budget'])
def test_changed_visible_saved_scope_blocks_actual_handlers_before_queue_or_chooser(
        tmp_path, monkeypatch, saved_translation, action, change):
    saved = saved_translation
    other = replace(saved.profile, id='other_account',
                    credential_ref='carr/provider/other_account/' + 'b' * 32)
    ui = native_protocol_double(monkeypatch, tmp_path, saved, [saved.profile, other])
    before = saved.service.jobs.path(saved.identity).read_bytes()
    calls, credentials = len(saved.connection['calls']), len(saved.vault.calls)

    def scenario(ui):
        ui.buttons['Saved jobs'].click()
        fields = visible(ui)
        if change == 'account':
            fields['account'].SetSelection(1)
        else:
            fields[change].ChangeValue({'text': 'Different source B.', 'source': 'auto',
                                       'target': 'fr', 'budget': '2'}[change])
        ui.buttons[action].click()
        assert 'Visible settings differ from saved job ' + saved.identity in fields['status'].GetValue()
        assert ui.focus is fields['status']

    ui.scenario = scenario
    workspace.show_translation(ui.frame)
    assert ui.jobs == ['Reading saved translation jobs'] and not ui.gateways and ui.chooser_count == 0
    assert saved.service.jobs.path(saved.identity).read_bytes() == before
    assert len(saved.connection['calls']) == calls and len(saved.vault.calls) == credentials


@pytest.mark.parametrize('account_state', ['missing', 'changed'])
def test_missing_saved_account_blocks_remote_resume_but_preserves_real_offline_export(
        tmp_path, monkeypatch, saved_translation, account_state):
    saved = saved_translation
    profiles = [] if account_state == 'missing' else [replace(saved.profile, name='Changed account snapshot')]
    ui = native_protocol_double(monkeypatch, tmp_path, saved, profiles)
    saved.service.gateway.mode = 'LOCAL_ONLY'
    before = saved.service.jobs.path(saved.identity).read_bytes()
    calls, credentials = len(saved.connection['calls']), len(saved.vault.calls)

    def scenario(ui):
        ui.buttons['Saved jobs'].click()
        fields = visible(ui)
        assert fields['account'].GetSelection() == -1
        assert 'remote resume is blocked' in fields['status'].GetValue()
        ui.buttons['Translate / resume'].click()
        assert ui.jobs == ['Reading saved translation jobs'] and not ui.gateways
        ui.buttons['Export'].click()

    ui.scenario = scenario
    workspace.show_translation(ui.frame)
    assert ui.jobs == ['Reading saved translation jobs', 'Exporting translation derivative'] and ui.chooser_count == 1
    output, = ui.directory.iterdir()
    assert (output / 'original.txt').read_text(encoding='utf-8') == saved.original
    assert (output / 'translation.reading.json').is_file()
    assert saved.service.jobs.path(saved.identity).read_bytes() == before
    assert len(saved.connection['calls']) == calls and len(saved.vault.calls) == credentials
    assert not ui.gateways and saved.service.gateway.mode == 'LOCAL_ONLY'
