"""Real canonical catalog/controller checks; inventory fixtures are metadata only."""
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.audio.engines import Voice, VoiceRegistry
from carr_library.audio import engines
from carr_library.application import LibraryApp
from carr_library.ui.language_matrix import (
    CAPABILITY_LABELS, InstalledVoiceInventory, LanguageMatrixController,
    collect_installed_voices, request_voice_inventory,
)


def test_initial_view_and_filters_never_discover_voices(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Initial catalog rendering must not discover a local voice.")

    monkeypatch.setattr(engines, "default_registry", forbidden)
    controller = LanguageMatrixController()
    assert len(controller.filter()) == 135
    assert len(controller.filter(region="europe")) == 37
    assert [row.language.label_uk for row in controller.filter("УКРАЇН")] == ["Українська"]
    assert len(controller.filter(capability="tts", status="unsupported")) == 135
    assert controller.filter(status="verified") == ()
    assert controller.inventory is None
    summary = controller.summary()
    assert "Canonical selected entries: 135" in summary
    assert "125 resource entries" in summary
    assert "acoustic tests reported there: 0" in summary
    assert "Qualified language entries: 0" in summary
    assert "Not enumerated" in summary


def test_all_22_capabilities_full_source_labels_and_geography_are_readable():
    controller = LanguageMatrixController()
    selected, = controller.filter("Китайська — стандартна мандаринська")
    detail = controller.details(selected)
    assert selected.language.label_uk in detail
    assert len(selected.capabilities) == len(CAPABILITY_LABELS) == 22
    assert all(label in detail for label in CAPABILITY_LABELS.values())
    assert "Canonical locale: not supplied" in detail
    assert "Canonical voice ID: not supplied" in detail
    assert controller.catalog.source.url in detail
    assert all(association.source_text in detail for association in selected.language.associations)
    assert all(country in detail for country in selected.language.countries)


def test_filter_preserves_canonical_order_and_preferred_selection_when_hidden():
    controller = LanguageMatrixController()
    ukrainian, = controller.filter("Українська")
    controller.select(ukrainian.language.id)
    all_rows = controller.filter()
    assert all_rows[controller.selection_index(all_rows)].language.id == ukrainian.language.id
    african = controller.filter(region="africa")
    assert [row.language.order for row in african] == sorted(row.language.order for row in african)
    assert controller.selection_index(african) == 0
    assert controller.selected_id == ukrainian.language.id
    assert controller.selection_index(()) == -1
    restored = controller.filter()
    assert restored[controller.selection_index(restored)].language.id == ukrainian.language.id


def synthetic_inventory():
    # Declared metadata for boundary tests, not synthetic pronunciation evidence.
    return InstalledVoiceInventory((
        Voice("fixture-uk", "Українська", ("uk-UA",), "metadata-fixture", license="Synthetic metadata fixture"),
        Voice("fixture-en", "Англійська", ("en",), "metadata-fixture", license="Synthetic metadata fixture"),
    ), ("A configured adapter was unavailable.",), "2026-11-26T00:00:00+00:00")


def test_inventory_counts_stay_separate_and_never_infer_language_bindings():
    controller = LanguageMatrixController()
    before = [row.language.id for row in controller.filter()]
    inventory = synthetic_inventory()
    controller.set_inventory(inventory)
    assert [row.language.id for row in controller.filter()] == before
    values = controller.matrix.summary()
    assert values["selected_entries"] == 135 and values["qualified_entries"] == 0
    assert values["entries_with_declared_voice_matches"] == values["distinct_matched_voices"] == 0
    assert values["unmatched_voices"] == 2
    assert len(controller.filter(capability="tts", status="unsupported")) == 135
    text = controller.inventory_text()
    assert "Enumerated voices: 2" in text and "uk-UA" in text
    assert "No automatic language bindings" in text and inventory.notices[0] in text
    assert "Voices enumerated on this installation: 2" in controller.summary()
    with pytest.raises(FrozenInstanceError):
        inventory.enumerated_at = "changed"


def test_explicit_refresh_schedules_configured_registry_on_existing_frame_worker(tmp_path, monkeypatch):
    calls = []
    voices = synthetic_inventory().voices

    def loader():
        calls.append("default registry")
        adapter = SimpleNamespace(id="metadata-fixture", voices=lambda: voices)
        return VoiceRegistry((adapter,), notices=[{"engine": "metadata-fixture", "available": False, "reason": "Configured adapter notice"}])

    monkeypatch.setattr(engines, "default_registry", loader)
    jobs = []
    frame = SimpleNamespace(library=LibraryApp(tmp_path / "chosen-library"), cancel_event=Event(),
        working=False, busy=False, _closing=False,
        run_job=lambda title, operation, done, failed: jobs.append((title, operation, done, failed)))
    controller = LanguageMatrixController()
    completed, errors = [], []
    assert not calls and not jobs
    request_voice_inventory(frame, controller, completed=completed.append, failed=errors.append)
    assert not calls and len(jobs) == 1  # It has only been scheduled, not executed on the GUI thread.
    _, operation, done, failed = jobs[0]
    inventory = operation()
    assert calls == ["default registry"]
    assert controller.inventory is None
    done(inventory)
    assert completed == [inventory] and not errors
    assert controller.inventory == inventory and controller.matrix.summary()["qualified_entries"] == 0


def test_cancelled_enumeration_retains_previous_inventory(tmp_path, monkeypatch):
    cancel = Event()
    calls = []

    def loader():
        calls.append("default registry")
        def enumerate_voices():
            cancel.set()
            return synthetic_inventory().voices
        return VoiceRegistry((SimpleNamespace(id="metadata-fixture", voices=enumerate_voices),))

    monkeypatch.setattr(engines, "default_registry", loader)
    controller = LanguageMatrixController()
    previous = synthetic_inventory()
    controller.set_inventory(previous)
    library = SimpleNamespace(root=tmp_path / "stateful-library")
    with pytest.raises(InterruptedError):
        collect_installed_voices(library, cancelled=cancel.is_set)
    assert len(calls) == 1 and controller.inventory is previous
    with pytest.raises(InterruptedError):
        collect_installed_voices(library, cancelled=cancel.is_set)
    assert len(calls) == 1


def test_changed_library_or_busy_frame_cannot_apply_an_old_inventory(tmp_path):
    jobs = []
    frame = SimpleNamespace(library=LibraryApp(tmp_path / "first"), cancel_event=Event(),
        working=True, busy=False, _closing=False,
        run_job=lambda title, operation, done, failed: jobs.append((operation, done)))
    controller = LanguageMatrixController()
    with pytest.raises(ValueError, match="current operation"):
        request_voice_inventory(frame, controller, completed=lambda result: None, failed=lambda error: None)
    assert not jobs
    frame.working = False
    request_voice_inventory(frame, controller, completed=lambda result: None, failed=lambda error: None)
    frame.library = LibraryApp(tmp_path / "second")
    with pytest.raises(InterruptedError, match="changed"):
        jobs[0][1](synthetic_inventory())
    assert controller.inventory is None
