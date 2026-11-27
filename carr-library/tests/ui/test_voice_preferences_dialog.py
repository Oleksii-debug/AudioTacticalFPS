"""Real preference handlers, actual saved files and a serial worker boundary."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import get_ident
from types import SimpleNamespace
import sys

import pytest

from carr_library.runtime_settings import (load_voice_preferences, save_voice_preferences,
    review_voice_preference, settings_path)
from carr_library.speech import Voice, VoiceRegistry
from carr_library.ui.voice_preferences import show_voice_preferences


class Widget:
    def __init__(self, parent=None, *args, **kwargs):
        self.owner, self.parent = get_ident(), parent
        self.name = kwargs.get('label', next((item for item in args if isinstance(item, str)), ''))
        self.value = kwargs.get('value', kwargs.get('initial', ''))
        self.items, self.selection = list(kwargs.get('choices', [])), -1
        self.handlers, self.destroyed, self.enabled = {}, False, True
        self.widgets.append(self)
    def native(self):
        assert get_ident() == self.owner and not self.destroyed
    def GetId(self): self.native(); return id(self)
    def SetName(self, value): self.native(); self.name = value
    def GetValue(self): self.native(); return self.value
    def SetValue(self, value): self.native(); self.value = value
    def ChangeValue(self, value): self.SetValue(value)
    def SetItems(self, value): self.native(); self.items, self.selection = list(value), -1
    def SetSelection(self, value): self.native(); self.selection = value
    def GetSelection(self): self.native(); return self.selection
    def SetFocus(self): self.native()
    def Add(self, *args, **kwargs): self.native()
    def SetSizer(self, value): self.native()
    def SetEscapeId(self, value): self.native()
    def Bind(self, event, callback, *args): self.native(); self.handlers[event] = callback
    def Enable(self, value=True): self.native(); self.enabled = value
    def Disable(self): self.Enable(False)
    def EndModal(self, value): self.native()
    def Destroy(self):
        self.native()
        self.destroyed = True
        callback = self.handlers.get('destroy')
        if callback is not None:
            callback(SimpleNamespace(GetId=lambda: id(self), Skip=lambda: None))


@pytest.fixture
def ui(tmp_path, monkeypatch):
    widgets, pending, applied = [], [], []
    Widget.widgets = widgets
    state = SimpleNamespace(driver=lambda _window: None, authorized=True, confirm=False,
        state_root=tmp_path / 'state', initial=None, pending=pending, applied=applied, widgets=widgets,
        cancelled=False, reviews=[], inventory=None, factory_calls=0)
    class Dialog(Widget):
        def ShowModal(self):
            self.native()
            state.driver(self)
            return 2
    class StaticText(Widget):
        pass
    wx = SimpleNamespace(Dialog=Dialog, StaticText=StaticText, TextCtrl=Widget, Choice=Widget,
        SpinCtrl=Widget, Button=Widget, BoxSizer=Widget, GridSizer=Widget, ID_CANCEL=2,
        EVT_BUTTON='button', EVT_CHOICE='choice', EVT_CLOSE='close', EVT_WINDOW_DESTROY='destroy', YES=1, YES_NO=2, NO_DEFAULT=4,
        MessageBox=lambda *_args: 1 if state.confirm else 0)
    for name in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'EXPAND', 'ALL',
                 'TE_READONLY', 'TE_MULTILINE', 'ALIGN_RIGHT'):
        setattr(wx, name, 0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    voices = [Voice('uk:one', 'Український один', 'Fixture', 'uk'),
              Voice('sk:one', 'Slovenský jeden', 'Fixture', 'sk'),
              Voice('uk:two', 'Український два', 'Fixture', 'uk')]
    selected = {'voice_id': 'uk:one', 'language': 'uk', 'rate': -1, 'volume': 70}
    state.inventory = list(voices)
    class Provider:
        name = 'Preference dialog metadata fixture'
        def voices(self):
            return tuple(state.inventory)
        def fingerprint(self, _identity):
            return 'a' * 64
    borrowed = VoiceRegistry([Provider()])
    def factory():
        state.factory_calls += 1
        return VoiceRegistry([Provider()])
    def review(value, **kwargs):
        state.reviews.append(get_ident())
        return review_voice_preference(state.state_root, borrowed, value,
                                       registry_factory=factory, **kwargs)
    def guard():
        if not state.authorized:
            raise ValueError('The active library changed.')
    def execute(work, done, *, on_error):
        state.cancelled = False  # The existing reader run() admits a new request.
        pending.append((work, done, on_error))
        return True
    def finish():
        work, done, failed = pending.pop(0)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(work)
            try:
                result = future.result(timeout=5)
            except Exception as exc:
                failed(exc)
            else:
                done(result)
    def show():
        preferences = deepcopy(state.initial) if state.initial is not None else load_voice_preferences(state.state_root)
        return show_voice_preferences(object(), state_root=state.state_root, voices=voices,
            preferences=preferences, selected=selected, execute=execute, guard=guard,
            apply_selection=applied.append, review_selection=review,
            cancelled=lambda: state.cancelled)
    def find(name):
        found = [item for item in widgets if not isinstance(item, StaticText) and item.name == name]
        assert len(found) == 1, name
        return found[0]
    state.show, state.finish, state.find = show, finish, find
    state.press = lambda name: find(name).handlers['button'](None)
    def choose(name, index):
        item = find(name)
        item.SetSelection(index)
        if 'choice' in item.handlers:
            item.handlers['choice'](None)
    state.choose = choose
    yield state
    borrowed.close()
    assert not pending, 'The test must settle every admitted preferences operation.'


def seed(ui):
    initial = load_voice_preferences(ui.state_root)
    value = {'favorites': ['uk:two', 'removed:voice'], 'defaults': {'uk': 'uk:two', 'en-US': 'removed:voice'},
             'profiles': {'Нічне читання': {'voice_id': 'uk:two', 'language': 'uk', 'rate': -3, 'volume': 45}}}
    save_voice_preferences(ui.state_root, value, expected=initial)
    return value


def test_open_close_does_not_discover_speak_or_write_preferences(ui):
    ui.show()
    assert not ui.applied and not ui.pending and not settings_path(ui.state_root).exists()


def test_favorite_captures_selected_id_before_background_save(ui):
    def drive(_window):
        ui.press('Add or remove favorite')
        assert len(ui.pending) == 1 and not settings_path(ui.state_root).exists()
        ui.choose('Installed and saved voices', 1)
        ui.finish()
        assert load_voice_preferences(ui.state_root)['favorites'] == ['uk:one']
        assert ui.find('Installed and saved voices').items[0].startswith('Favorite; ')
    ui.driver = drive
    ui.show()
    assert not ui.applied


def test_default_requires_exact_current_voice_language(ui):
    def drive(_window):
        ui.choose('Exact voice language', 0)  # Slovak; current voice is Ukrainian.
        assert ui.find('Exact voice language').items[0] == 'sk'
        ui.press('Save default for language')
        assert not ui.pending and 'no language fallback' in ui.find('Voice preference status').GetValue()
        ui.choose('Installed and saved voices', 1)
        ui.press('Save default for language')
        ui.finish()
        assert load_voice_preferences(ui.state_root)['defaults'] == {'sk': 'sk:one'}
    ui.driver = drive
    ui.show()


def test_saved_default_changes_only_selection_and_keeps_reviewed_controls(ui):
    seed(ui)
    before = settings_path(ui.state_root).read_bytes()
    def drive(_window):
        ui.press('Use language default')
        assert not ui.applied
        ui.finish()
    ui.driver = drive
    ui.show()
    assert ui.applied == [{'voice_id': 'uk:two', 'language': 'uk', 'rate': -1, 'volume': 70}]
    assert settings_path(ui.state_root).read_bytes() == before and not ui.pending


def test_saved_profile_displays_and_applies_its_exact_controls(ui):
    saved = seed(ui)
    def drive(_window):
        ui.choose('Saved voice profiles', 0)
        assert ui.find('Voice profile name').GetValue() == 'Нічне читання'
        assert ui.find('Profile volume, zero to one hundred').GetValue() == 45
        ui.press('Use voice profile')
        assert not ui.applied
        ui.finish()
    ui.driver = drive
    ui.show()
    assert ui.applied == [saved['profiles']['Нічне читання']]


def test_missing_default_stays_visible_and_never_selects_substitute(ui):
    seed(ui)
    def drive(_window):
        languages = ui.find('Exact voice language').items
        ui.choose('Exact voice language', languages.index('en-US'))
        ui.press('Use language default')
        assert not ui.applied and 'no substitute' in ui.find('Voice preference status').GetValue()
        choices = ui.find('Installed and saved voices')
        index = next(i for i, label in enumerate(choices.items) if 'Unavailable saved voice: removed:voice' in label)
        ui.choose('Installed and saved voices', index)
        ui.press('Add or remove favorite')
        ui.finish()
        assert 'removed:voice' not in load_voice_preferences(ui.state_root)['favorites']
        assert load_voice_preferences(ui.state_root)['defaults']['en-US'] == 'removed:voice'
    ui.driver = drive
    ui.show()


@pytest.mark.parametrize('boundary', ['close', 'library'])
def test_pending_write_rechecks_owner_before_touching_settings(ui, boundary):
    def drive(_window):
        ui.press('Add or remove favorite')
        if boundary == 'library':
            ui.authorized = False
            ui.finish()
            assert 'library changed' in ui.find('Voice preference status').GetValue()
    ui.driver = drive
    ui.show()
    if boundary == 'close':
        for widget in ui.widgets:
            widget.destroyed = True
        ui.finish()
    assert not settings_path(ui.state_root).exists() and not ui.applied


def test_stale_window_preserves_other_settings_and_can_explicitly_reload(ui):
    def drive(_window):
        ui.press('Add or remove favorite')
        seed(ui)
        before = settings_path(ui.state_root).read_bytes()
        ui.finish()
        assert settings_path(ui.state_root).read_bytes() == before
        assert 'another window' in ui.find('Voice preference status').GetValue()
        ui.press('Reload saved preferences')
        ui.finish()
        ui.press('Add or remove favorite')
        ui.finish()
        assert load_voice_preferences(ui.state_root)['favorites'] == ['uk:two', 'removed:voice', 'uk:one']
    ui.driver = drive
    ui.show()


def test_profile_replacement_requires_explicit_confirmation(ui):
    seed(ui)
    before = settings_path(ui.state_root).read_bytes()
    def drive(_window):
        ui.choose('Saved voice profiles', 0)
        ui.find('Profile volume, zero to one hundred').SetValue(60)
        ui.press('Save voice profile')
        assert not ui.pending and settings_path(ui.state_root).read_bytes() == before
        ui.confirm = True
        ui.press('Save voice profile')
        ui.find('Profile volume, zero to one hundred').SetValue(90)
        ui.finish()
        assert load_voice_preferences(ui.state_root)['profiles']['Нічне читання']['volume'] == 60
    ui.driver = drive
    ui.show()


def test_no_callback_can_apply_a_voice_after_modal_close(ui):
    seed(ui)
    retained = []
    ui.driver = lambda _window: retained.append(ui.find('Use language default').handlers['button'])
    ui.show()
    for widget in ui.widgets:
        widget.destroyed = True
    retained[0](None)
    assert not ui.applied


def test_invalid_profile_controls_fail_before_queueing(ui):
    def drive(_window):
        ui.find('Voice profile name').ChangeValue('Invalid')
        ui.find('Profile synthesis rate, minus ten to ten').SetValue(11)
        ui.press('Save voice profile')
        assert not ui.pending and 'rate from -10' in ui.find('Voice preference status').GetValue()
    ui.driver = drive
    ui.show()


def test_use_rechecks_fresh_inventory_on_the_existing_worker(ui):
    seed(ui)
    owner = get_ident()
    def drive(_window):
        ui.press('Use language default')
        ui.inventory = [voice for voice in ui.inventory if voice.id != 'uk:two']
        ui.finish()
        assert 'no substitute' in ui.find('Voice preference status').GetValue()
        assert not ui.applied
    ui.driver = drive
    ui.show()
    assert ui.factory_calls == 1 and len(ui.reviews) == 1
    assert ui.reviews[0] != owner


@pytest.mark.parametrize('action', ['Save default for language', 'Save voice profile'])
def test_saved_choices_require_a_fresh_available_voice(ui, action):
    def drive(_window):
        ui.find('Voice profile name').ChangeValue('Current voice')
        ui.press(action)
        assert len(ui.pending) == 1
        ui.inventory = [voice for voice in ui.inventory if voice.id != 'uk:one']
        ui.finish()
        assert 'no substitute' in ui.find('Voice preference status').GetValue()
        assert not settings_path(ui.state_root).exists()
    ui.driver = drive
    ui.show()
    assert ui.factory_calls == 1 and not ui.applied


@pytest.mark.parametrize('boundary', ['close', 'destroy', 'scope', 'cancel'])
def test_successful_review_cannot_apply_after_owner_revocation(ui, boundary):
    seed(ui)
    deliveries = []
    before = settings_path(ui.state_root).read_bytes()
    def drive(window):
        ui.press('Use language default')
        work, done, failed = ui.pending.pop(0)
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(work).result(timeout=5)
        if boundary == 'scope':
            ui.authorized = False
        elif boundary == 'cancel':
            ui.cancelled = True
        elif boundary == 'destroy':
            window.Destroy()
        if boundary in ('scope', 'cancel'):
            done(result)
        else:
            deliveries.append(lambda: done(result))
    ui.driver = drive
    ui.show()
    for deliver in deliveries:
        deliver()
    assert not ui.applied and ui.factory_calls == 1
    assert settings_path(ui.state_root).read_bytes() == before


def test_profile_use_captures_native_values_before_worker_entry(ui):
    seed(ui)
    def drive(_window):
        ui.press('Use language default')
        ui.find('Profile synthesis rate, minus ten to ten').SetValue(8)
        ui.find('Profile volume, zero to one hundred').SetValue(2)
        ui.choose('Installed and saved voices', 1)
        ui.finish()
    ui.driver = drive
    ui.show()
    assert ui.applied == [{'voice_id': 'uk:two', 'language': 'uk', 'rate': -1, 'volume': 70}]

def test_late_cancel_keeps_committed_save_and_the_next_expected_snapshot(ui, monkeypatch):
    from carr_library import runtime_settings
    publish = runtime_settings.atomic_json
    def committed(*args, **kwargs):
        result = publish(*args, **kwargs)
        ui.cancelled = True
        return result
    monkeypatch.setattr(runtime_settings, 'atomic_json', committed)
    def drive(_window):
        ui.press('Add or remove favorite')
        ui.finish()
        assert load_voice_preferences(ui.state_root)['favorites'] == ['uk:one']
        assert 'preferences saved' in ui.find('Voice preference status').GetValue()
        # A second explicit edit must use the committed snapshot, not an old CAS.
        ui.press('Add or remove favorite')
        ui.finish()
        assert load_voice_preferences(ui.state_root)['favorites'] == []
        assert 'preferences saved' in ui.find('Voice preference status').GetValue()
    ui.driver = drive
    ui.show()
