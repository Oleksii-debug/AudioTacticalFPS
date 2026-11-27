"""Exact selected quotations require the complete authorized original stream."""
from contextlib import closing, contextmanager
from hashlib import sha256
import os
from types import SimpleNamespace
import threading

import pytest

from carr_library.catalog import intake
from carr_library.citations import text_attestation
from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor, resolve_span
from carr_library.citations.historical import historical_loader
from carr_library.storage.revisions import Page, Storage


FIRST = "Київ — Žilina; cafe\u0301.\r\nExact selected original.\r\n"
LAST = "\ufeffLater original BOM remains text; unchanged tail.\r\n"
MIDDLE = ("Original unselected paragraph.\r\n" * (2 * intake._BLOCK // 31 + 100))


@pytest.fixture
def source_case(tmp_path):
    counter = 0

    @contextmanager
    def make(records=None, *, stored_records=None):
        nonlocal counter
        counter += 1
        selected = tuple(records if records is not None else (FIRST, MIDDLE, LAST))
        raw = b"\xef\xbb\xbf" + "\f".join(selected).encode("utf-8")
        path = tmp_path / ("original-" + str(counter) + ".txt")
        path.write_bytes(raw)
        captured = intake.file_snapshot(path.parent, path.name, max_bytes=len(raw))
        assert isinstance(captured, intake.FileSnapshot)
        assert captured.digest == sha256(raw).hexdigest() and captured.size == len(raw)
        with closing(Storage(tmp_path / ("library-" + str(counter) + ".sqlite"))) as store:
            identity = Identity("A", "edition", captured.digest, "sealed-source")
            retained = selected if stored_records is None else tuple(stored_records)
            store.publish_revision("A", identity.edition_id, identity.revision,
                                   identity.asset_sha256, text_attestation.CONFIG,
                                   [Page(value) for value in retained])
            yield SimpleNamespace(path=path, bytes=raw, records=selected,
                                  captured=captured, store=store, identity=identity)

    return make


def trace_segments(monkeypatch, *, after_yield=lambda ordinal, text: None):
    """Observe the genuine iterator and its final hash; never substitute a row."""
    original = text_attestation.text_segments
    events = []

    def actual(asset, **options):
        try:
            for ordinal, text in enumerate(original(asset, **options), 1):
                events.append(("yield", ordinal))
                yield text
                events.append(("continued", ordinal))
                after_yield(ordinal, text)
            events.append(("exhausted",))
        except BaseException as error:
            events.append(("error", type(error).__name__, str(error)))
            raise

    monkeypatch.setattr(text_attestation, "text_segments", actual)
    return events


def hold_observed_source_timestamps(monkeypatch, captured):
    """Keep only timestamp observations stable; inode, mode, size and links are real."""
    original = intake._stamp
    observed = []

    def stamped(info):
        value = original(info)
        if value[:2] == captured.identity[:2]:
            value = (*value[:5], *captured.identity[5:7], *value[7:])
            observed.append(value)
        return value

    monkeypatch.setattr(intake, "_stamp", stamped)
    return observed


def change_unselected_tail(case):
    # The changed byte is beyond the first actual 1 MiB parser read and belongs
    # to the last unselected segment. Every earlier source byte is unchanged.
    offset = len(case.bytes) - 3
    assert offset > 2 * intake._BLOCK
    before = case.path.stat()
    with case.path.open("r+b") as stream:
        stream.seek(offset)
        previous = stream.read(1)
        changed = b"X" if previous != b"X" else b"Y"
        stream.seek(offset)
        assert stream.write(changed) == 1
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(case.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = case.path.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    actual = case.path.read_bytes()
    assert actual[:offset] == case.bytes[:offset] and actual[offset + 1:] == case.bytes[offset + 1:]
    assert actual[offset:offset + 1] != case.bytes[offset:offset + 1]
    assert sha256(actual).hexdigest() != case.captured.digest
    return offset


@pytest.mark.parametrize("selected_index", [1, 3])
def test_exact_historical_quote_waits_for_every_original_byte_and_final_hash(
        source_case, monkeypatch, selected_index):
    with source_case() as case:
        # A newer active revision does not retarget the selected historical one.
        newer = b"A separate current original."
        case.store.publish_revision("A", "edition", "new-active", sha256(newer).hexdigest(),
                                    text_attestation.CONFIG, [Page(newer.decode("ascii"))])
        events = trace_segments(monkeypatch)
        chosen = case.records[selected_index - 1]
        locator = Locator("logical_segment", selected_index)
        anchor = make_anchor(case.identity, locator, chosen, 0, len(chosen),
                             Metadata("Synthetic author", "Exact stream"),
                             text_verified=True)
        resolutions = []

        def resolve(identity):
            resolutions.append(identity)
            assert identity == case.identity
            return case.captured

        with case.store.transaction():
            load = historical_loader(case.store.db, "A", text_attestation.strict_text_attestor(
                case.store.db, "A", resolve))
            result = resolve_span((anchor,), load)
            assert result == ((anchor, chosen),)
            assert events == [
                ("yield", 1), ("continued", 1),
                ("yield", 2), ("continued", 2),
                ("yield", 3), ("continued", 3),
                ("exhausted",),
            ]
        assert resolutions == [case.identity]
        assert "\r\n" in chosen
        assert case.records[2].startswith("\ufeff")
        assert case.path.read_bytes() == case.bytes


@pytest.mark.parametrize("when", ["before_read", "after_first_yield"])
def test_same_size_unselected_tail_change_is_refused_by_the_actual_complete_hash(
        source_case, monkeypatch, when):
    with source_case() as case:
        observations = hold_observed_source_timestamps(monkeypatch, case.captured)
        changed = []

        def alter(ordinal, _text):
            if when == "after_first_yield" and ordinal == 1:
                changed.append(change_unselected_tail(case))

        if when == "before_read":
            changed.append(change_unselected_tail(case))
        events = trace_segments(monkeypatch, after_yield=alter)
        with case.store.transaction():
            load = historical_loader(case.store.db, "A", text_attestation.strict_text_attestor(
                case.store.db, "A", lambda identity: case.captured))
            with pytest.raises(ValueError, match="unverified extraction"):
                load(case.identity, Locator("logical_segment", 1))
        assert len(changed) == 1
        assert observations and all(value == case.captured.identity for value in observations)
        # All actual segment yields occur. The refusal is the complete source
        # digest, not a timestamp mismatch or a substituted selected-text value.
        assert [event for event in events if event[0] == "yield"] == [
            ("yield", 1), ("yield", 2), ("yield", 3)]
        assert events[-1] == ("error", "IntakeError", "TXT source digest changed during verification.")
        assert ("exhausted",) not in events
        assert case.path.read_bytes().startswith(b"\xef\xbb\xbf" + FIRST.encode("utf-8") + b"\f")


@pytest.mark.parametrize("after_selected_yield", [1, 3])
def test_real_cancellation_during_the_remaining_stream_is_not_converted_to_unverified(
        source_case, monkeypatch, after_selected_yield):
    with source_case() as case:
        stopped = threading.Event()

        def stop(ordinal, _text):
            if ordinal == after_selected_yield:
                stopped.set()

        events = trace_segments(monkeypatch, after_yield=stop)
        with case.store.transaction():
            load = historical_loader(case.store.db, "A", text_attestation.strict_text_attestor(
                case.store.db, "A", lambda identity: case.captured, cancelled=stopped.is_set))
            with pytest.raises(InterruptedError, match="cancelled"):
                load(case.identity, Locator("logical_segment", 1))
        assert ("continued", after_selected_yield) in events
        assert events[-1][0:2] == ("error", "InterruptedError")
        assert ("exhausted",) not in events
        assert case.path.read_bytes() == case.bytes


def test_cancellation_after_the_real_complete_sha256_still_prevents_selected_text(
        source_case, monkeypatch):
    with source_case() as case:
        stopped = threading.Event()
        completed_hashes = []
        real_hash = intake.sha256

        class ObservedHash:
            def __init__(self, *args, **kwargs):
                self.delegate = real_hash(*args, **kwargs)
                self.consumed = len(args[0]) if args else 0

            def update(self, value):
                self.consumed += len(value)
                return self.delegate.update(value)

            def hexdigest(self):
                value = self.delegate.hexdigest()
                completed_hashes.append((self.consumed, value))
                stopped.set()
                return value

            def __getattr__(self, name):
                return getattr(self.delegate, name)

        monkeypatch.setattr(intake, "sha256", ObservedHash)
        with case.store.transaction():
            load = historical_loader(case.store.db, "A", text_attestation.strict_text_attestor(
                case.store.db, "A", lambda identity: case.captured, cancelled=stopped.is_set))
            with pytest.raises(InterruptedError, match="cancelled"):
                load(case.identity, Locator("logical_segment", 1))
        assert completed_hashes == [(len(case.bytes), case.captured.digest)]
        assert case.path.read_bytes() == case.bytes


def test_file_snapshot_uses_the_actual_sealed_count_beyond_the_legacy_thousand(
        source_case):
    records = tuple(f"Original segment {number:04d}; Київ.\r\n" for number in range(1001))
    with source_case(records) as case, case.store.transaction():
        locator = Locator("logical_segment", 1001)
        current = text_attestation.strict_text_attestor(case.store.db, "A", lambda _: case.captured)
        assert current(case.identity, locator, digest(records[-1])) is True
        assert historical_loader(case.store.db, "A", current)(case.identity, locator)[2:] == (
            records[-1], True)
        # Legacy immutable-byte callers retain their original total-count guard.
        legacy = text_attestation.strict_text_attestor(case.store.db, "A", lambda _: case.bytes)
        assert legacy(case.identity, locator, digest(records[-1])) is False


@pytest.mark.parametrize("stored_records", [(FIRST, LAST), (FIRST, MIDDLE, LAST, "fabricated extra")])
def test_selected_text_cannot_hide_a_disagreement_with_the_sealed_original_count(
        source_case, stored_records):
    with source_case(stored_records=stored_records) as case, case.store.transaction():
        attest = text_attestation.strict_text_attestor(case.store.db, "A", lambda _: case.captured)
        assert attest(case.identity, Locator("logical_segment", 1), digest(FIRST)) is False
        with pytest.raises(ValueError, match="unverified extraction"):
            historical_loader(case.store.db, "A", attest)(case.identity, Locator("logical_segment", 1))
        assert case.path.read_bytes() == case.bytes
