"""Actual durable draft saves behind explicit control doubles, without native claims."""
import ast
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.audio.voices import Voice
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.studio.project import ProjectStore as SceneProjectStore
from carr_library.ui import audio_studio, save_actions, speech_dialog, studio_dialog
from carr_library.ui.save_actions import cancel_save_all, install_save_coordinator, save_all_open
from carr_library.ui.save_all import SaveCoordinator, SaveResult


class Text:
    def __init__(self, value):
        self.value = value
    def GetValue(self):
        return self.value
    def ChangeValue(self, value):
        self.value = value
    SetValue = ChangeValue
    def GetInsertionPoint(self):
        return len(self.value)
    def GetStringSelection(self):
        return self.value
    def SetFocus(self):
        pass


def owner():
    frame = SimpleNamespace(library=SimpleNamespace(root=Path('library')), active=('workspace', 'topic'),
        evidence=SimpleNamespace(), _closing=False, working=False, busy=False, cancel_event=Event(),
        report=Mock(), _dirty=lambda: False, _restore_note_focus=Mock())
    install_save_coordinator(frame)
    return frame


def test_shared_request_awaits_callback_and_does_not_reset_a_live_request():
    frame = owner()
    pending = []
    dirty = [True]
    frame._save_coordinator.register('editor', 'Open editor', dirty=lambda: dirty[0], save=pending.append)
    modal_report = Mock()
    modal = SimpleNamespace(GetParent=lambda: frame)
    assert save_all_open(modal, report=modal_report)
    assert frame._save_coordinator.busy
    assert not frame.report.called and not modal_report.called
    assert not save_all_open(modal, report=modal_report)
    dirty[0] = False
    pending[0](SaveResult(True))
    assert not frame._save_coordinator.busy
    assert modal_report.call_args.args[0] == 'All 1 open drafts saved.'
    assert install_save_coordinator(frame) is frame._save_coordinator
    assert len(frame._save_coordinator.participants) == 2


@pytest.mark.parametrize('boundary', ['cancel', 'library', 'scope', 'panel', 'close'])
def test_request_boundary_stops_next_participant_after_a_committed_save(boundary):
    frame = owner()
    dirty = [True]
    pending, later = [], Mock()
    frame._save_coordinator.register('first', 'First document', dirty=lambda: dirty[0], save=pending.append)
    frame._save_coordinator.register('next', 'Next document', dirty=lambda: True, save=later)
    reports = []
    assert save_all_open(frame, report=reports.append)
    if boundary == 'cancel':
        frame.cancel_event.set()
    elif boundary == 'library':
        frame.library = SimpleNamespace(root=Path('different-library'))
    elif boundary == 'scope':
        frame.active = ('other-workspace', 'other-topic')
    elif boundary == 'panel':
        frame.evidence = SimpleNamespace()
    else:
        frame._closing = True
    dirty[0] = False
    pending[0](SaveResult(True))
    later.assert_not_called()
    assert not frame._save_coordinator.busy
    assert '1 drafts saved.' in reports[-1] and 'cancelled' in reports[-1]
    assert 'Next document' in reports[-1]


def test_unrelated_active_work_is_not_cancelled_or_replaced_by_save_all():
    frame = owner()
    frame.working = True
    frame.cancel_event.set()
    assert not save_all_open(frame)
    assert frame.cancel_event.is_set()
    assert not frame._save_coordinator.busy


def callbacks(module, outer_name, names, factory, environment):
    """Execute actual installed callback source, keeping its lexical bindings."""
    source = Path(module.__file__).resolve()
    tree = ast.parse(source.read_text(encoding='utf-8'))
    outer = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == outer_name)
    found = {node.name: node for node in outer.body if isinstance(node, ast.FunctionDef)}
    wrapper = ast.parse(factory)
    function = wrapper.body[0]
    insertion = next(i for i, node in enumerate(function.body)
                     if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                     and getattr(node.value.func, 'id', None) == 'INSERT')
    function.body[insertion:insertion + 1] = [found[name] for name in names]
    environment.setdefault('__name__', module.__name__)
    environment.setdefault('__package__', module.__package__)
    exec(compile(ast.fix_missing_locations(wrapper), str(source), 'exec'), environment)
    return environment['make']


def reader_callbacks(tmp_path, text='Changed reading copy', *, blocked=False):
    document = ReadingDocument('Immutable original source.')
    editor = Text(text)
    draft = ReaderDraftStore(tmp_path / 'library', slot='save-all-test')
    guard = Mock(side_effect=ValueError('Source authorization revoked') if blocked else None)
    env = dict(Path=Path, ReaderResume=ReaderResume, SaveResult=SaveResult,
               SimpleNamespace=SimpleNamespace)
    make = callbacks(speech_dialog, 'show_speech_reader',
        ('sync_document', 'save_recovery', 'save_project', 'unsaved', 'save_registered'),
        '''
def make(document, editor, draft_store, source_guard):
    alive, busy, source_loaded = True, False, True
    save_path = last_audio = last_recovery_signature = None
    recovery_notice = ''
    dialog = None
    source_status = Text('')
    voice_choice = SimpleNamespace(GetSelection=lambda: -1)
    voices = ()
    rate, volume = Text(0), Text(80)
    player = SimpleNamespace(position_seconds=0)
    report = lambda *args, **kwargs: None
    playback_guard = source_guard
    INSERT()
    return SimpleNamespace(save=save_registered, dirty=unsaved)
''', dict(env, Text=Text))
    return make(document, editor, draft, guard), document, editor, draft, guard


@pytest.mark.parametrize('text', ['Changed reading copy', ''])
def test_reader_participant_commits_real_recovery_including_empty_edit(tmp_path, text):
    reader, document, _, draft, guard = reader_callbacks(tmp_path, text)
    coordinator = SaveCoordinator()
    coordinator.register('reader', 'Voice reading copy', dirty=reader.dirty, save=reader.save)
    summaries = []
    coordinator.save_all(summaries.append)
    assert summaries[0].complete and summaries[0].saved == ('Voice reading copy',)
    reopened = ReaderDraftStore(tmp_path / 'library', slot='save-all-test').load()
    assert reopened.document.text == text
    assert reopened.document.source_text == document.source_text == 'Immutable original source.'
    assert not reader.dirty()
    assert guard.call_count >= 2


def test_reader_source_revocation_keeps_draft_uncommitted(tmp_path):
    reader, _, editor, draft, _ = reader_callbacks(tmp_path, blocked=True)
    results = []
    reader.save(results.append)
    assert results == [SaveResult(False, 'Source authorization revoked')]
    assert reader.dirty() and editor.GetValue() == 'Changed reading copy'
    assert not draft.path.exists()


def test_failed_reader_commit_retains_earlier_file_and_dirty_text(tmp_path, monkeypatch):
    reader, document, editor, draft, _ = reader_callbacks(tmp_path)
    first = []
    reader.save(first.append)
    before = draft.path.read_bytes()
    editor.SetValue('Later unsaved text')
    def fail(*args, **kwargs):
        raise OSError('Synthetic read-only recovery folder')
    monkeypatch.setattr('carr_library.speech.session.atomic_json', fail)
    results = []
    reader.save(results.append)
    assert not results[0].saved and reader.dirty()
    assert editor.GetValue() == document.text == 'Later unsaved text'
    assert draft.path.read_bytes() == before


def scene_callbacks(tmp_path):
    library = SimpleNamespace(root=tmp_path / 'library')
    service = SimpleNamespace(projects=SceneProjectStore(library.root / 'state' / 'studio'))
    controller = studio_dialog.StudioController(library, service=service)
    editor, title = Text('Narrator: Scene draft to commit.'), Text('Scene project')
    frame = SimpleNamespace(library=library)
    env = dict(SimpleNamespace=SimpleNamespace, SaveResult=SaveResult)
    make = callbacks(studio_dialog, 'show_studio',
        ('editor_signature', 'save', 'unsaved', 'save_registered'),
        '''
def make(frame, controller, editor, title):
    alive, busy, draft_dirty = True, False, True
    saved_signature = None
    save_library, save_root = frame.library, frame.library.root
    selected_voices = lambda: ('fixture:voice',)
    report = lambda *args, **kwargs: None
    INSERT()
    return SimpleNamespace(save=save_registered, dirty=unsaved)
''', env)
    return make(frame, controller, editor, title), controller, editor, frame


def test_scene_save_all_waits_for_real_project_and_keeps_newer_editor_text(tmp_path):
    editor_part, controller, editor, _ = scene_callbacks(tmp_path)
    actual = controller.save
    def write_then_edit(*args):
        project = actual(*args)
        editor.SetValue('Narrator: A newer unsaved scene.')
        return project
    controller.save = write_then_edit
    coordinator = SaveCoordinator()
    coordinator.register('scene', 'Scene project', dirty=editor_part.dirty, save=editor_part.save)
    summaries = []
    coordinator.save_all(summaries.append)
    assert summaries[0].saved == ('Scene project',) and not summaries[0].complete
    assert controller.service.projects.load(controller.project.id).original_script == 'Narrator: Scene draft to commit.'
    assert editor_part.dirty() and editor.GetValue() == 'Narrator: A newer unsaved scene.'


def test_scene_library_change_blocks_project_write(tmp_path):
    editor, controller, _, frame = scene_callbacks(tmp_path)
    frame.library = SimpleNamespace(root=tmp_path / 'different-library')
    results = []
    editor.save(results.append)
    assert not results[0].saved and editor.dirty()
    assert controller.project is None and not controller.draft.exists()


def audio_editor(tmp_path):
    registry = SimpleNamespace(list_voices=lambda: (
        Voice('flite:kal', 'flite', 'Declared fixture voice; no synthesis', ('en',), (), '', ''),))
    controller = audio_studio.StudioController(tmp_path / 'library', registry)
    view = object.__new__(audio_studio.StudioDialog)
    view.parent = SimpleNamespace(library=SimpleNamespace(root=controller.store.root))
    view._save_library, view._save_root = view.parent.library, view.parent.library.root
    view.controller, view.closed, view.busy, view.dirty = controller, False, False, True
    view.title, view.script, view.language, view.format = (
        Text('Integrated project'), Text('Narrator: Studio draft to commit.'), Text('en'), Text('wav'))
    view._saved_signature = None
    view.refresh, view.say = Mock(), Mock()
    return view


def test_integrated_studio_uses_real_store_and_preserves_newer_editor_text(tmp_path):
    view = audio_editor(tmp_path)
    actual = view.controller.save
    def write_then_edit(*args, **kwargs):
        project = actual(*args, **kwargs)
        view.script.SetValue('Narrator: Newer unsaved narration.')
        return project
    view.controller.save = write_then_edit
    coordinator = SaveCoordinator()
    coordinator.register('studio', 'Audio Studio', dirty=view.unsaved, save=view.save_registered)
    summaries = []
    coordinator.save_all(summaries.append)
    assert summaries[0].saved == ('Audio Studio',) and not summaries[0].complete
    assert view.controller.store.load(view.controller.project.id).script == 'Narrator: Studio draft to commit.'
    assert view.unsaved() and view.dirty
    assert view.script.GetValue() == 'Narrator: Newer unsaved narration.'


def test_integrated_studio_rejects_invalid_script_without_claiming_saved(tmp_path):
    view = audio_editor(tmp_path)
    view.script.SetValue('@unsupported direction')
    results = []
    view.save_registered(results.append)
    assert not results[0].saved and view.unsaved()
    assert view.controller.project is None
    assert view.controller.store.new_drafts()[0]['script'] == '@unsupported direction'


@pytest.mark.parametrize('blocked', ['busy', 'closed', 'library'])
def test_integrated_studio_rejects_unowned_or_busy_save(tmp_path, blocked):
    view = audio_editor(tmp_path)
    if blocked == 'library':
        view.parent.library = SimpleNamespace(root=tmp_path / 'different-library')
    else:
        setattr(view, blocked, True)
    results = []
    view.save_registered(results.append)
    assert not results[0].saved and view.controller.project is None
    assert view.controller.store.new_drafts() == ()


@pytest.mark.parametrize('blocked', [False, True])
def test_legacy_recovery_is_read_only_and_requires_current_matching_evidence(tmp_path, monkeypatch, blocked):
    import sys
    from carr_library.application import LibraryApp
    from carr_library.durable_state import atomic_json
    app = LibraryApp(tmp_path / 'legacy-library')
    workspace = app.create_workspace('Legacy recovery scope')
    topic = app.create_topic(workspace, 'Legacy recovery topic')
    app.add_text(workspace, 'Original', 'Exact original passage.')
    hit, = app.search(workspace, 'Exact')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Committed note')
    row, = app.evidence(workspace, topic)
    path = app.root / 'state' / 'recovery' / 'research-note.json'
    atomic_json(path, dict(schema=1, workspace=workspace, topic=topic,
                          evidence=row.evidence_id, note='Earlier one-slot draft'), replace=False)
    before = path.read_bytes()
    panel = SimpleNamespace(workspace=workspace, topic=topic,
        _authorized_recovery_row=lambda: app.evidence(workspace, topic)[0])
    frame = SimpleNamespace(library=app, evidence=panel, report=Mock())
    show = Mock()
    monkeypatch.setitem(sys.modules, 'carr_library.ui.desktop_app', SimpleNamespace(show_text=show))
    make = callbacks(save_actions, 'install_research_recovery', ('recover_legacy',),
        '''
def make(frame):
    INSERT()
    return recover_legacy
''', dict(read_json=save_actions.read_json))
    if blocked:
        app.archive_workspace(workspace)
    make(frame)()
    assert path.read_bytes() == before
    if blocked:
        show.assert_not_called()
        assert 'Could not recover' in frame.report.call_args.args[0]
    else:
        assert show.call_count == 1, frame.report.call_args
        assert 'Earlier one-slot draft' in show.call_args.args[2]
        assert app.evidence(workspace, topic)[0].note == 'Committed note'


def test_modal_cancel_targets_only_its_active_save_all_request():
    frame = owner()
    modal = SimpleNamespace(GetParent=lambda: frame)
    frame.working = True
    assert not cancel_save_all(modal)
    assert not frame.cancel_event.is_set()
    frame.working = False
    pending = []
    dirty = [True]
    frame._save_coordinator.register('editor', 'Open editor', dirty=lambda: dirty[0], save=pending.append)
    assert save_all_open(modal)
    report = Mock()
    assert cancel_save_all(modal, report=report)
    assert frame.cancel_event.is_set() and 'Completed saves remain saved' in report.call_args.args[0]
    dirty[0] = False
    pending[0](SaveResult(True))
    assert not frame._save_coordinator.busy
