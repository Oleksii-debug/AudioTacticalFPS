"""Independent pre-bulk mapping oracle and actual source/copy qualification."""
from hashlib import sha256
import random
import re
import unicodedata

import pytest

from carr_library.application import LibraryApp
from carr_library.search import literal


def original_mapping(text):
    """Exact finite old cluster algorithm, independent of new mapping helpers."""
    def fold(value):
        return re.sub(r"\s+", " ", unicodedata.normalize("NFD",
            unicodedata.normalize("NFD", value).casefold()))

    parts, starts, ends = [], {}, {}
    index = position = 0
    while index < len(text):
        following = index + 1
        if text[index].isspace():
            while following < len(text) and text[following].isspace():
                following += 1
        else:
            while (following < len(text)
                   and unicodedata.category(text[following]).startswith("M")):
                following += 1
        part = fold(text[index:following])
        starts[position] = index
        position += len(part)
        ends[position] = following
        parts.append(part)
        index = following
    joined = "".join(parts)
    if joined != fold(text):
        raise ValueError("unsupported normalization mapping")
    return joined, starts, ends


def compare_original(raw, monkeypatch):
    try:
        expected, starts, ends = original_mapping(raw)
    except ValueError:
        for threshold in (len(raw) + 1, 1):
            monkeypatch.setattr(literal, "_LONG_ASCII", threshold)
            with pytest.raises(ValueError, match="unsupported normalization mapping"):
                literal._mapped(raw)
        return
    for threshold in (len(raw) + 1, 1):
        monkeypatch.setattr(literal, "_LONG_ASCII", threshold)
        folded, actual_starts, actual_ends = literal._mapped(raw)
        assert folded == expected
        assert dict(actual_starts.items()) == starts
        assert dict(actual_ends.items()) == ends
        for position in range(len(expected) + 1):
            assert (position in actual_starts) == (position in starts)
            assert (position in actual_ends) == (position in ends)


@pytest.mark.parametrize("boundary", [1023, 1024, 1025, 2047, 2048, 2049])
@pytest.mark.parametrize("crossing", [
    "a\u0301\u0323", " \t\r\n\u2003\u00a0X", "Straße", "İA",
    "👩\u200d🔬", "\U0001f600\u0301A", "\r\n\t  A", "a\u0345\u0301",
])
def test_bulk_boundaries_match_original_complete_cluster_maps(monkeypatch, boundary, crossing):
    # The initial non-ASCII character keeps both compact/dictionary mixed paths
    # active, including a late cluster exactly at each real check boundary.
    raw = "Ж" + "A" * (boundary - 1) + crossing + " tail\r\n" + "z" * 1090
    compare_original(raw, monkeypatch)


def test_ascii_controls_and_mixed_whitespace_have_exact_old_raw_boundaries(monkeypatch):
    controls = "".join(chr(number) for number in range(128))
    raw = "Ї" + controls * 18 + "\u0085\u00a0\u2003\t\r\n" + controls + "a\u0301"
    compare_original(raw, monkeypatch)


def test_bounded_random_unicode_cluster_adjacency_keeps_old_mapping(monkeypatch):
    rng = random.Random(832761)
    pieces = ["Ж", "a", " \t\r\n", "\u0085\u00a0", "ß", "İ", "e\u0301",
              "x\u0323\u0301", "👩\u200d🔬", "中", "한", "\ufeff", "\x1c\x1d",
              "ASCII repeated words " * 20, "\u0301\u0345", "\U0001f600"]
    for _ in range(120):
        raw = "Ї" + "".join(rng.choice(pieces) for _ in range(rng.randrange(4, 35)))
        compare_original(raw, monkeypatch)


def test_long_mixed_ascii_uses_bounded_runs_without_per_letter_normalization(monkeypatch):
    raw = "Ї exact original.\r\n" + "ordinary source words " * 6000 + " Straße α 😀."
    actual_fold = literal._fold
    actual_ascii = literal._ascii_mapped
    calls, runs = [], []

    def folded(value):
        calls.append(len(value))
        return actual_fold(value)

    def mapped_ascii(value, check):
        runs.append(len(value))
        return actual_ascii(value, check)

    monkeypatch.setattr(literal, "_fold", folded)
    monkeypatch.setattr(literal, "_ascii_mapped", mapped_ascii)
    folded_text, starts, ends = literal._mapped(raw)
    assert runs and max(runs) <= 1024
    # This bounds the measured regression mechanism without a machine-dependent
    # throughput assertion: the unchanged Unicode tail still uses whole clusters.
    assert len(calls) < len(raw) // 128
    assert folded_text == actual_fold(raw)
    assert isinstance(starts, literal._CompactOffsets) and isinstance(ends, literal._CompactOffsets)
    spans = tuple(literal._spans(folded_text, starts, ends, actual_fold("STRASSE α 😀")))
    assert len(spans) == 1 and raw[slice(*spans[0])] == "Straße α 😀"


def test_real_bulk_progress_observes_cancellation_at_bounded_run_boundary(monkeypatch):
    raw = "Ї " + "ASCII text " * 20000 + "Ж"
    actual = literal._ascii_mapped
    scanned = []

    def observed(value, check):
        result = actual(value, check)
        scanned.append(len(value))
        return result

    def cancelled():
        if sum(scanned) >= 2048:
            raise InterruptedError("actual bounded mapping cancelled")

    monkeypatch.setattr(literal, "_ascii_mapped", observed)
    with pytest.raises(InterruptedError, match="actual bounded mapping cancelled"):
        literal._mapped(raw, cancelled)
    assert 2048 <= sum(scanned) < 4096
    assert max(scanned) <= 1024 and sum(scanned) < len(raw)


@pytest.mark.parametrize("result_mode", [None, "short", "thematic"])
def test_actual_mixed_original_keeps_exact_source_quote_context_and_scope(tmp_path, result_mode):
    app = LibraryApp(tmp_path / "library")
    workspace = app.create_workspace("Mixed source")
    foreign = app.create_workspace("Separate scope")
    raw = ("Heading Ж.\r\n" + "ordinary source filler. " * 4000
           + "\r\nStraße\t α cafe\u0301 😀 proof.\r\n")
    result = app.add_text(workspace, "Exact Unicode tail", raw)
    source = app.assets.resolve(result["asset"])
    before = source.read_bytes()
    try:
        page = app.search_page(workspace, "STRASSE α CAFÉ 😀", result_mode=result_mode)
        assert len(page.results) == 1
        hit = page.results[0]
        assert hit.verified and hit.quote in raw and "Straße\t α cafe\u0301 😀" in hit.quote
        assert app.copy(workspace, hit.ref, hit.revision, "quote") == hit.quote
        context = app.context(workspace, hit.ref, hit.revision).page(0)
        assert context.raw_text == raw and context.quote == hit.quote
        with pytest.raises(ValueError):
            app.copy(foreign, hit.ref, hit.revision, "quote")
        assert not app.search_page(foreign, "STRASSE α CAFÉ 😀").results
        assert source.read_bytes() == before == raw.encode("utf-8")
        assert sha256(before).hexdigest() == result["asset"]
    finally:
        assert app.close_search()
