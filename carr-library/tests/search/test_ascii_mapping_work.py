"""Exact ASCII spans and fresh authority around the measured mapping hotspot.

The sparse mapper is production code already used for long ASCII sources.
These regressions compare independent whole-run boundaries and actual SQLite /
retained-source paths; they do not claim uninstrumented corpus performance.
"""
from contextlib import closing, contextmanager
from hashlib import sha256
import re
import sqlite3

import pytest

import carr_library.application as application
from carr_library.application import LibraryApp
from carr_library.search import literal


def _ascii_oracle(raw, check=lambda: None):
    """Independent full-run ASCII boundaries, without the production mapper."""
    assert raw.isascii()
    check()
    parts, starts, ends = [], {}, {}
    length = 0
    for match in re.finditer(r"\s+|[^\s]", raw):
        value = match.group()
        folded = " " if value.isspace() else value.casefold()
        starts[length] = match.start()
        parts.append(folded)
        length += len(folded)
        ends[length] = match.end()
    check()
    return "".join(parts), starts, ends


@pytest.mark.parametrize("raw", [
    "", "A", "ALPHA beta.", "  Alpha  ", "\t\r\n\f", "one\r\n\t two",
    "C++ C+ C++17", "word_word other-word", "A\x1c\x1dB", "Alpha\t\tBeta\r\nGamma",
    " \tAlpha  beta\t \r\n", "first.\x00second!\x7fthird?",
])
def test_ascii_mapping_keeps_every_nonempty_raw_match_boundary(raw):
    expected, starts, ends = _ascii_oracle(raw)
    actual, actual_starts, actual_ends = literal._mapped(raw)
    assert actual == expected
    for position in range(len(expected)):
        assert actual_starts[position] == starts[position]
    for position in range(1, len(expected) + 1):
        assert actual_ends[position] == ends[position]
    needles = {expected[start:end]
               for start in range(len(expected))
               for end in range(start + 1, min(len(expected), start + 6) + 1)}
    for needle in needles:
        old_spans = tuple(literal._spans(expected, starts, ends, needle))
        new_spans = tuple(literal._spans(actual, actual_starts, actual_ends, needle))
        assert new_spans == old_spans
        assert all(raw[start:end] for start, end in new_spans)


@pytest.mark.parametrize("length", [1023, 1024, 1025, 65535, 65536, 65537])
def test_ascii_boundaries_are_identical_across_old_size_and_check_thresholds(length):
    raw = ("Alpha\t\tBETA\r\n\r\nGamma. " * (length // 20 + 1))[:length]
    assert len(raw) == length
    expected, starts, ends = _ascii_oracle(raw)
    actual, actual_starts, actual_ends = literal._mapped(raw)
    assert actual == expected
    assert all(actual_starts[position] == value for position, value in starts.items())
    assert all(actual_ends[position] == value for position, value in ends.items())
    needle = "alpha beta"
    spans = tuple(literal._spans(actual, actual_starts, actual_ends, needle))
    assert spans == tuple(literal._spans(expected, starts, ends, needle))
    assert spans and all(raw[start:end] == "Alpha\t\tBETA" for start, end in spans)


@contextmanager
def _rows(raws):
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE active_page(id,workspace,edition,revision,asset,"
                   "ordinal,physical,printed,verified,raw,digest)")
        for ordinal, raw in enumerate(raws, 1):
            db.execute("INSERT INTO active_page VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (ordinal, "workspace", "edition", "revision", "a" * 64,
                        ordinal, None, None, 0, raw, sha256(raw.encode()).hexdigest()))
        yield db


@pytest.mark.parametrize("mode,query", [
    ("word", "BETA"), ("phrase", "ALPHA BETA"), ("terms", "beta alpha"),
    ("advanced", '"alpha beta" OR delta'),
])
def test_actual_sqlite_modes_and_offsets_equal_independent_ascii_boundaries(monkeypatch, mode, query):
    raws = ["Alpha\t  BETA, gamma.", "alpha beta; delta.",
            "prefix ALPHA\r\nBETA suffix.", "unrelated exact source."]
    with _rows(raws) as db:
        for offset in (0, 1):
            options = dict(mode=mode, edition_ids=("edition",), page_ids=(1, 2, 3, 4),
                           limit=2, offset=offset)
            actual = literal.search(db, "workspace", query, **options)
            with monkeypatch.context() as patch:
                patch.setattr(literal, "_mapped", _ascii_oracle)
                expected = literal.search(db, "workspace", query, **options)
            assert actual == expected and actual
            for hit in actual:
                raw = raws[hit["page_id"] - 1]
                assert raw[hit["start"]:hit["end"]] == hit["raw_quote"]
                assert hit["raw_text_sha256"] == sha256(raw.encode()).hexdigest()
        assert db.execute("SELECT count(*) FROM active_page").fetchone() == (4,)


@pytest.mark.parametrize("stop_at", [1, 2, 3])
@pytest.mark.parametrize("exception", [InterruptedError, TimeoutError])
def test_sparse_ascii_mapping_preserves_stop_objects_during_actual_work(stop_at, exception):
    marker = exception("observed ASCII mapping stop")
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == stop_at:
            raise marker

    with pytest.raises(exception) as error:
        literal._mapped("Alpha\t\tBeta\r\n\r\nGamma", check)
    assert error.value is marker and calls == stop_at
    assert literal._mapped("Alpha\t\tBeta")[0] == "alpha beta"


def _app(tmp_path):
    app = LibraryApp(tmp_path / "library")
    workspace = app.create_workspace("Actual ASCII authority")
    text = "\f".join(f"Alpha\t  BETA exact segment {number:03d}.\r\n"
                     for number in range(1, 41))
    imported = app.add_text(workspace, "Exact ASCII source", text,
                            edition_id="edition", work_id="work")
    assert imported["state"] == "complete"
    return app, workspace, imported, text


def test_actual_ascii_page37_keeps_fresh_lookahead_attestation_copy_and_closed_owners(tmp_path, monkeypatch):
    app, workspace, imported, text = _app(tmp_path)
    reads, attestations, owners, indexes = [], [], [], []
    original_read = LibraryApp._search_read
    original_attestor = application.strict_text_attestor
    original_work = literal._CandidateWork

    @contextmanager
    def observed_read(self, workspace, timeout, cancelled):
        with original_read(self, workspace, timeout, cancelled) as value:
            reads.append(value[0])
            yield value

    def observed_attestor(db, *args, **kwargs):
        actual = original_attestor(db, *args, **kwargs)

        def attest(identity, locator, digest):
            result = actual(identity, locator, digest)
            attestations.append((db, locator.index, result))
            return result
        return attest

    class ObservedWork(original_work):
        def __init__(self):
            super().__init__()
            owners.append(self)

        def _bind(self, *args):
            index = super()._bind(*args)
            if not any(index is previous for previous in indexes):
                indexes.append(index)
            return index

    monkeypatch.setattr(LibraryApp, "_search_read", observed_read)
    monkeypatch.setattr(application, "strict_text_attestor", observed_attestor)
    monkeypatch.setattr(literal, "_CandidateWork", ObservedWork)
    result = app.search_page(workspace, "ALPHA BETA", mode="phrase", limit=37)
    assert len(result.results) == 37 and result.has_more_results and result.source_pages == 40
    assert len(reads) == 2 and reads[0] is not reads[1]
    assert [index for db, index, accepted in attestations if db is reads[0] and accepted] == list(range(1, 38))
    assert [(index, accepted) for db, index, accepted in attestations if db is reads[1]] == [(38, True)]
    assert len(owners) == len(indexes) == 1
    assert owners[0]._closed and owners[0]._index is None and owners[0]._rows is None
    assert owners[0]._maps == {} and owners[0]._map_characters == 0
    for db in (*reads, *indexes):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")
    hit = result.results[0]
    assert hit.quote == "Alpha\t  BETA"
    assert app.copy(workspace, hit.ref, hit.revision, "quote") == hit.quote
    original = app.assets.resolve(imported["asset"])
    assert original.read_bytes() == text.encode()
    original.write_bytes(text.replace("Alpha", "Omega", 1).encode())
    with pytest.raises(ValueError, match="retained|original|source|digest"):
        app.copy(workspace, hit.ref, hit.revision, "quote")


@pytest.mark.parametrize("mutation", ["same_size", "append"])
def test_fresh_ascii_lookahead_rejects_retained_mutation_before_publication(tmp_path, monkeypatch, mutation):
    app, workspace, imported, text = _app(tmp_path)
    selected = app.assets.resolve(imported["asset"])
    data = selected.read_bytes()
    reads = []
    before = dict(app._selections)
    original_read = LibraryApp._search_read

    @contextmanager
    def observed_read(self, workspace, timeout, cancelled):
        with original_read(self, workspace, timeout, cancelled) as value:
            reads.append(value[0])
            yield value
        if len(reads) == 1:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                reads[0].execute("SELECT 1")
            changed = (data.replace(b"Alpha", b"Omega", 1) if mutation == "same_size"
                       else data + b"\r\nchanged retained source")
            selected.write_bytes(changed)

    monkeypatch.setattr(LibraryApp, "_search_read", observed_read)
    with pytest.raises(ValueError, match="retained|original|source"):
        app.search_page(workspace, "ALPHA BETA", mode="phrase", limit=37)
    assert len(reads) == 2 and app._selections == before
    for db in reads:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")
    assert selected.read_bytes() != data == text.encode()


def test_non_ascii_cluster_mapping_never_enters_the_ascii_branch(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Unicode source entered the ASCII mapper")

    monkeypatch.setattr(literal, "_ascii_mapped", refuse)
    for raw in ["Straße", "İ ї", "e\u0301 \u0301", "Ж\u2003Ж", "👩\u200d🔬", "한글"]:
        folded, _, _ = literal._mapped(raw)
        assert folded == literal._fold(raw)


@pytest.mark.parametrize("padding", [0, 1024, 65537])
@pytest.mark.parametrize("result_mode", ["short", "thematic"])
def test_actual_nfc_ascii_focus_keeps_unicode_source_quote_and_copy(tmp_path, monkeypatch, padding, result_mode):
    from unicodedata import normalize
    from carr_library.search import presentation

    prefix = ("ordinary filler " * (padding // 16 + 1))[:padding]
    raw = prefix + "The \u212aELVIN record preserves exact source."
    assert not raw.isascii() and normalize("NFC", raw).isascii()
    app = LibraryApp(tmp_path / "nfc-library")
    workspace = app.create_workspace("NFC source identity")
    imported = app.add_text(workspace, "Kelvin source", raw)
    assert imported["state"] == "complete"
    inversions = []
    original_reverse = presentation._reverse_offsets

    def observe(offsets, length, check):
        inversions.append(offsets)
        return original_reverse(offsets, length, check)

    monkeypatch.setattr(presentation, "_reverse_offsets", observe)
    hits = app.search(workspace, "KELVIN", mode="thematic", result_mode=result_mode, limit=1)
    assert len(hits) == 1 and hits[0].verified
    assert len(inversions) == 2 and all(isinstance(value, literal._AsciiOffsets)
                                      for value in inversions)
    hit = hits[0]
    assert "\u212aELVIN" in hit.quote and "KELVIN" not in hit.quote
    assert hit.quote in raw
    assert app.copy(workspace, hit.ref, hit.revision, "quote") == hit.quote
    assert app.context(workspace, hit.ref, hit.revision).page(0).raw_text == raw
    assert app.assets.resolve(imported["asset"]).read_bytes() == raw.encode("utf-8")


def test_sparse_ascii_items_and_actual_inverse_keep_bounded_allocation_and_boundaries():
    import tracemalloc
    from carr_library.search import presentation

    raw = "Alpha\t\tBETA\r\n\r\n" * 6000
    folded, offsets, _ = literal._mapped(raw)
    assert isinstance(offsets, literal._AsciiOffsets) and len(raw) > 65536
    items = offsets.items()
    assert iter(items) is items and next(items) == (0, 0)
    items.close()
    calls = 0

    def check():
        nonlocal calls
        calls += 1

    tracemalloc.start()
    try:
        inverse = presentation._reverse_offsets(offsets, len(raw), check)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert calls > 2
    # Only the actual inverse operation is measured, not SQLite or whole RSS.
    # A per-boundary Python dictionary exceeds this fixed linear allowance.
    assert peak < len(raw) * 12 + 1024 * 1024
    expected_folded, starts, ends = _ascii_oracle(raw)
    assert folded == expected_folded
    expected = {original: position for position, original in (starts | ends).items()}
    for original in range(len(raw) + 1):
        assert (original in inverse) == (original in expected)
        if original in expected:
            assert inverse[original] == expected[original]


@pytest.mark.parametrize("exception", [InterruptedError, TimeoutError])
def test_actual_sparse_inverse_preserves_cancellation_during_iteration(exception):
    from carr_library.search import presentation

    raw = "Alpha\t\tBETA\r\n\r\n" * 6000
    _, offsets, _ = literal._mapped(raw)
    marker = exception("observed sparse inverse stop")
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise marker

    with pytest.raises(exception) as error:
        presentation._reverse_offsets(offsets, len(raw), check)
    assert error.value is marker and calls == 3
    current = presentation._reverse_offsets(offsets, len(raw), lambda: None)
    assert current[0] == 0
    assert current[len(raw)] == len(literal._fold(raw))
