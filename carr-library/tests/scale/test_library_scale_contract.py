"""Real-byte generator/resource contracts; these are not corpus benchmark results."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tracemalloc

import pytest


@pytest.fixture(scope="module")
def scale():
    path = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_library_scale.py"
    name = "_carr_library_scale_contract"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == path
    return module


def generate(scale, target, spec, **kwargs):
    # Small generator contracts need no planned product search spool. This does
    # not change the measured benchmark's explicit production resource profile.
    return scale.generate_fixture(target, spec, search_spool_budget_bytes=0,
                                  free_reserve_bytes=0, memory_headroom_bytes=1, **kwargs)


def test_actual_unicode_crlf_bom_and_all_segment_hashes(scale, tmp_path):
    target = tmp_path / "fixture"
    spec = scale.CorpusSpec(books=3, segments_per_book=40, segment_bytes=256)
    result = generate(scale, target, spec)
    assert result["status"] == "FIXTURE_GENERATED_NOT_BENCHMARKED"
    assert result["library_benchmark"] == "NOT_RUN"
    rows = list(scale.source_records(target, result["manifest_sha256"]))
    assert len(rows) == 3 and result["generated_bytes"] == spec.source_bytes
    for book, row in enumerate(rows):
        source = (target / "sources" / row["file"]).read_bytes()
        assert source.startswith(b"\xef\xbb\xbf") is (book == 0)
        raw_segments = source.decode("utf-8-sig").split("\f")
        assert len(raw_segments) == 40
        assert len(row["segments"]) == 40
        for ordinal, (raw, expected) in enumerate(zip(raw_segments, row["segments"]), 1):
            encoded = raw.encode("utf-8")
            assert len(encoded) == 256
            assert encoded == b"".join(scale.segment_parts(book, ordinal, 256))
            assert expected["sha256"] == sha256(encoded).hexdigest()
            assert expected["characters"] == len(raw)
            assert expected["anchor"] == scale.anchor(book, ordinal)
            assert raw.startswith(expected["anchor"] + " commonrailway")
            assert "Київ — Žilina; cafe\u0301; літо;" in raw
            assert "\r\n" in raw and raw.endswith("\r\n")
            assert source[expected["offset_bytes"]:expected["offset_bytes"] + 256] == encoded
        assert row["source_sha256"] == sha256(source).hexdigest()
    verified = scale.verify_originals(target, result)
    assert verified == {"verified_sources": 3, "verified_original_bytes": spec.source_bytes,
                        "declared_original_segments": 120}


def test_genuine_65_mib_source_is_fully_written_hashed_and_memory_bounded(scale, tmp_path):
    target = tmp_path / "large"
    requested = 65 * scale.MIB + 173
    spec = scale.LargeTextSpec(requested, segments=17)
    tracemalloc.start()
    try:
        result = generate(scale, target, spec)
        verified = scale.verify_originals(target, result)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    source = target / "sources" / "book-00000.txt"
    assert source.stat().st_size == requested > 64 * scale.MIB
    assert result["generated_bytes"] == requested
    assert result["segments"] == 17
    assert verified["verified_original_bytes"] == requested
    assert peak < 8 * scale.MIB, peak
    # Inspect the real final original range, not an asserted/truncated file size.
    row, = scale.source_records(target, result["manifest_sha256"])
    last = row["segments"][-1]
    hashed, consumed = sha256(), 0
    with source.open("rb") as stream:
        stream.seek(last["offset_bytes"])
        while block := stream.read(min(scale.CHUNK_BYTES, last["size_bytes"] - consumed)):
            hashed.update(block)
            consumed += len(block)
        assert consumed == last["size_bytes"] and stream.read(1) == b""
    assert hashed.hexdigest() == last["sha256"]


def test_every_actual_source_write_stays_within_64_kib(scale, tmp_path, monkeypatch):
    original = Path.open
    writes = []

    class Observed:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def write(self, data):
            writes.append(len(data))
            return self.stream.write(data)

    def opened(path, mode="r", *args, **kwargs):
        value = original(path, mode, *args, **kwargs)
        if mode == "xb" and path.name.startswith("book-"):
            return Observed(value)
        return value

    monkeypatch.setattr(Path, "open", opened)
    target = tmp_path / "bounded"
    result = generate(scale, target, scale.LargeTextSpec(3 * scale.MIB + 19, 2))
    assert writes and max(writes) == scale.CHUNK_BYTES
    assert sum(writes) == result["generated_bytes"] == 3 * scale.MIB + 19
    assert scale.verify_originals(target, result)["verified_sources"] == 1


def test_real_same_size_original_mutation_is_not_hidden_by_restored_mtime(scale, tmp_path):
    target = tmp_path / "changed"
    result = generate(scale, target, scale.CorpusSpec(2, 2, 256))
    source = target / "sources" / "book-00001.txt"
    before = source.stat()
    with source.open("r+b") as stream:
        stream.seek(10)
        old = stream.read(1)
        stream.seek(10)
        stream.write(b"X" if old != b"X" else b"Y")
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert source.stat().st_size == before.st_size
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(ValueError, match="hash or length changed"):
        scale.verify_originals(target, result)


def test_real_manifest_tamper_cannot_relabel_another_source(scale, tmp_path):
    target = tmp_path / "manifest"
    result = generate(scale, target, scale.CorpusSpec(1, 1, 256))
    manifest = target / "sources.jsonl"
    original = manifest.read_bytes()
    assert b"book-00000.txt" in original
    manifest.write_bytes(original.replace(b"book-00000.txt", b"book-99999.txt"))
    with pytest.raises(ValueError, match="manifest digest mismatch"):
        tuple(scale.source_records(target, result["manifest_sha256"]))


def test_cancel_after_real_partial_write_never_reports_complete_corpus(scale, tmp_path):
    target = tmp_path / "cancelled"
    source = target / "sources" / "book-00000.txt"
    observed = []

    def cancelled():
        if source.exists():
            size = source.stat().st_size
            if size > scale.CHUNK_BYTES:
                observed.append(size)
                return True
        return False

    with pytest.raises(scale.FixtureCancelled):
        generate(scale, target, scale.LargeTextSpec(4 * scale.MIB, 2),
                 cancelled=cancelled)
    result = json.loads((target / "generation.json").read_text(encoding="utf-8"))
    assert observed and 0 < source.stat().st_size < 4 * scale.MIB
    assert result["status"] == "INCOMPLETE_FIXTURE"
    assert result["completed_books"] == 0 and result["completed_source_bytes"] == 0
    assert result["library_benchmark"] == "NOT_RUN"
    with pytest.raises(ValueError, match="complete generated fixture"):
        scale.verify_originals(target, result)


def test_checkpoint_failure_preserves_actual_completed_source_without_pass(scale, tmp_path):
    target = tmp_path / "checkpoint"

    def broken(record):
        assert record["completed_books"] == 1
        source = target / "sources" / "book-00000.txt"
        assert source.is_file()
        assert sha256(source.read_bytes()).hexdigest() == record["last_source_sha256"]
        raise OSError("authored checkpoint failure")

    with pytest.raises(OSError, match="authored checkpoint"):
        generate(scale, target, scale.CorpusSpec(3, 1, 256), checkpoint=broken)
    result = json.loads((target / "generation.json").read_text(encoding="utf-8"))
    assert result["status"] == "INCOMPLETE_FIXTURE"
    assert result["completed_books"] == 1 and result["completed_source_bytes"] == 259
    assert result["library_benchmark"] == "NOT_RUN"
    assert not (target / "sources" / "book-00001.txt").exists()
    assert len((target / "sources.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_actual_insufficient_disk_preflight_remains_not_run(scale, tmp_path):
    target = tmp_path / "unavailable"
    # This exceeds the actual current volume's free space without allocating it.
    reserve = shutil.disk_usage(tmp_path).free + 1
    result = scale.generate_fixture(target, scale.CorpusSpec(1, 1, 256),
        search_spool_budget_bytes=0, free_reserve_bytes=reserve, memory_headroom_bytes=1)
    assert result["status"] == "NOT_RUN" and result["library_benchmark"] == "NOT_RUN"
    assert result["completed_books"] == result["generated_bytes"] == 0
    assert result["preflight"]["disk"]["required_estimate_bytes"] > (
        result["preflight"]["disk"]["free_bytes"])
    assert not (target / "sources").exists()
    assert not (target / "sources.jsonl").exists()


def test_existing_directory_is_never_overwritten_or_cleaned(scale, tmp_path):
    target = tmp_path / "selected"
    target.mkdir()
    sentinel = target / "unrelated.txt"
    sentinel.write_bytes(b"actual preexisting owner bytes")
    with pytest.raises(FileExistsError):
        generate(scale, target, scale.CorpusSpec(1, 1, 256))
    assert sentinel.read_bytes() == b"actual preexisting owner bytes"
    assert sorted(path.name for path in target.iterdir()) == ["unrelated.txt"]


def test_actual_disk_inventory_counts_all_files_and_refuses_links(scale, tmp_path):
    root = tmp_path / "inventory"
    child = root / "nested"
    child.mkdir(parents=True)
    (root / "one").write_bytes(b"a" * 13)
    (child / "two").write_bytes(b"b" * 29)
    value = scale.tree_bytes(root, maximum_entries=3)
    assert value["entries"] == 3 and value["files"] == 2 and value["logical_bytes"] == 42
    with pytest.raises(ValueError, match="entry allowance"):
        scale.tree_bytes(root, maximum_entries=2)
    os.link(child / "two", root / "other-name")
    with pytest.raises(ValueError, match="link or special"):
        scale.tree_bytes(root, maximum_entries=10)


@pytest.mark.parametrize("spec", [
    (True, 40, 256), (50_001, 40, 256), (1, 0, 256), (1, 40, 255),
    (1, 40, 4 * 1024**2 + 1),
])
def test_generator_bounds_are_explicit_not_silent_truncation(scale, tmp_path, spec):
    target = tmp_path / "bad"
    with pytest.raises(ValueError, match="Invalid synthetic corpus"):
        generate(scale, target, scale.CorpusSpec(*spec))
    assert not target.exists()


def test_real_measurement_fields_do_not_claim_simultaneous_family_peak(scale):
    value = scale.process_memory()
    assert value["coordinator_peak_bytes"] > 0
    assert value["simultaneous_family_peak_bytes"] is None
    if os.name == "nt":
        assert value["largest_reaped_child_peak_bytes"] is None
    else:
        assert value["largest_reaped_child_peak_bytes"] >= 0


def test_fifty_sample_distribution_and_unmeasured_values_are_distinct(scale):
    value = scale.distribution([number / 1000 for number in range(50)])
    assert value["samples"] == 50 and value["p50_seconds"] == .0245
    assert value["p95_seconds"] == .047 and value["p95_method"] == "nearest rank"
    for invalid in ([], [float("nan")], [float("inf")], [-1.0], [True]):
        with pytest.raises(ValueError, match="actual elapsed"):
            scale.distribution(invalid)


def test_two_gib_plan_accounts_for_all_material_storage_classes_without_generating_it(scale):
    # Arithmetic contract only: this does not claim a two-GiB source was imported.
    spec = scale.LargeTextSpec(2 * scale.GIB, 512).checked()
    assert sum(spec.sizes()) + spec.segments - 1 + 3 == 2 * scale.GIB
    plan = scale.capacity_plan(spec.source_bytes, 1, spec.segments)
    assert plan.original_bytes == plan.retained_bytes == 2 * scale.GIB
    assert plan.fixture_manifest_estimate_bytes > 0
    assert plan.catalog_estimate_bytes > spec.source_bytes
    assert plan.journal_estimate_bytes > spec.source_bytes
    assert plan.search_spool_budget_bytes == 2 * scale.GIB
    assert plan.free_reserve_bytes == scale.GIB
    assert plan.required_disk_bytes > 8 * spec.source_bytes


@pytest.mark.parametrize("selected", ["source", "manifest"])
@pytest.mark.parametrize("change", ["equal_bytes_replacement", "new_hardlink"])
def test_preopen_identity_change_is_refused_before_reading(scale, tmp_path, monkeypatch, selected, change):
    target = tmp_path / "preopen"
    generated = generate(scale, target, scale.CorpusSpec(1, 1, 256))
    chosen = (target / "sources" / "book-00000.txt" if selected == "source"
              else target / "sources.jsonl")
    actual_open = scale._open_json_file
    observed = []

    class NoRead:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, *args):
            pytest.fail("Changed preopen identity reached a read.")
        def readline(self, *args):
            pytest.fail("Changed preopen identity reached a line read.")

    def replaced(path):
        assert Path(path) == chosen and not observed
        observed.append(chosen.stat().st_ino)
        if change == "equal_bytes_replacement":
            copy = target / "replacement"
            copy.write_bytes(chosen.read_bytes())
            os.replace(copy, chosen)
            assert chosen.stat().st_ino != observed[0]
        else:
            os.link(chosen, target / "additional-name")
            assert chosen.stat().st_nlink == 2
        return NoRead(actual_open(path))

    monkeypatch.setattr(scale, "_open_json_file", replaced)
    with pytest.raises(ValueError, match="changed before its verification read"):
        if selected == "source":
            scale.digest_file(chosen)
        else:
            tuple(scale.source_records(target, generated["manifest_sha256"]))
    assert len(observed) == 1


def test_exact_parsed_manifest_bytes_are_hashed_even_if_original_is_restored(scale, tmp_path, monkeypatch):
    target = tmp_path / "parsed"
    generated = generate(scale, target, scale.CorpusSpec(1, 1, 256))
    manifest = target / "sources.jsonl"
    original_bytes = manifest.read_bytes()
    row = json.loads(original_bytes.splitlines()[1])
    before_hash = row["source_sha256"]
    changed_hash = ("0" if before_hash[0] != "0" else "1") + before_hash[1:]
    changed_bytes = original_bytes.replace(before_hash.encode(), changed_hash.encode())
    assert changed_bytes != original_bytes and len(changed_bytes) == len(original_bytes)
    actual_open = scale._open_json_file
    mutated, restored = [], []

    def write_actual(data):
        with manifest.open("r+b") as writer:
            assert writer.write(data) == len(data)
            writer.flush()
            os.fsync(writer.fileno())

    class Changing:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def seek(self, where, *args):
            assert where == 0 and not mutated
            write_actual(changed_bytes)
            mutated.append(True)
            return self.stream.seek(where, *args)
        def readline(self, *args):
            value = self.stream.readline(*args)
            if not value and mutated and not restored:
                write_actual(original_bytes)
                restored.append(True)
            return value

    monkeypatch.setattr(scale, "_open_json_file", lambda path: Changing(actual_open(path)))
    with pytest.raises(ValueError, match="Parsed synthetic manifest bytes differ"):
        tuple(scale.source_records(target, generated["manifest_sha256"]))
    assert mutated == restored == [True]
    assert manifest.read_bytes() == original_bytes


def test_manifest_initial_hash_is_actually_cancellable_between_bounded_reads(scale, tmp_path, monkeypatch):
    target = tmp_path / "manifest-cancel"
    generated = generate(scale, target, scale.CorpusSpec(20, 40, 256))
    assert (target / "sources.jsonl").stat().st_size > 2 * scale.CHUNK_BYTES
    actual_open = scale._open_json_file
    sizes, seeks = [], []

    class Observed:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, size):
            assert 0 < size <= scale.CHUNK_BYTES
            result = self.stream.read(size)
            sizes.append(len(result))
            return result
        def seek(self, *args):
            seeks.append(args)
            return self.stream.seek(*args)

    monkeypatch.setattr(scale, "_open_json_file", lambda path: Observed(actual_open(path)))
    with pytest.raises(scale.FixtureCancelled):
        scale.verify_originals(target, generated, cancelled=lambda: len(sizes) >= 2)
    assert sizes == [scale.CHUNK_BYTES, scale.CHUNK_BYTES] and not seeks
