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
    forbidden_access = []
    def native(self):
        if self.destroyed or _fixtures.get_ident() != self.owner:
            self.forbidden_access.append((self.name, 'dead' if self.destroyed else 'wrong-thread'))
        return super().native()
    def SetEscapeId(self, value): self.native(); self.escape = value
    def EndModal(self, result): self.native(); self.result = result
    def IsModal(self): self.native(); return getattr(self, '_modal', False)
    def ShowModal(self): self.native(); return getattr(self, 'result', 0)


VOICES = (Voice('fixture:uk', 'Українська', 'Synthetic identity', 'uk-UA'),
          Voice('fixture:en', 'English', 'Synthetic identity', 'en-US'))


@pytest.fixture
def view(tmp_path):
    Widget.forbidden_access.clear()
    root = tmp_path / 'library'; root.mkdir()
    state = SimpleNamespace(allowed=True)
    def guard():
        if not state.allowed: raise ValueError('Reader scope changed')
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL',
             'EXPAND', 'LEFT', 'TOP', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON',
             'EVT_CHOICE', 'EVT_LISTBOX', 'EVT_CLOSE', 'EVT_WINDOW_DESTROY', 'ID_OK', 'ID_CANCEL')
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
    parent, dialog, live = state.parent, object(), state.live
    alive, busy = True, state.busy
    def detach():
        nonlocal alive
        alive = False
        state.live.voice.Destroy()
    state.detach = detach
    def save_recovery(**kwargs): state.saved.append(live.voice.GetSelection())
    def report(message, **kwargs): state.messages.append(message)
    def placeholder(): pass
    return choose_voice_preferences
''')
    body = factory.body[0].body
    body[next(i for i, node in enumerate(body) if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')] = hook
    namespace = dict(reader_ui.__dict__)
    exec(compile(ast.fix_missing_locations(factory), reader_ui.__file__, 'exec'), namespace)
    return namespace['factory'](state)


@pytest.mark.parametrize('change', ['none', 'cancel', 'library', 'root', 'close', 'missing', 'busy',
                                    'playing', 'voices', 'voice-language', 'owner'])
def test_actual_reader_hook_uses_only_current_explicit_voice(view, monkeypatch, change):
    from dataclasses import replace
    choice = Widget(); choice.SetSelection(0)
    parent = SimpleNamespace(library=SimpleNamespace(root=view.root), _closing=False)
    state = SimpleNamespace(parent=parent, busy=change == 'busy', saved=[], refreshed=[], messages=[], calls=[])
    state.live = SimpleNamespace(voice=choice, _voices=VOICES,
        core=SimpleNamespace(busy=change == 'playing'), capabilities=lambda: state.refreshed.append(True))
    def choose(parent_dialog, **kwargs):
        state.calls.append(kwargs)
        assert kwargs['selected_id'] == 'fixture:uk'
        kwargs['guard']()
        if change == 'library': parent.library = SimpleNamespace(root=view.root)
        elif change == 'root': parent.library.root = view.root.parent
        elif change == 'close': parent._closing = True
        elif change == 'voices': state.live._voices = VOICES[:1]
        elif change == 'voice-language': state.live._voices = (replace(VOICES[0], language='en-US'), VOICES[1])
        elif change == 'owner': state.detach()
        return None if change == 'cancel' else 'missing' if change == 'missing' else 'fixture:en'
    monkeypatch.setattr(ui, 'show_voice_preferences', choose)
    reader_hook(state)()
    if change == 'none':
        assert choice.selection == 1 and state.saved == [1] and state.refreshed == [True]
    else:
        assert choice.selection == 0 and not state.saved and not state.refreshed
    if change in ('busy', 'playing'):
        assert not state.calls
    if change not in ('none', 'cancel', 'owner'):
        assert state.messages
    assert not Widget.forbidden_access


@pytest.mark.parametrize('close_path', ['direct', 'parent'])
def test_deleted_native_owner_ignores_every_queued_action_without_native_access_or_write(view, close_path):
    dialog = view.dialog
    select(view); dialog.on_default()
    before = view.store.path.read_bytes()
    owner_id = dialog.GetId()
    skipped = []
    if close_path == 'direct':
        dialog.Destroy()
    else:
        # Native parent deletion bypasses the Python Destroy override and the
        # event can hold a different wrapper. Only the captured ID is authority.
        _fixtures.Widget.Destroy(dialog)
        dialog.on_destroy(SimpleNamespace(GetId=lambda: owner_id,
            GetEventObject=lambda: SimpleNamespace(GetId=lambda: owner_id), Skip=lambda: skipped.append(True)))
    assert not dialog and not dialog.alive and dialog.selected_id is None
    for name in ('on_default', 'on_clear', 'on_favorite', 'on_forget', 'on_reload',
                 'on_use', 'on_close', 'on_language', 'on_voice', 'controls', 'refresh', 'populate'):
        getattr(dialog, name)()
    dialog.report('late queued status')
    assert dialog.Destroy() is False and dialog.Destroy() is False
    assert not Widget.forbidden_access
    assert view.store.path.read_bytes() == before
    if close_path == 'parent':
        assert skipped == [True]


def test_child_destroy_event_does_not_detach_owner(view):
    dialog = view.dialog
    skipped = []
    child_id = dialog.buttons['close'].GetId()
    dialog.on_destroy(SimpleNamespace(GetId=lambda: child_id, Skip=lambda: skipped.append(True)))
    assert skipped == [True] and dialog.alive
    select(view); dialog.on_default()
    assert dict(view.store.load()[0].defaults) == {'uk-ua': 'fixture:uk'}
    assert not Widget.forbidden_access


def test_guard_that_destroys_owner_cannot_resume_native_control_access_or_write(view):
    dialog = view.dialog
    select(view)
    # The real constructor guard consults state.allowed. Its truth check
    # triggers nested owner deletion without replacing any product method.
    class DestroyDuringGuard:
        def __bool__(self):
            dialog.Destroy()
            return True
    view.state.allowed = DestroyDuringGuard()
    dialog.on_use()
    assert not dialog and not dialog.alive and dialog.selected_id is None
    assert not view.store.path.exists() and not Widget.forbidden_access


def test_modal_parent_destruction_returns_no_selection_and_never_destroys_twice(view, monkeypatch):
    dialog = view.dialog
    owner_id = dialog.GetId()
    dialog.selected_id = 'fixture:uk'
    def deleted_modal(self):
        _fixtures.Widget.Destroy(self)
        self.on_destroy(SimpleNamespace(GetId=lambda: owner_id, Skip=lambda: None))
        return view.wx.ID_OK
    monkeypatch.setattr(Widget, 'ShowModal', deleted_modal)
    monkeypatch.setattr(ui, 'create_voice_preferences', lambda *args, **kwargs: dialog)
    assert ui.show_voice_preferences(None) is None
    assert not Widget.forbidden_access and dialog.selected_id is None


def test_live_modal_use_returns_only_explicit_selection_and_closes_once(view, monkeypatch):
    dialog = view.dialog
    select(view)
    def selected_modal(self):
        self.on_use()
        return view.wx.ID_OK
    monkeypatch.setattr(Widget, 'ShowModal', selected_modal)
    monkeypatch.setattr(ui, 'create_voice_preferences', lambda *args, **kwargs: dialog)
    assert ui.show_voice_preferences(None) == 'fixture:uk'
    assert not dialog and not dialog.alive and not Widget.forbidden_access


def test_unconfirmed_save_notice_replaces_the_ordinary_saved_caption(view, monkeypatch):
    from carr_library import durable_state
    select(view)
    def interrupted_sync(*args, **kwargs):
        raise OSError('directory durability was not confirmed after publication')
    monkeypatch.setattr(durable_state, 'sync_directory', interrupted_sync)
    view.dialog.on_default()
    assert dict(view.store.load()[0].defaults) == {'uk-ua': 'fixture:uk'}
    assert 'present and checked' in view.dialog.status.value
    assert 'completion could not be confirmed' in view.dialog.status.value
    assert not view.dialog.status.value.startswith('Saved ')
