"""Current reader appearance/preferences callbacks retain their exact UI owner."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.runtime_settings import load_voice_preferences, save_voice_preferences
from carr_library.speech.presentation import PresentationStore
from carr_library.ui import reading_desk, reading_presentation, speech_dialog, voice_preferences


@pytest.mark.parametrize('change', ['apply', 'cancel', 'library', 'root', 'root-identity',
                                    'closed', 'busy', 'source'])
def test_current_reading_desk_appearance_keeps_source_and_modal_owner(tmp_path, monkeypatch, change):
    root = tmp_path / 'library'
    root.mkdir()
    library = SimpleNamespace(root=root)
    parent = SimpleNamespace(library=library, _closing=False)
    state = SimpleNamespace(source_allowed=True, calls=[], focus=[], messages=[])
    def source_check():
        if not state.source_allowed:
            raise InterruptedError('The retained source is no longer authorized.')
    view = object.__new__(reading_desk.ReadingDialog)
    view.parent, view.closed, view.busy = parent, False, False
    view._appearance_library = library
    view._presentation_store = PresentationStore(root)
    view.reading_presentation = object()
    view.dialog = object()
    view.controller = SimpleNamespace(check=source_check)
    view.text = SimpleNamespace(SetFocus=lambda: state.focus.append('text'))
    view.status = SimpleNamespace(ChangeValue=lambda value: state.messages.append(value),
                                  SetFocus=lambda: state.focus.append('status'))
    def show(_parent, **kwargs):
        state.calls.append(kwargs)
        kwargs['guard']()
        assert kwargs['presenter'] is view.reading_presentation
        if change == 'library':
            parent.library = SimpleNamespace(root=root)
        elif change == 'root':
            library.root = tmp_path
        elif change == 'root-identity':
            root.rename(tmp_path / 'former-library')
            root.mkdir()
        elif change == 'closed':
            view.closed = True
        elif change == 'busy':
            view.busy = True
        elif change == 'source':
            state.source_allowed = False
        return change != 'cancel'
    monkeypatch.setattr(reading_presentation, 'show_reading_presentation', show)
    view.appearance()
    assert len(state.calls) == 1
    assert state.focus == (['text'] if change in ('apply', 'cancel') else [])
    assert bool(state.messages) == (change == 'apply')
    assert not (root / 'state').exists()


def preference_callback(state):
    tree = ast.parse(Path(speech_dialog.__file__).read_text(encoding='utf-8'))
    outer = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == 'show_speech_reader')
    methods = {node.name: node for node in outer.body if isinstance(node, ast.FunctionDef)}
    factory = ast.parse("""
def factory(state):
    parent, library = state.parent, state.library
    library_root = state.root
    information = regular(local_path(library_root, exists=True), directory=True)
    library_identity = information.st_dev, information.st_ino
    alive, busy, source_loaded = True, False, True
    voices = ()
    remembered_voice_id = 'unavailable:uk'
    voice_choice = SimpleNamespace(GetSelection=lambda: -1)
    rate = SimpleNamespace(GetValue=lambda: -2)
    volume = SimpleNamespace(GetValue=lambda: 55)
    dialog = object()
    def source_guard(): pass
    def report(message, **kwargs): state.messages.append(str(message))
    def run(work, done, **kwargs):
        state.pending.append((work, done))
        return True
    def placeholder(): pass
    return voice_preferences
""")
    body = factory.body[0].body
    index = next(i for i, node in enumerate(body)
                 if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    body[index:index + 1] = [methods[name] for name in
                           ('check_current', 'reader_tools_notice', 'playback_scope', 'voice_preferences')]
    namespace = dict(speech_dialog.__dict__, SimpleNamespace=SimpleNamespace)
    exec(compile(ast.fix_missing_locations(factory), speech_dialog.__file__, 'exec'), namespace)
    return namespace['factory'](state)


@pytest.mark.parametrize('change', ['unchanged', 'library', 'root-identity'])
def test_missing_voice_preferences_remain_reviewable_but_late_owner_changes_are_refused(
        tmp_path, monkeypatch, change):
    root = tmp_path / 'library'
    root.mkdir()
    library = SimpleNamespace(root=root)
    state = SimpleNamespace(root=root, library=library,
        parent=SimpleNamespace(library=library, _closing=False), pending=[], messages=[], shown=[])
    value = {'favorites': ['unavailable:uk'], 'defaults': {'uk': 'unavailable:uk'}, 'profiles': {}}
    save_voice_preferences(root / 'state', value, expected=load_voice_preferences(root / 'state'))
    before = (root / 'state/audio/engines.json').read_bytes()
    def show(_dialog, **kwargs):
        kwargs['guard']()
        state.shown.append(kwargs)
    monkeypatch.setattr(voice_preferences, 'show_voice_preferences', show)
    preference_callback(state)()
    work, done = state.pending.pop()
    result = work()
    if change == 'library':
        state.parent.library = SimpleNamespace(root=root)
    elif change == 'root-identity':
        root.rename(tmp_path / 'previous-library')
        root.mkdir()
    if change == 'unchanged':
        done(result)
        shown, = state.shown
        assert shown['voices'] == ()
        assert shown['preferences'] == value
        assert shown['selected'] == {
            'voice_id': 'unavailable:uk', 'language': '', 'rate': -2, 'volume': 55}
    else:
        with pytest.raises(InterruptedError):
            done(result)
        assert not state.shown
    retained = tmp_path / 'previous-library' if change == 'root-identity' else root
    assert (retained / 'state/audio/engines.json').read_bytes() == before
    assert not state.messages
