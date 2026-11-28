"""Validate the preserved integrated Windows source inventory and its partitions.

The workflow keeps the original ordered list once. Only scheduling changes:
the academic suite remains serial, two measured long suites run next, and
six independent Windows VMs execute the remaining suites serially. The original
ordered manifest, strict runner, suite limits and 35-minute phases stay unchanged.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

ACADEMIC_SUITE = "tests/research/test_academic_library.py"
ISOLATED_SUITES = (
    "tests/search/test_federated_advanced.py",
    "tests/security/test_portable_handoff.py",
)
COVERAGE_SUITE = "tests/packaging/test_windows_source_suites.py"
REPEATED_SUITE = "tests/ui/test_ocr_selection.py"
BOOTSTRAP_SUITE = "bootstrap/semantic_compat/test_probe.py"
EXPECTED_OCCURRENCES = 404
EXPECTED_DISTINCT = 403
MANIFEST_SHA256 = "f5895a418aa852aa56d4a820c6abf17f6d787767919710f97d4519697b44a2c5"
MAX_MANIFEST_BYTES = 64 * 1024
_MEMBER = re.compile(r"(?:tests|bootstrap)/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py")


def isolated_plan():
    return {
        "schema": 1,
        "manifest_sha256": MANIFEST_SHA256,
        "original_occurrences": EXPECTED_OCCURRENCES,
        "original_distinct": EXPECTED_DISTINCT,
        "academic": ACADEMIC_SUITE,
        "isolated": list(ISOLATED_SUITES),
        "coverage_guard": COVERAGE_SUITE,
    }


def partition(suites):
    if type(suites) is not list or len(suites) != EXPECTED_OCCURRENCES:
        raise ValueError("The original source inventory must contain exactly 404 entries.")
    if any(type(item) is not str or len(item) > 512 or not _MEMBER.fullmatch(item)
           for item in suites):
        raise ValueError("The source inventory contains an invalid test path.")
    counts = Counter(suites)
    if len(counts) != EXPECTED_DISTINCT or counts[REPEATED_SUITE] != 2:
        raise ValueError("The original OCR duplicate and 403 distinct suites are required.")
    if any(count != (2 if name == REPEATED_SUITE else 1)
           for name, count in counts.items()):
        raise ValueError("The source inventory contains an unexpected duplicate.")
    scheduled_first = (ACADEMIC_SUITE, *ISOLATED_SUITES)
    if any(counts[name] != 1 for name in (*scheduled_first, BOOTSTRAP_SUITE)):
        raise ValueError("Every required first-phase and bootstrap suite must occur once.")
    digest = hashlib.sha256(("\n".join(suites) + "\n").encode("utf-8")).hexdigest()
    if digest != MANIFEST_SHA256:
        raise ValueError("The original ordered source inventory changed.")
    bulk = [name for name in suites if name not in scheduled_first]
    if len(bulk) != 401 or Counter((*scheduled_first, *bulk)) != counts:
        raise ValueError("Source partitions must preserve every original occurrence.")
    return {**isolated_plan(), "bulk": bulk, "bulk_occurrences": len(bulk)}



METADATA_SUITE = "tests/ingest/test_import_metadata_shape.py"
MAX_REPORT_BYTES = 64 * 1024 * 1024
_REPORT_START = re.compile(r"(PASS|FAIL): " + re.escape(METADATA_SUITE) + r": ")
_PROGRESS_PREFIX = re.compile(
    r"(?:[.F \t]|"
    r"(?:tests|bootstrap)[\\/](?:[A-Za-z0-9_]+[\\/])*[A-Za-z0-9_]+\.py[ \t]|"
    r"\[[ \t]*(?:100|[0-9]{1,2})%\])*"
)
_METADATA_COUNTS = "junit_passed=58, failed=0, skipped=0, pytest_exit=0"


def metadata_verdict(lines):
    """Require one complete exact report, permitting only pytest progress prefixes."""
    records = 0
    characters = 0
    for line in lines:
        characters += len(line)
        if characters > MAX_REPORT_BYTES:
            raise ValueError("Source report exceeds its input bound.")
        match = _REPORT_START.search(line)
        if match is None:
            continue
        prefix = line[:match.start()]
        # Quoted commands, assertions, ANSI command echoes and arbitrary text
        # cannot become verdicts just because they mention the expected result.
        if len(prefix) > 1024 or _PROGRESS_PREFIX.fullmatch(prefix) is None:
            continue
        records += 1
        if (not line.endswith("\n") or match.group(1) != "PASS"
                or line[match.end():].rstrip("\r\n") != _METADATA_COUNTS):
            raise ValueError("The metadata suite did not report exactly 58 successful cases.")
    if records != 1:
        raise ValueError("Exactly one completed metadata verdict is required.")
    return {
        "schema": 1, "suite": METADATA_SUITE, "status": "PASS",
        "junit_passed": 58, "failed": 0, "skipped": 0, "pytest_exit": 0,
        "completed_records": records,
    }


BULK_SHARDS = 6
_COMMIT = re.compile(r"[0-9a-f]{40}")


def bulk_plan(suites, shard, shard_count, source_commit):
    """Bind one serial VM to exact occurrence positions in the original inventory."""
    if type(shard_count) is not int or shard_count != BULK_SHARDS:
        raise ValueError("All six required source partitions must be scheduled.")
    if type(shard) is not int or not 1 <= shard <= BULK_SHARDS:
        raise ValueError("The source partition number must be between one and six.")
    if type(source_commit) is not str or _COMMIT.fullmatch(source_commit) is None:
        raise ValueError("Source execution requires an exact lowercase Git commit.")
    full = partition(suites)
    first = (ACADEMIC_SUITE, *ISOLATED_SUITES)
    occurrences = [{"occurrence": index + 1, "suite": name}
                   for index, name in enumerate(suites) if name not in first]
    selected = occurrences[shard - 1::BULK_SHARDS]
    if len(selected) != (67 if shard < BULK_SHARDS else 66):
        raise ValueError("Every required source partition must retain its exact size.")
    return {
        "schema": 1, "source_commit": source_commit,
        "manifest_sha256": full["manifest_sha256"],
        "original_occurrences": EXPECTED_OCCURRENCES,
        "original_distinct": EXPECTED_DISTINCT,
        "bulk_occurrences": len(occurrences),
        "shard": shard, "shards": BULK_SHARDS,
        "selected_occurrences": len(selected),
        "occurrences": selected,
        "suites": [row["suite"] for row in selected],
        "scope": "source suites; no installed native or audio-device qualification",
    }


_SUITE_REPORT_START = re.compile(r"(PASS|FAIL): (" + _MEMBER.pattern + r"): ")
_SUITE_COUNTS = re.compile(
    r"junit_passed=([0-9]{1,9}), failed=([0-9]{1,9}), "
    r"skipped=([0-9]{1,9}), pytest_exit=(-?[0-9]{1,9})"
)


def bulk_verdict(suites, shard, shard_count, source_commit, lines, runner_exit):
    """Require every assigned post-cleanup ci_check verdict, in occurrence order."""
    plan = bulk_plan(suites, shard, shard_count, source_commit)
    if type(runner_exit) is not int or runner_exit != 0:
        raise ValueError("The unchanged required-suite runner did not complete successfully.")
    records = []
    characters = 0
    metadata = None
    for line in lines:
        characters += len(line)
        if characters > MAX_REPORT_BYTES:
            raise ValueError("Source report exceeds its input bound.")
        match = _SUITE_REPORT_START.search(line)
        if match is None:
            continue
        prefix = line[:match.start()]
        if len(prefix) > 1024 or _PROGRESS_PREFIX.fullmatch(prefix) is None:
            continue
        counts = _SUITE_COUNTS.fullmatch(line[match.end():].rstrip("\r\n"))
        if not line.endswith("\n") or counts is None or match.group(1) != "PASS":
            raise ValueError("An assigned source suite did not report a complete successful verdict.")
        passed, failed, skipped, pytest_exit = map(int, counts.groups())
        position = len(records)
        if (position >= len(plan["occurrences"])
                or match.group(2) != plan["occurrences"][position]["suite"]):
            raise ValueError("Source verdicts omit, repeat, reorder or replace an assigned occurrence.")
        if passed < 1 or failed != 0 or skipped != 0 or pytest_exit != 0:
            raise ValueError("Every assigned source suite must pass without failures or skips.")
        if match.group(2) == METADATA_SUITE:
            metadata = metadata_verdict([line])
        records.append({**plan["occurrences"][position], "junit_passed": passed,
                        "failed": failed, "skipped": skipped, "pytest_exit": pytest_exit})
    if len(records) != plan["selected_occurrences"]:
        raise ValueError("Every assigned source occurrence must have a completed verdict.")
    return {
        **plan, "status": "PASS", "runner_exit": runner_exit,
        "completed_occurrences": len(records),
        "junit_passed": sum(row["junit_passed"] for row in records),
        "failed": 0, "skipped": 0, "records": records,
        "metadata_shape": metadata,
    }


def _read_report(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Source report exceeds its input byte bound.")
    return raw.decode("utf-8-sig").splitlines(keepends=True)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("isolated", "partition", "metadata", "bulk", "bulk-verdict"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--shard", type=int)
    parser.add_argument("--shards", type=int)
    parser.add_argument("--source-commit")
    parser.add_argument("--runner-exit", type=int)
    args = parser.parse_args(argv)
    bulk = args.mode in ("bulk", "bulk-verdict")
    if not bulk and any(value is not None for value in
                        (args.shard, args.shards, args.source_commit, args.runner_exit)):
        parser.error("Partition identity options are only valid for bulk execution.")
    if bulk and (args.shard is None or args.shards is None or args.source_commit is None):
        parser.error("Bulk execution requires its partition, six-part count and exact source commit.")
    if args.mode == "metadata":
        if args.report is None:
            parser.error("Metadata validation requires the completed source report.")
        try:
            result = metadata_verdict(_read_report(args.report))
        except (OSError, ValueError, UnicodeError):
            parser.error("Exactly one successful completed metadata report is required.")
    elif args.mode == "isolated":
        if args.report is not None:
            parser.error("--report is only valid for completed report validation.")
        result = isolated_plan()
    else:
        if args.mode != "bulk-verdict" and (args.report is not None or args.runner_exit is not None):
            parser.error("Completed report options require bulk-verdict mode.")
        if args.mode == "bulk-verdict" and (args.report is None or args.runner_exit is None):
            parser.error("Bulk verdicts require the complete report and actual runner exit.")
        raw = sys.stdin.buffer.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            parser.error("Source inventory exceeds its input byte bound.")
        try:
            suites = json.loads(raw.decode("utf-8"))
            if args.mode == "partition":
                result = partition(suites)
            elif args.mode == "bulk":
                result = bulk_plan(suites, args.shard, args.shards, args.source_commit)
            else:
                result = bulk_verdict(suites, args.shard, args.shards, args.source_commit,
                                      _read_report(args.report), args.runner_exit)
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            parser.error("Source execution is not the complete required partition of the original manifest.")
    print(json.dumps(result, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
