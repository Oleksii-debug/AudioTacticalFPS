"""Real scoped SQLite/CSL with modeled Word, preserving source and receipt guards."""
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library.academic_integration import CitationError
from carr_library.academic_integration.word_fields import (
    decode_field, encoded, field_code, code_identity, WordField, WordCitationService)
from carr_library.academic_integration.word_com import WordComBridge
from carr_library.application import LibraryApp
from carr_library.library_backup import verify_backup, restore_backup
from carr_library.ui.citation_formatting import CitationFormattingController

_spec = importlib.util.spec_from_file_location("_word_model_fields",
    Path(__file__).resolve().parents[1] / "fixtures" / "word_model.py")
model = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = model
_spec.loader.exec_module(model)


@pytest.fixture
def ready(tmp_path):
    values, app, bridge, service = model.service_fixture(tmp_path)
    try:
        yield values, app, bridge, service
    finally:
        service.close()


def test_real_csl_insert_edit_and_bibliography_preserve_originals_and_foreign_fields(ready):
    values, app, bridge, service = ready
    library, workspace, other, academic, reference, second, source = values
    foreign = app.document.foreign_field()
    note = app.document.foreign_note()
    source_file = library.assets.resolve(source["asset"])
    original_bytes = source_file.read_bytes()
    hit, = library.search(workspace, "spectroscopy")
    quotation = library.copy(workspace, hit.ref, hit.revision, "quote")
    first, receipt = model.insert(service, app, reference)
    stable = first.insert_id
    assert receipt.field_ids == (stable,)
    assert "PRIVATE" not in first.preview and "Excluded foreign" not in first.preview
    before = service.capture()
    plan = service.preview(before, operation="edit", target_id=stable,
        reference_ids=(reference.id,), locator="12", locator_verified=True)
    assert plan.insert_id is None and plan.changes[0].id == stable
    assert "12" in plan.changes[0].text
    service.apply(plan)
    app.document.move_end()
    context = service.capture()
    plan = service.preview(context, operation="bibliography",
        reference_ids=(reference.id, second.id), review_existing_locators=True)
    service.apply(plan)
    current = service.capture()
    assert {row.kind for row in current.document.fields} == {"citation", "bibliography"}
    assert stable in {row.id for row in current.document.fields}
    assert foreign.result == "foreign field result" and foreign.code == " DATE "
    assert note.Range.Text == "Foreign endnote; never rewrite.\r"
    assert source_file.read_bytes() == original_bytes
    assert academic.get(reference.id) == reference
    assert library.copy(workspace, hit.ref, hit.revision, "quote") == quotation
    assert not any(word in first.preview for word in ("PRIVATE NOTE", "PRIVATE ABSTRACT"))


def test_publication_extent_is_not_promoted_to_a_citation_locator(ready):
    values, app, bridge, service = ready
    plan, _ = model.insert(service, app, values[4])
    record = json.loads(plan.changes[0].data)
    assert "locator" not in record["cluster"]["items"][0]
    assert record["cluster"]["locator_evidence"][0]["locator_verified"] is None


@pytest.mark.parametrize("verified", [False, 0, 1, "yes", None], ids=["false", "zero", "one", "text", "none"])
def test_pinpoint_requires_exact_new_source_review_before_word_writes(ready, verified):
    values, app, bridge, service = ready
    context = service.capture()
    before = list(app.document.mutations)
    with pytest.raises((ValueError, CitationError)):
        service.preview(context, operation="insert", reference_ids=(values[4].id,),
                        locator="7", locator_verified=verified)
    assert app.document.mutations == before


def test_stored_locator_flag_is_historical_not_new_attachment_authority(ready):
    values, app, bridge, service = ready
    model.insert(service, app, values[4], locator="12", verified=True)
    context = service.capture()
    before = len(app.document.mutations)
    with pytest.raises(CitationError, match="historical"):
        service.preview(context, operation="refresh")
    assert len(app.document.mutations) == before
    plan = service.preview(context, operation="refresh", review_existing_locators=True)
    assert service.apply(plan).token == plan.token


def test_changed_linked_source_rejects_retained_locators_even_after_bulk_review(ready):
    values, app, bridge, service = ready
    library, workspace, _, academic, reference, second, source = values
    model.insert(service, app, reference, locator="12", verified=True)
    changed = library.add_text(workspace, "Other exact source", "Different immutable source and pagination.")
    academic.update(reference, {"edition_id": changed["edition"]})
    context = service.capture()
    before = len(app.document.mutations)
    with pytest.raises(CitationError, match="cited source changed"):
        service.preview(context, operation="refresh", review_existing_locators=True)
    assert len(app.document.mutations) == before


def test_new_explicit_edit_can_replace_changed_locator_without_changing_stable_id(ready):
    values, app, bridge, service = ready
    library, workspace, _, academic, reference, second, source = values
    first, _ = model.insert(service, app, reference, locator="12", verified=True)
    academic.update(reference, {"title": "Explicitly reviewed new metadata"})
    context = service.capture()
    plan = service.preview(context, operation="edit", target_id=first.insert_id,
        reference_ids=(reference.id,), locator="15", locator_verified=True)
    service.apply(plan)
    current = service.capture()
    assert len(current.document.fields) == 1
    assert current.document.fields[0].id == first.insert_id
    assert decode_field(current.document.fields[0])["cluster"]["items"][0]["locator"] == "15"


def test_native_endnote_indices_include_untouched_foreign_notes(ready):
    values, app, bridge, service = ready
    foreign = app.document.foreign_note("Foreign endnote exact text.")
    plan, receipt = model.insert(service, app, values[4], style="chicago-notes")
    assert plan.note_index == 2
    captured = service.capture()
    owned, = captured.document.fields
    assert owned.placement == "endnote" and owned.note_index == 2
    assert decode_field(owned)["cluster"]["note_index"] == 2
    assert foreign.Range.Text == "Foreign endnote exact text.\r"
    assert app.document.Endnotes.Count == 2
    edited = service.preview(captured, operation="edit", target_id=owned.id,
        reference_ids=(values[4].id,), locator="8", locator_verified=True, style="chicago-notes")
    service.apply(edited)
    assert app.document.Endnotes.Count == 2
    assert foreign.Range.Text == "Foreign endnote exact text.\r"


def test_inserting_an_earlier_owned_note_recalculates_existing_note_indices(ready):
    values, app, bridge, service = ready
    first, _ = model.insert(service, app, values[4], style="chicago-notes")
    app.document.cursor = app.document.selection_end = 0
    context = service.capture()
    next_plan = service.preview(context, operation="insert", reference_ids=(values[5].id,),
                                style="chicago-notes")
    service.apply(next_plan)
    rows = service.capture().document.fields
    assert [(row.id, row.note_index) for row in rows] == [(next_plan.insert_id, 1), (first.insert_id, 2)]
    assert all(decode_field(row)["cluster"]["note_index"] == row.note_index for row in rows)


@pytest.mark.parametrize("attribute,value", [("NumberingRule", 1), ("StartingNumber", 4), ("NumberStyle", 1)],
                         ids=["restart", "start-four", "roman"])
def test_unsupported_native_numbering_is_rejected_without_policy_changes(ready, attribute, value):
    values, app, bridge, service = ready
    setattr(app.document.Endnotes, attribute, value)
    context = service.capture()
    with pytest.raises(CitationError, match="continuous Arabic"):
        service.preview(context, operation="insert", reference_ids=(values[4].id,), style="chicago-notes")
    assert getattr(app.document.Endnotes, attribute) == value
    assert not app.document.mutations


def test_style_switch_cannot_silently_move_inline_citations_into_notes(ready):
    values, app, bridge, service = ready
    model.insert(service, app, values[4])
    context = service.capture()
    before = len(app.document.mutations)
    with pytest.raises(CitationError, match="placement"):
        service.preview(context, operation="refresh", style="chicago-notes")
    assert len(app.document.mutations) == before and app.document.Endnotes.Count == 0


def test_reference_change_during_real_render_invalidates_preview_without_word_write(ready, monkeypatch):
    values, app, bridge, service = ready
    context = service.capture()
    actual = service.controller.render
    def changing(*args, **kwargs):
        result = actual(*args, **kwargs)
        values[3].update(values[4], {"title": "Changed after actual CSL"})
        return result
    monkeypatch.setattr(service.controller, "render", changing)
    with pytest.raises(ValueError, match="changed"):
        service.preview(context, operation="insert", reference_ids=(values[4].id,))
    assert not app.document.mutations and service._plan is None


def test_library_archive_between_preview_and_apply_blocks_remote_mutation(ready):
    values, app, bridge, service = ready
    context = service.capture()
    plan = service.preview(context, operation="insert", reference_ids=(values[4].id,))
    values[0].archive_workspace(values[1])
    with pytest.raises(ValueError):
        service.apply(plan)
    assert not app.document.mutations


def test_foreign_controller_context_and_forged_plan_are_not_reusable(ready):
    values, app, bridge, service = ready
    context = service.capture()
    plan = service.preview(context, operation="insert", reference_ids=(values[4].id,))
    with pytest.raises(CitationError, match="expired"):
        service.apply(replace(plan, token=plan.token))
    foreign = WordCitationService(CitationFormattingController(values[0], values[2]), bridge)
    with pytest.raises(CitationError, match="exact Word document"):
        foreign.preview(context, operation="insert", reference_ids=(values[4].id,))
    assert not app.document.mutations


def test_same_reviewed_plan_cannot_replay_after_success_or_partial_error(ready):
    values, app, bridge, service = ready
    context = service.capture()
    plan = service.preview(context, operation="insert", reference_ids=(values[4].id,))
    service.apply(plan)
    count = len(app.document.mutations)
    with pytest.raises(CitationError, match="expired"):
        service.apply(plan)
    assert len(app.document.mutations) == count


def test_complete_backup_restore_preserves_sources_but_never_supplies_word_locator_approval(ready, tmp_path):
    values, app, bridge, service = ready
    library, workspace, _, academic, reference, second, source = values
    model.insert(service, app, reference, locator="12", verified=True)
    backup = tmp_path / "complete-backup"
    seal = library.backup(backup)
    verify_backup(backup, seal)
    restored_root = restore_backup(backup, tmp_path / "restored", seal)
    restored = LibraryApp(restored_root)
    assert restored.assets.resolve(source["asset"]).read_bytes() == library.assets.resolve(source["asset"]).read_bytes()
    current = WordCitationService(CitationFormattingController(restored, workspace),
        WordComBridge(connect=lambda: app, identity=lambda doc: doc.identity))
    try:
        captured = current.capture()
        with pytest.raises(CitationError, match="historical"):
            current.preview(captured, operation="refresh")
    finally:
        current.close()
    assert not any("word" in path.name.casefold() for path in library.root.rglob("*") if path.is_file())


@pytest.mark.parametrize("kind", ["text-hash", "workspace-bool", "upper-id", "cluster-id",
                                  "verified-number", "extra-field", "duplicate-ref", "wrong-story"],
                         ids=["result-hash", "workspace-bool", "upper-id", "cluster-id",
                              "numeric-review", "unknown-key", "duplicate-ref", "wrong-story"])
def test_strict_field_metadata_rejects_resigned_inconsistent_records(ready, kind):
    values, app, bridge, service = ready
    model.insert(service, app, values[4], locator="12", verified=True)
    row, = service.capture().document.fields
    record = json.loads(row.data)
    if kind == "text-hash":
        record["text_sha256"] = "0" * 64
    elif kind == "workspace-bool":
        record["workspace"] = True
    elif kind == "upper-id":
        record["id"] = "A" * 32
    elif kind == "cluster-id":
        record["cluster"]["id"] = "other"
    elif kind == "verified-number":
        record["cluster"]["locator_evidence"][0]["locator_verified"] = 1
    elif kind == "extra-field":
        record["authority"] = True
    elif kind == "duplicate-ref":
        record["reference_ids"].append(record["reference_ids"][0])
    else:
        record["placement"] = "endnote"
    with pytest.raises(CitationError):
        decode_field(replace(row, data=encoded(record)))


@pytest.mark.parametrize("code", ["ADDIN CARR_CITATION_bad", "ADDIN CARR_CITATION_" + "A" * 32,
                                  "HYPERLINK CARR_CITATION_" + "1" * 32])
def test_malformed_owned_code_is_not_treated_as_an_editable_foreign_field(code):
    with pytest.raises(CitationError):
        code_identity(code)
    assert code_identity(" DATE ") is None
