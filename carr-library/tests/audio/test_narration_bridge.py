"""Real PR98 reading-copy producer → PR99 literal audio contracts.

These tests use real library files and model validation. They do not claim TTS
quality, native keyboard acceptance, or fresh quotation approval for derivatives.
"""
from dataclasses import FrozenInstanceError, asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.models import Line, ProductionPlan
from carr_library.audio.narration_bridge import (load_saved_reading_snapshot,
    saved_reading_copies, snapshot_reading_project, validate_source_reference)
from carr_library.reader.narration import (NarrationStore, PronunciationRule,
    encode_project, new_project, revise, undo)
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.audio_studio import AudioStudioDialog, StudioController


@pytest.fixture
def saved(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Current reading library')
    text = 'Alice: Привіт e\u0301.\r\n[pause 2]\n# A printed heading'
    book = app.add_text(workspace, 'Source', text, authors=('Synthetic author',))
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    project = new_project(page.raw_text, title='Reading copy', source_location=page.location)
    project = revise(project, page.raw_text.replace('Привіт', 'Приві́т'))
    store = NarrationStore(app.root)
    digest = store.save(project)
    return SimpleNamespace(app=app, workspace=workspace, book=book, page=page,
                           project=project, store=store, digest=digest)


def select_saved(saved):
    return next(row for row in saved_reading_copies(saved.app.root, workspace=saved.workspace)
                if row.project_id == saved.project.id)


def load_selected(saved, row=None, **kwargs):
    row = row or select_saved(saved)
    return load_saved_reading_snapshot(saved.app.root, row.project_id, workspace=saved.workspace,
        expected_project_sha256=row.project_sha256, **kwargs)


def test_reference_validator_accepts_actual_snapshot_and_later_audio_history(saved):
    snapshot = load_selected(saved)
    reference, draft = snapshot.source_reference(), snapshot.new_draft()
    assert validate_source_reference(reference, draft) is None
    draft.edit_text('A later audio-only revision.')
    assert validate_source_reference(reference, draft) is None
    draft.undo()
    draft.undo()  # Undo the initial reading capture as well.
    draft.edit_text('An explicit new branch starting from the immutable original.')
    restored = type(draft).from_dict(draft.to_dict())
    assert restored.text != snapshot.reading_text
    assert validate_source_reference(reference, restored) is None


def test_reference_validator_does_not_reopen_or_require_original_producer(saved):
    snapshot = load_selected(saved)
    reference, draft = snapshot.source_reference(), snapshot.new_draft()
    (saved.app.root / 'narration-projects' / (saved.project.id + '.json')).unlink()
    assert validate_source_reference(reference, draft) is None
    independent = snapshot_reading_project(new_project('Independent authored text.'))
    assert validate_source_reference(independent.source_reference(), independent.new_draft()) is None


@pytest.mark.parametrize('field,value', [
    ('kind', 'verified-original'), ('version', True), ('quotation_authority', True),
    ('quotation_authority', 0), ('provenance', 'Source independently verified'),
    ('reading_project_id', 'a' * 32), ('reading_revision', True), ('reading_revision', 0),
    ('reading_revision', 999), ('reading_project_sha256', 'b' * 64),
    ('original_sha256', 'c' * 64), ('reading_sha256', 'd' * 64),
    ('reading_sha256', 'NOT A HASH'), ('source_location', {}),
])
def test_reference_validator_rejects_conflicting_provenance(saved, field, value):
    snapshot = load_selected(saved)
    reference = {**snapshot.source_reference(), field: value}
    with pytest.raises(ValueError):
        validate_source_reference(reference, snapshot.new_draft())


def test_reference_validator_rejects_missing_extra_fields_and_location_text_mismatch(saved):
    snapshot = load_selected(saved)
    reference, draft = snapshot.source_reference(), snapshot.new_draft()
    for altered in ({key: value for key, value in reference.items() if key != 'reading_sha256'},
                    {**reference, 'command': 'anything'},
                    {**reference, 'source_location': {**reference['source_location'], 'text_sha256': 'e' * 64}}):
        with pytest.raises(ValueError):
            validate_source_reference(altered, draft)


def test_actual_reading_source_snapshot_audio_save_reopen_preserves_each_identity(saved, tmp_path):
    file = saved.app.root / 'narration-projects' / (saved.project.id + '.json')
    before = file.read_bytes()
    snapshot = load_selected(saved)
    assert snapshot.original_text == saved.page.raw_text
    assert snapshot.reading_text == saved.project.current_text
    assert snapshot.original_sha256 == saved.page.location.text_sha256
    assert snapshot.reading_sha256 == sha256(saved.project.current_text.encode()).hexdigest()
    assert snapshot.project_sha256 == sha256(encode_project(saved.project)).hexdigest()
    assert snapshot.source_location == saved.page.location
    with pytest.raises(FrozenInstanceError):
        snapshot.title = 'Changed'

    controller = StudioController(private_root=saved.app.root)
    controller.configure(voice='piper:explicit-profile', language='uk')
    controller.use_reading_snapshot(snapshot)
    assert controller.input_kind == 'narration'
    assert (controller.voice, controller.language) == ('piper:explicit-profile', 'uk')
    assert controller.draft.source_text == saved.page.raw_text
    assert controller.script == saved.project.current_text
    assert controller.source_reference['source_location'] == asdict(saved.page.location)
    assert controller.source_reference['quotation_authority'] is False
    reference = controller.source_reference.copy()
    audio = tmp_path / 'separate-audio-project.json'
    controller.save(audio)
    controller.edit_script('A later audio-only revision.')
    controller.open(audio)
    assert controller.source_reference == reference
    assert controller.script == saved.project.current_text
    assert controller.plan().input_kind == 'narration'
    assert file.read_bytes() == before
    assert ReadingDesk(saved.app, saved.workspace, saved.book['edition']).page(1).raw_text == saved.page.raw_text


def test_snapshot_uses_selected_saved_revision_without_rules_or_recovery_draft(saved):
    project = replace(saved.project, rules=(PronunciationRule('Alice', 'DO NOT APPLY'),))
    project = revise(project, 'A future saved reading revision.')
    project = undo(project)
    saved.store.save(project, expected_digest=saved.digest)
    saved.store.save(revise(project, 'Recovery draft must stay separate.'), draft=True)
    snapshot = load_selected(saved)
    assert snapshot.reading_text == saved.project.current_text
    assert snapshot.revision == saved.project.revisions[saved.project.cursor].number
    assert 'DO NOT APPLY' not in snapshot.reading_text
    assert snapshot.new_draft().text == saved.project.current_text


def test_literal_production_plan_keeps_directives_names_and_page_boundaries_as_text():
    text = '# Chapter heading\r\nAlice: A source line.\n[pause 120]\n[music mood=dark]\fNext page.'
    project = new_project(text, title='Literal source text')
    snapshot = snapshot_reading_project(project)
    plan = snapshot.production_plan(voice='test:explicit', language='sk')
    assert isinstance(plan, ProductionPlan) and plan.input_kind == 'narration'
    assert plan.original_script == text
    assert len(plan.scenes) == 2 and all(scene.music is None for scene in plan.scenes)
    events = [event for scene in plan.scenes for event in scene.events]
    assert all(type(event) is Line and event.literal for event in events)
    assert all(event.text == text[event.source_start:event.source_end] for event in events)
    assert ''.join(event.text for event in events) == text.replace('\f', '')
    assert snapshot.source_reference()['source_location'] is None
    assert snapshot.source_reference()['quotation_authority'] is False


def test_picker_scope_is_current_library_plus_labelled_independent_text(saved):
    other = saved.app.create_workspace('Different reading library')
    book = saved.app.add_text(other, 'Source', saved.page.raw_text)
    page = ReadingDesk(saved.app, other, book['edition']).page(1)
    foreign = new_project(page.raw_text, title=saved.project.title, source_location=page.location)
    saved.store.save(foreign)
    independent = new_project('Independent authored reading.', title=saved.project.title)
    saved.store.save(independent)
    draft_only = new_project('An unsaved recovery draft.')
    saved.store.save(draft_only, draft=True)
    rows = saved_reading_copies(saved.app.root, workspace=saved.workspace)
    assert {row.project_id for row in rows} == {saved.project.id, independent.id}
    assert next(row for row in rows if row.project_id == independent.id).independent is True
    assert 'independent text' in next(row for row in rows if row.project_id == independent.id).label
    with pytest.raises(ValueError, match='another library'):
        load_saved_reading_snapshot(saved.app.root, foreign.id, workspace=saved.workspace,
            expected_project_sha256=snapshot_reading_project(foreign).project_sha256)
    with pytest.raises(FileNotFoundError):
        load_saved_reading_snapshot(saved.app.root, draft_only.id, workspace=saved.workspace,
            expected_project_sha256=snapshot_reading_project(draft_only).project_sha256)


def test_changed_saved_selection_fails_without_modifying_either_project(saved):
    selected = select_saved(saved)
    changed = revise(saved.project, 'New reading revision after the chooser opened.')
    saved.store.save(changed, expected_digest=saved.digest)
    with pytest.raises(ValueError, match='changed after selection'):
        load_selected(saved, selected)
    assert saved.store.load(saved.project.id)[0] == changed


def test_content_identity_ignores_json_layout_but_preserves_exact_text(saved):
    selected = select_saved(saved)
    path = saved.app.root / 'narration-projects' / (saved.project.id + '.json')
    path.write_text(json.dumps(json.loads(path.read_bytes()), ensure_ascii=False, indent=4), encoding='utf-8')
    snapshot = load_selected(saved, selected)
    assert snapshot.project_sha256 == selected.project_sha256
    assert snapshot.original_text == saved.project.original_text


def test_cancel_after_real_read_rejects_snapshot_and_keeps_files(saved, monkeypatch):
    selected = select_saved(saved)
    cancelled = Event()
    real_load = NarrationStore.load
    def load_then_cancel(self, *args, **kwargs):
        value = real_load(self, *args, **kwargs)
        cancelled.set()
        return value
    monkeypatch.setattr(NarrationStore, 'load', load_then_cancel)
    with pytest.raises(InterruptedError):
        load_selected(saved, selected, cancelled=cancelled.is_set)
    assert real_load(saved.store, saved.project.id) == (saved.project, saved.digest)


def test_cancel_before_listing_does_not_create_store(tmp_path):
    with pytest.raises(InterruptedError):
        saved_reading_copies(tmp_path / 'absent', workspace='workspace', cancelled=lambda: True)
    assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('value', [{}, SimpleNamespace(original_text='not a producer object'), None])
def test_only_actual_reading_project_type_is_accepted(value):
    with pytest.raises(ValueError, match='validated reading project'):
        snapshot_reading_project(value)


def test_producer_revalidation_rejects_mutated_original_hash(saved):
    project = replace(saved.project)
    object.__setattr__(project, 'original_text', 'Changed without updating its hash.')
    with pytest.raises(ValueError, match='content digest'):
        snapshot_reading_project(project)


def test_audio_drafts_are_detached_and_do_not_retarget_after_reading_revision(saved):
    snapshot = snapshot_reading_project(saved.project)
    first, second = snapshot.new_draft(), snapshot.new_draft()
    first.edit_text('Audio-only edit.')
    assert second.text == snapshot.reading_text == saved.project.current_text
    assert snapshot.source_reference() is not snapshot.source_reference()
    changed = revise(saved.project, 'A later reading revision.')
    saved.store.save(changed, expected_digest=saved.digest)
    assert snapshot.reading_text != changed.current_text
    assert first.source_text == saved.project.original_text


def test_audio_save_cannot_overwrite_original_saved_reading_project(saved):
    controller = StudioController(private_root=saved.app.root)
    controller.use_reading_snapshot(load_selected(saved))
    path = saved.app.root / 'narration-projects' / (saved.project.id + '.json')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        controller.save(path, overwrite=True)
    assert path.read_bytes() == before


def test_optional_missing_reading_producer_does_not_break_audio_import(tmp_path):
    script = '''
import importlib.abc, sys
class MissingProducer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'carr_library.reader.narration':
            raise ModuleNotFoundError('Deliberately absent optional producer', name=fullname)
sys.meta_path.insert(0, MissingProducer())
from carr_library.ui.audio_studio import StudioController
from carr_library.audio.narration_bridge import ReadingCopyUnavailable, saved_reading_copies
StudioController(private_root=sys.argv[1])
try:
    saved_reading_copies(sys.argv[1], workspace='current')
except ReadingCopyUnavailable as error:
    assert 'integrated reading editor' in str(error)
else:
    raise AssertionError('Missing capability was not reported')
'''
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[2] / 'src')
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def reading_picker(saved, *, on_choose=lambda: None, after_read=lambda: None, accept=True):
    """Headless native handler harness; producer, filesystem and audio models are real."""
    view = object.__new__(AudioStudioDialog)
    view.controller = StudioController(private_root=saved.app.root)
    view.controller.new('Existing audio project.', input_kind='script')
    frame = SimpleNamespace(library=saved.app, active=(saved.workspace, 'Current library'))
    frame._workspace = lambda: frame.active[0]
    view.parent, view.dialog = frame, object()
    view.busy = view.closed = view.close_pending = False
    view.cancelled = Event()
    view._unsaved = lambda: True
    view._refresh_text = lambda: None
    view.plan_text = SimpleNamespace(ChangeValue=lambda value: None)
    messages, errors, labels, disposed = [], [], [], []
    view._message = lambda message, **kwargs: messages.append(message)
    view._error = errors.append

    class Choice:
        def __init__(self, parent, message, title, choices):
            labels.extend(choices)
        def ShowModal(self):
            on_choose()
            return 1 if accept else 0
        def GetSelection(self):
            return 0
        def Destroy(self):
            disposed.append(True)

    view.wx = SimpleNamespace(SingleChoiceDialog=Choice, ID_OK=1)
    def run(label, operation, complete):
        view.cancelled.clear()
        try:
            result = operation()
            if label.startswith('Checking'):
                after_read()
            complete(result)
        except Exception as error:
            errors.append(error)
    view._run = run
    return SimpleNamespace(view=view, frame=frame, messages=messages, errors=errors,
                           labels=labels, disposed=disposed)


def test_studio_reading_picker_uses_real_saved_copy_in_literal_mode(saved):
    picker = reading_picker(saved)
    before = saved.store.load(saved.project.id)
    picker.view.use_saved_reading_copy()
    assert not picker.errors
    assert len(picker.labels) == 1 and 'current library' in picker.labels[0]
    assert picker.disposed == [True]
    assert picker.view.controller.script == saved.project.current_text
    assert picker.view.controller.input_kind == 'narration'
    assert picker.view.controller.plan().input_kind == 'narration'
    assert saved.store.load(saved.project.id) == before


def test_studio_picker_cancel_preserves_existing_audio_project(saved):
    picker = reading_picker(saved, accept=False)
    before = picker.view.controller.to_dict()
    picker.view.use_saved_reading_copy()
    assert not picker.errors and picker.disposed == [True]
    assert picker.view.controller.to_dict() == before


@pytest.mark.parametrize('change', ['workspace', 'library', 'root', 'project', 'close', 'cancel'])
def test_studio_picker_rechecks_scope_after_nested_modal(saved, tmp_path, change):
    picker = None
    def change_scope():
        if change == 'workspace':
            picker.frame.active = ('another-workspace', 'Different library')
        elif change == 'library':
            picker.frame.library = LibraryApp(tmp_path / 'different-library')
        elif change == 'root':
            picker.view.controller.private_root = tmp_path / 'different-root'
        elif change == 'project':
            picker.view.controller.new('A different audio project opened during the chooser.')
        elif change == 'close':
            picker.view.closed = True
        else:
            picker.view.cancelled.set()
    picker = reading_picker(saved, on_choose=change_scope)
    picker.view.use_saved_reading_copy()
    assert not picker.errors and picker.disposed == [True]
    assert picker.view.controller.source_reference is None
    assert picker.view.controller.script != saved.project.current_text


def test_studio_picker_does_not_accept_a_changed_saved_revision(saved):
    changed = revise(saved.project, 'Changed while the chooser was open.')
    picker = reading_picker(saved, on_choose=lambda: saved.store.save(changed, expected_digest=saved.digest))
    before = picker.view.controller.to_dict()
    picker.view.use_saved_reading_copy()
    assert len(picker.errors) == 1 and 'changed after selection' in str(picker.errors[0])
    assert picker.view.controller.to_dict() == before
    assert saved.store.load(saved.project.id)[0] == changed


def test_studio_picker_rejects_late_cancel_after_real_snapshot_load(saved):
    picker = None
    picker = reading_picker(saved, after_read=lambda: picker.view.cancelled.set())
    before = picker.view.controller.to_dict()
    picker.view.use_saved_reading_copy()
    assert not picker.errors
    assert picker.view.controller.to_dict() == before
    assert any('discarded' in message for message in picker.messages)
