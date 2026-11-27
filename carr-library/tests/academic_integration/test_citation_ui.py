"""Headless native-action controllers against real SQLite, canonical QuickJS CSL and DOCX."""
from dataclasses import replace
import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.academic_integration import CitationCluster, CitationRequest, CitationError, read_citation_docx
from carr_library.ui.academic_library_dialog import AcademicController
from carr_library.ui.citation_formatting import (
    CitationFormattingController, _scope, _guard, formatted_preview_text,
    reconcile_citations,
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


def test_canonical_doi_preview_never_imports_or_grants_source_verification(prepared):
    from carr_library.acquisition.service import AcquisitionService
    library, workspace, _, academic, reference, _ = prepared
    calls = []
    body = json.dumps({'status': 'ok', 'message-type': 'work', 'message': {
        'DOI': '10.5555/preview', 'type': 'journal-article', 'title': ['External metadata'],
        'author': [{'given': 'Jane', 'family': 'Example'}], 'issued': {'date-parts': [[2026]]}}}).encode()
    def factory(permission):
        assert permission.allowed is True
        def transport(request, *, cancelled):
            calls.append(request)
            return body
        return transport
    service = AcquisitionService(library, transport_factory=factory)
    with pytest.raises(PermissionError):
        service.lookup(workspace, '10.5555/preview')
    assert not calls
    preview = service.lookup(workspace, '10.5555/preview', online_allowed=True)
    assert preview.metadata.title == 'External metadata'
    assert not preview.sources and len(calls) == 1
    assert service._provenance(preview)['source_text_verified'] is False
    assert academic.list() == (reference,)
    library.archive_workspace(workspace)
    with pytest.raises((ValueError, PermissionError)):
        service.save_reference(workspace, preview)


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


@pytest.mark.parametrize('phase', ['pending', 'queued'])
def test_document_dialog_parent_destroy_detaches_late_callbacks(monkeypatch, phase):
    import sys
    from concurrent.futures import Future
    from carr_library.ui import citation_formatting as ui

    queued, shutdowns = [], []
    class Window:
        def __init__(self, *args, **kwargs):
            self.bindings = {}
        def GetId(self):
            return 701
        def Bind(self, event, callback):
            self.bindings[event] = callback
        def GetParent(self):
            raise AssertionError('A destroyed dialog must not query its native parent')
    native = SimpleNamespace(Dialog=Window, EVT_CLOSE='close', EVT_WINDOW_DESTROY='destroy',
                             CallAfter=lambda callback, future: queued.append((callback, future)))
    monkeypatch.setitem(sys.modules, 'wx', native)
    future = Future()
    class Executor:
        def submit(self, operation):
            return future
        def shutdown(self, **kwargs):
            shutdowns.append(kwargs)
    monkeypatch.setattr(ui, 'ThreadPoolExecutor', lambda **kwargs: Executor())
    task_type = ui._native_types().__mro__[1]
    dialog = task_type()
    dialog.initialize_tasks(SimpleNamespace(library=object(), workspace='workspace'))
    controls = []
    dialog.update_controls = lambda: controls.append('update')
    dialog.report = lambda *args, **kwargs: controls.append('report')
    dialog.run('Pending format', lambda: 'result',
               lambda result: pytest.fail('Destroyed dialog cannot deliver a result'))
    if phase == 'queued':
        future.set_result('result')
        assert len(queued) == 1
    before = list(controls)
    event = SimpleNamespace(GetId=lambda: 701, Skip=lambda: None,
        GetEventObject=lambda: pytest.fail('Do not query a dying wx wrapper'))
    dialog.on_destroy(event)
    assert dialog._destroyed and not dialog._alive and dialog.cancel_event.is_set()
    assert shutdowns == [{'wait': False, 'cancel_futures': True}]
    if phase == 'pending':
        future.set_result('result')
        assert queued == []
    for callback, result in queued:
        callback(result)
    assert controls == before
