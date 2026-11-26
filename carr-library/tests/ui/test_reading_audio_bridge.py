"""Actual reading derivatives enter Studio without becoming source evidence."""
from dataclasses import asdict

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.narration import NarrationStore, new_project
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.narration_editor import NarrationController
from carr_library.ui.audio_studio import StudioController


def test_source_bound_reading_copy_becomes_separate_durable_audio_project(tmp_path):
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Reading to audio')
    imported = library.add_text(workspace, 'Synthetic source', 'Original railway text. Українська мова.')
    page = ReadingDesk(library, workspace, imported['edition']).page(1)
    hit, = library.search(workspace, 'railway')
    original = library.assets.resolve(imported['asset']).read_bytes()
    project = new_project(page.raw_text, title='Prepared reading', source_location=page.location)
    controller = NarrationController(NarrationStore(library.root), project)
    changed = page.raw_text.replace('railway', 'rail way') + '\nExplicit performance direction.'
    controller.set_text(changed)
    controller.autosave()
    studio = controller.audio_copy()
    assert studio.script == changed
    assert studio.draft.source_text == page.raw_text
    assert studio.draft.source_sha256 == project.original_sha256
    assert studio.draft.source_id == 'reading-copy:' + project.id
    assert studio.source_reference['source_location'] == asdict(page.location)
    target = tmp_path / 'separate-audio-project.json'
    studio.save(target)
    reopened = StudioController(private_root=library.root).open(target)
    assert reopened.script == changed
    assert reopened.draft.source_text == page.raw_text
    assert reopened.source_reference == studio.source_reference
    assert library.assets.resolve(imported['asset']).read_bytes() == original
    assert library.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    assert controller.store.load(project.id, draft=True)[0].current_text == changed


def test_standalone_copy_and_studio_edits_remain_independent(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    original = 'Привіт.\r\nDobrý deň!'
    project = new_project(original, title='Independent script')
    controller = NarrationController(NarrationStore(root), project)
    controller.save()
    controller.set_text('Changed for speech.')
    studio = controller.audio_copy()
    studio.edit_script('A later studio edit.')
    assert controller.pending_text == 'Changed for speech.'
    assert controller.project.original_text == original
    assert controller.store.load(project.id)[0].current_text == original
    assert studio.draft.source_text == original
    assert studio.source_reference['source_location'] is None
    assert studio.result is None


def test_closed_source_scope_cannot_start_audio_copy(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    def closed():
        raise ValueError('Source scope closed')
    controller = NarrationController(NarrationStore(root), new_project('Original'), guard=closed)
    with pytest.raises(ValueError, match='scope closed'):
        controller.audio_copy()
    assert list(root.iterdir()) == []
