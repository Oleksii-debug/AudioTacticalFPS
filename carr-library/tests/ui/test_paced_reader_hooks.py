"""Run current reader hooks with exact UTF-16 positions and source guards."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.reader.narration import NarrationStore, encode_project, new_project
from carr_library.ui import narration_editor, paced_reading
from carr_library.ui.live_reading import LiveReadingDialog, native_offset, selected_source_offsets
from tests.spelling.test_reader_hook import NativeText
from tests.ui.test_live_reading import window


def hook(harness):
    module = ast.parse(Path(narration_editor.__file__).read_text(encoding='utf-8'))
    outer = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                 and node.name == 'open_narration_editor')
    actual = next(node for node in outer.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'pace_reading_copy')
    template = ast.parse('''
def factory(harness):
    controller, store, editor = harness.controller, harness.store, harness.editor
    dialog, preview, timer = harness.dialog, harness.preview, harness.timer
    state, pacing_factory = harness.flags, harness.factory
    def capture():
        if state['text_dirty']:
            controller.set_text(editor.GetValue())
            state['text_dirty'] = False
    def say(value, **kwargs): harness.messages.append(value)
    def placeholder(): pass
    return pace_reading_copy
''')
    body = template.body[0].body
    index = next(index for index, node in enumerate(body)
                 if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    body[index] = actual
    namespace = dict(narration_editor.__dict__)
    exec(compile(ast.fix_missing_locations(template), narration_editor.__file__, 'exec'), namespace)
    return namespace['factory'](harness)


@pytest.mark.parametrize('normalize', [False, True], ids=['crlf-native', 'lf-native'])
@pytest.mark.parametrize('fail', [False, True], ids=['normal-close', 'failed-construction'])
def test_narration_hook_preserves_exact_text_history_and_selection(tmp_path, normalize, fail):
    source = 'Original 😀\r\nOne two e\u0301 最後。'
    store = NarrationStore(tmp_path)
    controller = narration_editor.NarrationController(store, new_project(source))
    original = encode_project(controller.project)
    editor = NativeText(source, normalize)
    first, last = source.index('One'), len(source)
    editor.SetSelection(native_offset(editor, source, first), native_offset(editor, source, last))
    selected = editor.GetSelection()
    stops, starts = [], []
    def factory(parent, root, text, selection, **callbacks):
        assert root == store.root and text == source and selection == (first, last)
        if fail:
            raise RuntimeError('Synthetic native view construction failure.')
        assert callbacks['current_text']() == source
        callbacks['highlight'](source.index('two'), source.index('two') + 3)
        assert selected_source_offsets(editor, source) == (source.index('two'), source.index('two') + 3)
        callbacks['restore_selection']()
    harness = SimpleNamespace(controller=controller, store=store, editor=editor,
        dialog=object(), preview=SimpleNamespace(busy=False), flags={'closed': False, 'text_dirty': False},
        timer=SimpleNamespace(Stop=lambda: stops.append(True), StartOnce=starts.append),
        messages=[], factory=factory)
    hook(harness)()
    assert stops == [True] and starts == [1200]
    assert editor.GetSelection() == selected
    assert controller.pending_text == source and encode_project(controller.project) == original
    assert bool(harness.messages) is fail
    assert not (tmp_path / 'narration-projects').exists()


def test_live_pacing_refuses_an_active_speech_owner_before_touching_text(window, monkeypatch):
    live, _, _, _, messages, _, _ = window
    def forbidden(*args, **kwargs):
        raise AssertionError('A pacing modal opened during speech.')
    monkeypatch.setattr(paced_reading, 'show_paced_reader', forbidden)
    live.show_pacing()
    assert messages[-1].startswith('Stop the current speech operation')


def test_live_idle_hook_uses_current_source_and_restores_only_its_exact_selection(window, monkeypatch):
    live, _, _, _, messages, selected, _ = window
    live.session.close()
    live.session = None
    live.text = NativeText(live.source, True)
    live.text.SetSelection(1, 5)
    calls = []
    def show(parent, root, text, selection, **callbacks):
        assert parent is live.dialog and root == live.root and text == live.source
        assert callbacks['current_text']() == text
        callbacks['highlight'](0, 5)
        assert selected[-1] == (0, 5)
        callbacks['restore_selection']()
        assert live.text.GetSelection() == (1, 5)
        calls.append(True)
    monkeypatch.setattr(paced_reading, 'show_paced_reader', show)
    live.show_pacing()
    assert calls == [True] and live.paced_view is None and not messages
