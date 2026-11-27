"""Actual scoped records, citeproc-js and Word round trips for ordered citations."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.cluster_workbench import CitationClusterWorkbench, CitationDraft
from carr_library.citations.documents import read_citation_docx
from carr_library.research.export_file import capture_export_destination
from carr_library.ui.academic_library_dialog import AcademicController


@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Ordered citation acceptance')
    controller = AcademicController(app, workspace)
    first = controller.create(dict(title='Synthetic source alpha', authors=('Smith, Jane',),
        year='2020', publisher='Example Press', pages='11-99', notes='PrivateNotesExcluded'))
    second = controller.create(dict(title='Synthetic source beta', authors=('Jones, Alex',),
        year='2022', publisher='Second Press'))
    workbench = CitationClusterWorkbench(controller)
    selected = workbench.selection(name_mode='family-given')
    return controller, first, second, workbench, selected


def test_actual_ordered_clusters_roundtrip_locators_affixes_and_source_scope(research, tmp_path):
    controller, first, second, workbench, selected = research
    draft = CitationDraft(selected)
    item = {'id': first.id, 'locator': '12', 'label': 'page', 'prefix': 'see ',
            'suffix': ' for context', 'suppress-author': True}
    draft.add(item)
    draft.add({'id': second.id}, group=0)
    draft.add({'id': first.id, 'locator': '3', 'label': 'chapter'})
    saved_order = draft.captured
    # Caller-owned mutable input cannot change an already validated draft.
    item['locator'] = '999'
    assert draft.captured == saved_order
    preview = workbench.preview(selected, saved_order)
    assert len(preview.result.citations) == 2
    assert '12' in preview.citations and 'see ' in preview.citations
    assert '11-99' not in preview.citations and 'PrivateNotes' not in preview.explanation
    assert any('manually' in warning for warning in preview.result.warnings)
    destination = tmp_path / 'ordered.docx'
    workbench.export_document(preview, destination)
    stored = read_citation_docx(destination)
    assert [group['id'] for group in stored['clusters']] == [group['id'] for group in draft.clusters]
    assert stored['clusters'][0]['items'][0] == json.loads(saved_order)[0]['items'][0]
    assert stored['quotation_verified'] is False and stored['visible_edits_verified'] is False
    assert 'PrivateNotes' not in json.dumps(stored)
    loaded = workbench.load_document_order(selected, destination)
    assert loaded.captured == draft.captured
    assert controller.get(first.id) == first and controller.get(second.id) == second


def test_real_note_context_preserves_occurrences_and_requires_explicit_renumbering(research, tmp_path):
    _controller, first, second, workbench, selected = research
    draft = CitationDraft(selected)
    draft.add({'id': first.id})
    draft.add({'id': second.id})
    original_ids = tuple(group['id'] for group in draft.clusters)
    preview = workbench.preview(selected, draft.captured, style='chicago-note')
    assert preview.result.note_indices == (1, 2)
    source = tmp_path / 'notes.docx'
    workbench.export_document(preview, source)
    before = source.read_bytes()
    loaded = workbench.load_document_order(selected, source)
    loaded.move_group(1, -1)
    reordered = workbench.preview(selected, loaded.captured, style='chicago-note')
    assert reordered.result.citation_ids == tuple(reversed(original_ids))
    assert reordered.result.note_indices == (2, 1)
    with pytest.raises(ValueError, match='consecutive'):
        workbench.export_document(reordered, tmp_path / 'wrong-notes.docx')
    assert not (tmp_path / 'wrong-notes.docx').exists()
    loaded.use_document_order()
    renumbered = workbench.preview(selected, loaded.captured, style='chicago-note')
    assert renumbered.result.note_indices == (1, 2)
    assert renumbered.result.citation_ids == tuple(reversed(original_ids))
    workbench.export_document(renumbered, tmp_path / 'reordered-notes.docx')
    assert source.read_bytes() == before


@pytest.mark.parametrize('bad', ['duplicate', 'unknown', 'long-affix', 'invalid-note'])
def test_rejected_edit_never_partially_changes_a_valid_draft(research, bad):
    _controller, first, _second, _workbench, selected = research
    draft = CitationDraft(selected)
    draft.add({'id': first.id})
    original = draft.captured
    with pytest.raises(ValueError):
        if bad == 'duplicate':
            draft.add({'id': first.id}, group=0)
        elif bad == 'unknown':
            draft.add({'id': 'outside-selected-library'})
        elif bad == 'long-affix':
            draft.replace_item(0, 0, {'id': first.id, 'prefix': 'x' * 4097})
        else:
            draft.replace_item(0, 0, {'id': first.id}, note_index=0)
    assert draft.captured == original


def test_changed_metadata_cannot_reuse_old_preview_or_saved_locator_context(research, tmp_path):
    controller, first, _second, workbench, selected = research
    draft = CitationDraft(selected)
    draft.add({'id': first.id, 'locator': '12'})
    preview = workbench.preview(selected, draft.captured)
    source = tmp_path / 'saved.docx'
    workbench.export_document(preview, source)
    original = source.read_bytes()
    controller.update(first, {'title': 'Metadata changed after citation review'})
    with pytest.raises(ValueError, match='changed'):
        workbench.copy_text(preview)
    with pytest.raises(ValueError, match='changed'):
        workbench.export_document(preview, tmp_path / 'stale.docx')
    current = workbench.selection(name_mode='family-given')
    with pytest.raises(ValueError, match='differ'):
        workbench.load_document_order(current, source)
    assert source.read_bytes() == original and not (tmp_path / 'stale.docx').exists()


def test_other_workspace_and_late_revocation_refuse_output(research, tmp_path, monkeypatch):
    controller, first, _second, workbench, selected = research
    draft = CitationDraft(selected)
    draft.add({'id': first.id})
    preview = workbench.preview(selected, draft.captured)
    other = controller.library.create_workspace('Different library')
    foreign = CitationClusterWorkbench(AcademicController(controller.library, other))
    with pytest.raises(ValueError, match='another library'):
        foreign.copy_text(preview)
    from carr_library.citations import documents
    actual = documents.citation_document
    def revoke_after_render(*args, **kwargs):
        content = actual(*args, **kwargs)
        controller.library.archive_workspace(controller.workspace)
        return content
    monkeypatch.setattr(documents, 'citation_document', revoke_after_render)
    output = tmp_path / 'revoked.docx'
    with pytest.raises(ValueError):
        workbench.export_document(preview, output)
    assert not output.exists()


def test_concurrent_new_file_is_preserved_after_actual_word_render(research, tmp_path, monkeypatch):
    controller, first, _second, workbench, selected = research
    draft = CitationDraft(selected)
    draft.add({'id': first.id})
    preview = workbench.preview(selected, draft.captured)
    output = tmp_path / 'new.docx'
    approval = capture_export_destination(output, private_root=controller.library.root)
    from carr_library.citations import documents
    actual = documents.citation_document
    competing = b'Another program saved its own document.'
    def competing_save(*args, **kwargs):
        result = actual(*args, **kwargs)
        output.write_bytes(competing)
        return result
    monkeypatch.setattr(documents, 'citation_document', competing_save)
    with pytest.raises((ValueError, FileExistsError)):
        workbench.export_document(preview, output, approval=approval)
    assert output.read_bytes() == competing
    assert not list(tmp_path.glob('.research-export-*'))


def test_existing_destination_and_cancelled_operation_do_not_write(research, tmp_path):
    _controller, first, _second, workbench, selected = research
    draft = CitationDraft(selected)
    draft.add({'id': first.id})
    preview = workbench.preview(selected, draft.captured)
    output = tmp_path / 'kept.docx'
    output.write_bytes(b'Original document')
    with pytest.raises(FileExistsError):
        workbench.export_document(preview, output)
    with pytest.raises(TimeoutError):
        workbench.preview(selected, draft.captured, cancelled=lambda: True)
    cancelled = tmp_path / 'cancelled.docx'
    with pytest.raises(TimeoutError):
        workbench.export_document(preview, cancelled, cancelled=lambda: True)
    assert output.read_bytes() == b'Original document' and not cancelled.exists()
