"""Execute the actual narration closure with UTF-16 positions and retained originals."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.reader.narration import NarrationStore, encode_project, new_project
from carr_library.reader.spellcheck import review_spelling
from carr_library.ui import narration_editor
from carr_library.ui import spellcheck_dialog as spelling
from carr_library.ui.live_reading import native_offset, selected_source_offsets
from ._fixtures import dictionary


def editor_hook(harness):
    module = ast.parse(Path(narration_editor.__file__).read_text(encoding='utf-8'))
    outer = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                 and node.name == 'open_narration_editor')
    hook = next(node for node in outer.body if isinstance(node, ast.FunctionDef)
                and node.name == 'review_spelling_copy')
    template = ast.parse('''
def factory(harness):
    controller, store, editor = harness.controller, harness.store, harness.editor
    dialog, preview, timer = harness.dialog, harness.preview, harness.timer
    state = harness.flags
    spelling_factory = harness.factory
    def capture():
        if state['text_dirty']:
            controller.set_text(editor.GetValue())
            state['text_dirty'] = False
    def refresh(): editor.ChangeValue(controller.pending_text)
    def say(value): harness.messages.append(value)
    def placeholder(): pass
    return review_spelling_copy
''')
    nodes = template.body[0].body
    index = next(i for i, node in enumerate(nodes)
                 if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    nodes[index] = hook
    namespace = dict(narration_editor.__dict__)
    exec(compile(ast.fix_missing_locations(template), narration_editor.__file__, 'exec'), namespace)
    return namespace['factory'](harness)


class NativeText:
    def __init__(self, source, normalize):
        self.normalize, self.selection = normalize, (0, 0)
        self.ChangeValue(source)

    def ChangeValue(self, text):
        self.text = text.replace('\r\n', '\n') if self.normalize else text
        self.selection = (0, 0)

    def GetValue(self): return self.text
    def GetLastPosition(self): return len(self.text.encode('utf-16-le')) // 2
    def GetRange(self, start, end):
        return self.text.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')
    def GetSelection(self): return self.selection
    def SetSelection(self, start, end): self.selection = (start, end)
    def SetInsertionPoint(self, value): self.selection = (value, value)


@pytest.fixture
def reader(tmp_path, request):
    root = tmp_path / 'library'
    root.mkdir()
    selected = dictionary(tmp_path)
    source = '😀 retained\r\nhello wurld world'
    store = NarrationStore(root)
    controller = narration_editor.NarrationController(store, new_project(source, title='Reading fixture'))
    editor = NativeText(source, getattr(request, 'param', False))
    first, last = source.index('wurld'), source.index('wurld') + 5
    editor.SetSelection(native_offset(editor, source, first), native_offset(editor, source, last))
    frame = SimpleNamespace(library=SimpleNamespace(root=root), run_job=lambda *a, **k: None)
    dialog = SimpleNamespace(GetParent=lambda: frame)
    stops, starts = [], []
    return SimpleNamespace(root=root, selected=selected, source=source, bounds=(first, last),
        store=store, controller=controller, editor=editor, dialog=dialog, frame=frame,
        preview=SimpleNamespace(busy=False), flags={'closed': False, 'text_dirty': False},
        timer=SimpleNamespace(Stop=lambda: stops.append(True), StartOnce=lambda n: starts.append(n)),
        stops=stops, starts=starts, messages=[], factory=None)


@pytest.mark.parametrize('reader', [False, True], indirect=True, ids=['crlf-control', 'lf-control'])
def test_actual_hook_applies_one_selected_correction_and_undo_through_existing_store(reader):
    original = encode_project(reader.controller.project)
    accepted = []
    def launch(parent, **callbacks):
        assert parent is reader.dialog and callbacks['frame'] is reader.frame
        snapshot = callbacks['capture']()
        assert snapshot.text == reader.source and snapshot.selection == reader.bounds
        start, end = snapshot.selection
        review = review_spelling(snapshot.text[start:end], reader.selected, library_root=reader.root)
        proposal = spelling.preview_spelling(snapshot, review, (start, end), 0, 'world')
        applied, warning = callbacks['apply'](proposal)
        assert 'Recovery draft' in warning
        assert reader.controller.pending_text == reader.source[:start] + 'world' + reader.source[end:]
        assert reader.controller.project.original_text == reader.source
        assert len(reader.controller.project.revisions) == 2
        assert selected_source_offsets(reader.editor, reader.controller.pending_text) == (start, start + 5)
        saved, _ = reader.store.load(reader.controller.project.id, draft=True)
        assert saved.current_text == reader.controller.pending_text
        callbacks['undo'](applied)
        assert reader.controller.pending_text == reader.source
        assert reader.controller.project.original_text == reader.source
        accepted.append(True)
    reader.factory = launch
    editor_hook(reader)()
    assert accepted == [True] and not reader.messages
    assert reader.stops == [True] and reader.starts == [1200]
    assert encode_project(reader.controller.project) != original  # Undo keeps checked history.


def test_hook_retains_changed_copy_when_recovery_storage_fails(reader, monkeypatch):
    def launch(parent, **callbacks):
        snapshot = callbacks['capture']()
        start, end = snapshot.selection
        review = review_spelling(snapshot.text[start:end], reader.selected, library_root=reader.root)
        proposal = spelling.preview_spelling(snapshot, review, (start, end), 0, 'world')
        monkeypatch.setattr(reader.store, 'save',
            lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic disk full')))
        applied, warning = callbacks['apply'](proposal)
        assert applied.text == reader.controller.pending_text
        assert 'remains in this open reading copy' in warning and 'disk full' in warning
        assert reader.controller.project.original_text == reader.source
    reader.factory = launch
    editor_hook(reader)()
    assert not reader.messages and reader.controller.pending_text != reader.source


@pytest.mark.parametrize('failure', ['busy', 'closed', 'wrong-root', 'source-revoked'])
def test_hook_cannot_open_for_an_unavailable_reading_source(reader, failure):
    if failure == 'busy':
        reader.preview.busy = True
    elif failure == 'closed':
        reader.flags['closed'] = True
    elif failure == 'wrong-root':
        reader.frame.library.root = reader.root.parent
    else:
        reader.controller.guard = lambda: (_ for _ in ()).throw(ValueError('Source revoked'))
    reader.factory = lambda *a, **k: pytest.fail('Unavailable source opened spelling review')
    editor_hook(reader)()
    assert reader.controller.pending_text == reader.source
    if failure in ('wrong-root', 'source-revoked'):
        assert reader.messages and reader.starts == [1200]


def test_hook_revalidates_after_preview_and_does_not_reset_unrelated_revision(reader):
    def launch(parent, **callbacks):
        snapshot = callbacks['capture']()
        start, end = snapshot.selection
        review = review_spelling(snapshot.text[start:end], reader.selected, library_root=reader.root)
        proposal = spelling.preview_spelling(snapshot, review, (start, end), 0, 'world')
        reader.controller.apply('Independent accepted revision', label='Other explicit edit')
        reader.editor.ChangeValue(reader.controller.pending_text)
        with pytest.raises(ValueError, match='changed'):
            callbacks['apply'](proposal)
    reader.factory = launch
    editor_hook(reader)()
    assert not reader.messages
    assert reader.controller.pending_text == 'Independent accepted revision'
    assert reader.controller.project.original_text == reader.source


def test_nested_reader_finds_only_existing_executor_with_exact_library_root(reader):
    nested = SimpleNamespace(GetParent=lambda: reader.dialog)
    assert spelling.spelling_frame(nested, reader.root) is reader.frame
    nested.GetParent = lambda: nested
    with pytest.raises(ValueError, match='current library'):
        spelling.spelling_frame(nested, reader.root)
