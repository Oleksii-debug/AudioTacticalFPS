"""Current draft stores behind Save All; no alternate project or approval store."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.narration import NarrationStore, new_project
from carr_library.ui.audio_studio import AudioStudioDialog, StudioController
from carr_library.ui.narration_editor import NarrationController
from carr_library.ui.save_all import SaveCoordinator, SaveResult


def native_boundary(controller, coordinator):
    """Only widget access is synthetic; the real Studio save/controller runs."""
    view = AudioStudioDialog.__new__(AudioStudioDialog)
    view.parent = SimpleNamespace(_save_coordinator=coordinator)
    view.dialog = SimpleNamespace()
    view.script = SimpleNamespace(SetFocus=lambda: None)
    view.controller = controller
    view.closed = view.busy = view.editing_dirty = False
    view.messages = []
    view._capture = lambda: None
    view._refresh_text = lambda: setattr(view, 'editing_dirty', controller.dirty)
    view._message = lambda value, **kwargs: view.messages.append(str(value))
    view._error = lambda error: view.messages.append(str(error))
    view._register_save_participant()
    return view


def test_save_all_writes_current_managed_studio_and_reading_copy_without_source_edits(tmp_path):
    library = LibraryApp(tmp_path / 'library')
    coordinator = SaveCoordinator()
    reading = NarrationController(NarrationStore(library.root), new_project('Immutable original'))
    reading.set_text('Explicit reading derivative')

    def save_reading(completed):
        reading.save()
        completed(SaveResult(True))

    coordinator.register('reading', 'Reading copy', dirty=lambda: reading.dirty, save=save_reading)
    studio = StudioController(private_root=library.root)
    studio.new('Immutable studio original', title='Managed audio draft')
    studio.edit_script('Explicit audio derivative')
    view = native_boundary(studio, coordinator)
    assert view.dialog._save_coordinator is coordinator
    summaries = []
    coordinator.save_all(summaries.append)
    assert len(summaries) == 1 and summaries[0].complete
    saved_reading = NarrationStore(library.root).load(reading.project.id)[0]
    assert saved_reading.original_text == 'Immutable original'
    assert saved_reading.current_text == 'Explicit reading derivative'
    assert studio.project_path.is_relative_to(library.root / 'audio-projects')
    reopened = StudioController(private_root=library.root).open(studio.project_path)
    assert reopened.draft.source_text == 'Immutable studio original'
    assert reopened.script == 'Explicit audio derivative'
    assert not reading.dirty and not studio.dirty
    view._save_unregister()
    assert len(coordinator.participants) == 1


def test_studio_conflict_retains_the_other_writer_and_saves_other_open_drafts(tmp_path):
    library = LibraryApp(tmp_path / 'library')
    coordinator = SaveCoordinator()
    studio = StudioController(private_root=library.root)
    studio.new('Original')
    studio.save_in_library()
    studio.edit_script('Uncommitted editor text')
    view = native_boundary(studio, coordinator)
    original = studio.project_path.read_bytes()
    # A real changed destination must still be rejected by the existing hash lease.
    studio.project_path.write_bytes(original + b'\n')
    completed_file = tmp_path / 'other-draft.txt'
    other = {'dirty': True}
    def save_other(completed):
        completed_file.write_text('Other valid draft', encoding='utf-8')
        other['dirty'] = False
        completed(SaveResult(True))
    coordinator.register('other', 'Other draft', dirty=lambda: other['dirty'], save=save_other)
    summaries = []
    coordinator.save_all(summaries.append)
    assert not summaries[0].complete and summaries[0].saved == ('Other draft',)
    assert studio.project_path.read_bytes() == original + b'\n'
    assert studio.script == 'Uncommitted editor text' and studio.dirty and not view.closed
    assert completed_file.read_text(encoding='utf-8') == 'Other valid draft'


def test_running_studio_job_is_reported_as_pending_without_a_concurrent_save(tmp_path, monkeypatch):
    library = LibraryApp(tmp_path / 'library')
    coordinator = SaveCoordinator()
    studio = StudioController(private_root=library.root)
    studio.new('Uncommitted audio text')
    view = native_boundary(studio, coordinator)
    view.busy = True
    monkeypatch.setattr(studio, 'save', lambda *args, **kwargs: pytest.fail('Concurrent project write'))
    summaries = []
    coordinator.save_all(summaries.append)
    assert not summaries[0].complete and not studio.project_path
    assert 'operation is running' in summaries[0].remaining[0][1]
    assert studio.script == 'Uncommitted audio text'
