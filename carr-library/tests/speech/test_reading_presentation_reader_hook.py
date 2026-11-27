"""Execute the installed reader's real appearance closure with bounded UI stand-ins."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.speech.presentation import PresentationStore
from carr_library.speech.reading import ReadingDocument
from carr_library.ui import speech_dialog as reader, reading_presentation as ui


def hook(state):
    tree = ast.parse(Path(reader.__file__).read_text(encoding='utf-8'))
    outer = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    methods = {node.name: node for node in outer.body if isinstance(node, ast.FunctionDef)}
    factory = ast.parse('''
def factory(state):
    parent, dialog, presentation, editor = state.parent, object(), state.presenter, state.editor
    presentation_store = state.store
    library, library_root, library_identity = state.library, state.store.root, state.store.identity
    alive, busy, source_loaded = True, state.busy, state.source_loaded
    def source_guard():
        if not state.source_allowed: raise ValueError('The retained source scope changed.')
    def report(message, **kwargs): state.messages.append(message)
    def placeholder(): pass
    return choose_reading_appearance
''')
    body = factory.body[0].body
    index = next(i for i, node in enumerate(body) if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    body[index:index + 1] = [methods[name] for name in (
        'check_current', 'reader_tools_notice', 'choose_reading_appearance')]
    namespace = dict(reader.__dict__)
    exec(compile(ast.fix_missing_locations(factory), reader.__file__, 'exec'), namespace)
    return namespace['factory'](state)


@pytest.mark.parametrize('change', ['apply', 'cancel', 'library', 'root', 'root_identity', 'close', 'source', 'busy', 'loading'])
def test_real_reader_hook_retains_library_source_and_focus_authority(tmp_path, monkeypatch, change):
    root = tmp_path / 'library'; root.mkdir()
    library = SimpleNamespace(root=root)
    parent = SimpleNamespace(library=library, _closing=False)
    document = ReadingDocument('😀 Original\r\nNext.'); document.set_text('😀 Edited\r\nNext.')
    before = document.to_dict()
    state = SimpleNamespace(parent=parent, library=library, store=PresentationStore(root),
        presenter=object(), busy=change == 'busy', source_loaded=change != 'loading', source_allowed=True,
        messages=[], focused=[], calls=[])
    state.editor = SimpleNamespace(SetFocus=lambda: state.focused.append(True))
    def show(parent_dialog, **kwargs):
        state.calls.append(kwargs); kwargs['guard']()
        assert kwargs['library_root'] == root and kwargs['presenter'] is state.presenter
        if change == 'library': parent.library = SimpleNamespace(root=root)
        elif change == 'root': library.root = tmp_path
        elif change == 'root_identity': root.rename(tmp_path / 'old'); root.mkdir()
        elif change == 'close': parent._closing = True
        elif change == 'source': state.source_allowed = False
        return change != 'cancel'
    monkeypatch.setattr(ui, 'show_reading_presentation', show)
    hook(state)()
    assert document.to_dict() == before and not (root / 'state').exists()
    assert state.focused == ([True] if change in ('apply', 'cancel') else [])
    if change in ('busy', 'loading'): assert not state.calls
    assert bool(state.messages) == (change in ('apply', 'busy', 'loading'))
