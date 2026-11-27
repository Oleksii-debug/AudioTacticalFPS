"""Actual renewal dialog function and store with controlled native widgets."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.assistant.google_oauth import parse_refresh_credential
from carr_library.assistant.profiles import ProfileStore, ProviderProfile
from carr_library.ui.google_oauth import show_google_refresh

CLIENT = '123456789-synthetic.apps.googleusercontent.com'
SECRET = 'SYNTHETIC_DESKTOP_SECRET'
REFRESH = 'SYNTHETIC_REFRESH_GRANT'
ENDPOINT = 'https://translation.googleapis.com/v3/projects/123456789/locations/global'


@pytest.fixture
def window(tmp_path):
    root = tmp_path / 'library'
    store = ProfileStore(root / 'state' / 'providers')
    p = ProviderProfile('renewal_ui', 'Reviewed account', 'google-translation-v3',
        ENDPOINT, 'general/nmt', ('translation.text', 'translation.languages'),
        credential_ref='carr/provider/renewal_ui/' + 'a' * 32, enabled=False)
    store.save(p)
    state = SimpleNamespace(store=store, profile=p, frame=SimpleNamespace(
        library=SimpleNamespace(root=root), _closing=False), controls=[], writes=[],
        reads=[], questions=[], on_modal=lambda: None, on_question=lambda: None,
        answer=4, modal_result=1, destroyed=False,
        values=[CLIENT, SECRET, REFRESH])
    state.vault = SimpleNamespace(put=lambda identity, value, reference=None:
        state.writes.append((identity, value, reference)) or reference,
        delete=lambda *args: pytest.fail('Same-profile replacement must not delete another credential'))
    wx = SimpleNamespace(ID_OK=1, ID_CANCEL=2, YES=4, NO=8,
        DEFAULT_DIALOG_STYLE=16, RESIZE_BORDER=32, VERTICAL=64,
        ALL=128, EXPAND=256, TE_PASSWORD=512, OK=1024, CANCEL=2048,
        YES_NO=4096, NO_DEFAULT=8192, ICON_QUESTION=16384)

    class Dialog:
        def __init__(self, *args, **kwargs):
            state.dialog = self
        def CreateButtonSizer(self, *args):
            return object()
        def SetSizer(self, value):
            pass
        def ShowModal(self):
            for control, value in zip(state.controls, state.values):
                control.value = value
            state.on_modal()
            return state.modal_result
        def Destroy(self):
            state.destroyed = True

    class Text:
        def __init__(self, parent, style=0):
            self.value, self.name, self.style = '', '', style
            state.controls.append(self)
        def SetName(self, name):
            self.name = name
        def GetValue(self):
            state.reads.append(self.name)
            return self.value
        def ChangeValue(self, value):
            self.value = value

    class Static:
        def __init__(self, *args, **kwargs):
            pass
        def Wrap(self, value):
            pass

    class Sizer:
        def __init__(self, *args):
            pass
        def Add(self, *args):
            pass

    def question(message, title, style, parent):
        state.questions.append((message, title, style, parent))
        state.on_question()
        return state.answer

    wx.Dialog, wx.TextCtrl, wx.StaticText, wx.BoxSizer = Dialog, Text, Static, Sizer
    wx.MessageBox = question
    state.wx = wx
    state.run = lambda: show_google_refresh(object(), state.frame, store, state.vault, p, wx_module=wx)
    return state


def test_explicit_default_no_confirmation_saves_only_selected_windows_vault_record(window):
    h = window
    before = h.store.path(h.profile.id).read_bytes()
    result = h.run()
    assert result == h.profile and not result.enabled
    assert h.store.path(result.id).read_bytes() == before
    identity, record, reference = h.writes[0]
    assert identity == h.profile.id and reference == h.profile.credential_ref
    parsed = parse_refresh_credential(record, ENDPOINT)
    assert parsed['client_id'] == CLIENT and parsed['refresh_token'] == REFRESH
    message, _, style, parent = h.questions[0]
    assert style == h.wx.YES_NO | h.wx.NO_DEFAULT | h.wx.ICON_QUESTION
    assert parent is h.dialog and ENDPOINT not in message
    assert 'oauth2.googleapis.com/token' in message
    assert SECRET not in message and REFRESH not in message
    assert h.controls[1].style == h.controls[2].style == h.wx.TE_PASSWORD
    assert h.controls[1].value == h.controls[2].value == '' and h.destroyed


@pytest.mark.parametrize('answer', [8, 2])
def test_no_or_cancel_at_confirmation_never_writes_credential(window, answer):
    h = window
    h.answer = answer
    assert h.run() is None
    assert not h.writes and len(h.questions) == 1
    assert h.controls[1].value == h.controls[2].value == '' and h.destroyed


def test_cancelled_form_does_not_read_secret_fields_or_ask_confirmation(window):
    h = window
    h.modal_result = h.wx.ID_CANCEL
    assert h.run() is None
    assert not h.reads and not h.questions and not h.writes
    assert h.controls[1].value == h.controls[2].value == '' and h.destroyed


def test_invalid_grant_is_rejected_before_confirmation(window):
    h = window
    h.values[0] = 'https://wrong.invalid/client'
    with pytest.raises(ValueError, match='client ID'):
        h.run()
    assert not h.questions and not h.writes
    assert h.controls[1].value == h.controls[2].value == '' and h.destroyed


@pytest.mark.parametrize('boundary', ['on_modal', 'on_question'])
@pytest.mark.parametrize('change', ['profile', 'library', 'closing'])
def test_changed_modal_authority_cannot_save_any_secret(window, boundary, change):
    h = window
    def mutate():
        if change == 'profile':
            h.store.save(replace(h.profile, name='A newer account review'))
        elif change == 'library':
            h.frame.library = SimpleNamespace(root=h.frame.library.root)
        else:
            h.frame._closing = True
    setattr(h, boundary, mutate)
    with pytest.raises(ValueError, match='changed'):
        h.run()
    assert not h.writes and h.destroyed
    if boundary == 'on_modal':
        assert not h.reads and not h.questions


@pytest.mark.parametrize('boundary', ['on_modal', 'on_question'])
def test_same_path_replaced_library_with_identical_profile_never_writes_global_vault(window, boundary):
    h = window
    root = h.frame.library.root
    original = h.store.path(h.profile.id).read_bytes()
    def replace_root():
        root.rename(root.with_name('original-library'))
        ProfileStore(root / 'state' / 'providers').save(h.profile)
        assert h.store.path(h.profile.id).read_bytes() == original
    setattr(h, boundary, replace_root)
    with pytest.raises(ValueError, match='changed'):
        h.run()
    assert not h.writes and h.destroyed
    if boundary == 'on_modal':
        assert not h.reads and not h.questions


def test_wrong_adapter_is_rejected_before_dialog_or_secret_read(window):
    h = window
    other = ProviderProfile(h.profile.id, 'Other adapter', 'google-translation-v2',
        'https://translation.googleapis.com/language/translate/v2', 'nmt',
        ('translation.text', 'translation.languages'), credential_ref=h.profile.credential_ref)
    h.store.save(other)
    with pytest.raises(ValueError, match='Advanced v3'):
        show_google_refresh(object(), h.frame, h.store, h.vault, other, wx_module=h.wx)
    assert not h.controls and not h.reads and not h.writes


def test_review_captures_exact_fields_before_confirmation(window):
    h = window
    h.on_question = lambda: [setattr(control, 'value', 'changed_after_review')
                            for control in h.controls]
    assert h.run() == h.profile
    _, record, _ = h.writes[0]
    assert parse_refresh_credential(record, ENDPOINT)['refresh_token'] == REFRESH
    assert h.controls[1].value == h.controls[2].value == ''
