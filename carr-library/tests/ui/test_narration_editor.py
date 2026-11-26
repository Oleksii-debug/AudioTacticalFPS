from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.reader.narration import NarrationStore, PronunciationRule, new_project, revise
from carr_library.ui.narration_editor import NarrationController, _parse_rules, show_narration_projects


@pytest.fixture
def controller(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    return NarrationController(NarrationStore(root), new_project('Original', title='Reading copy'))


def test_pending_typing_is_one_revision_and_recovery_does_not_claim_manual_save(controller):
    for text in ['E', 'Ed', 'Edited']:
        controller.set_text(text)
        assert 'Recovery draft' in controller.autosave()
    assert len(controller.project.revisions) == 1
    draft, _ = controller.store.load(controller.project.id, draft=True)
    assert draft.current_text == 'Edited' and len(draft.revisions) == 2
    assert controller.dirty and controller.saved_digest is None
    assert 'project saved' in controller.save()
    assert not controller.dirty and len(controller.project.revisions) == 2
    assert controller.draft_digest is None


def test_failed_save_leaves_pending_text_and_dirty_state(controller, monkeypatch):
    controller.save()
    old_project = controller.project
    controller.set_text('Unsaved text')
    monkeypatch.setattr(controller.store, 'save', lambda *_a, **_k: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        controller.save()
    assert controller.project is old_project
    assert controller.pending_text == 'Unsaved text' and controller.dirty


def test_stale_writer_cannot_silently_overwrite_other_editor(controller):
    controller.save()
    other = NarrationController(controller.store, controller.project, expected_digest=controller.saved_digest)
    controller.set_text('First editor edit')
    controller.save()
    other.set_text('Second editor edit')
    with pytest.raises(ValueError, match='changed elsewhere'):
        other.save()
    assert other.pending_text == 'Second editor edit' and other.dirty
    assert other.store.load(other.project.id)[0].current_text == 'First editor edit'


def test_controller_undo_redo_and_transform_keep_source(controller):
    controller.set_text('Edited')
    controller.undo()
    assert controller.pending_text == 'Original'
    controller.redo()
    assert controller.pending_text == 'Edited'
    controller.apply('Prepared', label='Explicit normalization')
    assert controller.pending_text == 'Prepared' and controller.project.original_text == 'Original'


def test_save_as_new_identity_does_not_overwrite_prior_project(controller):
    controller.save()
    original_id = controller.project.id
    controller.set_text('New copy')
    controller.save_as('Second project')
    assert controller.project.id != original_id
    assert controller.store.load(original_id)[0].current_text == 'Original'
    assert controller.project.original_text == 'Original'
    assert controller.pending_text == 'New copy' and not controller.dirty


def test_revoked_guard_blocks_save_and_draft(controller):
    controller.guard = lambda: (_ for _ in ()).throw(ValueError('Source scope closed'))
    with pytest.raises(ValueError, match='scope closed'):
        controller.save()
    with pytest.raises(ValueError, match='scope closed'):
        controller.autosave()
    assert not (controller.store.root / 'narration-projects').exists()


def test_dictionary_interchange_rejects_unexpected_fields_and_duplicates():
    assert _parse_rules('[{"written":"a","spoken":"b"}]') == (PronunciationRule('a', 'b'),)
    for invalid in ['{}', '[null]', '[{"written":"a","spoken":"b","execute":"shell"}]',
                    '[{"written":"a","spoken":"b"},{"written":"a","spoken":"c"}]']:
        with pytest.raises(ValueError):
            _parse_rules(invalid)


class Dialogs:
    def __init__(self, choices):
        self.choices = iter(choices)
        self.messages = []

    def choose(self, *args):
        self.messages.append(args)
        return next(self.choices)

    def show(self, *args):
        self.messages.append(args)


def test_project_picker_explicitly_recovers_draft_and_preserves_saved_lease(controller, monkeypatch):
    import carr_library.ui.narration_editor as ui
    controller.save()
    saved_digest = controller.saved_digest
    controller.set_text('Recovered words')
    controller.autosave()
    captured = {}
    monkeypatch.setattr(ui, 'open_narration_editor',
                        lambda parent, store, project, **kwargs: captured.update(project=project, **kwargs))
    show_narration_projects(None, controller.store, dialogs=Dialogs([0, 0]))
    assert captured['project'].current_text == 'Recovered words'
    assert captured['expected_digest'] == saved_digest
    assert captured['draft_digest'] == controller.draft_digest


def test_picker_can_choose_last_saved_text_or_cancel_without_deleting_recovery(controller, monkeypatch):
    import carr_library.ui.narration_editor as ui
    controller.save()
    controller.set_text('Draft')
    controller.autosave()
    captured = []
    monkeypatch.setattr(ui, 'open_narration_editor', lambda *args, **kwargs: captured.append(args[2]))
    show_narration_projects(None, controller.store, dialogs=Dialogs([0, 1]))
    assert captured[0].current_text == 'Original'
    show_narration_projects(None, controller.store, dialogs=Dialogs([0, 2]))
    assert len(captured) == 1
    assert controller.store.load(controller.project.id, draft=True)[0].current_text == 'Draft'


def test_draft_only_project_recovery_and_no_saved_project_message(controller, monkeypatch):
    import carr_library.ui.narration_editor as ui
    dialogs = Dialogs([])
    assert show_narration_projects(None, controller.store, dialogs=dialogs) is None
    assert 'no saved' in dialogs.messages[0][-1]
    controller.set_text('Draft only')
    controller.autosave()
    captured = {}
    monkeypatch.setattr(ui, 'open_narration_editor',
                        lambda parent, store, project, **kwargs: captured.update(project=project, **kwargs))
    show_narration_projects(None, controller.store, dialogs=Dialogs([0, 0]))
    assert captured['expected_digest'] is None
    assert captured['project'].current_text == 'Draft only'


@pytest.mark.parametrize('tamper_draft', [False, True])
def test_picker_rechecks_loaded_project_and_recovery_source_workspace(controller, monkeypatch, tamper_draft):
    from dataclasses import replace
    import carr_library.ui.narration_editor as ui
    from carr_library.reader.narration import text_digest
    from carr_library.reader.reading_state import ReadingLocation
    location = ReadingLocation('a', 'edition', 'revision', 'logical_segment', 1,
                               text_digest('Original'), '0' * 64)
    source = new_project('Original', source_location=location)
    controller.store.save(source)
    if tamper_draft:
        controller.store.save(source, draft=True)
    real_load = controller.store.load

    def changed_load(project_id, *, draft=False):
        project, digest = real_load(project_id, draft=draft)
        if draft == tamper_draft:
            project = replace(project, source_location=replace(project.source_location, workspace='b'))
        return project, digest

    class ChangedAfterList(Dialogs):
        def choose(self, *args):
            monkeypatch.setattr(controller.store, 'load', changed_load)
            return 0

    monkeypatch.setattr(ui, 'open_narration_editor', lambda *a, **k: pytest.fail('Cross-library project displayed'))
    with pytest.raises(ValueError, match='different library'):
        show_narration_projects(None, controller.store, workspace='a', dialogs=ChangedAfterList([]))


def test_picker_without_library_only_exposes_independent_authored_projects(controller, monkeypatch):
    import carr_library.ui.narration_editor as ui
    from carr_library.reader.narration import text_digest
    from carr_library.reader.reading_state import ReadingLocation
    source = new_project('Source text', title='Private source copy', source_location=ReadingLocation(
        'archived-library', 'edition', 'revision', 'logical_segment', 1,
        text_digest('Source text'), '0' * 64))
    controller.store.save(source)
    controller.save()
    captured = []
    dialogs = Dialogs([0])
    monkeypatch.setattr(ui, 'open_narration_editor', lambda *args, **kwargs: captured.append(args[2]))
    show_narration_projects(None, controller.store, dialogs=dialogs)
    assert [project.id for project in captured] == [controller.project.id]
    assert 'Private source copy' not in str(dialogs.messages)
