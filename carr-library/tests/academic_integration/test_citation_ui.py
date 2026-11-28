"""Headless native-action controllers against real SQLite, embedded CSL and DOCX."""
from dataclasses import replace
from concurrent.futures import Future
import json
import sys
import threading
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.academic_integration import CitationCluster, CitationRequest, CitationError, DoiMetadataClient, read_citation_docx
from carr_library.academic_integration.doi import MetadataResponse
from carr_library.ui.academic_library_dialog import AcademicController
from carr_library.ui.citation_formatting import (
    CitationFormattingController, _scope, _guard, formatted_preview_text,
    metadata_preview_text, reconcile_citations,
)


@pytest.fixture
def prepared(tmp_path):
    library = LibraryApp(tmp_path / "library-data")
    workspace = library.create_workspace("Selected academic library")
    other = library.create_workspace("Separate academic library")
    source = library.add_text(workspace, "Original source", "Exact immutable wording about spectroscopy.", authors=("Source Author",))
    academic = AcademicController(library, workspace)
    reference = academic.create({"title": "Synthetic research methods", "authors": ("Example Research Group",),
        "item_type": "book", "year": "2024", "publisher": "Example Press", "pages": "1-400",
        "abstract": "Private abstract must not enter citation output.", "notes": "Private workspace notes.",
        "edition_id": source["edition"]})
    AcademicController(library, other).create({"title": "Excluded other library", "authors": ("Other Author",), "year": "2025"})
    return library, workspace, other, academic, reference, source


def test_actual_workspace_to_csl_to_docx_without_mutating_source_or_private_notes(prepared, tmp_path):
    library, workspace, other, academic, reference, source = prepared
    hit, = library.search(workspace, "spectroscopy")
    original_quote = library.copy(workspace, hit.ref, hit.revision, "quote")
    controller = CitationFormattingController(library, workspace)
    snapshot = controller.load()
    assert len(snapshot.references) == 1 and snapshot.references[0]["id"] == reference.id
    assert "abstract" not in snapshot.references[0] and "note" not in snapshot.references[0]
    cluster = CitationCluster((CitationRequest(reference.id, "12", locator_verified=True),), id="checked-page")
    result = controller.render(snapshot, style="apa", citations=(cluster,))
    assert "p. 12" in result.citations[0].text and "1–400" in result.bibliography[0].text
    preview = formatted_preview_text(result)
    assert result.attribution in preview and "Excluded other library" not in preview
    destination = controller.export_docx(snapshot, result, tmp_path / "citation-document.docx")
    recovered = read_citation_docx(destination)
    assert recovered["references"][0]["id"] == reference.id
    with ZipFile(destination) as archive:
        document_xml = archive.read("word/document.xml").decode()
        metadata = archive.read("customXml/carr-citations.xml").decode()
    for value in (document_xml, metadata, preview):
        assert "Private workspace" not in value and "Private abstract" not in value
        assert "Excluded other library" not in value
    assert academic.get(reference.id) == reference
    assert library.copy(workspace, hit.ref, hit.revision, "quote") == original_quote


def test_bibliography_only_is_a_real_supported_export(prepared, tmp_path):
    library, workspace, _, _, reference, _ = prepared
    controller = CitationFormattingController(library, workspace)
    snapshot = controller.load()
    result = controller.render(snapshot, reference_ids=(reference.id,), style="ieee")
    assert not result.citations and result.bibliography[0].text.startswith("[1]")
    target = controller.export_docx(snapshot, result, tmp_path / "bibliography.docx")
    assert target.exists() and read_citation_docx(target)["clusters"] == []


def test_bibtex_import_uses_existing_csl_and_docx_with_literal_authors_and_exact_sources(
        prepared, tmp_path):
    library, workspace, other, academic, _, source = prepared
    original = library.assets.resolve(source['asset']).read_bytes()
    hit, = library.search(workspace, 'spectroscopy')
    quote = library.copy(workspace, hit.ref, hit.revision, 'quote')
    raw = r'''@book{unselected,title={Unselected BibTeX}}
@book{kept,title={Synthetic BibTeX methods},
 author={{Research and Development, Inc.} and , Jr., John},
 year=2026,publisher={Reviewed Press},pages={11--19},note={PrivateImportedNote}}'''.encode()
    preview = academic.preview(raw, 'bibtex')
    assert preview.entries[1].fields['authors'] == ('Research and Development, Inc.', ', Jr., John')
    saved = academic.import_selected(preview, (1,))
    reference = academic.get(saved.imported_ids[0])
    assert reference.edition_id is None and reference.id != 'kept'
    assert 'Citation key: kept' in reference.notes
    assert not academic.list(query='Unselected BibTeX')
    controller = CitationFormattingController(library, workspace)
    snapshot = controller.load()
    imported = next(row for row in snapshot.references if row['id'] == reference.id)
    assert imported['author'] == [{'literal': 'Research and Development, Inc.'}, {'literal': ', Jr., John'}]
    result = controller.render(snapshot, reference_ids=(reference.id,), style='apa')
    assert 'Research and Development, Inc.' in result.bibliography[0].text
    assert 'Synthetic BibTeX methods' in result.bibliography[0].text
    destination = controller.export_docx(snapshot, result, tmp_path / 'bibtex-citations.docx')
    recovered = read_citation_docx(destination)
    assert recovered['references'][0]['author'] == imported['author']
    assert 'PrivateImportedNote' not in json.dumps(recovered)
    assert library.assets.resolve(source['asset']).read_bytes() == original
    assert library.copy(workspace, hit.ref, hit.revision, 'quote') == quote
    reopened = AcademicController(LibraryApp(library.root), workspace)
    assert reopened.get(reference.id) == reference
    assert not AcademicController(library, other).list(query='Synthetic BibTeX')


def test_foreign_scope_and_another_controller_snapshot_cannot_be_reused(prepared):
    library, workspace, other, _, reference, _ = prepared
    controller = CitationFormattingController(library, workspace)
    snapshot = controller.load()
    with pytest.raises(ValueError, match="different library"):
        CitationFormattingController(library, other).render(snapshot)
    with pytest.raises(ValueError, match="different library"):
        CitationFormattingController(library, workspace).render(snapshot)
    with pytest.raises(ValueError, match="unavailable"):
        controller.render(snapshot, reference_ids=("not-in-this-library",))


def test_changed_metadata_invalidates_completed_result_and_never_publishes_old_docx(prepared, tmp_path):
    library, workspace, _, academic, reference, _ = prepared
    controller = CitationFormattingController(library, workspace)
    snapshot = controller.load()
    result = controller.render(snapshot)
    academic.update(reference, {"title": "Changed reference title"})
    with pytest.raises(ValueError, match="changed"):
        controller.export_docx(snapshot, result, tmp_path / "stale.docx")
    assert not (tmp_path / "stale.docx").exists()
    with pytest.raises(ValueError, match="changed"):
        controller.render(snapshot)


def test_reload_retracts_locator_verification_when_linked_source_changes_under_same_reference_id(prepared):
    library, workspace, _, academic, reference, _ = prepared
    controller = CitationFormattingController(library, workspace)
    snapshot = controller.load()
    cluster = CitationCluster((CitationRequest(reference.id, "12", locator_verified=True),), id="same-id")
    unchanged, changed = reconcile_citations(snapshot, controller.load(), (cluster,))
    assert not changed and unchanged[0].items[0].locator_verified
    another_source = library.add_text(workspace, "Other source copy", "A different source and different pages.")
    academic.update(reference, {"edition_id": another_source["edition"]})
    refreshed = controller.load()
    assert snapshot.references == refreshed.references  # CSL data did not reveal the replacement.
    assert snapshot.sha256 != refreshed.sha256
    retained, changed = reconcile_citations(snapshot, refreshed, (cluster,))
    assert changed and retained[0].items[0].locator == "12"
    assert not retained[0].items[0].locator_verified
    with pytest.raises(CitationError, match="Verify"):
        controller.render(refreshed, citations=retained)


def test_existing_export_and_private_storage_are_not_overwritten(prepared, tmp_path):
    library, workspace, _, _, _, _ = prepared
    controller = CitationFormattingController(library, workspace)
    snapshot = controller.load()
    result = controller.render(snapshot)
    destination = tmp_path / "existing.docx"
    destination.write_bytes(b"Existing user file")
    with pytest.raises(FileExistsError):
        controller.export_docx(snapshot, result, destination)
    assert destination.read_bytes() == b"Existing user file"
    with pytest.raises(ValueError, match="outside"):
        controller.export_docx(snapshot, result, library.root / "not-a-library-record.docx")
    assert not (library.root / "not-a-library-record.docx").exists()


def test_empty_or_archived_workspace_cannot_fall_back_to_another_library(prepared):
    library, workspace, other, _, _, _ = prepared
    empty = library.create_workspace("Empty academic workspace")
    with pytest.raises(ValueError, match="active academic"):
        CitationFormattingController(library, empty).load()
    library.archive_workspace(workspace)
    with pytest.raises(ValueError, match="archived"):
        CitationFormattingController(library, workspace)
    assert len(CitationFormattingController(library, other).load().references) == 1


def test_controller_cancellation_does_not_publish_or_modify_reference(prepared, tmp_path):
    library, workspace, _, academic, reference, _ = prepared
    controller = CitationFormattingController(library, workspace)
    with pytest.raises(InterruptedError):
        controller.load(cancelled=lambda: True)
    snapshot = controller.load()
    with pytest.raises(InterruptedError):
        controller.render(snapshot, cancelled=lambda: True)
    assert academic.get(reference.id) == reference
    assert not (tmp_path / "cancelled.docx").exists()


def test_doi_preview_controller_exports_only_review_metadata_and_never_imports(prepared, tmp_path):
    library, workspace, _, academic, reference, _ = prepared
    controller = CitationFormattingController(library, workspace)
    calls = []
    body = json.dumps({"status": "ok", "message-type": "work", "message": {
        "DOI": "10.5555/preview", "type": "journal-article", "title": ["External metadata"],
        "author": [{"given": "Jane", "family": "Example"}], "issued": {"date-parts": [[2026]]}}}).encode()

    def transport(request, *, cancelled):
        calls.append(request)
        return MetadataResponse(200, {"Content-Type": "application/json"}, body, request.url)

    disabled = DoiMetadataClient(transport=transport)
    with pytest.raises(PermissionError):
        controller.lookup_doi(disabled, "10.5555/preview")
    assert not calls
    client = DoiMetadataClient(allow_network=True, transport=transport)
    preview = controller.lookup_doi(client, "10.5555/preview")
    text = metadata_preview_text(preview)
    assert "External metadata" in text and "Permissions are unknown" in text
    target = controller.save_doi_preview(preview, tmp_path / "preview.json")
    stored = json.loads(target.read_text())
    assert stored["review_required"] is True and stored["schema"] == "carr-doi-preview/1"
    assert academic.list() == (reference,)
    assert len(calls) == 1
    with pytest.raises(FileExistsError):
        controller.save_doi_preview(preview, target)


def test_entrypoint_guard_requires_same_active_library_object_and_scope(prepared):
    library, workspace, other, _, _, _ = prepared
    frame = SimpleNamespace(library=library, active=(workspace, None), _workspace=lambda: workspace,
                            working=False, busy=False, _closing=False)
    assert _scope(frame) == (library, workspace)
    _guard(frame, library, workspace)
    frame.active = (other, None)
    with pytest.raises(ValueError, match="changed"):
        _guard(frame, library, workspace)
    with pytest.raises(ValueError, match="Open"):
        _scope(frame)
    frame.active = None
    with pytest.raises(ValueError, match="Open"):
        _scope(frame)
    frame.active = (workspace, None)
    frame.working = True
    with pytest.raises(ValueError, match="current operation"):
        _scope(frame)


def saved_document(prepared, tmp_path):
    library, workspace, _, _, _, _ = prepared
    controller = CitationFormattingController(library, workspace)
    snapshot = controller.load()
    result = controller.render(snapshot)
    path = controller.export_docx(snapshot, result, tmp_path / 'selected-citations.docx')
    return controller, path


def test_saved_docx_regenerates_from_reviewed_snapshot_in_an_empty_active_workspace(prepared, tmp_path):
    from carr_library.ui.citation_formatting import REGENERATION_NOTICE
    library, *_ = prepared
    _, source = saved_document(prepared, tmp_path)
    original = source.read_bytes()
    before = read_citation_docx(source)
    empty = library.create_workspace('Independent saved document review')
    controller = CitationFormattingController(library, empty)
    inspected = controller.inspect_docx(source)
    assert REGENERATION_NOTICE in inspected.summary
    assert inspected.sha256 in inspected.summary
    assert 'Private workspace notes' not in inspected.summary
    target = controller.regenerate_docx(inspected, tmp_path / 'new-snapshot.docx')
    assert read_citation_docx(target) == before
    assert source.read_bytes() == original
    assert AcademicController(library, empty).list() == ()
    with pytest.raises(FileExistsError):
        controller.regenerate_docx(inspected, source)
    with pytest.raises(FileExistsError):
        controller.regenerate_docx(inspected, target)
    with pytest.raises(ValueError, match='outside'):
        controller.regenerate_docx(inspected, library.root / 'unapproved.docx')
    assert source.read_bytes() == original and read_citation_docx(target) == before


def test_changed_docx_after_review_is_rejected_before_processor_admission(prepared, tmp_path, monkeypatch):
    from carr_library.academic_integration import regeneration
    controller, source = saved_document(prepared, tmp_path)
    inspected = controller.inspect_docx(source)
    changed = b'Newer independently saved document'
    source.write_bytes(changed)
    monkeypatch.setattr(regeneration, 'render_citations',
                        lambda *args, **kwargs: pytest.fail('A stale reviewed document reached the processor'))
    target = tmp_path / 'stale-review.docx'
    with pytest.raises(CitationError, match='Open and review'):
        controller.regenerate_docx(inspected, target)
    assert not target.exists() and source.read_bytes() == changed


def test_regeneration_rechecks_workspace_access_after_actual_document_assembly(prepared, tmp_path, monkeypatch):
    from docx.document import Document
    library, workspace, *_ = prepared
    controller, source = saved_document(prepared, tmp_path)
    inspected = controller.inspect_docx(source)
    original = source.read_bytes()
    actual_save = Document.save
    def assemble_then_archive(document, destination):
        actual_save(document, destination)
        library.archive_workspace(workspace)
    monkeypatch.setattr(Document, 'save', assemble_then_archive)
    target = tmp_path / 'archived-scope.docx'
    with pytest.raises(ValueError, match='archived'):
        controller.regenerate_docx(inspected, target)
    assert not target.exists() and source.read_bytes() == original


def test_live_export_rechecks_reference_identity_after_actual_document_assembly(prepared, tmp_path, monkeypatch):
    from docx.document import Document
    library, workspace, _, academic, reference, _ = prepared
    controller = CitationFormattingController(library, workspace)
    snapshot = controller.load()
    result = controller.render(snapshot)
    actual_save = Document.save
    def assemble_then_edit_reference(document, destination):
        actual_save(document, destination)
        academic.update(reference, {'title': 'New reference saved during document assembly'})
    monkeypatch.setattr(Document, 'save', assemble_then_edit_reference)
    target = tmp_path / 'stale-live-export.docx'
    with pytest.raises(ValueError, match='changed'):
        controller.export_docx(snapshot, result, target)
    assert not target.exists()
    assert academic.get(reference.id).title == 'New reference saved during document assembly'


def test_saved_docx_review_is_controller_bound_and_cancellable(prepared, tmp_path):
    library, workspace, other, *_ = prepared
    controller, source = saved_document(prepared, tmp_path)
    inspected = controller.inspect_docx(source)
    for foreign in (CitationFormattingController(library, workspace),
                    CitationFormattingController(library, other)):
        with pytest.raises(ValueError, match='in this dialog'):
            foreign.regenerate_docx(inspected, tmp_path / 'foreign.docx')
    with pytest.raises(InterruptedError):
        controller.inspect_docx(source, cancelled=lambda: True)
    with pytest.raises(InterruptedError):
        controller.regenerate_docx(inspected, tmp_path / 'cancelled.docx', cancelled=lambda: True)
    assert not (tmp_path / 'foreign.docx').exists() and not (tmp_path / 'cancelled.docx').exists()


def held_native_task(prepared, tmp_path, monkeypatch, *, regenerate=False):
    """Execute the shipped handlers, holding only their native callback queue."""
    from carr_library.ui import citation_formatting

    queued, reports = [], []
    target = tmp_path / 'published-before-callback.docx'
    class Picker:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def ShowModal(self): return 1
        def GetPath(self): return str(target)
    wx = SimpleNamespace(Dialog=object, ID_OK=1, FD_SAVE=2,
        FileDialog=lambda *args, **kwargs: Picker(),
        CallAfter=lambda callback, *args: queued.append((callback, args)))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    CitationDialog, _ = citation_formatting._native_types()
    controller, source = saved_document(prepared, tmp_path)
    dialog_type = CitationDialog
    if regenerate:
        closures = dict(zip(CitationDialog.open_regeneration.__code__.co_freevars,
                           (cell.cell_contents for cell in CitationDialog.open_regeneration.__closure__)))
        dialog_type = closures['RegenerationDialog']
    dialog = object.__new__(dialog_type)
    library, workspace, other, *_ = prepared
    frame = SimpleNamespace(library=library, active=(workspace, None), _closing=False)
    dialog.controller, dialog._alive, dialog.pending = controller, True, False
    dialog.cancel_event = threading.Event()
    dialog.GetParent = lambda: frame
    dialog.report = lambda text, **kwargs: reports.append(text)
    dialog.update_controls = lambda: None
    class Executor:
        def submit(self, operation):
            future = Future()
            try:
                future.set_result(operation())
            except BaseException as error:
                future.set_exception(error)
            return future
    dialog.executor = Executor()
    if regenerate:
        dialog.document = controller.inspect_docx(source)
        dialog.style_path = SimpleNamespace(GetValue=lambda: '')
        invoke = dialog.regenerate
    else:
        dialog.snapshot = controller.load()
        dialog.result = controller.render(dialog.snapshot)
        dialog.document_title = SimpleNamespace(GetValue=lambda: 'Committed citation document')
        invoke = dialog.export
    return SimpleNamespace(dialog=dialog, invoke=invoke, queued=queued, reports=reports,
        target=target, frame=frame, workspace=workspace, other=other, library=library)


@pytest.mark.parametrize('regenerate', [False, True])
@pytest.mark.parametrize('late_change', ['cancel', 'scope'])
def test_real_docx_publication_remains_successful_before_held_ui_callback(
        prepared, tmp_path, monkeypatch, regenerate, late_change):
    h = held_native_task(prepared, tmp_path, monkeypatch, regenerate=regenerate)
    h.invoke()
    saved = read_citation_docx(h.target)
    assert saved['references'] and len(h.queued) == 1 and h.dialog.pending
    if late_change == 'cancel':
        h.dialog.cancel_event.set()
    else:
        h.library.archive_workspace(h.workspace)
        h.frame.active = (h.other, None)
    callback, args = h.queued.pop()
    callback(*args)
    assert not h.dialog.pending
    assert h.reports[-1].startswith('Saved ')
    assert str(h.target) in h.reports[-1]
    assert not any('did not complete' in text or text == 'Operation cancelled.' for text in h.reports)
    assert read_citation_docx(h.target) == saved


def test_nonpublished_inspection_does_not_cross_a_changed_ui_scope(prepared, tmp_path, monkeypatch):
    h = held_native_task(prepared, tmp_path, monkeypatch)
    admitted = []
    h.dialog.run('Loading references', h.dialog.controller.load, admitted.append)
    h.frame.active = (h.other, None)
    callback, args = h.queued.pop()
    callback(*args)
    assert admitted == []
    assert h.reports[-1].startswith('Operation did not complete.')
    assert not h.target.exists()


def test_metadata_preview_rechecks_access_after_serialization(prepared, tmp_path):
    library, workspace, *_ = prepared
    controller = CitationFormattingController(library, workspace)
    class Preview:
        def to_dict(self):
            library.archive_workspace(workspace)
            return {'doi': '10.1000/synthetic'}
    target = tmp_path / 'revoked-preview.json'
    with pytest.raises(ValueError, match='archived'):
        controller.save_doi_preview(Preview(), target)
    assert not target.exists()
