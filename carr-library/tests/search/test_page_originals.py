"""Current-page original reuse; actual files, public import and complete-stream proofs."""
from hashlib import sha256
import os

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog import intake
from carr_library.citations import text_attestation
from carr_library.ingest.resource_budget import ImportBudget, MIB
from carr_library.search import originals
from carr_library.search.paging import SearchResources


def observed_proofs(monkeypatch):
    created, closed = [], []
    init, close = originals.PageOriginals.__init__, originals.PageOriginals.close

    def start(self, *args, **options):
        init(self, *args, **options)
        created.append(self)

    def finish(self):
        closed.append({
            "objects": len(self._originals), "bytes": self.bytes,
            "planned": self._wanted_count,
            "digests": sum(len(item.digests or {}) for item in self._originals.values()),
            "sealed": tuple(item.segment_count for item in self._originals.values()
                            if item.segment_count is not None),
        })
        close(self)

    monkeypatch.setattr(originals.PageOriginals, "__init__", start)
    monkeypatch.setattr(originals.PageOriginals, "close", finish)
    return created, closed


def assert_released(created, closed):
    assert created and len(created) == len(closed)
    assert all(item.closed and item.bytes == 0 and not item._originals
               and not item._wanted and item._wanted_count == 0 for item in created)
    assert all(row["objects"] <= 101 and row["planned"] <= 101
               and row["digests"] <= 101 and row["bytes"] <= 64*MIB for row in closed)


def test_same_page_captures_one_actual_original_and_each_public_call_is_fresh(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / "library")
    workspace = app.create_workspace("One operation owns its original proof")
    raw = "\f".join("Needle α cafe\u0301 source %03d.\r\n" % index for index in range(105))
    imported = app.add_text(workspace, "Exact shared original", raw)
    source = app.assets.resolve(imported["asset"])
    before = source.read_bytes()
    actual = LibraryApp._asset_source
    captures = []

    def capture(self, *args, **options):
        value = actual(self, *args, **options)
        captures.append(value)
        return value

    created, closed = observed_proofs(monkeypatch)
    monkeypatch.setattr(LibraryApp, "_asset_source", capture)
    try:
        first = app.search_page(workspace, "Needle", limit=37)
        assert len(first.results) == 37 and first.has_more_results
        assert all(item.verified and item.quote == "Needle" for item in first.results)
        assert len(captures) == 1 and type(captures[0]) is bytes and captures[0] == before
        assert closed[-1]["bytes"] == len(before) and closed[-1]["planned"] == 38
        second = app.search_page(workspace, "Needle", cursor=first.cursor, limit=37, offset=37)
        assert len(second.results) == 37 and second.has_more_results and len(captures) == 2
        assert {item.page for item in first.results}.isdisjoint(item.page for item in second.results)
        count = len(captures)
        hit = second.results[0]
        assert app.copy(workspace, hit.ref, hit.revision, "quote") == "Needle"
        assert len(captures) > count  # The evidence action owns a fresh full source proof.
        assert_released(created, closed)
        assert source.read_bytes() == before
    finally:
        assert app.close_search()


def test_capture_memory_limit_keeps_real_over_budget_resolver_and_final_original_checks(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / "library")
    workspace = app.create_workspace("Bounded uncached original")
    # Each selected segment fits the 16 MiB/32 character window. The whole
    # original exceeds memory/8, so no page capture can retain its complete bytes.
    raw = "Needle.\fNeedle.\f" + "\f".join("x" * (128*1024) for _ in range(18))
    imported = app.add_text(workspace, "Uncached retained original", raw)
    source = app.assets.resolve(imported["asset"])
    before = source.read_bytes()
    actual = LibraryApp._asset_source
    calls = []

    def capture(self, *args, **options):
        value = actual(self, *args, **options)
        calls.append(type(value))
        return value

    created, closed = observed_proofs(monkeypatch)
    monkeypatch.setattr(LibraryApp, "_asset_source", capture)
    try:
        page = app.search_page(workspace, "Needle", limit=1,
                               resources=SearchResources(memory_bytes=16*MIB))
        assert len(page.results) == 1 and page.has_more_results and page.results[0].verified
        assert calls == [bytes, bytes]
        assert closed[-1]["bytes"] == 0
        assert app.copy(workspace, page.results[0].ref, page.results[0].revision, "quote") == "Needle"
        assert source.read_bytes() == before
        assert_released(created, closed)
    finally:
        assert app.close_search()


@pytest.fixture(scope="module")
def streamed_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("page-complete-stream")
    app = LibraryApp(root / "library")
    workspace = app.create_workspace("Actual source beyond the old 1000-segment boundary")
    records = tuple("Needle α exact source %04d.\r\n" % index for index in range(1001))
    raw = b"\xef\xbb\xbf" + "\f".join(records).encode("utf-8")
    external = root / "original.txt"
    external.write_bytes(raw)
    policy = ImportBudget(source_bytes=2*MIB, segment_bytes=MIB, segments=1001,
        steps=1002, seconds=120, memory_bytes=512*MIB, library_bytes=64*MIB,
        temporary_bytes=128*MIB)
    result = app.import_file(workspace, external, title="Real streamed source",
                             import_budget=policy)
    assert result["state"] == "complete" and result["completed"] == 1001, result
    assert result["isolated"] and result["verified"]
    path = app.assets.resolve(sha256(raw).hexdigest())
    assert path.read_bytes() == raw
    yield app, workspace, path, raw
    assert app.close_search()
    assert path.read_bytes() == raw


def observe_real_streams(monkeypatch):
    real = originals.text_segments
    streams = []

    def observed(source, **options):
        record = {"source": source, "yielded": 0, "complete": False}
        streams.append(record)
        for segment in real(source, **options):
            record["yielded"] += 1
            yield segment
        record["complete"] = True

    monkeypatch.setattr(originals, "text_segments", observed)
    return streams


def test_one_complete_stream_seals_only_requested_page_digests(streamed_source, monkeypatch):
    app, workspace, path, raw = streamed_source
    created, closed = observed_proofs(monkeypatch)
    streams = observe_real_streams(monkeypatch)
    actual = LibraryApp._asset_source
    captures = []

    def capture(self, *args, **options):
        value = actual(self, *args, **options)
        captures.append(value)
        return value

    monkeypatch.setattr(LibraryApp, "_asset_source", capture)
    try:
        page = app.search_page(workspace, "Needle", limit=100, timeout=120)
        assert len(page.results) == 100 and all(item.verified for item in page.results)
        assert not page.has_more_results and page.next_source_cursor is not None
        assert len(captures) == 1 and type(captures[0]) is intake.FileSnapshot
        assert len(streams) == 1 and streams[0]["yielded"] == 1001 and streams[0]["complete"]
        assert closed[-1] == {"objects": 1, "bytes": 0, "planned": 100,
                              "digests": 100, "sealed": (1001,)}
        first = page.results[0]
        before = len(captures)
        assert app.copy(workspace, first.ref, first.revision, "quote") == "Needle"
        assert len(captures) > before
        assert path.read_bytes() == raw
        assert_released(created, closed)
    finally:
        assert app.close_search()


def test_new_lookahead_ordinal_requires_another_complete_stream_and_no_cross_call_reuse(
        streamed_source, monkeypatch):
    app, workspace, path, raw = streamed_source
    created, closed = observed_proofs(monkeypatch)
    streams = observe_real_streams(monkeypatch)
    try:
        first = app.search_page(workspace, "Needle", limit=37, timeout=120)
        assert len(first.results) == 37 and first.has_more_results
        assert len(streams) == 2 and all(row["complete"] and row["yielded"] == 1001 for row in streams)
        assert closed[-1]["digests"] == closed[-1]["planned"] == 38
        second = app.search_page(workspace, "Needle", cursor=first.cursor,
                                 limit=37, offset=37, timeout=120)
        assert len(second.results) == 37 and second.has_more_results
        assert len(streams) == 4 and all(row["complete"] and row["yielded"] == 1001 for row in streams)
        assert all(item.verified for item in (*first.results, *second.results))
        assert {row.page for row in first.results}.isdisjoint(row.page for row in second.results)
        assert path.read_bytes() == raw
        assert_released(created, closed)
    finally:
        assert app.close_search()


@pytest.mark.parametrize("damage", ["late_utf8", "extra_segment", "late_hash"])
def test_real_unselected_tail_failure_never_seals_a_partial_digest_table(
        streamed_source, monkeypatch, damage):
    app, workspace, path, raw = streamed_source
    previous = app.search_page(workspace, "Needle", limit=1, timeout=120)
    selected = dict(app._selections)
    created, closed = observed_proofs(monkeypatch)
    actual_stream, actual_stamp = originals.text_segments, intake._stamp
    events = []
    captured_stamp = None

    def stable_observation(info):
        value = actual_stamp(info)
        # Disclose only stable observed timestamps, as in the existing complete-
        # stream hash tests. Real inode, size, mode, links and all bytes remain.
        if captured_stamp is not None and value[:2] == captured_stamp[:2]:
            return (*value[:5], *captured_stamp[5:7], *value[7:])
        return value

    def damaged_stream(source, **options):
        nonlocal captured_stamp
        captured_stamp = source.identity
        data = bytearray(raw)
        position = len(data) - 4
        data[position] = 0xff if damage == "late_utf8" else (12 if damage == "extra_segment" else ord("X"))
        assert data != raw and len(data) == len(raw)
        before = path.stat()
        with path.open("r+b") as output:
            output.seek(position)
            output.write(data[position:position + 1])
            output.flush()
            os.fsync(output.fileno())
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        try:
            for index, segment in enumerate(actual_stream(source, **options), 1):
                events.append(index)
                yield segment
            events.append("EOF")
        except (ValueError, OSError, UnicodeError) as error:
            events.append(type(error).__name__)
            raise

    monkeypatch.setattr(intake, "_stamp", stable_observation)
    monkeypatch.setattr(originals, "text_segments", damaged_stream)
    try:
        with pytest.raises((OSError, ValueError), match="changed|source|original|Original"):
            app.search_page(workspace, "Needle", limit=37, timeout=120)
        assert events and events[0] == 1 and "EOF" not in events
        assert closed[-1]["digests"] == 0 and closed[-1]["sealed"] == ()
        assert app._selections == selected
        assert_released(created, closed)
    finally:
        assert app.close_search()
        path.write_bytes(raw)
    assert path.read_bytes() == raw and previous.results[0].verified


def test_cancellation_after_real_stream_yield_clears_unsealed_table(streamed_source, monkeypatch):
    app, workspace, path, raw = streamed_source
    prior = app.search_page(workspace, "Needle", limit=1, timeout=120)
    before = dict(app._selections)
    created, closed = observed_proofs(monkeypatch)
    real = originals.text_segments
    cancelled = False
    yields = []

    def stop_after_actual_yield(source, **options):
        nonlocal cancelled
        for index, segment in enumerate(real(source, **options), 1):
            yields.append(index)
            yield segment
            cancelled = True

    monkeypatch.setattr(originals, "text_segments", stop_after_actual_yield)
    try:
        with pytest.raises(TimeoutError, match="Search cancelled or timed out"):
            app.search_page(workspace, "Needle", limit=37, timeout=120,
                            cancelled=lambda: cancelled)
        assert yields and len(yields) < 1001
        assert closed[-1]["digests"] == 0 and closed[-1]["sealed"] == ()
        assert app._selections == before and prior.results[0].verified
        assert_released(created, closed)
        assert path.read_bytes() == raw
    finally:
        assert app.close_search()


@pytest.mark.parametrize("code", list(range(32)) + [127, 128, 133, 159])
def test_classification_preserves_every_original_ascii_and_c1_control_contract(code):
    data = ("Start" + chr(code) + "End").encode("utf-8")
    if code < 32 and code not in (9, 10, 12, 13):
        with pytest.raises(intake.IntakeError):
            intake.classify(data, ".txt")
    else:
        assert intake.classify(data, ".txt") == "text/plain"


@pytest.mark.parametrize("data, accepted", [
    (b"\xef\xbb\xbfOriginal\r\n\fLater", True),
    ("Original\ufefflater Ї.".encode("utf-8"), True),
    (b"\xef\xbbOriginal", False), (b"Original\xc0\x80", False),
    (b"\xef\xbb\xbfOriginal\x00", False), (b"Original\xff", False),
])
def test_classification_keeps_strict_bom_and_utf8_identity(data, accepted):
    if accepted:
        assert intake.classify(data, ".txt") == "text/plain"
    else:
        with pytest.raises(intake.IntakeError):
            intake.classify(data, ".txt")
