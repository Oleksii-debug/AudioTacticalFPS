"""Validate the preserved integrated Windows source inventory and its partitions.

The workflow keeps the original ordered list once. Only scheduling changes:
the academic suite remains serial, two measured long suites run next, and
the remaining suites use the existing bulk worker count and deadlines.
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
    r"(?:tests|bootstrap)[\\/](?:[A-Za-z0-9_]+[\\/])*[A-Za-z0-9_]+\.py[ \t]+|"
    r"\[[ \t]*(?:100|[0-9]{1,2})%\][ \t]*)*"
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("isolated", "partition", "metadata"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "metadata":
        if args.report is None:
            parser.error("Metadata validation requires the completed source report.")
        try:
            with args.report.open("rb") as stream:
                raw = stream.read(MAX_REPORT_BYTES + 1)
            if len(raw) > MAX_REPORT_BYTES:
                raise ValueError("Source report exceeds its input byte bound.")
            result = metadata_verdict(raw.decode("utf-8-sig").splitlines(keepends=True))
        except (OSError, ValueError, UnicodeError):
            parser.error("Exactly one successful completed metadata report is required.")
    elif args.report is not None:
        parser.error("--report is only valid for metadata validation.")
    elif args.mode == "isolated":
        result = isolated_plan()
    else:
        raw = sys.stdin.buffer.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            parser.error("Source inventory exceeds its input byte bound.")
        try:
            suites = json.loads(raw.decode("utf-8"))
            result = partition(suites)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            parser.error("Source inventory is not the exact required ordered manifest.")
    print(json.dumps(result, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
