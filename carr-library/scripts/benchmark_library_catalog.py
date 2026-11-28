"""Measure real public import, restart, catalog and search workflows.

This is a synthetic-corpus benchmark adapter, not a product importer or index.
All catalog writes use LibraryApp. Read-only SQLite checks are independent
oracles for the original manifest; they never seed or repair product rows.
Run init, import and verify/search in separate invocations for a real restart.
"""
from __future__ import annotations

import argparse
from array import array
from contextlib import contextmanager
from hashlib import sha256
import importlib.util
import json
import math
import os
from pathlib import Path
import importlib
import re
import sys
import time

from filelock import FileLock

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, local_path, read_json_snapshot, regular
from carr_library.ingest.streamed_text import TextProfile
from carr_library.ingest.resource_budget import ImportBudget
from carr_library.operations import ResourceBroker, ResourceLimits

SCHEMA = "carr-library-catalog-benchmark/2"
MIB, GIB = 1024**2, 1024**3
MAX_STATE = 512 * 1024
MAX_ROWS = 2_000_000
MAX_EVENTS = 200_000
_TITLE = re.compile(r"book-([0-9]{5})\Z")
_ANCHOR = re.compile(r"carrscale([0-9]{5})p([0-9]{4})")
_PAGE = re.compile(r"Segment ([1-9][0-9]*) \(unpaginated\)\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")

# Import exactly the adjacent authored generator, without changing sys.path.
_generator_path = Path(__file__).with_name("benchmark_library_scale.py")
_spec = importlib.util.spec_from_file_location("_carr_scale_generator", _generator_path)
if _spec is None or _spec.loader is None:
    raise RuntimeError("The exact fixture generator is unavailable.")
fixture = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fixture
_spec.loader.exec_module(fixture)

_ORIGINS = (
    "application", "catalog.intake", "ingest.batches", "ingest.batch_pages",
    "ingest.streamed_text", "ingest.streamed_ingest", "ingest.worker", "ingest.pipeline",
    "ingest.resource_budget", "ingest.retained_assets", "ingest.session", "catalog.registration",
    "storage.revisions", "citations.streamed_text_attestation", "search.pagination",
    "search.source_windows", "search.federated", "search.federated_pages",
    "audio.process", "audio.cleanup", "durable_state",
)


def read_json(path, *, maximum):
    """Use the current guarded bounded JSON reader, including duplicate rejection."""
    return read_json_snapshot(path, max_bytes=maximum)[0]


def implementation_identity():
    result = {}
    for name in _ORIGINS:
        module = importlib.import_module("carr_library." + name)
        path = Path(module.__file__).resolve()
        result[name] = dict(path=str(path), sha256=fixture.digest_file(path)[0])
    for name, path in (("benchmark_script", Path(__file__)), ("fixture_generator", _generator_path)):
        result[name] = dict(path=str(path.resolve()), sha256=fixture.digest_file(path)[0])
    return result


def _implementation_bytes(value):
    return {name: record["sha256"] for name, record in value.items()}


def worker_peaks(directory, deadline):
    count, peak = 0, 0
    methods = set()
    with os.scandir(directory / "benchmark" / "worker-peaks") as entries:
        for entry in entries:
            deadline.check()
            if not entry.name.endswith(".json"):
                raise ValueError("Unexpected worker measurement entry.")
            count += 1
            if count > MAX_EVENTS:
                raise ValueError("The worker measurement inventory exceeds its bound.")
            value = read_json(Path(entry.path), maximum=8192)
            if (type(value) is not dict or type(value.get("pid")) is not int or value["pid"] <= 0
                    or type(value.get("peak_bytes")) is not int or value["peak_bytes"] <= 0):
                raise ValueError("Invalid actual worker memory measurement.")
            peak = max(peak, value["peak_bytes"])
            methods.add(value["method"])
    return dict(measured_worker_exits=count, maximum_worker_own_peak_bytes=peak if count else None,
                methods=sorted(methods), simultaneous_family_peak_bytes=None,
                note="Actual per-worker exit peaks; concurrent family peak is not measured.")


class Deadline:
    def __init__(self, seconds, cancelled=lambda: False):
        if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                or not .1 <= seconds <= 86400 or not callable(cancelled)):
            raise ValueError("Choose a finite benchmark operation allowance.")
        self.end = time.monotonic() + seconds
        self.cancelled = cancelled

    def stopped(self):
        return bool(self.cancelled()) or time.monotonic() >= self.end

    def check(self):
        if self.stopped():
            raise InterruptedError("Benchmark cancelled or its explicit allowance elapsed.")

    def remaining(self):
        self.check()
        return self.end - time.monotonic()


def _require(value, message):
    if not value:
        raise AssertionError(message)


def _root(directory):
    selected = local_path(Path(directory).absolute(), exists=True)
    regular(selected, directory=True)
    return selected


def _identity(path):
    value = regular(path, directory=True)
    return [value.st_dev, value.st_ino, value.st_mode,
            getattr(value, "st_file_attributes", 0)]


def _generation(directory, cancelled=lambda: False):
    value = read_json(directory / "generation.json", maximum=MAX_STATE)
    if (type(value) is not dict or value.get("schema") != fixture.SCHEMA
            or value.get("status") != "FIXTURE_GENERATED_NOT_BENCHMARKED"
            or type(value.get("segments")) is not int
            or not 1 <= value["segments"] <= MAX_ROWS):
        raise ValueError("A complete, bounded generated fixture is required.")
    # Validate the complete original manifest and every selected original.
    fixture.verify_originals(directory, value, cancelled=cancelled)
    return value


def _state(directory):
    state = read_json(directory / "benchmark" / "state.json", maximum=MAX_STATE)
    if (type(state) is not dict or state.get("schema") != SCHEMA
            or state.get("fixture") != str(directory)
            or not _ID.fullmatch(str(state.get("workspace", "")))
            or not _ID.fullmatch(str(state.get("collection", "")))
            or type(state.get("foreign_workspaces")) is not list
            or len(state["foreign_workspaces"]) > 24
            or any(not _ID.fullmatch(str(item)) for item in state["foreign_workspaces"])
            or state.get("library_identity") != _identity(directory / "library")
            or type(state.get("events")) is not int or not 0 <= state["events"] <= MAX_EVENTS):
        raise ValueError("The private benchmark profile or state changed.")
    ImportBudget.from_dict(state["import_budget"])
    for key in ("first_batch", "current_batch"):
        if not _ID.fullmatch(str(state.get(key, ""))):
            raise ValueError("The saved public batch identity changed.")
    if fixture.digest_file(directory / "generation.json")[0] != state["generation_sha256"]:
        raise ValueError("The completed generation record changed.")
    if _implementation_bytes(implementation_identity()) != _implementation_bytes(state["implementation"]):
        raise ValueError("The measured application or benchmark implementation bytes changed.")
    generation = read_json(directory / "generation.json", maximum=MAX_STATE)
    if (generation.get("manifest_sha256") != state["manifest_sha256"]
            or generation.get("completed_books") != state["books"]
            or generation.get("segments") != state["authored_fields"]
            or state["segments"] != sum(row["count"] for row in state["fragment_geometry"]) * state["books"]
            or state["fragment_geometry"] != list(_fragment_geometry(generation))):
        raise ValueError("The benchmark source population changed.")
    return state, generation


def _save(directory, state):
    atomic_json(directory / "benchmark" / "state.json", state, replace=True)


@contextmanager
def _locked(directory):
    directory = _root(directory)
    regular(local_path(directory / "benchmark", exists=True), directory=True)
    with FileLock(directory / "benchmark" / "run.lock", timeout=0, thread_local=False):
        yield directory


def _event(directory, state, kind, **values):
    index = state["events"]
    if index >= MAX_EVENTS:
        raise ValueError("The benchmark event allowance is full.")
    event = dict(schema=SCHEMA, index=index, kind=kind, pid=os.getpid(),
                 monotonic=time.monotonic(), **values)
    path = directory / "benchmark" / "events" / f"{index:06d}.json"
    # Events are immutable. A previous interrupted state save is preserved.
    while path.exists():
        index += 1
        if index >= MAX_EVENTS:
            raise ValueError("The benchmark event allowance is full.")
        path = path.with_name(f"{index:06d}.json")
    event["index"] = index
    atomic_json(path, event)
    state["events"] = index + 1
    _save(directory, state)
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)
    return event


def _field_sizes(generation):
    spec = generation["spec"]
    if spec["kind"] == "corpus":
        return (spec["segment_bytes"],) * spec["segments_per_book"]
    if spec["kind"] == "large_text":
        return tuple(fixture.LargeTextSpec(spec["source_bytes"], spec["segments"]).sizes())
    raise ValueError("Unknown generated source profile.")


def _authored_field(generation, book, field):
    sizes = _field_sizes(generation)
    _require(0 <= book < generation["completed_books"] and 1 <= field <= len(sizes),
             "An original field is outside the authored source population.")
    return b"".join(fixture.segment_parts(book, field, sizes[field - 1])).decode("utf-8")


def _fragment_geometry(generation):
    """Independent authored oracle for the current fixed Unicode-offset contract.

    These sizes derive from generated fixture bytes, not from the application's
    parser or catalog. Source fields and catalog fragments remain distinct.
    """
    _require(fixture.FILLER_BLOCK.isascii() and fixture.SUFFIX.isascii(),
             "The authored geometry oracle requires the exact ASCII filler contract.")
    ordinal, rows = 1, []
    for field, size in enumerate(_field_sizes(generation), 1):
        prefix = next(fixture.segment_parts(0, field, size))
        characters = size - len(prefix) + len(prefix.decode("utf-8"))
        count = (characters + 65535) // 65536
        rows.append(dict(field=field, first=ordinal, count=count,
                         characters=characters, size_bytes=size))
        ordinal += count
    return tuple(rows)


def _source_fragments(generation, source):
    ordinal = 1
    for field in source["segments"]:
        text = _authored_field(generation, source["book"], field["ordinal"])
        _require(sha256(text.encode("utf-8")).hexdigest() == field["sha256"]
                 and len(text) == field["characters"]
                 and len(text.encode("utf-8")) == field["size_bytes"],
                 "The independent authored field differs from the original manifest.")
        for start in range(0, len(text), 65536):
            raw = text[start:start + 65536]
            yield dict(ordinal=ordinal, field=field["ordinal"], offset=start,
                       raw=raw, characters=len(raw), size_bytes=len(raw.encode("utf-8")),
                       sha256=sha256(raw.encode("utf-8")).hexdigest(),
                       anchor=field["anchor"] if start == 0 else None)
            ordinal += 1


def _profile(generation, *, seconds, steps, memory_bytes):
    spec, sizes = generation["spec"], _field_sizes(generation)
    source = (sum(sizes) + len(sizes) + 2 if spec["kind"] == "corpus"
              else spec["source_bytes"])
    # Every real fragment has at most 65536 Unicode scalars. The upper bound
    # uses four UTF-8 bytes per scalar, not the donor's whole-field geometry.
    segment = min(max(sizes), 4 * 65536)
    segments = sum(row["count"] for row in _fragment_geometry(generation))
    if segments * generation["completed_books"] > MAX_ROWS:
        raise ValueError("The actual catalog fragment population exceeds the benchmark bound.")
    growth = max(64*MIB, generation["generated_bytes"] * 6
                 + segments * generation["completed_books"] * 1024
                 + generation["completed_books"] * 16384)
    return ImportBudget(source_bytes=source, segment_bytes=segment, segments=segments,
        steps=min(steps, segments + 1), seconds=seconds, memory_bytes=memory_bytes,
        library_bytes=growth, temporary_bytes=source + 128*MIB)

def initialize(directory, *, page_files=1000, steps=1001,
               slice_seconds=600.0, memory_bytes=GIB, search_disk_bytes=2*GIB,
               reserve_bytes=GIB, foreign_libraries=21, cancelled=lambda: False):
    """Create a new private LibraryApp profile and its first real review page."""
    directory = _root(directory)
    if (type(page_files) is not int or not 1 <= page_files <= 1000
            or type(steps) is not int or not 1 <= steps <= MAX_ROWS + 1
            or type(foreign_libraries) is not int or not 1 <= foreign_libraries <= 24
            or type(search_disk_bytes) is not int or not 4*MIB <= search_disk_bytes <= 64*GIB
            or type(reserve_bytes) is not int or reserve_bytes < 0):
        raise ValueError("Invalid explicit benchmark profile.")
    generation = _generation(directory, cancelled)
    budget = _profile(generation, seconds=slice_seconds, steps=steps, memory_bytes=memory_bytes)
    # Conservative coordinator headroom for the current fixed source windows.
    # This is an estimate, not a nonexistent per-search RAM policy or OS limit.
    search_memory_bytes = max(64*MIB, budget.segment_bytes * 32 + MIB)
    capacity = fixture.capacity_plan(generation["generated_bytes"],
        generation["completed_books"], generation["segments"],
        search_spool_budget_bytes=search_disk_bytes, free_reserve_bytes=reserve_bytes,
        memory_headroom_bytes=max(memory_bytes, search_memory_bytes) + 256*MIB)
    admission = fixture.preflight(directory, capacity)
    if admission["status"] == "NOT_RUN":
        return dict(schema=SCHEMA, status="NOT_RUN", stage="application admission", admission=admission)
    # Existing user libraries are never benchmark targets.
    (directory / "benchmark").mkdir()
    (directory / "benchmark" / "events").mkdir()
    (directory / "benchmark" / "worker-peaks").mkdir()
    (directory / "benchmark" / "worker-starts").mkdir()
    # Exclusive directory creation prevents adopting an existing user profile.
    (directory / "library").mkdir()
    app = LibraryApp(directory / "library")
    from carr_library.keybindings import encode_preferences
    (app.root / 'keybindings.json').write_bytes(encode_preferences({}))
    from carr_library.ingest.session import WRITE_HEADROOM
    limits = ResourceLimits(cpu_slots=1, memory_bytes=memory_bytes,
        disk_bytes=budget.library_bytes + budget.temporary_bytes + WRITE_HEADROOM,
        min_free_disk_bytes=reserve_bytes)
    ResourceBroker.for_app(app.root, limits=limits)
    workspace = app.create_workspace("Measured synthetic original corpus")
    collection = app.create_collection(workspace, "Explicit measured corpus")
    foreign = []
    for index in range(foreign_libraries):
        fixture._cancel(cancelled)
        identity = app.create_workspace(f"Excluded scope {index:02d}")
        result = app.add_text(identity, f"Private source {index:02d}",
                             f"commonrailway PRIVATE_UNSELECTED_MARKER_{index:02d}.\r\n")
        _require(result["state"] == "complete", "The genuine scope-control import did not complete.")
        foreign.append(identity)
    started = time.perf_counter()
    page = app.plan_import_page(workspace, [directory / "sources"],
        collections=(collection,), import_budget=budget, max_files=page_files,
        max_entries=20000, timeout=60, max_bytes=4*MIB, cancelled=cancelled)
    state = dict(schema=SCHEMA, fixture=str(directory),
        library_identity=_identity(app.root), workspace=workspace, collection=collection,
        foreign_workspaces=foreign, manifest_sha256=generation["manifest_sha256"],
        generation_sha256=fixture.digest_file(directory / "generation.json")[0],
        implementation=implementation_identity(),
        books=generation["completed_books"], authored_fields=generation["segments"],
        segments=budget.segments * generation["completed_books"],
        fragment_geometry=list(_fragment_geometry(generation)),
        import_budget=budget.to_dict(), page_files=page_files,
        first_batch=page["batch_id"], current_batch=page["batch_id"],
        scan_complete=False, import_complete=False, events=0,
        created_pid=os.getpid(), search_disk_bytes=search_disk_bytes,
        search_memory_bytes=search_memory_bytes,
        admission=admission)
    atomic_json(directory / "benchmark" / "state.json", state)
    with _locked(directory):
        _event(directory, state, "initialized", initial_scan_seconds=time.perf_counter()-started,
               initial_rows=len(page["rows"]), initial_scan_reason=page["scan"]["reason"],
               source_books=state["books"], source_segments=state["segments"],
               import_budget=budget.to_dict(), coordinator_memory=fixture.process_memory())
    return state


# Only the instrumentation prefix is added; the real worker/bootstrap/runner,
# exact requests, isolated interpreter, deadlines and native owner are retained.
_PEAK_CODE = r'''
import atexit as _ca, json as _cj, os as _co, sys as _cs
def _carr_record_peak():
    if _co.name == "nt":
        import ctypes as c
        from ctypes import wintypes as w
        class P(c.Structure):
            _fields_ = [("cb", w.DWORD), ("faults", w.DWORD)] + [(n, c.c_size_t) for n in ("peak","working","pqp","qp","pqnp","qnp","page","peakpage")]
        k=c.WinDLL("kernel32",use_last_error=True); p=c.WinDLL("psapi",use_last_error=True)
        k.GetCurrentProcess.argtypes=[]; k.GetCurrentProcess.restype=w.HANDLE
        p.GetProcessMemoryInfo.argtypes=[w.HANDLE,c.POINTER(P),w.DWORD]; p.GetProcessMemoryInfo.restype=w.BOOL
        v=P(); v.cb=c.sizeof(v)
        if not p.GetProcessMemoryInfo(k.GetCurrentProcess(),c.byref(v),v.cb): raise c.WinError(c.get_last_error())
        peak=int(v.peak); method="GetProcessMemoryInfo.PeakWorkingSetSize"
    else:
        import resource as r
        peak=int(r.getrusage(r.RUSAGE_SELF).ru_maxrss)*(1 if _cs.platform=="darwin" else 1024)
        method="resource.getrusage(RUSAGE_SELF).ru_maxrss"
    value={"pid":_co.getpid(),"peak_bytes":peak,"method":method,"executable":_cs.executable,"prefix":_cs.prefix,"protocol_sha256":_carr_protocol_sha256}
    with open(_carr_peak_path,"x",encoding="utf-8",newline="\n") as f:
        f.write(_cj.dumps(value,sort_keys=True)+"\n"); f.flush(); _co.fsync(f.fileno())
_ca.register(_carr_record_peak)
'''


@contextmanager
def observe_import_workers(directory, *, cancellation=None):
    """Measure only this profile's actual owned worker, retaining its native runner."""
    from carr_library.audio import process
    from carr_library.audio.models import AudioCancelled
    from carr_library.ingest.worker import MAX_REQUEST
    from uuid import uuid4
    original, records = process.run_process, []
    anchor = "runpy.run_module('carr_library.ingest.worker',run_name='__main__')"

    def observe(args, **options):
        if not isinstance(args, (tuple, list)) or "-c" not in args:
            return original(args, **options)
        offset = args.index("-c") + 1
        if offset + 2 >= len(args) or anchor not in args[offset]:
            return original(args, **options)
        protocol, protocol_digest = read_json_snapshot(Path(args[offset + 1]), max_bytes=MAX_REQUEST)
        if not isinstance(protocol, dict) or protocol.get("data_dir") != str(directory / "library"):
            return original(args, **options)
        if (protocol_digest != args[offset + 2] or args[offset].count(anchor) != 1
                or options.get("owned_tree") is not True):
            raise AssertionError("The measured import must retain its exact current protocol and owner.")
        token = uuid4().hex
        path = directory / "benchmark" / "worker-peaks" / (token + ".json")
        code = ("_carr_peak_path=" + repr(str(path)) + "\n_carr_protocol_sha256="
                + repr(protocol_digest) + "\n" + _PEAK_CODE)
        selected = cancellation is not None and not cancellation["attempted"]
        if selected:
            cancellation["attempted"] = True
            ready = directory / "benchmark" / "worker-starts" / (token + ".json")
            # The genuine interpreter publishes a closed atomic readiness marker.
            # This acknowledges native worker startup, not a fabricated parser checkpoint.
            code += ("\n_carr_start=" + repr(str(ready)) + "\n"
                "_carr_pending=_carr_start+'.pending'\n"
                "with open(_carr_pending,'x',encoding='ascii',newline='\\n') as _f:\n"
                "    _f.write(_cj.dumps({'token':" + repr(token) + ",'pid':_co.getpid()})); _f.flush(); _co.fsync(_f.fileno())\n"
                "_co.replace(_carr_pending,_carr_start)\n")
            current = options.get("cancelled", lambda: False)
            def stop():
                if current():
                    return True
                if cancellation["observed"] is None and ready.exists():
                    value = read_json(ready, maximum=8192)
                    _require(set(value) == {"token", "pid"} and value["token"] == token
                             and type(value["pid"]) is int and value["pid"] > 0,
                             "The actual worker startup acknowledgment changed.")
                    cancellation["observed"] = time.monotonic()
                    cancellation["pid"] = value["pid"]
                if (cancellation["observed"] is not None
                        and time.monotonic() - cancellation["observed"] >= cancellation["delay"]):
                    if cancellation["requested"] is None:
                        cancellation["requested"] = time.monotonic()
                    return True
                return False
            options = dict(options, cancelled=stop)
        command = list(args)
        command[offset] = command[offset].replace(anchor, "exec(" + repr(code) + "); " + anchor)
        began = time.perf_counter()
        try:
            native = original(command, **options)
        except BaseException as error:
            if selected:
                cancellation["native_cancelled"] = isinstance(error, AudioCancelled)
            records.append(dict(elapsed_seconds=time.perf_counter() - began,
                outcome="raised", error_type=type(error).__name__,
                own_peak_bytes=None, protocol_sha256=protocol_digest))
            raise
        else:
            value = read_json(path, maximum=8192)
            if (type(value) is not dict or type(value.get("pid")) is not int or value["pid"] <= 0
                    or type(value.get("peak_bytes")) is not int or value["peak_bytes"] <= 0):
                raise ValueError("The actual worker did not publish a valid own-process peak.")
            records.append(dict(value, elapsed_seconds=native.elapsed, returncode=native.returncode,
                                result_sha256=sha256(native.stdout).hexdigest(),
                                protocol_sha256=protocol_digest, outcome="returned"))
            return native
        finally:
            if selected:
                cancellation["returned"] = time.monotonic()
                cancellation["finished"] = True

    process.run_process = observe
    try:
        yield records
    finally:
        process.run_process = original


def cancel_import(directory, *, seconds=600, after_worker_seconds=.5):
    """Measure one genuine owned-worker cancellation, then require explicit resume."""
    if (type(after_worker_seconds) not in (int, float) or not math.isfinite(after_worker_seconds)
            or not 0 <= after_worker_seconds <= 60):
        raise ValueError("Choose a bounded interval after positively observed worker startup.")
    deadline = Deadline(seconds)
    control = dict(delay=after_worker_seconds, attempted=False, observed=None,
                   requested=None, returned=None, pid=None, finished=False, native_cancelled=False)
    with _locked(directory) as directory:
        state, generation = _state(directory)
        _require(not state["import_complete"], "Cancellation needs an unfinished explicit import.")
        app = LibraryApp(directory / "library")
        page = app.import_batch(state["workspace"], state["current_batch"])
        _require(any(row["state"] != "complete" for row in page["rows"]),
                 "Select a saved page with an unfinished source before measuring cancellation.")
        identities = [(row["row_id"], row["request_id"], row["edition_id"]) for row in page["rows"]]
        before = time.perf_counter()
        with observe_import_workers(directory, cancellation=control) as records:
            page = app.run_import_batch(state["workspace"], page["batch_id"],
                timeout=min(state["import_budget"]["seconds"], deadline.remaining()),
                cancelled=lambda: deadline.stopped() or control["finished"])
        _require([(row["row_id"], row["request_id"], row["edition_id"]) for row in page["rows"]] == identities,
                 "Cancellation replaced a saved queue/request/edition identity.")
        _require(all(row["state"] in ("queued", "complete") for row in page["rows"]),
                 "The actual import worker failed; no cancellation measurement is reported.")
        proved = (control["native_cancelled"] and control["requested"] is not None
                  and control["observed"] is not None
                  and any((row.get("result") or {}).get("state") == "cancelled" for row in page["rows"]))
        completed_before_boundary = (control["finished"] and control["requested"] is None
            and any(record["outcome"] == "returned" for record in records))
        result = dict(schema=SCHEMA,
            status="OWNED_WORKER_CANCELLATION_MEASURED" if proved else "NOT_RUN",
            reason=(None if proved else
                "The actual worker completed before the selected cancellation boundary."
                if completed_before_boundary else
                "The selected cancellation boundary was not proved; no cancellation measurement is reported."),
            batch_id=page["batch_id"], worker_pid=control["pid"], requested_delay_seconds=after_worker_seconds,
            child_start_observed=control["observed"] is not None,
            elapsed_seconds=time.perf_counter() - before,
            cancellation_to_owned_return_seconds=(control["returned"] - control["requested"]
                if control["requested"] is not None else None),
            completed_rows=page["counts"].get("complete", 0), counts=page["counts"],
            same_saved_identities=True, automatic_resume=False,
            native_cancelled=control["native_cancelled"], worker_measurements=records,
            family_proof=("unchanged owned run_process returned through cancellation cleanup"
                          if proved else "NO_CANCEL_QUALIFICATION"),
            killed_worker_peak="NOT_MEASURED", memory=fixture.process_memory(),
            originals=fixture.verify_originals(directory, generation, cancelled=deadline.stopped))
        atomic_json(directory / "benchmark" / "cancellation.json", result)
        _event(directory, state, "import_cancellation", **{k: v for k, v in result.items()
            if k not in ("schema", "originals")})
        return result


def run_import(directory, *, seconds=86400, cancelled=lambda: False,
               crash_after_first_commit=False, stop_after_slices=None):
    """Continue the saved public queue; an authored crash occurs after real commit."""
    deadline = Deadline(seconds, cancelled)
    if type(crash_after_first_commit) is not bool:
        raise TypeError("The authored crash boundary must be explicit.")
    if stop_after_slices is not None and (type(stop_after_slices) is not int or stop_after_slices < 1):
        raise ValueError("Choose a positive finite slice stop.")
    with _locked(directory) as directory:
        state, generation = _state(directory)
        app = LibraryApp(directory / "library")
        page = app.import_batch(state["workspace"], state["current_batch"])
        slices = 0
        with observe_import_workers(directory) as workers:
            while True:
                deadline.check()
                bad = [row for row in page["rows"] if row["state"] in
                       ("failed", "quarantined", "rejected", "duplicate", "excluded")]
                if bad:
                    raise AssertionError("A generated unique TXT row was not imported: " +
                                         json.dumps(bad[0], ensure_ascii=False)[:3000])
                pending = [row for row in page["rows"] if row["state"] != "complete"]
                if pending:
                    before = [(r["request_id"], r["state"], r.get("result")) for r in page["rows"]]
                    def progress(record):
                        if crash_after_first_commit and any(row["state"] == "complete" for row in record["rows"]):
                            # The real batch calls this after owned-family return
                            # and its durable queue save. No child is fabricated.
                            atomic_json(directory / "benchmark" / "authored-crash.json",
                                dict(schema=SCHEMA, pid=os.getpid(), batch_id=record["batch_id"],
                                     committed=sum(r["state"] == "complete" for r in record["rows"]),
                                     elapsed_seconds=time.perf_counter()-started,
                                     workers=workers[previous_workers:],
                                     boundary="durable batch save after actual owned worker return"))
                            os._exit(73)
                    started = time.perf_counter()
                    previous_workers = len(workers)
                    page = app.run_import_batch(state["workspace"], page["batch_id"],
                        cancelled=deadline.stopped,
                        timeout=min(state["import_budget"]["seconds"], deadline.remaining()),
                        progress=progress)
                    elapsed = time.perf_counter() - started
                    slices += 1
                    _event(directory, state, "import_slice", batch_id=page["batch_id"],
                        elapsed_seconds=elapsed, counts=page["counts"],
                        measured_workers=len(workers) - previous_workers,
                        worker_receipts="benchmark/worker-peaks",
                        coordinator_memory=fixture.process_memory())
                    workers.clear()
                    after = [(r["request_id"], r["state"], r.get("result")) for r in page["rows"]]
                    if before == after:
                        raise RuntimeError("The real saved import made no progress; no benchmark PASS is reported.")
                    if stop_after_slices is not None and slices >= stop_after_slices:
                        return dict(status="PAUSED_BETWEEN_REAL_SLICES", batch_id=page["batch_id"],
                                    slices=slices, counts=page["counts"])
                    continue
                if page["scan"]["cursor"]["complete"]:
                    state.update(scan_complete=True, import_complete=True)
                    _event(directory, state, "import_complete", final_batch=page["batch_id"],
                           slices_this_invocation=slices, coordinator_memory=fixture.process_memory())
                    return dict(status="IMPORT_COMPLETE", final_batch=page["batch_id"], slices=slices)
                started = time.perf_counter()
                following = app.continue_import_scan(state["workspace"], page["batch_id"],
                    max_files=state["page_files"], max_entries=20000, max_bytes=4*MIB,
                    timeout=min(60, deadline.remaining()), cancelled=deadline.stopped)
                _require(following["batch_id"] != page["batch_id"], "A nonfinal scan did not advance.")
                state["current_batch"] = following["batch_id"]
                _event(directory, state, "scan_page", elapsed_seconds=time.perf_counter()-started,
                       batch_id=following["batch_id"], rows=len(following["rows"]),
                       page=following["scan"]["page"], reason=following["scan"]["reason"])
                page = following


def _edition_map(app, state, deadline):
    """At most 800 KiB for 50k identities; never materialize all row dictionaries."""
    editions = bytearray(state["books"] * 16)
    identity, previous, page_number, count = state["first_batch"], None, 0, 0
    while True:
        deadline.check()
        page = app.import_batch(state["workspace"], identity)
        scan = page["scan"]
        _require(scan["page"] == page_number and scan["previous"] == previous,
                 "The saved scan page chain changed.")
        for row in page["rows"]:
            deadline.check()
            name = Path(row["source"]).name
            match = _TITLE.fullmatch(Path(name).stem)
            _require(match is not None and name.endswith(".txt") and row["state"] == "complete",
                     "The saved queue contains an incomplete or unexpected source.")
            book = int(match[1])
            _require(0 <= book < state["books"] and _ID.fullmatch(row["edition_id"]),
                     "The saved edition identity changed.")
            start = book * 16
            _require(not any(editions[start:start+16]), "The saved scan duplicated a source.")
            editions[start:start+16] = bytes.fromhex(row["edition_id"])
            _require(row["result"].get("state") == "complete" and row["result"].get("isolated") is True,
                     "The saved row lacks its actual isolated completion.")
            count += 1
        if scan["cursor"]["complete"]:
            break
        _require(scan["next"] is not None and page_number < state["books"] + 1000,
                 "The saved scan chain has no bounded continuation.")
        previous, identity, page_number = identity, scan["next"], page_number + 1
    _require(count == state["books"], "The durable batch population differs from the original manifest.")
    return editions, page_number + 1


def operation_measurements(directory, deadline):
    scans, imports = array("d"), array("d")
    count = 0
    with os.scandir(directory / "benchmark" / "events") as entries:
        for entry in entries:
            deadline.check()
            count += 1
            if count > MAX_EVENTS:
                raise ValueError("The actual operation-event inventory exceeds its bound.")
            value = read_json(Path(entry.path), maximum=MAX_STATE)
            if value.get("kind") == "import_slice":
                imports.append(value["elapsed_seconds"])
            elif value.get("kind") == "scan_page":
                scans.append(value["elapsed_seconds"])
            elif value.get("kind") == "initialized":
                scans.append(value["initial_scan_seconds"])
    crash = directory / "benchmark" / "authored-crash.json"
    crash_record = None
    if crash.exists():
        crash_record = read_json(crash, maximum=MAX_STATE)
        if crash_record.get("boundary") != "durable batch save after actual owned worker return":
            raise ValueError("The authored restart boundary changed.")
        imports.append(crash_record["elapsed_seconds"])
    return dict(
        scan=fixture.distribution(scans) if scans else None,
        import_slices=fixture.distribution(imports) if imports else None,
        measured_scan_seconds=sum(scans), measured_import_seconds=sum(imports),
        authored_postcommit_crash=crash_record,
        note="Measured API calls, including actual worker cleanup; coordinator downtime is excluded.")

def _disk(directory, state, deadline):
    result = {}
    for label, path in (
        ("external_originals", directory / "sources"),
        ("retained_originals", directory / "library" / "originals"),
        ("application_profile", directory / "library"),
        ("benchmark_evidence", directory / "benchmark"),
    ):
        result[label] = fixture.tree_bytes(path, maximum_entries=20_000_000,
                                          cancelled=deadline.stopped)
    for name in ("library.sqlite", "imports.sqlite"):
        result[name] = {suffix or "main": regular(directory / "library" / (name + suffix)).st_size
                       for suffix in ("", "-wal", "-shm", "-journal")
                       if (directory / "library" / (name + suffix)).exists()}
    result["note"] = "application_profile includes retained originals and SQLite files; categories overlap."
    return result


def verify_catalog(directory, *, seconds=86400, cancelled=lambda: False):
    """Reopen the real catalog and compare every original raw segment."""
    deadline = Deadline(seconds, cancelled)
    with _locked(directory) as directory:
        state, generation = _state(directory)
        _require(state["import_complete"], "Complete the durable public import before verification.")
        started = time.perf_counter()
        app = LibraryApp(directory / "library")
        startup = time.perf_counter() - started
        editions, scan_pages = _edition_map(app, state, deadline)
        verified, characters, raw_bytes = 0, 0, 0
        with app.read(state["workspace"], budget=deadline) as db:
            count = db.execute("SELECT count(*) FROM catalog_edition WHERE workspace=?",
                               (state["workspace"],)).fetchone()[0]
            _require(count == state["books"], "The actual catalog book count differs.")
            for source in fixture.source_records(directory, state["manifest_sha256"],
                                                  cancelled=deadline.stopped):
                deadline.check()
                book = source["book"]
                edition = bytes(editions[book*16:book*16+16]).hex()
                selected = db.execute(
                    "SELECT e.asset,w.title,a.mime,a.size FROM catalog_edition e "
                    "JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work "
                    "JOIN catalog_asset a ON a.digest=e.asset WHERE e.workspace=? AND e.id=?",
                    (state["workspace"], edition)).fetchone()
                _require(selected is not None and tuple(selected) ==
                    (source["source_sha256"], Path(source["file"]).stem, "text/plain", source["size_bytes"]),
                    "The actual catalog source binding differs from the generated original.")
                rows = db.execute("SELECT ordinal,raw,digest FROM active_page "
                                  "WHERE workspace=? AND edition=? ORDER BY ordinal",
                                  (state["workspace"], edition))
                observed = 0
                oracles = iter(_source_fragments(generation, source))
                for observed, row in enumerate(rows, 1):
                    deadline.check()
                    oracle = next(oracles, None)
                    _require(oracle is not None, "The catalog has an extra raw fragment.")
                    encoded = row["raw"].encode("utf-8")
                    _require(row["ordinal"] == oracle["ordinal"]
                        and row["raw"] == oracle["raw"]
                        and sha256(encoded).hexdigest() == row["digest"] == oracle["sha256"]
                        and len(encoded) == oracle["size_bytes"]
                        and len(row["raw"]) == oracle["characters"]
                        and (oracle["anchor"] is None or oracle["anchor"] in row["raw"]),
                        "Original Unicode, CRLF, form-feed order or fragment offset changed.")
                    verified += 1
                    raw_bytes += len(encoded)
                    characters += len(row["raw"])
                _require(observed > 0 and next(oracles, None) is None,
                         "The last original fragment is missing.")
                retained = app.assets.resolve(source["source_sha256"],
                    profile=TextProfile(state["import_budget"]["source_bytes"],
                                        ImportBudget.from_dict(state["import_budget"])),
                    cancelled=deadline.stopped)
                _require(fixture.digest_file(retained, cancelled=deadline.stopped) ==
                         (source["source_sha256"], source["size_bytes"]), "Retained original bytes changed.")
            actual = db.execute("SELECT count(*) FROM active_page WHERE workspace=?",
                                (state["workspace"],)).fetchone()[0]
            _require(actual == verified == state["segments"], "The genuine catalog segment total differs.")
        originals = fixture.verify_originals(directory, generation, cancelled=deadline.stopped)
        result = dict(schema=SCHEMA, status="CATALOG_VERIFIED", pid=os.getpid(),
            created_pid=state["created_pid"], separate_process=os.getpid() != state["created_pid"],
            startup_seconds=startup, elapsed_seconds=time.perf_counter()-started,
            source_books=state["books"], source_segments=verified,
            authored_source_fields=state["authored_fields"], raw_utf8_bytes=raw_bytes,
            raw_characters=characters, durable_scan_pages=scan_pages, originals=originals,
            disk=_disk(directory, state, deadline), memory=fixture.process_memory(),
            worker_peaks=worker_peaks(directory, deadline),
            operations=operation_measurements(directory, deadline),
            implementation_unchanged=_implementation_bytes(implementation_identity()) == _implementation_bytes(state["implementation"]),
            actual_implementation=implementation_identity(),
            library_benchmark="SEARCH_NOT_RUN")
        result["elapsed_seconds"] = time.perf_counter() - started
        _require(result["worker_peaks"]["measured_worker_exits"] > 0,
                 "No actual primary import worker memory measurement exists.")
        _require(result["implementation_unchanged"], "Measured implementation changed during verification.")
        atomic_json(directory / "benchmark" / "catalog-verification.json", result, replace=True)
        _event(directory, state, "catalog_verified", source_books=state["books"],
               source_segments=verified, separate_process=result["separate_process"])
        return result


def _segment(generation, book, ordinal, geometry=None):
    geometry = _fragment_geometry(generation) if geometry is None else geometry
    for row in geometry:
        if row["first"] <= ordinal < row["first"] + row["count"]:
            original = _authored_field(generation, book, row["field"])
            offset = (ordinal - row["first"]) * 65536
            return original[offset:offset + 65536], sum(item["count"] for item in geometry), row["field"]
    raise AssertionError("A search locator is outside the actual authored fragment population.")


def _checked_hit(app, state, generation, editions, hit, *, copy=False, cancelled=lambda: False):
    title, location = _TITLE.fullmatch(hit.title), _PAGE.fullmatch(hit.page)
    _require(hit.workspace == state["workspace"] and hit.verified
             and title is not None and location is not None and hit.quote
             and "PRIVATE_UNSELECTED_MARKER" not in hit.quote,
             "Search crossed scope or lost independent original-source verification.")
    book, ordinal = int(title[1]), int(location[1])
    _require(0 <= book < state["books"] and hit.edition_id ==
             bytes(editions[book*16:book*16+16]).hex(),
             "Search retargeted a different saved catalog edition.")
    original, count, field = _segment(generation, book, ordinal, state["fragment_geometry"])
    for found in _ANCHOR.finditer(hit.quote):
        _require((int(found[1]), int(found[2])) == (book, field),
                 "A quoted authored-field anchor differs from its actual fragment locator.")
    _require(hit.quote in original, "Search returned text absent from the exact original fragment.")
    if copy:
        _require(app.copy(state["workspace"], hit.ref, hit.revision, "quote", cancelled=cancelled)
                 == hit.quote, "Public quotation copy changed the selected original.")
        try:
            app.copy(state["foreign_workspaces"][0], hit.ref, hit.revision, "quote", cancelled=cancelled)
        except ValueError:
            pass
        else:
            raise AssertionError("A foreign workspace adopted the selected quotation.")
    return book * count + ordinal - 1


def search_catalog(directory, *, seconds=86400, query_seconds=60,
                   query_samples=20, cancelled=lambda: False):
    """Measure the current opaque source pages and bounded window ranking.

    The current application does not expose the donor's persistent whole-scope
    ranked index. No cache, global ranking or hard memory-enforcement result is
    inferred from these current public calls.
    """
    if (type(query_samples) is not int or not 2 <= query_samples <= 100
            or type(query_seconds) not in (int, float) or not math.isfinite(query_seconds)
            or not .1 <= query_seconds <= 60):
        raise ValueError("Choose 2 through 100 query samples and at most 60 seconds per call.")
    deadline = Deadline(seconds, cancelled)
    with _locked(directory) as directory:
        state, generation = _state(directory)
        prior = read_json(directory / "benchmark" / "catalog-verification.json", maximum=MAX_STATE)
        _require(prior["status"] == "CATALOG_VERIFIED" and prior["source_segments"] == state["segments"],
                 "Verify the complete real catalog before measuring retrieval.")
        started = time.perf_counter()
        app = LibraryApp(directory / "library")
        startup = time.perf_counter() - started
        editions, _ = _edition_map(app, state, deadline)
        page_budget, character_budget = 500, 5_000_000
        def query(text, **options):
            return app.search_page(state["workspace"], text,
                collection=state["collection"], limit=100, page_budget=page_budget,
                character_budget=character_budget,
                timeout=min(query_seconds, deadline.remaining()),
                cancelled=deadline.stopped, **options)
        fragments_per_book = sum(row["count"] for row in state["fragment_geometry"])
        expected_offsets = {row["first"] - 1 for row in state["fragment_geometry"]}
        visited = bytearray((state["segments"] + 7) // 8)
        timings, scanned = array("d"), 0
        cursor, total = None, 0
        sampled = {0, (state["books"] - 1) * fragments_per_book + state["fragment_geometry"][-1]["first"] - 1}
        while True:
            deadline.check()
            began = time.perf_counter()
            page = query("commonrailway", mode="phrase", result_mode="short", cursor=cursor)
            timings.append(time.perf_counter() - began)
            scanned += page.scanned_segments
            _require(len(timings) <= state["segments"] * 2 + 1,
                     "Search continuation repeated without bounded progress.")
            for hit in page.results:
                index = _checked_hit(app, state, generation, editions, hit, cancelled=deadline.stopped)
                _require(0 <= index < state["segments"] and index % fragments_per_book in expected_offsets,
                         "A literal marker escaped its independently authored field start.")
                bit = 1 << (index % 8)
                _require(not visited[index // 8] & bit, "A result repeated across actual continuations.")
                visited[index // 8] |= bit
                total += 1
                if index in sampled:
                    _checked_hit(app, state, generation, editions, hit, copy=True, cancelled=deadline.stopped)
            if page.next_cursor is None:
                break
            _require(page.next_cursor != cursor, "An opaque source cursor did not advance.")
            cursor = page.next_cursor
        _require(total == state["authored_fields"], "Literal traversal omitted an authored source marker.")
        small = []
        for index in range(query_samples):
            deadline.check()
            book = index * (state["books"] - 1) // (query_samples - 1)
            began = time.perf_counter()
            page = query(fixture.anchor(book, 1), mode="phrase", title=f"book-{book:05d}",
                         result_mode="short")
            small.append(time.perf_counter() - began)
            _require(len(page.results) == 1, "The exact scoped anchor query was not unique.")
            _checked_hit(app, state, generation, editions, page.results[0],
                         copy=True, cancelled=deadline.stopped)
        unicode_quotes = []
        for book in dict.fromkeys((0, state["books"] - 1)):
            count = len(state["fragment_geometry"])
            text = (fixture.anchor(book, count) + " commonrailway archive.\r\n"
                    "Київ — Žilina; cafe\u0301; літо")
            cursor, matches, pages = None, 0, 0
            while True:
                deadline.check()
                page = query(text, mode="phrase", result_mode="short",
                             title=f"book-{book:05d}", cursor=cursor)
                pages += 1
                _require(pages <= fragments_per_book * 2 + 1, "The Unicode source cursor repeated.")
                for hit in page.results:
                    matches += 1
                    _require(matches == 1 and hit.quote == text,
                             "The exact Unicode/combining/CRLF quotation changed.")
                    _checked_hit(app, state, generation, editions, hit,
                                 copy=True, cancelled=deadline.stopped)
                if page.next_cursor is None:
                    break
                _require(page.next_cursor != cursor, "The Unicode continuation did not advance.")
                cursor = page.next_cursor
            _require(matches == 1, "The exact final authored-field quotation is missing.")
            unicode_quotes.append(dict(book=book, last_authored_field=count,
                actual_fragment=state["fragment_geometry"][-1]["first"], source_pages=pages,
                quote_sha256=sha256(text.encode("utf-8")).hexdigest()))
        from carr_library.search.federated import FederatedSearch
        federation = FederatedSearch(app)
        library_pages, libraries, cursor, last_hit = 0, [], None, None
        try:
            while True:
                page = federation.search_page(state["foreign_workspaces"], "commonrailway",
                    mode="phrase", result_mode="short", cursor=cursor, limit_per_library=1,
                    library_budget=20, page_budget=500, character_budget=5_000_000,
                    timeout=min(query_seconds, deadline.remaining()), cancelled=deadline.stopped)
                _require(page.selected_libraries == len(state["foreign_workspaces"])
                         and len(page.groups) <= 20, "The explicit library selection changed.")
                for group in page.groups:
                    _require(group.workspace != state["workspace"] and len(group.hits) == 1,
                             "An auxiliary selected library was lost or another scope appeared.")
                    hit = group.hits[0]
                    _require(hit.result.verified and
                        federation.copy(hit, "quote", cancelled=deadline.stopped) == hit.result.quote,
                        "The actual federated quotation lost its current source authority.")
                    libraries.append(group.workspace)
                    last_hit = hit
                library_pages += 1
                if page.next_cursor is None:
                    break
                _require(page.next_cursor != cursor and library_pages <= 2,
                         "The finite explicit library cursor did not advance.")
                cursor = page.next_cursor
            _require(libraries == state["foreign_workspaces"],
                     "The complete explicit library selection was not traversed.")
        finally:
            federation.clear()
        _require(last_hit is not None, "No real selected-library handle was produced.")
        try:
            federation.copy(last_hit, "quote", cancelled=deadline.stopped)
        except (ValueError, RuntimeError):
            handle_revoked = True
        else:
            raise AssertionError("The cleared federated handle remained usable.")
        ranked = []
        for label in ("first_bounded_window", "repeated_bounded_window"):
            began = time.perf_counter()
            page = query("commonrailway", mode="thematic", result_mode="thematic")
            elapsed = time.perf_counter() - began
            _require(page.scope_editions == state["books"] and len(page.results) <= 100
                     and page.scanned_segments <= page_budget and page.results,
                     "The measured thematic page violates the actual source-window contract.")
            for hit in page.results:
                _checked_hit(app, state, generation, editions, hit, cancelled=deadline.stopped)
            ranked.append(dict(label=label, first_page_seconds=elapsed,
                scanned_segments=page.scanned_segments, scope_editions=page.scope_editions,
                returned_results=len(page.results), ordering=page.ordering,
                has_continuation=page.next_cursor is not None))
        originals = fixture.verify_originals(directory, generation, cancelled=deadline.stopped)
        result = dict(schema=SCHEMA, status="MEASURED_SYNTHETIC_CORPUS", pid=os.getpid(),
            created_pid=state["created_pid"], separate_process=os.getpid() != state["created_pid"],
            startup_seconds=startup, elapsed_seconds=time.perf_counter() - started,
            source_books=state["books"], source_segments=state["segments"],
            authored_source_fields=state["authored_fields"], literal_results=total,
            source_pages=len(timings), scanned_segments_reported=scanned,
            source_window_limits=dict(page_budget=page_budget, character_budget=character_budget,
                                      per_call_seconds=query_seconds),
            literal_page_latency=fixture.distribution(timings),
            bounded_title_anchor_latency=fixture.distribution(small),
            bounded_window_ranking=ranked, originals=originals,
            unicode_quotations=unicode_quotes,
            explicit_library_navigation=dict(selected=len(libraries), pages=library_pages),
            federated_handle_revoked=handle_revoked,
            disk=_disk(directory, state, deadline), memory=fixture.process_memory(),
            worker_peaks=worker_peaks(directory, deadline),
            operations=operation_measurements(directory, deadline),
            implementation_unchanged=_implementation_bytes(implementation_identity()) ==
                                     _implementation_bytes(state["implementation"]),
            actual_implementation=implementation_identity(),
            global_ranking="NOT_PROVIDED_BY_CURRENT_APPLICATION", semantic_model="NOT_RUN",
            hard_memory_enforcement="NOT_MEASURED", simultaneous_family_peak="NOT_MEASURED",
            physical_or_human_quality="NOT_ASSESSED",
            cold_definition=("fresh process and LibraryApp; OS caches are not cleared"
                if os.getpid() != state["created_pid"] else
                "fresh LibraryApp in the same process; not a process restart; OS caches are not cleared"))
        result["elapsed_seconds"] = time.perf_counter() - started
        _require(result["worker_peaks"]["measured_worker_exits"] > 0,
                 "No actual import worker memory measurement exists.")
        _require(result["implementation_unchanged"], "Measured implementation changed during retrieval.")
        atomic_json(directory / "benchmark" / "search-measurements.json", result, replace=True)
        _event(directory, state, "search_measured", source_books=state["books"],
               source_segments=state["segments"], literal_results=total, source_pages=len(timings))
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "import", "cancel", "verify", "search"))
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--seconds", type=float, default=86400)
    parser.add_argument("--slice-seconds", type=float, default=600)
    parser.add_argument("--page-files", type=int, default=1000)
    parser.add_argument("--steps", type=int, default=1001)
    parser.add_argument("--memory-mib", type=int, default=1024)
    parser.add_argument("--search-disk-mib", type=int, default=2048)
    parser.add_argument("--reserve-mib", type=int, default=1024)
    parser.add_argument("--foreign-libraries", type=int, default=21)
    parser.add_argument("--crash-after-first-commit", action="store_true")
    parser.add_argument("--stop-after-slices", type=int)
    parser.add_argument("--cancel-after-worker-seconds", type=float, default=.5)
    parser.add_argument("--query-seconds", type=float, default=60)
    parser.add_argument("--query-samples", type=int, default=20)
    args = parser.parse_args(argv)
    if args.command == "init":
        result = initialize(args.fixture, page_files=args.page_files,
            steps=args.steps, slice_seconds=args.slice_seconds, memory_bytes=args.memory_mib*MIB,
            search_disk_bytes=args.search_disk_mib*MIB, reserve_bytes=args.reserve_mib*MIB,
            foreign_libraries=args.foreign_libraries)
    elif args.command == "import":
        result = run_import(args.fixture, seconds=args.seconds,
            crash_after_first_commit=args.crash_after_first_commit,
            stop_after_slices=args.stop_after_slices)
    elif args.command == "cancel":
        result = cancel_import(args.fixture, seconds=args.seconds,
            after_worker_seconds=args.cancel_after_worker_seconds)
    elif args.command == "verify":
        result = verify_catalog(args.fixture, seconds=args.seconds)
    else:
        result = search_catalog(args.fixture, seconds=args.seconds,
            query_seconds=args.query_seconds, query_samples=args.query_samples)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 3 if result.get("status") == "NOT_RUN" else 0


if __name__ == "__main__":
    from carr_library.audio.cleanup import drain_pending_cleanup
    try:
        code = main()
    except BaseException as error:
        drain_pending_cleanup(error)
        raise
    interruption = drain_pending_cleanup()
    if interruption is not None:
        raise interruption
    raise SystemExit(code)
