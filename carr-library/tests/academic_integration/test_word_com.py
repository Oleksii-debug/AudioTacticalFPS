"""Word COM protocol controls; modeled Office is explicitly not native evidence."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from carr_library.academic_integration import CitationError
from carr_library.academic_integration import word_com
from carr_library.academic_integration.word_com import (
    WordComBridge, WordUnavailable, WordUpdateIncomplete, _Calls)
from carr_library.academic_integration.word_fields import MAX_FIELD_BYTES

_spec = importlib.util.spec_from_file_location("_word_model_com",
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


def prepare(ready):
    values, app, bridge, service = ready
    app.document.move_end()
    context = service.capture()
    return service.preview(context, operation="insert", reference_ids=(values[4].id,))


def test_offline_import_and_non_windows_capture_do_not_require_office(monkeypatch):
    monkeypatch.setattr(word_com, "os", SimpleNamespace(name="posix"))
    bridge = WordComBridge()
    with pytest.raises(WordUnavailable, match="optional"):
        bridge.capture()
    assert bridge.close()


def test_optional_attachment_calls_only_get_active_object_and_never_starts_word(monkeypatch):
    calls = []
    com = ModuleType("comtypes")
    com.CoInitializeEx = lambda mode: calls.append(("initialize", mode))
    com.CoUninitialize = lambda: calls.append(("uninitialize",))
    client = ModuleType("comtypes.client")
    def existing(progid, *, dynamic):
        calls.append(("get-active", progid, dynamic))
        raise OSError("No current Word instance")
    client.GetActiveObject = existing
    monkeypatch.setitem(sys.modules, "comtypes", com)
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    monkeypatch.setattr(word_com, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(word_com, "_NativeCancellation", lambda: SimpleNamespace(
        enable=lambda: None, disable=lambda: None, request=lambda: None))
    bridge = WordComBridge()
    with pytest.raises(WordUnavailable, match="not launched"):
        bridge.capture()
    bridge.close()
    assert calls == [("initialize", 2), ("get-active", "Word.Application", True), ("uninitialize",)]


def test_read_only_inspection_never_reads_foreign_addin_data_or_evaluates_fields(ready):
    values, app, bridge, service = ready
    foreign = app.document.foreign_field()
    captured = service.capture()
    assert not captured.document.fields
    assert foreign.result == "foreign field result" and not app.document.mutations


@pytest.mark.parametrize("change", ["document", "filename", "window", "cursor"],
                         ids=["new-document-same-name", "save-as-name", "new-window", "moved-cursor"])
def test_document_and_selection_identity_are_rechecked_before_any_write(ready, change):
    values, app, bridge, service = ready
    plan = prepare(ready)
    old = app.document
    if change == "document":
        app.document = model.Document()
        app.document.FullName = old.FullName
    elif change == "filename":
        app.document.FullName = "Another document name.docx"
    elif change == "window":
        app.ActiveWindow.Hwnd += 1
    else:
        app.document.cursor = app.document.selection_end = 0
    with pytest.raises(CitationError, match="changed"):
        service.apply(plan)
    assert not old.mutations and not app.document.mutations


@pytest.mark.parametrize("attribute,value", [("ReadOnly", True), ("ProtectionType", 3), ("TrackRevisions", True)],
                         ids=["read-only", "protected", "track-changes"])
def test_unsupported_document_policies_fail_closed_without_changing_them(ready, attribute, value):
    values, app, bridge, service = ready
    setattr(app.document, attribute, value)
    with pytest.raises(CitationError):
        service.capture()
    assert getattr(app.document, attribute) == value
    assert not app.document.mutations


def test_noncollapsed_selection_is_not_replaced_by_insert(ready):
    values, app, bridge, service = ready
    app.document.cursor, app.document.selection_end = 0, 5
    context = service.capture()
    with pytest.raises(CitationError, match="collapsed cursor"):
        service.preview(context, operation="insert", reference_ids=(values[4].id,))
    assert not app.document.mutations


def test_cursor_inside_foreign_field_is_rejected_before_add(ready):
    values, app, bridge, service = ready
    foreign = app.document.foreign_field()
    app.document.cursor = app.document.selection_end = foreign.Result.Start
    context = service.capture()
    plan = service.preview(context, operation="insert", reference_ids=(values[4].id,))
    with pytest.raises(CitationError, match="outside existing fields"):
        service.apply(plan)
    assert foreign.result == "foreign field result" and not app.document.mutations


def test_manually_edited_owned_result_is_not_silently_overwritten(ready):
    values, app, bridge, service = ready
    first, _ = model.insert(service, app, values[4])
    native = bridge._fields[first.insert_id]
    native.result = "User's manual changed citation"
    count = len(app.document.mutations)
    with pytest.raises(CitationError, match="provenance changed"):
        service.capture()
    assert native.result == "User's manual changed citation"
    assert len(app.document.mutations) == count


def test_excessive_foreign_field_count_is_rejected_before_iteration(ready):
    values, app, bridge, service = ready
    original = app.document.main.range
    class TooMany(model.Range):
        @property
        def Fields(self):
            return SimpleNamespace(Count=word_com.MAX_NATIVE_FIELDS + 1)
    app.document.main.range = lambda: TooMany(app.document.main, 0, len(app.document.main.text()))
    with pytest.raises(CitationError, match="field count"):
        service.capture()
    assert not app.document.mutations
    app.document.main.range = original


def test_cancelled_initial_capture_never_connects_or_launches_word():
    calls = []
    bridge = WordComBridge(connect=lambda: calls.append("connect"),
                           identity=lambda value: value)
    with pytest.raises(InterruptedError):
        bridge.capture(cancelled=lambda: True)
    assert calls == []
    bridge.close()


def test_failed_field_write_is_reported_partial_and_plan_cannot_replay(ready):
    values, app, bridge, service = ready
    plan = prepare(ready)
    def stop(kind, value):
        if kind == "result":
            raise OSError("Synthetic exact Word Result write failure")
    app.document.before_mutation = stop
    with pytest.raises(WordUpdateIncomplete, match="No automatic replay"):
        service.apply(plan)
    assert [kind for kind, _ in app.document.mutations] == ["add"]
    app.document.before_mutation = lambda kind, value: None
    with pytest.raises(CitationError, match="expired"):
        service.apply(plan)
    with pytest.raises(CitationError, match="metadata"):
        service.capture()
    assert [kind for kind, _ in app.document.mutations] == ["add"]


def test_late_cancel_after_last_acknowledged_write_preserves_verified_receipt(ready):
    values, app, bridge, service = ready
    plan = prepare(ready)
    cancelled = threading.Event()
    app.document.before_mutation = lambda kind, value: cancelled.set() if kind == "data" else None
    receipt = service.apply(plan, cancelled=cancelled.is_set)
    assert cancelled.is_set() and receipt.token == plan.token
    assert receipt.field_ids == (plan.insert_id,)


def test_unrelated_edit_during_apply_is_retained_and_never_reported_as_exact_success(ready):
    values, app, bridge, service = ready
    plan = prepare(ready)
    def typing(kind, value):
        if kind == "data":
            app.document.main.parts.insert(0, "Concurrent user prose. ")
    app.document.before_mutation = typing
    with pytest.raises(WordUpdateIncomplete, match="unmanaged content"):
        service.apply(plan)
    assert app.document.main.text().startswith("Concurrent user prose. ")
    assert values[0].assets.resolve(values[-1]["asset"]).read_text() == "Exact immutable spectroscopy source."


def test_bridge_rejects_forged_oversized_field_plan_before_native_changes(ready):
    values, app, bridge, service = ready
    plan = prepare(ready)
    replacement = replace(plan.changes[0], data=" " * (MAX_FIELD_BYTES + 1))
    with pytest.raises(CitationError, match="metadata"):
        bridge.apply(replace(plan, changes=(replacement,)), guard=lambda: None)
    assert not app.document.mutations


def test_bridge_rejects_plan_that_omits_a_previously_owned_field(ready):
    values, app, bridge, service = ready
    model.insert(service, app, values[4])
    app.document.move_end()
    context = service.capture()
    plan = service.preview(context, operation="insert", reference_ids=(values[5].id,))
    count = len(app.document.mutations)
    new = tuple(row for row in plan.changes if row.id == plan.insert_id)
    with pytest.raises(CitationError, match="retarget"):
        bridge.apply(replace(plan, changes=new), guard=lambda: None)
    assert len(app.document.mutations) == count


def test_inflight_mutating_rpc_remains_owned_after_deadline_and_cancel():
    entered, release, done, cancelled = (threading.Event() for _ in range(4))
    state, failures, calls = {}, [], []
    native = SimpleNamespace(enable=lambda: calls.append("enable"),
        disable=lambda: calls.append("disable"), request=lambda: calls.append("cancel-rpc"))
    def worker():
        try:
            with _Calls(cancelled.is_set, timeout=.1, native=native) as bound:
                state["bound"] = bound
                def mutation():
                    entered.set()
                    assert release.wait(3), "test must release its exact simulated Word RPC"
                    return "acknowledged"
                assert bound.call(mutation, mutation=True) == "acknowledged"
                with pytest.raises(InterruptedError):
                    bound.call(lambda: failures.append("unapproved-next-write"), mutation=True)
        except BaseException as exc:
            failures.append(exc)
        finally:
            done.set()
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(2)
        cancelled.set()
        deadline = time.monotonic() + 2
        while not state["bound"].expired and time.monotonic() < deadline:
            done.wait(.01)
        assert state["bound"].expired
        assert not done.is_set() and thread.is_alive()
        assert "cancel-rpc" not in calls
        release.set()
        assert done.wait(2)
    finally:
        release.set()
        thread.join(timeout=3)
    assert failures == [] and calls == ["enable", "disable"]


def test_read_only_cancellation_requests_do_not_become_mutating_call_authority():
    entered, release, cancelled = (threading.Event() for _ in range(3))
    events, failures = [], []
    native = SimpleNamespace(enable=lambda: events.append("enable"),
        disable=lambda: events.append("disable"),
        request=lambda: (events.append("read-cancel"), release.set()))
    def worker():
        try:
            with _Calls(cancelled.is_set, timeout=2, native=native) as bound:
                def read():
                    entered.set()
                    assert release.wait(3)
                    return "read completed"
                bound.call(read)
                with pytest.raises(InterruptedError):
                    bound.call(lambda: events.append("mutation"), mutation=True)
        except BaseException as exc:
            failures.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(2)
        cancelled.set()
        thread.join(timeout=3)
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and not failures
    assert "read-cancel" in events and "mutation" not in events
    assert events[0] == "enable" and events[-1] == "disable"


def test_close_cannot_release_proxies_from_under_an_actual_active_worker():
    entered, release, finished = (threading.Event() for _ in range(3))
    app = model.App()
    class SlowApp(model.App):
        @property
        def ActiveDocument(self):
            entered.set()
            assert release.wait(3)
            return super().ActiveDocument
    app = SlowApp()
    bridge = WordComBridge(connect=lambda: app, identity=lambda doc: doc.identity)
    failures = []
    def worker():
        try:
            bridge.capture()
            bridge.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="still owns"):
            bridge.close()
        assert bridge._app is app and not bridge._closed
        release.set()
        assert finished.wait(2)
    finally:
        release.set()
        thread.join(timeout=3)
    assert not failures and bridge._closed


def test_signed_native_window_value_is_an_identity_not_a_fallback(ready):
    values, app, bridge, service = ready
    app.ActiveWindow.Hwnd = -2147483640
    context = service.capture()
    plan = service.preview(context, operation="insert", reference_ids=(values[4].id,))
    app.ActiveWindow.Hwnd = -2147483639
    with pytest.raises(CitationError, match="changed"):
        service.apply(plan)
    assert not app.document.mutations

@pytest.mark.parametrize("value,disabling,accepted", [
    (0, False, True), (0, True, True), (0x80010140, True, True),
    (0x80010140, False, False), (0x80004005, True, False),
], ids=["enable-ok", "disable-ok", "nested-owner-still-enabled", "enable-refused", "disable-failed"])
def test_documented_native_cancellation_hresult_is_not_misclassified(value, disabling, accepted):
    if accepted:
        word_com._NativeCancellation._result(value, disabling=disabling)
    else:
        with pytest.raises(OSError, match="HRESULT"):
            word_com._NativeCancellation._result(value, disabling=disabling)


def test_final_verified_read_borrows_balanced_cancellation_owner():
    events = []
    native = SimpleNamespace(enable=lambda: events.append("enable"),
                             disable=lambda: events.append("disable"),
                             request=lambda: events.append("request"))
    with _Calls(lambda: False, native=native) as outer:
        with _Calls(lambda: False, native=outer.native, manage_native=False) as verification:
            assert verification.call(lambda: "exact verified outcome") == "exact verified outcome"
    assert events == ["enable", "disable"]


def test_endnote_aggregate_is_checked_before_unbounded_native_text_read(ready):
    values, app, bridge, service = ready
    note = app.document.foreign_note()
    class Oversized(model.Range):
        @property
        def End(self):
            return word_com.MAX_DOCUMENT_CHARS + 1
        @property
        def Text(self):
            raise AssertionError("The oversized native note must not be read.")
    note.body.range = lambda: Oversized(note.body, 0, 1)
    with pytest.raises(CitationError, match="aggregate text"):
        service.capture()
    assert not app.document.mutations

def test_source_revocation_between_result_and_data_blocks_the_next_native_write(ready):
    values, app, bridge, service = ready
    plan = prepare(ready)
    def revoke(kind, value):
        if kind == "result":
            values[0].archive_workspace(values[1])
    app.document.before_mutation = revoke
    with pytest.raises(WordUpdateIncomplete):
        service.apply(plan)
    assert [kind for kind, _ in app.document.mutations] == ["add", "result"]


def test_active_document_change_between_result_and_data_never_targets_replacement(ready):
    values, app, bridge, service = ready
    plan = prepare(ready)
    original = app.document
    replacement = model.Document()
    def switch(kind, value):
        if kind == "result":
            app.document = replacement
    original.before_mutation = switch
    with pytest.raises(WordUpdateIncomplete, match="active document changed"):
        service.apply(plan)
    assert [kind for kind, _ in original.mutations] == ["add", "result"]
    assert not replacement.mutations
