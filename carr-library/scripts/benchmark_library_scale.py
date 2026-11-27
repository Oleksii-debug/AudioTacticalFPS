"""Generate and measure real synthetic library-scale fixtures.

This file deliberately separates fixture generation from product qualification.
Generation writes every byte, records exact source/segment hashes and returns
FIXTURE_GENERATED_NOT_BENCHMARKED. It never creates catalog rows or reports a
library benchmark PASS. Public import/search adapters are composed separately.

The bounded writer reuses the strategy in tests/ingest/large_source_fixtures.py
from PR152 9f2a838aa4914451bd7a3643361ff66c19268674: strict UTF-8, original
form-feed boundaries, real non-sparse writes and incremental SHA-256.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import time
from typing import Callable, Iterator
from uuid import uuid4

from carr_library.durable_state import _open_json_file, local_path

MIB = 1024 ** 2
GIB = 1024 ** 3
CHUNK_BYTES = 64 * 1024
MAX_BOOKS = 50_000
MAX_SEGMENTS = 1_000
MAX_SEGMENT_BYTES = 4 * MIB
MAX_MANIFEST_LINE = 256 * 1024
SCHEMA = "carr-library-scale/1"
FILLER = b"Original railway evidence remains exact; archive chronology preserves source words. "
FILLER_BLOCK = (FILLER * (CHUNK_BYTES // len(FILLER) + 1))[:CHUNK_BYTES]
SUFFIX = b" End of original segment.\r\n"


class FixtureCancelled(InterruptedError):
    """A real fixture write stopped before the requested corpus was complete."""


@dataclass(frozen=True)
class CorpusSpec:
    books: int = 1_000
    segments_per_book: int = 40
    segment_bytes: int = 256

    def checked(self) -> "CorpusSpec":
        for name, value, low, high in (
            ("books", self.books, 1, MAX_BOOKS),
            ("segments_per_book", self.segments_per_book, 1, MAX_SEGMENTS),
            ("segment_bytes", self.segment_bytes, 256, MAX_SEGMENT_BYTES),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid synthetic corpus {name}.")
        return self

    @property
    def source_bytes(self) -> int:
        self.checked()
        # One genuine UTF-8 BOM in the first file; separators are original bytes.
        return self.books * (self.segments_per_book * self.segment_bytes
                             + self.segments_per_book - 1) + 3

    @property
    def segments(self) -> int:
        return self.books * self.segments_per_book


@dataclass(frozen=True)
class LargeTextSpec:
    source_bytes: int
    segments: int

    def checked(self) -> "LargeTextSpec":
        if (type(self.source_bytes) is not int or not 1 <= self.source_bytes <= 2 * GIB
                or type(self.segments) is not int or not 1 <= self.segments <= MAX_SEGMENTS):
            raise ValueError("Invalid genuine large-source profile.")
        payload = self.source_bytes - (self.segments - 1) - 3
        smallest, extra = divmod(payload, self.segments)
        if smallest < 256 or smallest + bool(extra) > MAX_SEGMENT_BYTES:
            raise ValueError("Large-source segments exceed the explicit generator profile.")
        return self

    def sizes(self) -> Iterator[int]:
        self.checked()
        small, extra = divmod(self.source_bytes - self.segments - 2, self.segments)
        for index in range(self.segments):
            yield small + (index < extra)


@dataclass(frozen=True)
class CapacityPlan:
    original_bytes: int
    retained_bytes: int
    fixture_manifest_estimate_bytes: int
    catalog_estimate_bytes: int
    journal_estimate_bytes: int
    search_spool_budget_bytes: int
    free_reserve_bytes: int
    memory_headroom_bytes: int

    @property
    def required_disk_bytes(self) -> int:
        return (self.original_bytes + self.retained_bytes + self.fixture_manifest_estimate_bytes
                + self.catalog_estimate_bytes
                + self.journal_estimate_bytes + self.search_spool_budget_bytes
                + self.free_reserve_bytes)


def capacity_plan(source_bytes: int, books: int, segments: int, *,
                  search_spool_budget_bytes: int = 2 * GIB,
                  free_reserve_bytes: int = GIB,
                  memory_headroom_bytes: int = 1536 * MIB) -> CapacityPlan:
    """Conservative admission estimate, explicitly not a measured database size."""
    values = (source_bytes, books, segments, search_spool_budget_bytes,
              free_reserve_bytes, memory_headroom_bytes)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("Capacity inputs must be nonnegative integers.")
    if min(source_bytes, books, segments, memory_headroom_bytes) <= 0:
        raise ValueError("Source, count and memory inputs must be positive.")
    return CapacityPlan(
        original_bytes=source_bytes,
        retained_bytes=source_bytes,
        fixture_manifest_estimate_bytes=segments * 256 + books * 512 + 4096,
        catalog_estimate_bytes=source_bytes * 3 + segments * 1024,
        journal_estimate_bytes=source_bytes * 2 + books * 16 * 1024,
        search_spool_budget_bytes=search_spool_budget_bytes,
        free_reserve_bytes=free_reserve_bytes,
        memory_headroom_bytes=memory_headroom_bytes,
    )


def available_memory() -> dict:
    """Observed available physical memory, with Linux cgroup limits if present."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                        *[(name, ctypes.c_ulonglong) for name in (
                            "total_physical", "available_physical", "total_pagefile",
                            "available_pagefile", "total_virtual", "available_virtual",
                            "available_extended_virtual")]]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
        kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
        value = MemoryStatus()
        value.length = ctypes.sizeof(value)
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"available_bytes": int(value.available_physical),
                "method": "GlobalMemoryStatusEx.ullAvailPhys", "cgroup": None}
    if sys.platform.startswith("linux"):
        values = {}
        with open("/proc/meminfo", "r", encoding="ascii") as stream:
            for line in stream:
                key, _, value = line.partition(":")
                if key in ("MemAvailable", "MemTotal"):
                    values[key] = int(value.strip().split()[0]) * 1024
        if "MemAvailable" not in values:
            raise ValueError("The runner has no measurable MemAvailable value.")
        observed = values["MemAvailable"]
        cgroup = None
        # The unified root is a useful additional bound, never a substitute for
        # the reported physical measurement or a claimed enforced process limit.
        root = Path("/sys/fs/cgroup")
        try:
            limit_text = (root / "memory.max").read_text(encoding="ascii").strip()
            used_text = (root / "memory.current").read_text(encoding="ascii").strip()
        except FileNotFoundError:
            pass
        else:
            if limit_text != "max":
                limit, used = int(limit_text), int(used_text)
                if limit < 0 or used < 0:
                    raise ValueError("Invalid observed cgroup memory accounting.")
                remaining = max(0, limit - used)
                observed = min(observed, remaining)
                cgroup = {"limit_bytes": limit, "current_bytes": used,
                          "remaining_bytes": remaining}
        return {"available_bytes": observed, "method": "/proc/meminfo.MemAvailable",
                "cgroup": cgroup}
    return {"available_bytes": None, "method": "NOT_MEASURED", "cgroup": None}


def preflight(directory: Path, plan: CapacityPlan) -> dict:
    usage = shutil.disk_usage(directory)
    memory = available_memory()
    enough_memory = (memory["available_bytes"] is not None
                     and memory["available_bytes"] >= plan.memory_headroom_bytes)
    enough_disk = usage.free >= plan.required_disk_bytes
    return {
        "status": "READY_FOR_FIXTURE" if enough_disk and enough_memory else "NOT_RUN",
        "reason": None if enough_disk and enough_memory else "insufficient measured runner resources",
        "disk": {"free_bytes": usage.free, "total_bytes": usage.total,
                 "required_estimate_bytes": plan.required_disk_bytes,
                 "components": asdict(plan)},
        "memory": {**memory, "required_headroom_bytes": plan.memory_headroom_bytes},
        "note": "Admission estimates are not measured application use or enforced memory limits.",
    }


def _cancel(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise FixtureCancelled("Synthetic source generation was cancelled.")


def anchor(book: int, ordinal: int) -> str:
    if (type(book) is not int or not 0 <= book < MAX_BOOKS
            or type(ordinal) is not int or not 1 <= ordinal <= MAX_SEGMENTS):
        raise ValueError("Invalid synthetic source position.")
    return f"carrscale{book:05d}p{ordinal:04d}"


def segment_parts(book: int, ordinal: int, size: int) -> Iterator[bytes]:
    """One original logical segment, with a <=64 KiB write buffer at every step."""
    if type(size) is not int or not 256 <= size <= MAX_SEGMENT_BYTES:
        raise ValueError("Invalid synthetic segment size.")
    prefix = (anchor(book, ordinal) + " commonrailway archive.\r\n"
              "Київ — Žilina; cafe\u0301; літо; exact original evidence.\r\n").encode("utf-8")
    remaining = size - len(prefix) - len(SUFFIX)
    if remaining < 0:
        raise ValueError("The selected segment cannot contain its exact Unicode oracle.")
    yield prefix
    while remaining:
        chunk = FILLER_BLOCK[:min(CHUNK_BYTES, remaining)]
        yield chunk
        remaining -= len(chunk)
    yield SUFFIX


def _stamp(value) -> tuple:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _ordinary(path: Path):
    value = path.lstat()
    if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
            or getattr(value, "st_file_attributes", 0) & 0x400):
        raise ValueError("Synthetic fixture must be one ordinary file with a single link.")
    return value


@contextmanager
def _opened(path: Path, *, cancelled=lambda: False):
    """Reuse the application's non-following, delete-sharing descriptor opener."""
    _cancel(cancelled)
    selected = local_path(path, exists=True)
    before = _ordinary(selected)
    with _open_json_file(selected) as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, "st_file_attributes", 0) & 0x400
                or _stamp(opened) != _stamp(before)):
            raise ValueError("Synthetic source changed before its verification read.")
        local_path(selected, exists=True)
        _cancel(cancelled)
        yield stream, before
        _cancel(cancelled)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("Synthetic source changed during verification.")
    if _stamp(_ordinary(selected)) != _stamp(before):
        raise ValueError("Synthetic source changed after verification.")


def _hash_stream(stream, expected_size: int, *, cancelled=lambda: False) -> tuple[str, int]:
    hashed, size = sha256(), 0
    while True:
        _cancel(cancelled)
        block = stream.read(min(CHUNK_BYTES, expected_size - size + 1))
        if not block:
            break
        hashed.update(block)
        size += len(block)
        if size > expected_size:
            raise ValueError("Synthetic source grew during verification.")
    if size != expected_size:
        raise ValueError("Synthetic source length changed during verification.")
    return hashed.hexdigest(), size


def digest_file(path: Path, *, cancelled=lambda: False) -> tuple[str, int]:
    """Hash one actually held regular descriptor, with bounded cancellable reads."""
    with _opened(path, cancelled=cancelled) as (stream, before):
        result = _hash_stream(stream, before.st_size, cancelled=cancelled)
    return result


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _write_new(path: Path, value) -> None:
    data = _json_bytes(value)
    with path.open("xb") as stream:
        if stream.write(data) != len(data):
            raise OSError("Incomplete synthetic evidence write.")
        stream.flush()
        os.fsync(stream.fileno())


def _write_source(path: Path, book: int, sizes: tuple[int, ...], *,
                  bom: bool, cancelled=lambda: False) -> dict:
    """Write the complete real source. No truncate, seek, sparse or size mocking."""
    hashed, rows, size = sha256(), [], 0
    with path.open("xb") as stream:
        def write(block):
            nonlocal size
            _cancel(cancelled)
            if len(block) > CHUNK_BYTES:
                raise AssertionError("Synthetic write exceeds its buffer budget.")
            if stream.write(block) != len(block):
                raise OSError("Incomplete synthetic source write.")
            hashed.update(block)
            size += len(block)
        if bom:
            write(b"\xef\xbb\xbf")
        for ordinal, expected_size in enumerate(sizes, 1):
            if ordinal > 1:
                write(b"\f")
            offset, raw_hash, characters = size, sha256(), 0
            for block in segment_parts(book, ordinal, expected_size):
                write(block)
                raw_hash.update(block)
                characters += len(block.decode("utf-8"))
            if size - offset != expected_size:
                raise AssertionError("Synthetic segment byte count changed.")
            rows.append({"ordinal": ordinal, "offset_bytes": offset, "size_bytes": expected_size,
                         "characters": characters, "sha256": raw_hash.hexdigest(),
                         "anchor": anchor(book, ordinal)})
        stream.flush()
        os.fsync(stream.fileno())
    if _ordinary(path).st_size != size:
        raise ValueError("Synthetic source length changed after close.")
    return {"book": book, "file": path.name, "source_sha256": hashed.hexdigest(),
            "size_bytes": size, "utf8_bom": bom, "segments": rows}


def _spec_record(spec) -> tuple[dict, int, int, int]:
    if isinstance(spec, CorpusSpec):
        spec.checked()
        return {"kind": "corpus", **asdict(spec)}, spec.books, spec.segments, spec.source_bytes
    if isinstance(spec, LargeTextSpec):
        spec.checked()
        return {"kind": "large_text", **asdict(spec)}, 1, spec.segments, spec.source_bytes
    raise TypeError("A checked corpus or large-text specification is required.")


def generate_fixture(directory: Path, spec: CorpusSpec | LargeTextSpec, *,
                     search_spool_budget_bytes=2 * GIB, free_reserve_bytes=GIB,
                     memory_headroom_bytes=1536 * MIB, cancelled=lambda: False,
                     checkpoint: Callable[[dict], None] | None = None) -> dict:
    """Create one new owned fixture directory and an append-only manifest."""
    record, books, segments, expected_bytes = _spec_record(spec)
    plan = capacity_plan(expected_bytes, books, segments,
                         search_spool_budget_bytes=search_spool_budget_bytes,
                         free_reserve_bytes=free_reserve_bytes,
                         memory_headroom_bytes=memory_headroom_bytes)
    directory = Path(directory).absolute()
    directory.mkdir(parents=False, exist_ok=False)
    owner = {"schema": SCHEMA, "owner": uuid4().hex, "spec": record,
             "created_utc": datetime.now(timezone.utc).isoformat()}
    _write_new(directory / "owner.json", owner)
    admission = preflight(directory, plan)
    _write_new(directory / "preflight.json", admission)
    if admission["status"] == "NOT_RUN":
        result = {"schema": SCHEMA, "status": "NOT_RUN", "spec": record,
                  "preflight": admission, "completed_books": 0,
                  "generated_bytes": 0, "library_benchmark": "NOT_RUN"}
        _write_new(directory / "generation.json", result)
        return result
    sources = directory / "sources"
    sources.mkdir()
    manifest_path = directory / "sources.jsonl"
    started = time.perf_counter()
    manifest_hash, completed, actual_bytes = sha256(), 0, 0
    result = None
    try:
        with manifest_path.open("xb") as manifest:
            def line(value):
                block = _json_bytes(value)
                if len(block) > MAX_MANIFEST_LINE:
                    raise ValueError("Synthetic manifest row exceeds its explicit bound.")
                manifest.write(block)
                manifest_hash.update(block)
            line({"schema": SCHEMA, "spec": record, "owner": owner["owner"]})
            for book in range(books):
                _cancel(cancelled)
                sizes = ((spec.segment_bytes,) * spec.segments_per_book
                         if isinstance(spec, CorpusSpec) else tuple(spec.sizes()))
                source = _write_source(sources / f"book-{book:05d}.txt", book, sizes,
                                       bom=book == 0, cancelled=cancelled)
                line(source)
                manifest.flush()
                os.fsync(manifest.fileno())
                completed += 1
                actual_bytes += source["size_bytes"]
                if checkpoint is not None:
                    checkpoint({"completed_books": completed, "generated_bytes": actual_bytes,
                                "last_source_sha256": source["source_sha256"]})
        if completed != books or actual_bytes != expected_bytes:
            raise AssertionError("Actual source generation does not match the requested profile.")
        result = {"schema": SCHEMA, "status": "FIXTURE_GENERATED_NOT_BENCHMARKED",
                  "spec": record, "completed_books": completed, "segments": segments,
                  "generated_bytes": actual_bytes, "manifest_sha256": manifest_hash.hexdigest(),
                  "generation_seconds": time.perf_counter() - started,
                  "library_benchmark": "NOT_RUN", "preflight": admission}
    except BaseException as error:
        result = {"schema": SCHEMA, "status": "INCOMPLETE_FIXTURE", "spec": record,
                  "completed_books": completed, "completed_source_bytes": actual_bytes,
                  "generation_seconds": time.perf_counter() - started,
                  "error_type": type(error).__name__, "error": str(error)[:1024],
                  "library_benchmark": "NOT_RUN", "preflight": admission}
        _write_new(directory / "generation.json", result)
        raise
    _write_new(directory / "generation.json", result)
    return result


def source_records(directory: Path, expected_manifest_sha256: str, *, cancelled=lambda: False):
    """Parse the same held bytes whose exact digest is checked before and after."""
    directory = Path(directory)
    if (type(expected_manifest_sha256) is not str or len(expected_manifest_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_manifest_sha256)):
        raise ValueError("Invalid expected manifest hash.")
    with _opened(directory / "sources.jsonl", cancelled=cancelled) as (stream, before):
        actual, _ = _hash_stream(stream, before.st_size, cancelled=cancelled)
        if actual != expected_manifest_sha256:
            raise ValueError("Synthetic manifest digest mismatch.")
        stream.seek(0)
        _cancel(cancelled)
        parsed_hash = sha256()
        first = stream.readline(MAX_MANIFEST_LINE + 1)
        if len(first) > MAX_MANIFEST_LINE or not first.endswith(b"\n"):
            raise ValueError("Synthetic manifest header is incomplete or too large.")
        parsed_hash.update(first)
        header = json.loads(first)
        if not isinstance(header, dict) or header.get("schema") != SCHEMA:
            raise ValueError("Unexpected synthetic manifest schema.")
        index = 0
        while True:
            _cancel(cancelled)
            raw = stream.readline(MAX_MANIFEST_LINE + 1)
            if not raw:
                break
            if len(raw) > MAX_MANIFEST_LINE or not raw.endswith(b"\n"):
                raise ValueError("Synthetic manifest row is incomplete or too large.")
            parsed_hash.update(raw)
            row = json.loads(raw)
            if (not isinstance(row, dict) or row.get("book") != index
                    or row.get("file") != f"book-{index:05d}.txt"):
                raise ValueError("Synthetic manifest source order changed.")
            if not 0 <= index < MAX_BOOKS:
                raise ValueError("Synthetic manifest contains too many books.")
            yield row
            index += 1
        _cancel(cancelled)
        if parsed_hash.hexdigest() != expected_manifest_sha256:
            raise ValueError("Parsed synthetic manifest bytes differ from their expected digest.")


def verify_originals(directory: Path, generation: dict, *, cancelled=lambda: False) -> dict:
    if generation.get("status") != "FIXTURE_GENERATED_NOT_BENCHMARKED":
        raise ValueError("Only a complete generated fixture can be verified.")
    count, total, segments = 0, 0, 0
    for row in source_records(directory, generation["manifest_sha256"], cancelled=cancelled):
        _cancel(cancelled)
        actual, size = digest_file(Path(directory) / "sources" / row["file"], cancelled=cancelled)
        if (actual, size) != (row["source_sha256"], row["size_bytes"]):
            raise ValueError("Synthetic original source hash or length changed.")
        count += 1
        total += size
        segments += len(row["segments"])
    if (count, total, segments) != (generation["completed_books"], generation["generated_bytes"],
                                    generation["segments"]):
        raise ValueError("Synthetic source population changed.")
    return {"verified_sources": count, "verified_original_bytes": total,
            "declared_original_segments": segments}


def tree_bytes(directory: Path, *, maximum_entries: int, cancelled=lambda: False) -> dict:
    """Actual file accounting; depth-first iterators never materialize a directory."""
    if type(maximum_entries) is not int or not 1 <= maximum_entries <= 20_000_000:
        raise ValueError("An explicit finite inventory allowance is required.")
    entries, files, logical, allocated = 0, 0, 0, 0
    allocation_measured = True

    def walk(folder, depth):
        nonlocal entries, files, logical, allocated, allocation_measured
        _cancel(cancelled)
        if depth > 128:
            raise ValueError("Fixture inventory exceeded its finite directory depth.")
        metadata = folder.lstat()
        if (not stat.S_ISDIR(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & 0x400):
            raise ValueError("Fixture inventory contains a nonordinary directory.")
        with os.scandir(folder) as iterator:
            for item in iterator:
                _cancel(cancelled)
                entries += 1
                if entries > maximum_entries:
                    raise ValueError("Fixture inventory exceeded its measured entry allowance.")
                value = item.stat(follow_symlinks=False)
                if getattr(value, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Fixture inventory contains a reparse entry.")
                if stat.S_ISDIR(value.st_mode):
                    walk(Path(item.path), depth + 1)
                elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
                    files += 1
                    logical += value.st_size
                    if hasattr(value, "st_blocks"):
                        allocated += value.st_blocks * 512
                    else:
                        allocation_measured = False
                else:
                    raise ValueError("Fixture inventory contains a link or special object.")
    walk(Path(directory), 0)
    return {"entries": entries, "files": files, "logical_bytes": logical,
            "allocated_bytes": allocated if allocation_measured else None,
            "allocation_method": "stat.st_blocks * 512" if allocation_measured else "NOT_MEASURED"}


def distribution(seconds: list[float]) -> dict:
    if (not seconds or any(type(value) not in (int, float) or not math.isfinite(value)
                           or value < 0 for value in seconds)):
        raise ValueError("Finite nonnegative actual elapsed times are required.")
    ordered = sorted(seconds)
    size = len(ordered)
    middle = size // 2
    median = ordered[middle] if size % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {"samples": size, "p50_seconds": median,
            "p95_seconds": ordered[math.ceil(size * .95) - 1],
            "p95_method": "nearest rank", "minimum_seconds": ordered[0],
            "maximum_seconds": ordered[-1]}


def process_memory() -> dict:
    """Exact lifetime peak for this coordinator; never mislabeled family RSS."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        *[(name, ctypes.c_size_t) for name in (
                            "PeakWorkingSetSize", "WorkingSetSize",
                            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                            "PagefileUsage", "PeakPagefileUsage")]]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters),
                                             wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),
                                          ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"coordinator_peak_bytes": int(counters.PeakWorkingSetSize),
                "coordinator_method": "GetProcessMemoryInfo.PeakWorkingSetSize",
                "largest_reaped_child_peak_bytes": None,
                "simultaneous_family_peak_bytes": None}
    import resource
    multiplier = 1 if sys.platform == "darwin" else 1024
    return {"coordinator_peak_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                                          * multiplier),
            "coordinator_method": "resource.getrusage(RUSAGE_SELF).ru_maxrss",
            "largest_reaped_child_peak_bytes": int(
                resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * multiplier),
            "simultaneous_family_peak_bytes": None}


def main(argv=None) -> int:
    if not __debug__:
        raise ValueError("The benchmark requires executable assertions; do not use -O.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    profile = parser.add_mutually_exclusive_group(required=True)
    profile.add_argument("--books", type=int)
    profile.add_argument("--large-bytes", type=int)
    parser.add_argument("--segments", type=int)
    parser.add_argument("--segment-bytes", type=int, default=256)
    parser.add_argument("--search-spool-bytes", type=int, default=2 * GIB)
    parser.add_argument("--free-reserve-bytes", type=int, default=GIB)
    parser.add_argument("--memory-headroom-bytes", type=int, default=1536 * MIB)
    args = parser.parse_args(argv)
    if args.books is not None:
        spec = CorpusSpec(args.books, args.segments if args.segments is not None else 40,
                          args.segment_bytes).checked()
    else:
        if args.segments is None:
            parser.error("--large-bytes requires an explicit --segments value.")
        spec = LargeTextSpec(args.large_bytes, args.segments).checked()
    result = generate_fixture(args.output_dir, spec,
        search_spool_budget_bytes=args.search_spool_bytes,
        free_reserve_bytes=args.free_reserve_bytes,
        memory_headroom_bytes=args.memory_headroom_bytes)
    if result["status"] != "NOT_RUN":
        result["original_verification"] = verify_originals(args.output_dir, result)
        result["memory"] = process_memory()
        result["environment"] = {"python": sys.version, "platform": platform.platform()}
        _write_new(args.output_dir / "verified-generation.json", result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 2 if result["status"] == "NOT_RUN" else 0


if __name__ == "__main__":
    raise SystemExit(main())
