"""Production native handlers with widget stand-ins and actual preference files."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.speech.runtime import Voice
from carr_library.speech.voice_preferences import VoicePreferences, VoicePreferenceStore
from carr_library.ui import voice_preferences as ui, speech_dialog as reader_ui

_spec = importlib.util.spec_from_file_location('carr_voice_preference_widgets',
    Path(__file__).with_name('test_pronunciation_handlers.py'))
_fixtures = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_fixtures)


class Widget(_fixtures.Widget):
    def GetId(self): return id(self)
    def SetEscapeId(self, value): self.native(); self.escape = value
    def EndModal(self, result): self.native(); self.result = result


VOICES = (Voice('fixture:uk', 'Українська', 'Synthetic identity', 'uk-UA'),
          Voice('fixture:en', 'English', 'Synthetic identity', 'en-US'))


@pytest.fixture
def view(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    state = SimpleNamespace(allowed=True)
    def guard():
        if not state.allowed: raise ValueError('Reader scope changed')
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL',
             'EXPAND', 'LEFT', 'TOP', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON',
             'EVT_CHOICE', 'EVT_LISTBOX', 'EVT_CLOSE', 'ID_OK', 'ID_CANCEL')
    wx = SimpleNamespace(**{name: 1 << index for index, name in enumerate(names)})
    for name in ('Dialog', 'StaticText', 'Choice', 'ListBox', 'TextCtrl', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = _fixtures.Sizer
    dialog = ui.create_voice_preferences(None, library_root=root, voices=VOICES, guard=guard, wx_module=wx)
    yield SimpleNamespace(dialog=dialog, state=state, store=VoicePreferenceStore(root), root=root, wx=wx)
    dialog.alive = False
    if not dialog.destroyed: dialog.Destroy()


def select(view, language='uk-ua'):
    dialog = view.dialog
    dialog.language.SetSelection(dialog.languages.index(language) + 1); dialog.on_language()
    assert dialog.voices.GetSelection() == -1
    dialog.voices.SetSelection(0); dialog.on_voice()


def test_default_favorite_reload_and_use_are_separate_explicit_actions(view):
    select(view); dialog = view.dialog
    assert not view.store.path.exists()
    dialog.on_default(); dialog.on_favorite()
    saved, _ = view.store.load()
    assert dict(saved.defaults) == {'uk-ua': 'fixture:uk'} and saved.favorites == ('fixture:uk',)
    assert dialog.selected_id is None
    dialog.on_reload(); dialog.language.SetSelection(dialog.languages.index('uk-ua') + 1); dialog.on_language()
    assert dialog.selected().id == 'fixture:uk' and 'Saved default' in dialog.status.value
    dialog.on_use()
    assert dialog.selected_id == 'fixture:uk' and dialog.result == view.wx.ID_OK


def test_missing_default_is_visible_without_selecting_available_substitute(view):
    saved = VoicePreferences((('uk-ua', 'removed-voice'),), ('removed-voice',))
    view.store.save(saved, expected_token=None)
    view.dialog.on_reload()
    view.dialog.language.SetSelection(view.dialog.languages.index('uk-ua') + 1); view.dialog.on_language()
    assert view.dialog.voices.GetSelection() == -1 and 'no fallback' in view.dialog.status.value
    assert not view.dialog.buttons['use'].enabled and view.dialog.buttons['forget'].enabled
    view.dialog.on_forget()
    assert view.store.load()[0].favorites == ()
    assert dict(view.store.load()[0].defaults) == {'uk-ua': 'removed-voice'}
    view.dialog.on_clear()
    assert view.store.load()[0].defaults == ()


def test_all_languages_and_empty_selection_cannot_save_implicit_default(view):
    dialog = view.dialog
    dialog.voices.SetSelection(0); dialog.on_voice(); dialog.on_default()
    assert 'one declared language' in dialog.status.value and not view.store.path.exists()
    dialog.voices.SetSelection(-1); dialog.on_voice(); dialog.on_use()
    assert dialog.selected_id is None and dialog.alive


@pytest.mark.parametrize('action', ['on_default', 'on_favorite', 'on_use'])
def test_reader_authority_change_prevents_save_and_voice_use(view, action):
    select(view); view.state.allowed = False
    getattr(view.dialog, action)()
    assert 'Reader scope changed' in view.dialog.status.value
    assert not view.store.path.exists() and view.dialog.selected_id is None


def test_other_window_write_requires_explicit_reload(view):
    select(view)
    other = VoicePreferences().with_favorite('fixture:en', True, VOICES)
    view.store.save(other, expected_token=None)
    view.dialog.on_default()
    assert 'Another window' in view.dialog.status.value and view.store.load()[0] == other
    view.dialog.on_reload(); select(view); view.dialog.on_default()
    assert view.store.load()[0].favorites == ('fixture:en',)


def test_native_controls_have_distinct_meaningful_names(view):
    controls = [view.dialog.language, view.dialog.voices, view.dialog.status, *view.dialog.buttons.values()]
    assert all(control.name for control in controls)
    assert len({control.name for control in controls}) == len(controls)
    assert view.dialog.escape == view.dialog.buttons['close'].GetId()


def reader_hook(state):
    tree = ast.parse(Path(reader_ui.__file__).read_text(encoding='utf-8'))
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    hook = next(node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name == 'choose_voice_preferences')
    factory = ast.parse('''
def factory(state):
    parent, dialog, voices, voice_choice = state.parent, object(), state.voices, state.choice
    alive, busy = True, state.busy
    def capabilities(): state.refreshed.append(True)
    def save_recovery(**kwargs): state.saved.append(voice_choice.GetSelection())
    def report(message, **kwargs): state.messages.append(message)
    def placeholder(): pass
    return choose_voice_preferences
''')
    body = factory.body[0].body
    body[next(i for i, node in enumerate(body) if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')] = hook
    namespace = dict(reader_ui.__dict__)
    exec(compile(ast.fix_missing_locations(factory), reader_ui.__file__, 'exec'), namespace)
    return namespace['factory'](state)


@pytest.mark.parametrize('change', ['none', 'cancel', 'library', 'root', 'close', 'missing', 'busy'])
def test_actual_reader_hook_uses_only_current_explicit_voice(view, monkeypatch, change):
    choice = Widget(); choice.SetSelection(0)
    parent = SimpleNamespace(library=SimpleNamespace(root=view.root), _closing=False)
    state = SimpleNamespace(parent=parent, voices=VOICES, choice=choice, busy=change == 'busy',
                            saved=[], refreshed=[], messages=[], calls=[])
    def choose(parent_dialog, **kwargs):
        state.calls.append(kwargs)
        assert kwargs['selected_id'] == 'fixture:uk'
        kwargs['guard']()
        if change == 'library': parent.library = SimpleNamespace(root=view.root)
        elif change == 'root': parent.library.root = view.root.parent
        elif change == 'close': parent._closing = True
        return None if change == 'cancel' else 'missing' if change == 'missing' else 'fixture:en'
    monkeypatch.setattr(ui, 'show_voice_preferences', choose)
    reader_hook(state)()
    if change == 'none':
        assert choice.selection == 1 and state.saved == [1] and state.refreshed == [True]
    else:
        assert choice.selection == 0 and not state.saved and not state.refreshed
    if change == 'busy': assert not state.calls
    if change not in ('none', 'cancel'): assert state.messages
