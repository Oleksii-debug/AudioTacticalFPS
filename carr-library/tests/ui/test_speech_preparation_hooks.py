"""Execute the current narration hook with exact CRLF/UTF-16 selections."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.reader.narration import NarrationStore, encode_project, new_project
from carr_library.reader.speech_normalization import SpeechOptions
from carr_library.ui import narration_editor, speech_preparation as ui
from carr_library.ui.live_reading import native_offset, selected_source_offsets
from tests.spelling.test_reader_hook import NativeText


def hook(harness):
    module = ast.parse(Path(narration_editor.__file__).read_text(encoding='utf-8'))
    outer = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                 and node.name == 'open_narration_editor')
    actual = next(node for node in outer.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'prepare_reading_speech')
    template = ast.parse('''
def factory(harness):
    controller, store, editor = harness.controller, harness.store, harness.editor
    dialog, preview, timer = harness.dialog, harness.preview, harness.timer
    state, speech_factory = harness.flags, harness.factory
    def capture():
        if state['text_dirty']:
            controller.set_text(editor.GetValue())
            state['text_dirty'] = False
    def refresh():
        editor.ChangeValue(controller.pending_text)
        state['text_dirty'] = False
    def say(value, **kwargs): harness.messages.append(value)
    def placeholder(): pass
    return prepare_reading_speech
''')
    body = template.body[0].body
    index = next(index for index, node in enumerate(body)
                 if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    body[index] = actual
    namespace = dict(narration_editor.__dict__)
    exec(compile(ast.fix_missing_locations(template), narration_editor.__file__, 'exec'), namespace)
    return namespace['factory'](harness)


@pytest.fixture
def reader(tmp_path, request):
    root = tmp_path / 'library'
    root.mkdir()
    source = 'Original 😀\r\nKeep 21 кг; e\u0301 and 12 outside.'
    store = NarrationStore(root)
    controller = narration_editor.NarrationController(store, new_project(source, title='Reader fixture'))
    editor = NativeText(source, getattr(request, 'param', False))
    start = source.index('21')
    bounds = (start, start + len('21 кг'))
    editor.SetSelection(native_offset(editor, source, bounds[0]), native_offset(editor, source, bounds[1]))
    frame = SimpleNamespace(library=SimpleNamespace(root=root), run_job=lambda *a, **k: None)
    stops, starts = [], []
    return SimpleNamespace(root=root, source=source, bounds=bounds, store=store, controller=controller,
        editor=editor, frame=frame, dialog=SimpleNamespace(GetParent=lambda: frame),
        flags={'closed': False, 'text_dirty': False}, preview=SimpleNamespace(busy=False),
        timer=SimpleNamespace(Stop=lambda: stops.append(True), StartOnce=starts.append),
        stops=stops, starts=starts, factory=None, messages=[])


@pytest.mark.parametrize('reader', [False, True], indirect=True, ids=['crlf-native', 'lf-native'])
def test_actual_hook_preserves_original_and_outside_text_through_apply_and_undo(reader):
    accepted = []
    def opened(parent, **callbacks):
        assert parent is reader.dialog and callbacks['frame'] is reader.frame
        snapshot = callbacks['capture']()
        assert snapshot.text == reader.source and snapshot.selection == reader.bounds
        proposal = ui.prepare_selection(snapshot, snapshot.selection,
            SpeechOptions(mode='speech', locale='uk-UA', numbers=True, units=True))
        assert proposal.result.text == 'двадцять один кілограм'
        applied, warning = callbacks['apply'](proposal)
        expected = reader.source[:reader.bounds[0]] + proposal.result.text + reader.source[reader.bounds[1]:]
        assert reader.controller.pending_text == expected
        assert reader.controller.project.original_text == reader.source
        assert selected_source_offsets(reader.editor, expected) == (reader.bounds[0], reader.bounds[0] + len(proposal.result.text))
        assert 'Recovery draft saved' in warning
        retained, _ = reader.store.load(reader.controller.project.id, draft=True)
        assert retained.current_text == expected
        callbacks['undo'](applied)
        assert reader.controller.pending_text == reader.source and len(reader.controller.project.revisions) == 2
        accepted.append(True)
    reader.factory = opened
    hook(reader)()
    assert accepted == [True] and not reader.messages
    assert reader.stops == [True] and reader.starts == [1200]


def test_hook_reports_draft_storage_failure_without_undoing_accepted_text(reader, monkeypatch):
    accepted = []
    def opened(parent, **callbacks):
        snapshot = callbacks['capture']()
        proposal = ui.prepare_selection(snapshot, snapshot.selection,
            SpeechOptions(mode='speech', locale='uk-UA', numbers=True, units=True))
        monkeypatch.setattr(reader.store, 'save', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
        applied, warning = callbacks['apply'](proposal)
        assert applied.text == reader.controller.pending_text and 'disk full' in warning
        assert reader.controller.pending_text != reader.source
        accepted.append(True)
    reader.factory = opened
    hook(reader)()
    assert accepted == [True] and not reader.messages
    assert reader.controller.project.original_text == reader.source


@pytest.mark.parametrize('change', ['native-text', 'selection', 'revision', 'closed', 'source-revoked'])
def test_hook_rechecks_exact_live_editor_before_apply(reader, change):
    def opened(parent, **callbacks):
        snapshot = callbacks['capture']()
        proposal = ui.prepare_selection(snapshot, snapshot.selection,
            SpeechOptions(mode='speech', locale='uk-UA', numbers=True, units=True))
        if change == 'native-text':
            reader.editor.ChangeValue('New native edit')
            reader.flags['text_dirty'] = True
        elif change == 'selection':
            reader.editor.SetSelection(0, 1)
        elif change == 'revision':
            reader.controller.apply('Independent accepted revision', label='Explicit other edit')
            reader.editor.ChangeValue(reader.controller.pending_text)
        elif change == 'closed':
            reader.flags['closed'] = True
        else:
            reader.controller.guard = lambda: (_ for _ in ()).throw(ValueError('Source revoked'))
        before = encode_project(reader.controller.project)
        with pytest.raises((ValueError, InterruptedError)):
            callbacks['apply'](proposal)
        assert encode_project(reader.controller.project) == before
    reader.factory = opened
    hook(reader)()
    assert not reader.messages
    assert reader.controller.project.original_text == reader.source


@pytest.mark.parametrize('failure', ['busy', 'closed', 'wrong-root', 'guard'])
def test_unavailable_reading_editor_cannot_open_another_preparation(reader, failure):
    if failure == 'busy':
        reader.preview.busy = True
    elif failure == 'closed':
        reader.flags['closed'] = True
    elif failure == 'wrong-root':
        reader.frame.library.root = reader.root.parent
    else:
        reader.controller.guard = lambda: (_ for _ in ()).throw(ValueError('Source revoked'))
    reader.factory = lambda *a, **k: pytest.fail('Unavailable reading owner opened a new preparation')
    hook(reader)()
    assert reader.controller.pending_text == reader.source
    if failure in ('wrong-root', 'guard'):
        assert reader.messages and reader.starts == [1200]


def test_constructor_failure_restores_existing_autosave_timer(reader):
    reader.factory = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('Native construction failed'))
    hook(reader)()
    assert reader.stops == [True] and reader.starts == [1200]
    assert reader.messages == ['Native construction failed']
    assert reader.controller.pending_text == reader.source
