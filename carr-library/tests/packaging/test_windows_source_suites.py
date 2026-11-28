"""Keep the integrated Windows source partitions complete and independently required."""
from collections import Counter
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "windows_source_suites.py"
WORKFLOW = ROOT.parent / ".github" / "workflows" / "carr-windows.yml"
_spec = importlib.util.spec_from_file_location("_carr_windows_source_suites", SCRIPT)
assert _spec is not None and _spec.loader is not None
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


def workflow_inventory():
    value = WORKFLOW.read_text(encoding="utf-8")
    marker = "          $suites = @(\n"
    assert value.count(marker) == 1
    body = value.split(marker, 1)[1].split("\n          )", 1)[0]
    lines = body.splitlines()
    result = []
    for line in lines:
        match = re.fullmatch(r" {12,}'([^']+)'[,]?", line)
        assert match is not None, line
        result.append(match.group(1))
    return value, result


def run_cli(mode, raw=b""):
    return subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT), mode],
        input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )


def test_actual_workflow_inventory_preserves_all_occurrences_and_order():
    _, suites = workflow_inventory()
    plan = module.partition(suites)
    assert len(suites) == 404
    assert len(set(suites)) == 403
    assert Counter(suites)[module.REPEATED_SUITE] == 2
    assert Counter(suites)[module.BOOTSTRAP_SUITE] == 1
    first = [plan["academic"], *plan["isolated"]]
    assert len(first) == len(set(first)) == 3
    assert len(plan["bulk"]) == plan["bulk_occurrences"] == 401
    assert Counter(first + plan["bulk"]) == Counter(suites)
    assert plan["bulk"] == [item for item in suites if item not in first]
    assert plan["coverage_guard"] not in suites
    assert plan["manifest_sha256"] == module.MANIFEST_SHA256


@pytest.mark.parametrize("missing", [
    module.ACADEMIC_SUITE,
    *module.ISOLATED_SUITES,
    module.BOOTSTRAP_SUITE,
    module.REPEATED_SUITE,
])
def test_missing_required_occurrence_is_rejected(missing):
    _, suites = workflow_inventory()
    suites.remove(missing)
    with pytest.raises(ValueError):
        module.partition(suites)


@pytest.mark.parametrize("change", ["duplicate", "replace", "reorder"])
def test_same_length_inventory_changes_are_rejected(change):
    _, suites = workflow_inventory()
    if change == "duplicate":
        suites[0] = suites[1]
    elif change == "replace":
        suites[0] = "tests/storage/test_unreviewed_substitute.py"
    else:
        suites[0], suites[1] = suites[1], suites[0]
    with pytest.raises(ValueError):
        module.partition(suites)


@pytest.mark.parametrize("value", [None, {}, "not a list", (), [None] * 404])
def test_invalid_manifest_types_are_rejected(value):
    with pytest.raises(ValueError):
        module.partition(value)


@pytest.mark.parametrize("bad_path", ["../outside.py", "tests/x\nhidden.py", "x" * 513],
                         ids=("outside", "newline", "long-path"))
def test_invalid_member_is_rejected_before_partition(bad_path):
    _, suites = workflow_inventory()
    suites[0] = bad_path
    with pytest.raises(ValueError):
        module.partition(suites)


@pytest.mark.parametrize("mode", ["isolated", "partition"])
def test_actual_isolated_cli_emits_the_same_required_plan(mode):
    _, suites = workflow_inventory()
    result = run_cli(mode, json.dumps(suites).encode("utf-8"))
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    expected = module.isolated_plan() if mode == "isolated" else module.partition(suites)
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("raw", [
    b"[" + b" " * module.MAX_MANIFEST_BYTES + b"]",
    b"{broken",
    b"\xff",
], ids=("oversize", "invalid-json", "invalid-utf8"))
def test_actual_cli_rejects_oversize_or_invalid_input(raw):
    result = run_cli("partition", raw)
    assert result.returncode != 0
    assert result.stdout == b""
    assert len(result.stderr) < 2048


def test_both_phases_keep_common_prerequisites_and_all_required_verdicts():
    value, _ = workflow_inventory()
    first_name = "      - name: Run serial academic and required source tail suites\n"
    bulk_name = "      - name: Run required integrated source workflows\n"
    first = value.split(first_name, 1)[1].split(bulk_name, 1)[0]
    bulk = value.split(bulk_name, 1)[1].split("\n      - name:", 1)[0]
    condition = ("        if: ${{ !cancelled() && steps.desktop_dependencies.conclusion == 'success' "
                 "&& steps.ocr_runtime.conclusion == 'success' }}")
    assert first.count(condition) == bulk.count(condition) == 1
    assert "timeout-minutes: 12" in first
    assert "timeout-minutes: 35" in bulk
    assert "    timeout-minutes: 120\n" in value
    assert "scripts/windows_source_suites.py isolated" in first
    assert "scripts/windows_source_suites.py partition" in bulk
    assert first.index("--jobs 1 $academicSuite") < first.index("--jobs 2 @isolatedSuites $coverageSuite")
    assert first.index("$isolatedExit = $LASTEXITCODE") < first.index(
        "if ($academicExit -ne 0 -or $isolatedExit -ne 0) { exit 1 }")
    assert "--jobs 3 @parallelSuites" in bulk
    assert "if ($parallelExit -ne 0) { exit 1 }" in bulk
    assert "integrated-source-first-verdict.json" in first
    assert "integrated-source-bulk-verdict.json" in bulk
    assert "steps." not in bulk.split(condition, 1)[1]
    for phase in (first, bulk):
        assert "Verified audio and OCR prerequisites are required" in phase
        assert "foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe'))" in phase
        assert "Verified OCR executable is missing." in phase
        assert "$env:PATH = $previousPath" in phase
        assert "$env:CARR_OCR_DIR = $previousOcr" in phase
        assert "$env:TESSDATA_PREFIX = $previousTessdata" in phase
    assert "scripts/windows_source_suites.py metadata --report 'ci-results/integrated-tests.txt'" in bulk
    assert "if ($LASTEXITCODE -ne 0)" in bulk


GOOD_METADATA = ("PASS: tests/ingest/test_import_metadata_shape.py: "
                 "junit_passed=58, failed=0, skipped=0, pytest_exit=0")


@pytest.mark.parametrize("prefix", [
    "",
    ".",
    ".. [100%]",
    "tests\\operations\\test_worker_start_cleanup.py ",
    "tests\\studio\\test_studio_controller.py .",
    "tests/ui/test_reader_playback_actions.py ......",
    "F",
])
def test_metadata_accepts_real_interleaved_progress_prefixes(prefix):
    result = module.metadata_verdict([prefix + GOOD_METADATA + "\r\n"])
    assert result == {
        "schema": 1, "suite": "tests/ingest/test_import_metadata_shape.py",
        "status": "PASS", "junit_passed": 58, "failed": 0, "skipped": 0,
        "pytest_exit": 0, "completed_records": 1,
    }


@pytest.mark.parametrize("report", [
    [],
    [GOOD_METADATA.replace("test_import_metadata_shape.py", "test_other.py") + "\n"],
    [GOOD_METADATA.replace("PASS:", "FAIL:") + "\n"],
    [GOOD_METADATA.replace("junit_passed=58", "junit_passed=57") + "\n"],
    [GOOD_METADATA.replace("failed=0", "failed=1") + "\n"],
    [GOOD_METADATA.replace("skipped=0", "skipped=1") + "\n"],
    [GOOD_METADATA.replace("pytest_exit=0", "pytest_exit=1") + "\n"],
    [GOOD_METADATA + " unexpected trailing text\n"],
    [GOOD_METADATA],
    ["Write-Host '" + GOOD_METADATA + "'\n"],
    ["\x1b[36;1m" + GOOD_METADATA + "\x1b[0m\n"],
    [GOOD_METADATA + "\n", GOOD_METADATA + "\n"],
    [GOOD_METADATA + "\n", "." + GOOD_METADATA + "\n"],
    ["FAIL: tests/ingest/test_import_metadata_shape.py: "
     "required suite did not complete cleanly: OSError: cleanup\n"],
])
def test_metadata_rejects_missing_failed_incomplete_echoed_or_duplicate_verdicts(report):
    with pytest.raises(ValueError):
        module.metadata_verdict(report)


def test_command_echoes_cannot_add_or_replace_a_completed_metadata_record():
    lines = [
        "assert '" + GOOD_METADATA + "' in text\n",
        "\x1b[36;1mif ($value -contains '" + GOOD_METADATA + "') {\x1b[0m\n",
        "tests\\ui\\test_reader_playback_actions.py .." + GOOD_METADATA + "\n",
    ]
    assert module.metadata_verdict(lines)["completed_records"] == 1
    with pytest.raises(ValueError):
        module.metadata_verdict(lines[:2])


@pytest.mark.parametrize("correct", [True, False])
def test_actual_metadata_cli_validates_the_completed_report_file(tmp_path, correct):
    report = tmp_path / "integrated-tests.txt"
    line = GOOD_METADATA if correct else GOOD_METADATA.replace("failed=0", "failed=1")
    report.write_bytes(("\ufefftests\\studio\\test_studio_controller.py ." + line + "\r\n").encode("utf-8"))
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT), "metadata", "--report", str(report)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False,
    )
    if correct:
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert json.loads(result.stdout)["completed_records"] == 1
        assert json.loads(result.stdout)["junit_passed"] == 58
    else:
        assert result.returncode != 0
        assert result.stdout == b""
        assert len(result.stderr) < 2048


def test_repeated_malformed_progress_tokens_fail_in_bounded_cli(tmp_path):
    report = tmp_path / "malformed-progress.txt"
    prefix = ("[1%] " * 80) + "X"
    assert len(prefix) < 1024
    report.write_text(prefix + GOOD_METADATA + "\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT), "metadata", "--report", str(report)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False,
    )
    assert result.returncode != 0
    assert result.stdout == b""
    assert len(result.stderr) < 2048
