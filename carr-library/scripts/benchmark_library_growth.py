"""Bounded real small-TXT library pilot; no SQL seeding or product changes.

Only --sizes 100 1000 is exposed by the public entry. Each scale has a fresh
measurement child and a 900-second hard ceiling. Interrupted children retain
their owned files and last completed checkpoints; incomplete work is never PASS.
The installed package must match an explicit source proof and the reviewed
combined application modules. This measures generated logical segments, not OCR,
neural quality, native UI latency, cold storage or isolated-worker throughput.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import sqlite3
import stat
import subprocess
import sys
import threading
from time import monotonic, perf_counter

BASE_SOURCE_COMMIT = "313596afb39557b6718dfc0688e2e46c9b9fa35b"
SOURCE_BINDINGS = {
    "application.py": "a5aeac86640cb3221de726b452f9e8e2094a6035cc53f30775fdd173b5f0ee9f",
    "catalog/intake.py": "0abda3c6e588ea07aa8b74eae4e2e2ba5f929028ec9620e5ad4969cb06def08c",
    "ingest/pipeline.py": "fd77d10f20ba7a8a712a73cf4c1eae6f8f3903a37c97858901b64cb0a4f36f4e",
    "ingest/batches.py": "2761a2814275c6194157b30ae8a9fbf17e95ff8a327a6bb99051a77a8308cdfe",
    "ingest/worker.py": "d8e9ba1bb965679007481206018ff7aa641ca76d9a52eb0dae0777661f6b64a4",
    "search/paging.py": "49bc51b29c9d8967a887bbeeae6d2516a70ab0679964ca0441f7ea0db8f5eda7",
    "storage/revisions.py": "b27ff39ba22b691d3be76fdc550efa4c60d341c783091a27c03c74ffc0112e2b",
    "reader/reading_desk.py": "43ae80bab2fcfa73dc8a64d80fb11ffda7a0a3650b861f543cb2326558d19706"
}
CI_CHECK_SHA256 = "70281591d047dbe5f464db95457c35604b988b2aa0994e0706e4b317385a6ca6"
SQLITE_OBSERVER_SHA256 = "1ee483e2c79fa2f71c271136a2a9d329485f8672f241aee5409e8ce7a92aaeb5"
MAX_BOOKS = 1000
SEGMENTS = 40
CHILD_SECONDS = 900
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_DISK_FILES = 100_000
MARKER = "Pilotneedle"
SCHEMA = "carr-real-library-growth-pilot/1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def write_record(path, value):
    """One exclusive report; a previously published checkpoint is never replaced."""
    raw = canonical(value) + b"\n"
    require(len(raw) <= MAX_REPORT_BYTES, "Benchmark report exceeds its byte bound.")
    with Path(path).open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return sha256(raw).hexdigest()


def read_record(path):
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= MAX_REPORT_BYTES,
            "Missing or oversized benchmark record.")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    require(len(raw) <= MAX_REPORT_BYTES, "Benchmark record grew past its bound.")
    value = json.loads(raw)
    require(type(value) is dict, "Benchmark record must be an object.")
    return value, sha256(raw).hexdigest()


def source_identity(proof_path, tested_commit, *, require_installed=False):
    import carr_library
    require(re.fullmatch(r"[0-9a-f]{40}", tested_commit) is not None,
            "An exact tested commit is required.")
    proof, proof_hash = read_record(proof_path)
    require(proof.get("status") == "PASS" and proof.get("source_commit") == tested_commit,
            "Installed-source proof belongs to another execution.")
    package = Path(carr_library.__file__).resolve().parent
    require(Path(proof.get("installed_root", "")).resolve() == package,
            "Selected package differs from the source proof.")
    if require_installed:
        require(package.is_relative_to(Path(sys.prefix).resolve()),
                "The pilot requires the fresh installed package.")
    expected = proof.get("source_hashes")
    require(type(expected) is dict and 0 < len(expected) <= 1024
            and proof.get("installed_python_files") == len(expected),
            "Invalid complete source inventory.")
    actual = {}
    total = 0
    for relative, digest in sorted(expected.items()):
        require(type(relative) is str and type(digest) is str
                and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
                "Invalid source identity.")
        name = Path(relative)
        require(not name.is_absolute() and ".." not in name.parts
                and name.suffix == ".py", "Invalid source member.")
        path = package / name
        require(path.is_file() and path.stat().st_size <= MAX_REPORT_BYTES,
                "Source member is missing or oversized.")
        raw = path.read_bytes()
        total += len(raw)
        require(total <= 32 * 1024 * 1024, "Source inventory byte bound exceeded.")
        actual[relative] = sha256(raw).hexdigest()
        require(actual[relative] == digest, "Installed Python source changed: " + relative)
    require({p.relative_to(package).as_posix() for p in package.rglob("*.py")} == set(expected),
            "Installed Python source inventory changed.")
    require(all(actual.get(name) == digest for name, digest in SOURCE_BINDINGS.items()),
            "The pilot is bound to the reviewed combined production modules.")
    return dict(tested_commit=tested_commit, baseline_source_commit=BASE_SOURCE_COMMIT,
                package_root=str(package), python_files=len(actual),
                source_inventory_sha256=sha256(canonical(actual)).hexdigest(),
                source_proof_sha256=proof_hash,
                harness_sha256=sha256(Path(__file__).read_bytes()).hexdigest())


def samples(values):
    """Nearest-rank quantiles; empty measurements remain null, never zero."""
    require(type(values) is list and len(values) <= 10_000,
            "Invalid measurement sample count.")
    require(all(type(x) in (float, int) and math.isfinite(x) and x > 0 for x in values),
            "A duration must be a finite positive observation.")
    if not values:
        return dict(count=0, total_seconds=None, minimum_seconds=None,
                    maximum_seconds=None, p50_seconds=None, p95_seconds=None,
                    method="nearest rank of actual observations")
    ordered = sorted(values)
    return dict(count=len(values), total_seconds=sum(values),
                minimum_seconds=ordered[0], maximum_seconds=ordered[-1],
                p50_seconds=ordered[math.ceil(len(values) * .50) - 1],
                p95_seconds=ordered[math.ceil(len(values) * .95) - 1],
                method="nearest rank of actual observations")


def peak_rss_bytes():
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
        return int(counters.PeakWorkingSetSize)
    import resource
    amount = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(amount if sys.platform == "darwin" else amount * 1024)


def hardware_profile():
    result = dict(python=sys.version, platform=platform.platform(),
                  machine=platform.machine(), processor=platform.processor(),
                  logical_cpu_count=os.cpu_count(), sqlite=sqlite3.sqlite_version,
                  process_id=os.getpid(), total_physical_memory_bytes=None,
                  runner_image=os.environ.get("ImageOS"), runner_image_version=os.environ.get("ImageVersion"))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Memory(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                        *[(name, ctypes.c_ulonglong) for name in (
                            "total_physical", "available_physical", "total_pagefile", "available_pagefile",
                            "total_virtual", "available_virtual", "available_extended")]]
        state = Memory()
        state.length = ctypes.sizeof(state)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(Memory)]
        kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(state)):
            raise ctypes.WinError(ctypes.get_last_error())
        result["total_physical_memory_bytes"] = int(state.total_physical)
    elif hasattr(os, "sysconf"):
        result["total_physical_memory_bytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return result


def locator_query_plans(app, workspace, revision):
    """Observe actual scoped validation against the real read-only transaction."""
    from carr_library.storage.revisions import Storage

    statements = []
    plans = []
    with app.read(workspace) as db:
        class ObservedConnection:
            def execute(self, query, parameters=()):
                require(type(query) is str and len(statements) < 2,
                        "Reviewed locator diagnostic statements changed.")
                parameters = tuple(parameters)
                require(parameters == (revision,),
                        "Locator diagnostic lost its exact revision scope.")
                statements.append((query, parameters))
                return db.execute(query, parameters)
        observer = object.__new__(Storage)
        observer.db = ObservedConnection()
        Storage._check_locators(observer, revision)
        require(len(statements) == 2, "Reviewed locator diagnostic statements changed.")
        for query, parameters in statements:
            cursor = db.execute("EXPLAIN QUERY PLAN " + query, parameters)
            try:
                rows = cursor.fetchmany(257)
                require(len(rows) <= 256, "Locator plan diagnostic exceeded its bound.")
            finally:
                cursor.close()
            plans.append(dict(sql_sha256=sha256(query.encode("utf-8")).hexdigest(),
                              scope="real existing revision parameter", rows=[list(row) for row in rows]))
    return dict(sqlite=sqlite3.sqlite_version, plans=plans,
                claim="Planner observation only; no measured bottleneck or modified validation.")


def disk_bytes(root):
    total = count = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            path = Path(parent) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            require(stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                    "Unexpected linked benchmark directory.")
        for name in files:
            path = Path(parent) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            require(stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                    "Unexpected benchmark file kind.")
            total += info.st_size
            count += 1
            require(count <= MAX_DISK_FILES, "Benchmark file count exceeded its bound.")
    return total


class DiskSampler:
    """Sample logical bytes every .5s; this is explicitly not allocated disk peak."""
    def __init__(self, root, temporary_root=None):
        self.root = Path(root)
        self.temporary_root = Path(temporary_root) if temporary_root is not None else None
        self.stop = threading.Event()
        self.maximum = 0
        self.observations = 0
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="library-pilot-disk-observer")

    def observe(self):
        try:
            current = disk_bytes(self.root)
            if self.temporary_root is not None:
                current += disk_bytes(self.temporary_root)
            self.maximum = max(self.maximum, current)
            self.observations += 1
        except BaseException as exc:
            self.error = exc

    def _run(self):
        self.observe()
        while not self.stop.wait(.5):
            self.observe()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(2)
        require(not self.thread.is_alive(), "Disk observation did not stop.")
        self.observe()
        if self.error is not None:
            raise self.error


def corpus_segments(number, count=SEGMENTS):
    require(type(number) is int and 0 <= number < 1_000_000, "Invalid corpus ordinal.")
    require(type(count) is int and 2 <= count <= SEGMENTS, "Invalid segment count.")
    return [f"{MARKER} book {number:06d} segment {i:02d}. "
            "Exact source \u03a9 \u0423\u043a\u0440\u0430\u0457\u043d\u0430; line A.\r\n"
            "Line B preserves CRLF and spacing.\n" for i in range(1, count + 1)]


def corpus_text(number, count=SEGMENTS):
    return "\f".join(corpus_segments(number, count))


def book_id(number):
    return f"pilot-edition-{number:06d}"


def corpus_counts(app, workspace):
    with app.read(workspace) as db:
        books = db.execute("SELECT count(*) FROM catalog_edition WHERE workspace=?",
                           (workspace,)).fetchone()[0]
        active = db.execute("SELECT count(*) FROM active WHERE workspace=?",
                            (workspace,)).fetchone()[0]
        units = db.execute("SELECT count(*) FROM active_page WHERE workspace=?",
                           (workspace,)).fetchone()[0]
        indexed = db.execute("SELECT count(*) FROM indexed i JOIN active_page p ON p.id=i.id "
                             "WHERE p.workspace=?", (workspace,)).fetchone()[0]
        return dict(catalog_books=books, active_editions=active,
                    source_segments=units, indexed_segments=indexed)


def audit_corpus(app, workspace, records, segments, check):
    expected = {book_id(i): (i, record) for i, record in enumerate(records)}
    counts = corpus_counts(app, workspace)
    require(counts == dict(catalog_books=len(records), active_editions=len(records),
                           source_segments=len(records) * segments,
                           indexed_segments=len(records) * segments),
            "Actual catalog, source and index counts disagree.")
    observed = set()
    with app.read(workspace) as db:
        rows = db.execute("SELECT edition,revision,ordinal,raw,digest FROM active_page "
                          "WHERE workspace=? ORDER BY edition,ordinal", (workspace,))
        for row in rows:
            check()
            require(row["edition"] in expected, "Unexpected corpus edition.")
            number, record = expected[row["edition"]]
            require(row["revision"] == record["revision"], "Corpus revision changed.")
            ordinal = row["ordinal"]
            require(type(ordinal) is int and 1 <= ordinal <= segments,
                    "Invalid actual source segment ordinal.")
            raw = corpus_segments(number, segments)[ordinal - 1]
            require(row["raw"] == raw and row["digest"] == sha256(raw.encode("utf-8")).hexdigest(),
                    "Actual extracted source differs from the generated original.")
            key = (row["edition"], ordinal)
            require(key not in observed, "Repeated actual source segment.")
            observed.add(key)
    require(len(observed) == len(records) * segments, "Incomplete source coverage.")
    for number, record in enumerate(records):
        check()
        raw = corpus_text(number, segments).encode("utf-8")
        retained = app.assets.resolve(record["asset"])
        require(retained.read_bytes() == raw and sha256(raw).hexdigest() == record["asset"],
                "Retained original bytes changed.")
    return {**counts, "all_source_segments_checked": len(observed),
            "all_retained_originals_checked": len(records),
            "corpus_manifest_sha256": sha256(canonical(records)).hexdigest()}


def cancellation_probe(directory, attempts, segments, check, stopped):
    """Real committed-checkpoint cancellation, explicit retry from retained TXT."""
    from carr_library.application import LibraryApp
    from carr_library.ingest.pipeline import Ingest
    from carr_library.storage.revisions import Storage, CURRENT_SCHEMA_VERSION
    control = LibraryApp(Path(directory) / "cancel-library")
    scope = control.create_workspace("Cancellation controls; outside measured corpus")
    latencies, reopens, recoveries, evidence = [], [], [], []
    for number in range(attempts):
        check()
        edition = f"cancel-edition-{number}"
        fields = dict(title=f"Cancellation control {number}", edition_id=edition,
                      work_id=f"cancel-work-{number}")
        text = corpus_text(900_000 + number, segments)
        source = Path(directory) / f"cancel-input-{number}.txt"
        source.write_bytes(text.encode("utf-8"))
        requested_at = None
        def cancel_after_commit():
            nonlocal requested_at
            if stopped():
                return True
            journal = control.root / "imports.sqlite"
            if not journal.exists():
                return False
            with closing(sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True)) as observer:
                count = observer.execute(
                    "SELECT count(*) FROM checkpoint c JOIN job j ON j.id=c.job "
                    "WHERE j.workspace=? AND j.edition=?", (scope, edition)).fetchone()[0]
            if count >= 1:
                if requested_at is None:
                    requested_at = perf_counter()
                return True
            return False
        result = control.import_file(scope, source, cancelled=cancel_after_commit, **fields)
        returned_at = perf_counter()
        check()
        require(requested_at is not None and result["state"] == "cancelled"
                and result["completed"] == 1, "Cancellation missed the actual first checkpoint.")
        latencies.append(returned_at - requested_at)
        with closing(sqlite3.connect((control.root / "imports.sqlite").as_uri() + "?mode=ro",
                                     uri=True)) as observer:
            job = observer.execute("SELECT id,state,asset FROM job WHERE workspace=? AND edition=?",
                                   (scope, edition)).fetchone()
            checkpoints = observer.execute("SELECT ordinal,payload,digest FROM checkpoint WHERE job=?",
                                           (job[0],)).fetchall()
        require(job == (result["id"], "cancelled", result["asset"])
                and len(checkpoints) == 1 and checkpoints[0][0] == 1,
                "Cancelled journal identity is not durable.")
        payload = checkpoints[0][1]
        require(sha256(payload.encode("utf-8")).hexdigest() == checkpoints[0][2]
                and json.loads(payload)["raw_text"] == corpus_segments(900_000 + number, segments)[0],
                "Cancelled checkpoint differs from the exact input segment.")
        with control.read(scope) as db:
            require(db.execute("SELECT 1 FROM active WHERE workspace=? AND edition=?",
                               (scope, edition)).fetchone() is None,
                    "Cancelled source was unexpectedly published.")
        retained = control.assets.resolve(result["asset"])
        require(retained.read_bytes() == text.encode("utf-8"), "Cancelled original changed.")
        source.unlink()
        started = perf_counter()
        control = LibraryApp(control.root)
        reopens.append(perf_counter() - started)
        with closing(Storage(control.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
            with closing(Ingest(control.root / "imports.sqlite", storage)) as ingest:
                ingest.retry(scope, result["id"])
        started = perf_counter()
        resumed = control.import_file(scope, retained, cancelled=stopped, **fields)
        recoveries.append(perf_counter() - started)
        check()
        require(resumed["state"] == "complete" and resumed["id"] == result["id"]
                and resumed["asset"] == result["asset"] and resumed["completed"] == segments,
                "Explicit retained-source retry did not complete the same job.")
        with control.read(scope) as db:
            current = db.execute("SELECT revision FROM active WHERE workspace=? AND edition=?",
                                 (scope, edition)).fetchone()
            require(current is not None and current[0] == result["id"],
                    "Recovered active revision differs from the cancelled job.")
        require(not source.exists() and retained.read_bytes() == text.encode("utf-8"),
                "Recovery lost the retained original.")
        evidence.append(dict(job=result["id"], asset=result["asset"], cancelled_checkpoints=1,
                             recovered_segments=resumed["completed"], external_original_deleted=True))
    return dict(cancellation=samples(latencies), service_reopen=samples(reopens),
                retained_retry=samples(recoveries), evidence=evidence,
                observations=dict(cancellation_seconds=latencies, service_reopen_seconds=reopens,
                                  retained_retry_seconds=recoveries),
                scope="Cooperative inline TXT cancellation after a committed checkpoint; "
                      "explicit Ingest.retry and public import_file from retained bytes. "
                      "No native UI cancellation or isolated-worker startup measurement.")


PROFILE_LABELS = frozenset(("import-000001", "import-000500", "import-001000", "search-000001"))
PROFILE_FOCUS = (
    "/carr_library/application.py", "/carr_library/storage/revisions.py",
    "/carr_library/ingest/pipeline.py", "/carr_library/ingest/retained_assets.py",
    "/carr_library/catalog/intake.py", "/carr_library/catalog/registration.py",
    "/carr_library/citations/text_attestation.py", "/carr_library/durable_state.py",
)


def profile_call(directory, source, label, call_identity, function, *args, **kwargs):
    """Profile one unchanged real call and immediately preserve bounded JSON."""
    import cProfile
    import pstats

    require(label in PROFILE_LABELS, "Unknown bounded profile sample.")
    require(type(call_identity) is dict and len(canonical(call_identity)) <= 64 * 1024,
            "Profile argument identity exceeds its bound.")
    path = Path(directory) / ("profile-" + label + ".json")
    require(not path.exists(), "Profile receipt already exists; do not repeat the operation.")
    profiler = cProfile.Profile()
    started = perf_counter()

    def save(outcome, error=None):
        profiler.disable()
        elapsed = perf_counter() - started
        statistics = pstats.Stats(profiler)
        require(len(statistics.stats) <= 20_000, "Profile function inventory exceeds its bound.")

        def identity(key):
            filename, line, name = key
            require(type(filename) is str and len(filename.encode("utf-8")) <= 1024
                    and type(name) is str and len(name.encode("utf-8")) <= 1024
                    and type(line) is int and line >= 0,
                    "Invalid or oversized profile function identity.")
            return dict(filename=filename, line=line, function=name)

        def timing(value, *, caller=False):
            # CPython3.11 cProfile uses (cc,nc,tt,ct) for functions, but
            # (nc,cc,tt,ct) for caller edges; preserve that distinction.
            first, second, own, cumulative = value
            primitive, calls = (second, first) if caller else (first, second)
            require(type(calls) is int and type(primitive) is int
                    and 0 <= primitive <= calls
                    and all(type(x) in (int, float) and math.isfinite(x) and x >= 0
                            for x in (own, cumulative)),
                    "Invalid actual profile counters.")
            return dict(calls=calls, primitive_calls=primitive,
                        self_seconds=own, cumulative_seconds=cumulative)

        ordered = sorted(statistics.stats,
                         key=lambda key: (-statistics.stats[key][3], key))
        native_reads = [key for key in ordered if key[2] in (
            "<method 'read' of '_io.BufferedReader' objects>",
            "<method 'read' of '_io.FileIO' objects>")]
        focused = list(dict.fromkeys(native_reads + [key for key in ordered
            if key[0].replace("\\", "/").endswith(PROFILE_FOCUS)]))
        selected = list(dict.fromkeys(focused[:160] + ordered[:96]))
        rows = []
        for key in selected:
            value = statistics.stats[key]
            callers = sorted(value[4].items(), key=lambda item: (-item[1][3], item[0]))
            rows.append(dict(identity=identity(key), **timing(value[:4]),
                callers=[dict(identity=identity(parent), **timing(counts, caller=True))
                         for parent, counts in callers[:16]],
                omitted_callers=max(0, len(callers) - 16)))
        record = dict(schema="carr-library-selected-call-profile/1",
            source=source, sample=label, call_identity=call_identity,
            callable=dict(module=function.__module__, name=function.__qualname__),
            outcome=outcome, wall_seconds_with_profiling=elapsed,
            profiled_thread=threading.get_ident(), process_id=os.getpid(),
            total_calls=statistics.total_calls, primitive_calls=statistics.prim_calls,
            total_self_seconds=statistics.total_tt,
            total_functions=len(ordered), reported_functions=len(rows),
            omitted_functions=len(ordered) - len(rows),
            focused_functions=len(focused), omitted_focused_functions=max(0, len(focused) - 160),
            limits=dict(functions=256, focused_functions=160, hottest_functions=96,
                        callers_per_function=16, serialized_bytes=MAX_REPORT_BYTES),
            functions=rows,
            scope="cProfile of this actual current-thread call only. Other threads are not profiled. "
                  "Self and cumulative times include profiling overhead; cumulative rows overlap and "
                  "must not be added. This is call attribution, not an uninstrumented performance result.")
        if error is not None:
            record["exception"] = dict(type=type(error).__name__, message=str(error)[:2048])
        digest = write_record(path, record)
        receipt = dict(path=path.name, sha256=digest, bytes=path.stat().st_size,
                       sample=label, outcome=outcome)
        print(json.dumps(dict(event="pilot-call-profile", **receipt), ensure_ascii=True), flush=True)
        return receipt

    try:
        result = profiler.runcall(function, *args, **kwargs)
    except BaseException as exc:
        try:
            save("exception", exc)
        except Exception as receipt_error:
            # Preserve the operation's original failure; it can never be PASS.
            print(json.dumps(dict(event="pilot-call-profile-failed", sample=label,
                receipt_error=type(receipt_error).__name__,
                original_error=type(exc).__name__), ensure_ascii=True), flush=True)
        raise
    return result, save("returned")



def sqlite_profile_call(directory, source, label, call_identity, function, *args, **kwargs):
    """Opt-in native SQL attribution; never nested with the cProfile owner."""
    from carr_library.application import _CatalogPublisher
    from carr_library.storage.revisions import Storage

    require(label in PROFILE_LABELS and label.startswith("import-"),
            "Native SQL observation is limited to the three selected imports.")
    require(type(call_identity) is dict and len(canonical(call_identity)) <= 64 * 1024,
            "Profile argument identity exceeds its bound.")
    helper_path = Path(__file__).with_name("sqlite_call_observer.py")
    with helper_path.open("rb") as stream:
        helper_bytes = stream.read(MAX_REPORT_BYTES + 1)
    require(len(helper_bytes) <= MAX_REPORT_BYTES
            and sha256(helper_bytes).hexdigest() == SQLITE_OBSERVER_SHA256,
            "The reviewed native SQLite observer changed.")
    namespace = {"__name__": "_carr_growth_sqlite_observer", "__file__": str(helper_path)}
    exec(compile(helper_bytes, str(helper_path), "exec"), namespace)
    package = Path(source["package_root"]).resolve()
    expected = {package / relative: SOURCE_BINDINGS[relative]
                for relative in ("storage/revisions.py", "application.py")}
    observer = namespace["NativeSQLiteObserver"](
        (Storage._check, Storage._check_locators), expected,
        context_functions=(Storage.stage_revision, Storage.check, _CatalogPublisher.publish_revision))
    path = Path(directory) / ("profile-" + label + ".json")
    require(not path.exists(), "Profile receipt already exists; do not repeat the operation.")
    started = perf_counter()

    def save(outcome, error=None):
        elapsed = perf_counter() - started
        observation = observer.record()
        record = dict(schema="carr-library-selected-native-sqlite-profile/1",
            source=source, sample=label,
            call_identity_sha256=sha256(canonical(call_identity)).hexdigest(),
            callable=dict(module=function.__module__, name=function.__qualname__),
            outcome=outcome, wall_seconds_with_observation=elapsed,
            observer_source_sha256=SQLITE_OBSERVER_SHA256,
            native_sqlite=observation,
            scope="Opt-in native SQL intervals for this actual import. Existing cProfile is not "
                  "running concurrently. Public-call identity is hashed; no SQL binding/locals "
                  "are read. Other threads and implicit cursor iteration are excluded. "
                  "Times include diagnostic overhead and are not uninstrumented query costs.")
        if error is not None:
            record["exception_type"] = type(error).__name__
        digest = write_record(path, record)
        receipt = dict(path=path.name, sha256=digest, bytes=path.stat().st_size,
                       sample=label, outcome=outcome)
        print(json.dumps(dict(event="pilot-native-sqlite-profile", **receipt,
                              observation_status=observation["status"]), ensure_ascii=True), flush=True)
        return receipt, observation

    try:
        result = observer.run(function, *args, **kwargs)
    except BaseException as exc:
        try:
            save("exception", exc)
        except Exception as receipt_error:
            print(json.dumps(dict(event="pilot-native-sqlite-profile-failed", sample=label,
                receipt_error=type(receipt_error).__name__,
                original_error=type(exc).__name__), ensure_ascii=True), flush=True)
        raise
    receipt, observation = save("returned")
    require(observation["status"] == "COMPLETE",
            "Native SQLite observation failed; the returned operation is not profile evidence.")
    return result, receipt

def run_case(directory, books, proof_path, tested_commit, *, segments=SEGMENTS,
             seconds=CHILD_SECONDS, require_installed=False, temporary_root=None,
             profile_mode="cprofile"):
    require(profile_mode in ("cprofile", "sqlite"), "Unknown call attribution mode.")
    require(type(books) is int and 1 <= books <= MAX_BOOKS, "Invalid book count.")
    require(type(segments) is int and 2 <= segments <= SEGMENTS, "Invalid segment count.")
    require(type(seconds) in (int, float) and math.isfinite(seconds) and 0 < seconds <= CHILD_SECONDS,
            "Invalid child time bound.")
    identity = source_identity(proof_path, tested_commit, require_installed=require_installed)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    started = perf_counter()
    deadline = monotonic() + seconds
    stage = "startup"
    records, import_times, open_times, search_times = [], [], [], []
    checkpoint_number = 0
    report = dict(schema=SCHEMA, status="IN_PROGRESS", source=identity,
                  requested_books=books, requested_segments_per_book=segments,
                  requested_source_segments=books * segments, completed_books=0,
                  import_phase_complete=False, phase=stage, child_time_limit_seconds=seconds,
                  environment=hardware_profile(), baseline_peak_rss_bytes=peak_rss_bytes(),
                  not_measured=["cold filesystem cache", "neural retrieval", "OCR", "VRAM",
                                "native UI open/cancel", "isolated import worker throughput",
                                "full application process restart", "10k/50k books", "multi-GB files"])
    sampler = DiskSampler(directory, temporary_root)
    report["call_attribution"] = dict(
        enabled=books == 1000, selected_samples=sorted(PROFILE_LABELS) if books == 1000 else [],
        receipts=[], timing_scope="Selected calls and the case aggregate are instrumentation-affected; "
        "no uninstrumented speed or SQL-only causality claim.")
    if profile_mode == "sqlite":
        report["call_attribution"]["mode"] = "sqlite"

    def observed(label, describe, function, *args, **kwargs):
        if books != 1000 or label not in PROFILE_LABELS:
            return function(*args, **kwargs)
        selected_profiler = (sqlite_profile_call if profile_mode == "sqlite"
                             and label.startswith("import-") else profile_call)
        result, receipt = selected_profiler(directory, identity, label, describe(),
                                            function, *args, **kwargs)
        report["call_attribution"]["receipts"].append(receipt)
        return result

    def stopped():
        return monotonic() >= deadline or sampler.error is not None

    def check():
        if sampler.error is not None:
            raise sampler.error
        if stopped():
            raise TimeoutError("The finite library pilot deadline was reached.")

    def checkpoint(label):
        nonlocal checkpoint_number
        report.update(phase=label, completed_books=len(records),
                      completed_source_segments=sum(row["segments"] for row in records),
                      original_records=list(records),
                      observations=dict(import_seconds=list(import_times), open_seconds=list(open_times),
                                        search_seconds=list(search_times)),
                      import_receipts_sha256=sha256(canonical(records)).hexdigest(),
                      elapsed_seconds=perf_counter() - started,
                      imported=samples(import_times),
                      service_open=samples(open_times), scoped_search=samples(search_times),
                      process_peak_rss_bytes=peak_rss_bytes())
        write_record(directory / f"checkpoint-{checkpoint_number:04d}.json", report)
        checkpoint_number += 1
        print(json.dumps(dict(event="pilot-checkpoint", requested_books=books,
                              completed_books=len(records), phase=label,
                              elapsed_seconds=report["elapsed_seconds"]), ensure_ascii=True), flush=True)

    try:
        with sampler:
            from carr_library.application import LibraryApp
            from carr_library.reader.reading_desk import ReadingDesk
            from carr_library.citations.anchors import Locator
            launch = perf_counter()
            app = LibraryApp(directory / "library")
            report["new_library_seconds"] = perf_counter() - launch
            scope = app.create_workspace("Generated real TXT pilot")
            report["workspace"] = scope
            checkpoint("startup")
            stage = "import"
            for number in range(books):
                check()
                text = corpus_text(number, segments)
                raw = text.encode("utf-8")
                begin = perf_counter()
                result = observed(f"import-{number + 1:06d}",
                    lambda: dict(book_ordinal=number + 1, completed_books_before=len(records), workspace=scope,
                         edition=book_id(number), work=f"pilot-work-{number:06d}",
                         title_sha256=sha256(f"Pilot book {number:06d}".encode("utf-8")).hexdigest(),
                         source_sha256=sha256(raw).hexdigest(), source_bytes=len(raw), source_segments=segments),
                    app.add_text, scope, f"Pilot book {number:06d}", text,
                    edition_id=book_id(number), work_id=f"pilot-work-{number:06d}",
                    cancelled=stopped)
                duration = perf_counter() - begin
                check()
                require(result["state"] == "complete" and result["completed"] == segments
                        and result["asset"] == sha256(raw).hexdigest(),
                        "Actual import did not complete every generated segment.")
                require(app.assets.resolve(result["asset"]).read_bytes() == raw,
                        "Actual import did not retain the exact original.")
                records.append(dict(edition=book_id(number), revision=result["id"],
                                    asset=result["asset"], bytes=len(raw), segments=result["completed"]))
                import_times.append(duration)
                if len(records) % 100 == 0 or len(records) == books:
                    counts = corpus_counts(app, scope)
                    require(counts == dict(catalog_books=len(records), active_editions=len(records),
                                           source_segments=len(records) * segments,
                                           indexed_segments=len(records) * segments),
                            "Checkpoint counts do not describe the actual corpus.")
                    report["last_checkpoint_counts"] = counts
                    checkpoint("import")
            report["import_phase_complete"] = True
            report["original_input_bytes"] = sum(record["bytes"] for record in records)
            stage = "corpus-verification"
            report["corpus_verification"] = audit_corpus(app, scope, records, segments, check)
            report["scoped_locator_query_plans"] = locator_query_plans(app, scope, records[0]["revision"])
            checkpoint(stage)

            stage = "service-reopen"
            begin = perf_counter()
            reopened = LibraryApp(app.root)
            report["library_service_reopen_seconds"] = perf_counter() - begin
            require(corpus_counts(reopened, scope) == report["last_checkpoint_counts"],
                    "Reopened library changed the corpus counts.")
            app = reopened
            checkpoint(stage)
            stage = "service-open"
            positions = sorted({round(i * (books - 1) / max(1, min(24, books) - 1))
                                for i in range(min(24, books))})
            for number in positions:
                check()
                begin = perf_counter()
                desk = ReadingDesk(app, scope, book_id(number), cancelled=stopped)
                page = desk.page(1 + number % segments)
                open_times.append(perf_counter() - begin)
                require(page.verified and len(desk.pages) == segments
                        and page.raw_text == corpus_segments(number, segments)[page.ordinal - 1],
                        "Service-open returned different or unverified source bytes.")
            checkpoint(stage)

            stage = "cancellation-recovery"
            report["cancellation_recovery"] = cancellation_probe(
                directory, 3, segments, check, stopped)
            checkpoint(stage)

            stage = "scope-controls"
            other = app.create_workspace("Excluded pilot scope")
            outside = app.add_text(other, "Excluded source", MARKER + " outside the corpus.",
                                   cancelled=stopped)
            require(outside["state"] == "complete", "Isolation control did not import.")
            require(corpus_counts(app, scope) == report["last_checkpoint_counts"],
                    "Foreign workspace changed measured corpus counts.")
            denied = False
            try:
                ReadingDesk(app, other, book_id(0), cancelled=stopped)
            except ValueError:
                denied = True
            require(denied, "Foreign workspace unexpectedly opened a corpus edition.")
            negative = app.search_page(scope, "NotInPilotCorpusZzz", mode="terms",
                                       timeout=60, cancelled=stopped)
            require(not negative.results, "Negative lexical query returned an unexpected source.")
            report["scope_controls"] = dict(foreign_edition_open_refused=True,
                                           negative_query_results=0, other_workspace=other)

            stage = "paged-search"
            expected = {(book_id(i), Locator("logical_segment", ordinal).display())
                        for i in range(books) for ordinal in range(1, segments + 1)}
            seen, cursor, windows, calls = set(), None, 0, 0
            while True:
                check()
                offset = 0
                while True:
                    begin = perf_counter()
                    page = observed("search-000001" if calls == 0 else "unprofiled-search",
                        lambda: dict(call_ordinal=calls + 1, workspace=scope, completed_books=len(records),
                             corpus_receipts_sha256=sha256(canonical(records)).hexdigest(),
                             query_sha256=sha256(MARKER.encode("utf-8")).hexdigest(),
                             cursor_sha256=sha256(canonical(cursor)).hexdigest(),
                             mode="terms", offset=offset, limit=100, timeout=60),
                        app.search_page, scope, MARKER, mode="terms", cursor=cursor,
                        offset=offset, limit=100, timeout=60, cancelled=stopped)
                    search_times.append(perf_counter() - begin)
                    calls += 1
                    require(calls <= books * segments + 1 and page.source_pages <= 100
                            and page.source_editions <= 500 and page.source_characters <= 5_000_000,
                            "Paged search exceeded its source window bounds.")
                    for hit in page.results:
                        key = (hit.edition_id, hit.page)
                        require(hit.workspace == scope and hit.verified and hit.quote == MARKER
                                and key in expected and key not in seen,
                                "Paged search leaked, duplicated or changed a source match.")
                        seen.add(key)
                    if page.results:
                        hit = page.results[0]
                        require(app.copy(scope, hit.ref, hit.revision, "quote", cancelled=stopped) == MARKER,
                                "Quoted match did not retain exact source authority.")
                    if not page.has_more_results:
                        break
                    offset += 100
                windows += 1
                report["search_coverage"] = dict(complete=False, source_windows=windows,
                                                result_calls=calls, unique_source_matches=len(seen),
                                                expected_source_matches=len(expected))
                cursor = page.next_source_cursor
                if windows % 10 == 0:
                    checkpoint(stage)
                if cursor is None:
                    break
            require(seen == expected, "Full source-cursor traversal omitted corpus segments.")
            report["search_coverage"]["complete"] = True
            report["corpus_after_search"] = audit_corpus(app, scope, records, segments, check)
            require(report["corpus_after_search"] == report["corpus_verification"],
                    "Search changed the retained corpus.")
            check()
            require(source_identity(proof_path, tested_commit,
                                    require_installed=require_installed) == identity,
                    "Measured source identity changed during the case.")
            checkpoint("complete")
            report["status"] = "PASS"
    except BaseException as exc:
        report.update(status="TIMEOUT" if monotonic() >= deadline else "FAIL",
                      phase=stage, error_type=type(exc).__name__, error=str(exc)[:2048])
    report.update(completed_books=len(records), elapsed_seconds=perf_counter() - started,
                  imported=samples(import_times), service_open=samples(open_times),
                  scoped_search=samples(search_times),
                  process_peak_rss_bytes=peak_rss_bytes(),
                  sampled_peak_logical_disk_bytes=sampler.maximum,
                  disk_observations=sampler.observations,
                  disk_scope="Owned case tree plus the dedicated TMP/TEMP directory when supplied; .5s samples. "
                             "Logical file bytes, not allocated bytes or a guaranteed instantaneous peak.",
                  temporary_directory_measured=temporary_root is not None,
                  rss_scope="Peak resident/working-set bytes of the measurement child, including Python "
                            "and production services; no separately spawned worker or native UI.",
                  timing_scope="Import calls include production retention/extraction/publication; "
                               "independent benchmark verification is outside each import sample. "
                               "Search samples include search_page; subsequent copy checks are separate. "
                               "Service-open samples include ReadingDesk construction and exact page read.",
                  cache_scope="Fresh case directory and process; OS filesystem cache is not flushed.",
                  original_records=records,
                  observations=dict(import_seconds=import_times, open_seconds=open_times,
                                    search_seconds=search_times))
    write_record(directory / "case-report.json", report)
    return report


def validate_success(report, books, identity):
    require(report.get("schema") == SCHEMA and report.get("status") == "PASS"
            and report.get("source") == identity and report.get("requested_books") == books
            and report.get("completed_books") == books,
            "Child did not report a complete source-bound case.")
    segments = report.get("requested_segments_per_book")
    require(type(segments) is int and 2 <= segments <= SEGMENTS
            and report.get("requested_source_segments") == books * segments,
            "Child source-unit accounting differs from the requested case.")
    counts = report.get("corpus_verification", {})
    require(counts.get("catalog_books") == books and counts.get("active_editions") == books
            and counts.get("source_segments") == books * segments
            and counts.get("indexed_segments") == books * segments
            and counts.get("all_retained_originals_checked") == books
            and counts.get("all_source_segments_checked") == books * segments,
            "Child did not verify the actual complete corpus.")
    require(report.get("corpus_after_search") == counts,
            "Child lacks post-search corpus verification.")
    coverage = report.get("search_coverage", {})
    require(coverage.get("complete") is True
            and coverage.get("unique_source_matches") == books * segments
            and coverage.get("expected_source_matches") == books * segments,
            "Child search coverage is incomplete.")
    require(report.get("imported", {}).get("count") == books
            and report.get("service_open", {}).get("count", 0) > 0
            and report.get("scoped_search", {}).get("count", 0) > 0
            and report.get("cancellation_recovery", {}).get("cancellation", {}).get("count") == 3,
            "Child omitted required actual timing samples.")
    require(len(report.get("original_records", [])) == books,
            "Child original receipt inventory is incomplete.")
    require(report.get("import_phase_complete") is True and report.get("phase") == "complete",
            "Child phase accounting is incomplete.")
    records = report["original_records"]
    for number, record in enumerate(records):
        raw = corpus_text(number, segments).encode("utf-8")
        require(type(record) is dict and record.get("edition") == book_id(number)
                and record.get("bytes") == len(raw) and record.get("segments") == segments
                and record.get("asset") == sha256(raw).hexdigest()
                and type(record.get("revision")) is str
                and re.fullmatch(r"[0-9a-f]{64}", record["revision"]) is not None,
                "Child source receipt differs from the declared generated corpus.")
    require(counts.get("corpus_manifest_sha256") == sha256(canonical(records)).hexdigest(),
            "Child corpus receipt digest changed.")
    observed = report.get("observations", {})
    for key, name in (("import_seconds", "imported"), ("open_seconds", "service_open"),
                      ("search_seconds", "scoped_search")):
        require(samples(observed.get(key)) == report.get(name),
                "Child timing summaries differ from their actual observations.")
    recovery = report["cancellation_recovery"]
    for key, name in (("cancellation_seconds", "cancellation"),
                      ("service_reopen_seconds", "service_reopen"),
                      ("retained_retry_seconds", "retained_retry")):
        require(samples(recovery.get("observations", {}).get(key)) == recovery.get(name),
                "Child recovery summaries differ from their actual observations.")
    require(coverage.get("result_calls") == report["scoped_search"]["count"]
            and type(report.get("process_peak_rss_bytes")) is int
            and report["process_peak_rss_bytes"] > 0
            and type(report.get("sampled_peak_logical_disk_bytes")) is int
            and report["sampled_peak_logical_disk_bytes"] > 0,
            "Child omitted actual resource/search accounting.")



def cleanup_owner():
    path = Path(__file__).with_name("ci_check.py")
    require(sha256(path.read_bytes()).hexdigest() == CI_CHECK_SHA256,
            "The reviewed process-tree cleanup helper changed.")
    spec = importlib.util.spec_from_file_location("_carr_growth_cleanup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._terminate_process_tree


def validate_profile_receipts(directory, report, identity, *, profile_mode="cprofile"):
    """A completed instrumented case must retain all four exact sample receipts."""
    require(profile_mode in ("cprofile", "sqlite"), "Unknown call attribution mode.")
    attribution = report.get("call_attribution")
    require(type(attribution) is dict and attribution.get("enabled") is True,
            "Completed instrumented case omitted its profile scope.")
    require(attribution.get("mode", "cprofile") == profile_mode,
            "Completed profile mode differs from the selected observation.")
    receipts = attribution.get("receipts")
    require(type(receipts) is list and len(receipts) == len(PROFILE_LABELS)
            and {row.get("sample") for row in receipts if type(row) is dict} == PROFILE_LABELS,
            "Completed instrumented case omitted or duplicated a required sample.")
    for row in receipts:
        require(row.get("path") == "profile-" + row["sample"] + ".json"
                and row.get("outcome") == "returned",
                "Completed profile receipt has an invalid path or outcome.")
        path = Path(directory) / row["path"]
        profile, digest = read_record(path)
        native_sqlite = profile_mode == "sqlite" and row["sample"].startswith("import-")
        expected_schema = ("carr-library-selected-native-sqlite-profile/1" if native_sqlite
                           else "carr-library-selected-call-profile/1")
        if native_sqlite:
            require(profile.get("observer_source_sha256") == SQLITE_OBSERVER_SHA256
                    and profile.get("native_sqlite", {}).get("status") == "COMPLETE"
                    and profile.get("native_sqlite", {}).get("errors") == []
                    and profile.get("native_sqlite", {}).get("rows"),
                    "Native SQLite profile is incomplete or differs from its reviewed observer.")
        require(digest == row.get("sha256") and path.stat().st_size == row.get("bytes")
                and profile.get("schema") == expected_schema
                and profile.get("source") == identity and profile.get("sample") == row["sample"]
                and profile.get("outcome") == "returned",
                "Completed profile sample changed after measurement.")


def run_child(directory, books, proof_path, tested_commit, *, seconds=CHILD_SECONDS,
              profile_mode="cprofile"):
    require(profile_mode in ("cprofile", "sqlite"), "Unknown call attribution mode.")
    directory = Path(directory).resolve()
    require(not directory.exists(), "Child case directory already exists.")
    identity = source_identity(proof_path, tested_commit, require_installed=True)
    terminate = cleanup_owner()
    owner = directory.parent
    temporary = owner / f"temporary-{books}"
    temporary.mkdir(exist_ok=False)
    log = owner / f"child-{books}.log"
    command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child",
               "--output-dir", str(directory), "--books", str(books),
               "--source-proof", str(Path(proof_path).resolve()), "--tested-commit", tested_commit,
               "--seconds", str(seconds), "--expected-harness-sha256", identity["harness_sha256"],
               "--temporary-root", str(temporary)]
    if profile_mode == "sqlite":
        command.extend(("--profile-mode", "sqlite"))
    env = dict(os.environ, TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary),
               PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    started = perf_counter()
    timed_out = False
    cleanup_error = None
    with log.open("xb") as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, env=env, **options)
        try:
            process.wait(timeout=max(.001, seconds - (perf_counter() - started)))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                terminate(process)
            except BaseException as exc:
                cleanup_error = str(exc)[:2048]
        except BaseException:
            terminate(process)
            raise
    result = dict(requested_books=books, process_id=process.pid, returncode=process.returncode,
                  hard_timeout_seconds=seconds, hard_timeout=timed_out,
                  process_elapsed_seconds=perf_counter() - started,
                  process_reaped=process.returncode is not None,
                  cleanup_error=cleanup_error, status="FAIL", source=identity)
    child_report = directory / "case-report.json"
    if child_report.exists():
        try:
            report, digest = read_record(child_report)
            result.update(child_report_sha256=digest, child_status=report.get("status"),
                          completed_books=report.get("completed_books"),
                          completed_phase=report.get("phase"))
            if process.returncode == 0 and not timed_out and cleanup_error is None:
                validate_success(report, books, identity)
                if books == 1000:
                    validate_profile_receipts(directory, report, identity, profile_mode=profile_mode)
                result["status"] = "PASS"
        except (ValueError, OSError, TypeError, KeyError) as exc:
            result["receipt_error"] = str(exc)[:2048]
    else:
        checkpoints = sorted(directory.glob("checkpoint-*.json")) if directory.exists() else []
        if checkpoints:
            last, digest = read_record(checkpoints[-1])
            require(last.get("source") == identity and last.get("requested_books") == books,
                    "Partial checkpoint belongs to a different case.")
            result.update(last_checkpoint=checkpoints[-1].name, last_checkpoint_sha256=digest,
                          at_least_completed_books=last.get("completed_books"),
                          completed_phase=last.get("phase"))
        result["child_report"] = "UNAVAILABLE; no completed-case claim"
    if timed_out:
        result["status"] = "TIMEOUT"
    require(process.returncode is not None, "Measurement child was not reaped.")
    write_record(owner / f"child-{books}-outcome.json", result)
    return result


def main(argv=None):
    require(__debug__, "Run the pilot without optimization flags.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-proof", type=Path, required=True)
    parser.add_argument("--tested-commit", required=True)
    parser.add_argument("--sizes", nargs="+", type=int, choices=(100, 1000), default=[100, 1000])
    parser.add_argument("--profile-mode", choices=("cprofile", "sqlite"), default="cprofile",
                        help="Default cProfile; opt-in native SQLite intervals for selected imports.")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--books", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--seconds", type=float, default=CHILD_SECONDS, help=argparse.SUPPRESS)
    parser.add_argument("--expected-harness-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--temporary-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        require(args.expected_harness_sha256 == sha256(Path(__file__).read_bytes()).hexdigest(),
                "Child harness identity changed.")
        require(args.temporary_root is not None
                and args.temporary_root.resolve() == args.output_dir.resolve().parent / f"temporary-{args.books}"
                and args.temporary_root.is_dir(), "Child temporary directory is not owned by this case.")
        report = run_case(args.output_dir, args.books, args.source_proof, args.tested_commit,
                          seconds=args.seconds, require_installed=True,
                          temporary_root=args.temporary_root, profile_mode=args.profile_mode)
        print(json.dumps(dict(status=report["status"], requested_books=args.books,
                              completed_books=report["completed_books"],
                              phase=report["phase"]), ensure_ascii=True), flush=True)
        return 0 if report["status"] == "PASS" else 1
    require(args.books is None and args.seconds == CHILD_SECONDS
            and args.expected_harness_sha256 is None and args.temporary_root is None
            and args.sizes == sorted(set(args.sizes)), "Invalid public pilot options.")
    identity = source_identity(args.source_proof, args.tested_commit, require_installed=True)
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    write_record(root / "pilot-source.json", identity)
    outcomes = []
    for books in args.sizes:
        if outcomes and outcomes[-1]["status"] != "PASS":
            outcomes.append(dict(requested_books=books, status="NOT_RUN",
                                 reason="The smaller requested case did not complete successfully."))
            continue
        try:
            outcome = run_child(root / f"books-{books}", books, args.source_proof, args.tested_commit,
                                profile_mode=args.profile_mode)
        except BaseException as exc:
            outcome = dict(requested_books=books, status="FAIL", error_type=type(exc).__name__,
                           error=str(exc)[:2048], source=identity)
        outcomes.append(outcome)
        print(json.dumps(outcome, ensure_ascii=True, sort_keys=True), flush=True)
    result = dict(schema=SCHEMA, generated_at=datetime.now(timezone.utc).isoformat(),
                  source=identity, cases=outcomes,
                  status="PASS" if all(x["status"] == "PASS" for x in outcomes) else "INCOMPLETE",
                  scope="Instrumented generated-corpus call attribution. Selected calls and aggregate "
                        "times include profiling overhead; cumulative rows overlap and are not additive. "
                        "No uninstrumented speed, SQL-only causality or ordinary release-gate verdict.")
    write_record(root / "pilot-report.json", result)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
