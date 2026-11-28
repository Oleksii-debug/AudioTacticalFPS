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
    first = value.split(first_name, 1)[1].split("\n      - name:", 1)[0]
    bulk = value.split(bulk_name, 1)[1].split("\n      - name:", 1)[0]
    condition = ("        if: ${{ !cancelled() && steps.desktop_dependencies.conclusion == 'success' "
                 "&& steps.ocr_runtime.conclusion == 'success' }}")
    source_condition = condition[:-3] + " && steps.source_identity.conclusion == 'success' }}"
    assert first.count(condition) == bulk.count(source_condition) == 1
    assert "timeout-minutes: 12" in first
    assert "timeout-minutes: 35" in bulk
    assert "    timeout-minutes: 120\n" in value
    assert "scripts/windows_source_suites.py isolated" in first
    assert "scripts/windows_source_suites.py bulk --shard" in bulk
    assert first.index("--jobs 1 $academicSuite") < first.index("--jobs 2 @isolatedSuites $coverageSuite")
    assert first.index("$isolatedExit = $LASTEXITCODE") < first.index(
        "if ($academicExit -ne 0 -or $isolatedExit -ne 0) { exit 1 }")
    assert "--jobs 1 @selectedSuites" in bulk
    assert "if ($suiteExit -ne 0 -or $receiptExit -ne 0) { exit 1 }" in bulk
    assert "integrated-source-first-verdict.json" in first
    assert "integrated-source-bulk-verdict.json" in bulk
    assert "steps." not in bulk.split(source_condition, 1)[1]
    for phase in (first, bulk):
        assert "Verified audio and OCR prerequisites are required" in phase
        assert "foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe'))" in phase
        assert "Verified OCR executable is missing." in phase
        assert "$env:PATH = $previousPath" in phase
        assert "$env:CARR_OCR_DIR = $previousOcr" in phase
        assert "$env:TESSDATA_PREFIX = $previousTessdata" in phase
    assert "scripts/windows_source_suites.py bulk-verdict --shard" in bulk
    assert "--report 'ci-results/integrated-tests.txt'" in bulk
    assert "--runner-exit $suiteExit" in bulk
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


SOURCE_COMMIT = "a" * 40


def source_plan(shard):
    _, suites = workflow_inventory()
    return module.bulk_plan(suites, shard, 6, SOURCE_COMMIT)


def source_report(plan, prefix=""):
    return [
        prefix + "PASS: " + row["suite"] + ": junit_passed="
        + str(58 if row["suite"] == module.METADATA_SUITE else 3)
        + ", failed=0, skipped=0, pytest_exit=0\n"
        for row in plan["occurrences"]
    ]


def test_six_source_vms_cover_original_occurrences_exactly_once():
    _, suites = workflow_inventory()
    plans = [source_plan(shard) for shard in range(1, 7)]
    assert [plan["selected_occurrences"] for plan in plans] == [67, 67, 67, 67, 67, 66]
    first = {module.ACADEMIC_SUITE, *module.ISOLATED_SUITES}
    expected = [(index + 1, suite) for index, suite in enumerate(suites) if suite not in first]
    observed = [(row["occurrence"], row["suite"])
                for plan in plans for row in plan["occurrences"]]
    assert sorted(observed) == expected
    assert len({number for number, _ in observed}) == len(observed) == 401
    assert Counter(name for _, name in observed)[module.REPEATED_SUITE] == 2
    assert Counter(name for _, name in observed)[module.BOOTSTRAP_SUITE] == 1
    assert Counter(name for _, name in observed) + Counter(first) == Counter(suites)
    for shard, plan in enumerate(plans, 1):
        assert plan["source_commit"] == SOURCE_COMMIT
        assert plan["manifest_sha256"] == module.MANIFEST_SHA256
        assert plan["original_occurrences"] == 404
        assert plan["original_distinct"] == 403
        assert plan["bulk_occurrences"] == 401
        assert plan["shard"] == shard and plan["shards"] == 6
        assert plan["suites"] == [name for _, name in expected[shard - 1::6]]
        assert [row["occurrence"] for row in plan["occurrences"]] == [
            number for number, _ in expected[shard - 1::6]
        ]


@pytest.mark.parametrize("shard", [0, 7, True, 1.5, "1", None],
                         ids=("zero", "seventh", "boolean", "fraction", "text", "missing"))
def test_invalid_source_shard_is_rejected(shard):
    _, suites = workflow_inventory()
    with pytest.raises(ValueError):
        module.bulk_plan(suites, shard, 6, SOURCE_COMMIT)


@pytest.mark.parametrize("shards", [0, 1, 5, 7, True],
                         ids=("zero", "one", "missing-vm", "extra-vm", "boolean"))
def test_source_matrix_must_contain_all_six_required_vms(shards):
    _, suites = workflow_inventory()
    with pytest.raises(ValueError):
        module.bulk_plan(suites, 1, shards, SOURCE_COMMIT)


@pytest.mark.parametrize("commit", [None, "HEAD", "A" * 40, "a" * 39, "g" * 40, "a" * 41],
                         ids=("missing", "symbolic", "uppercase", "short", "nonhex", "long"))
def test_source_plan_requires_exact_commit_identity(commit):
    _, suites = workflow_inventory()
    with pytest.raises(ValueError):
        module.bulk_plan(suites, 1, 6, commit)


@pytest.mark.parametrize("shard", range(1, 7))
def test_every_vm_requires_every_assigned_post_cleanup_verdict(shard):
    _, suites = workflow_inventory()
    plan = source_plan(shard)
    result = module.bulk_verdict(suites, shard, 6, SOURCE_COMMIT,
                                 source_report(plan, "tests\\ui\\test_reader_playback_actions.py ."), 0)
    assert result["status"] == "PASS"
    assert result["source_commit"] == SOURCE_COMMIT
    assert result["completed_occurrences"] == plan["selected_occurrences"]
    assert [(row["occurrence"], row["suite"]) for row in result["records"]] == [
        (row["occurrence"], row["suite"]) for row in plan["occurrences"]
    ]
    assert result["failed"] == result["skipped"] == result["runner_exit"] == 0
    assert result["junit_passed"] == sum(
        58 if row["suite"] == module.METADATA_SUITE else 3
        for row in plan["occurrences"]
    )
    if module.METADATA_SUITE in plan["suites"]:
        assert result["metadata_shape"]["junit_passed"] == 58
    else:
        assert result["metadata_shape"] is None


@pytest.mark.parametrize("damage", [
    "omit", "duplicate", "reorder", "substitute", "empty", "fail-status",
    "failed", "skipped", "pytest-exit", "incomplete-line", "runner-failure",
    "not-run-77", "command-echo", "ansi-echo",
])
def test_source_receipt_rejects_incomplete_or_forged_coverage(damage):
    _, suites = workflow_inventory()
    plan = source_plan(1)
    lines = source_report(plan)
    runner_exit = 0
    if damage == "omit":
        del lines[3]
    elif damage == "duplicate":
        lines.insert(4, lines[3])
    elif damage == "reorder":
        lines[3], lines[4] = lines[4], lines[3]
    elif damage == "substitute":
        lines[3] = lines[3].replace(plan["suites"][3], "tests/packaging/test_unselected.py")
    elif damage == "empty":
        lines = []
    elif damage == "fail-status":
        lines[3] = lines[3].replace("PASS:", "FAIL:")
    elif damage == "failed":
        lines[3] = lines[3].replace("failed=0", "failed=1")
    elif damage == "skipped":
        lines[3] = lines[3].replace("skipped=0", "skipped=1")
    elif damage == "pytest-exit":
        lines[3] = lines[3].replace("pytest_exit=0", "pytest_exit=1")
    elif damage == "incomplete-line":
        lines[-1] = lines[-1].rstrip("\n")
    elif damage == "runner-failure":
        runner_exit = 1
    elif damage == "not-run-77":
        runner_exit = 77
    elif damage == "command-echo":
        lines[3] = "Write-Host '" + lines[3].rstrip("\n") + "'\n"
    elif damage == "ansi-echo":
        lines[3] = "\x1b[36;1m" + lines[3].rstrip("\n") + "\x1b[0m\n"
    else:
        raise AssertionError(damage)
    with pytest.raises(ValueError):
        module.bulk_verdict(suites, 1, 6, SOURCE_COMMIT, lines, runner_exit)


def test_other_vm_verdicts_cannot_replace_this_occurrence_assignment():
    _, suites = workflow_inventory()
    with pytest.raises(ValueError):
        module.bulk_verdict(suites, 1, 6, SOURCE_COMMIT, source_report(source_plan(2)), 0)


def test_assigned_metadata_suite_still_requires_its_exact_58_cases():
    _, suites = workflow_inventory()
    plan = next(source_plan(shard) for shard in range(1, 7)
                if module.METADATA_SUITE in source_plan(shard)["suites"])
    lines = source_report(plan)
    index = plan["suites"].index(module.METADATA_SUITE)
    assert module.bulk_verdict(suites, plan["shard"], 6, SOURCE_COMMIT, lines, 0)[
        "metadata_shape"
    ]["junit_passed"] == 58
    lines[index] = lines[index].replace("junit_passed=58", "junit_passed=57")
    with pytest.raises(ValueError):
        module.bulk_verdict(suites, plan["shard"], 6, SOURCE_COMMIT, lines, 0)


def source_cli(mode, suites, *options):
    return subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT), mode, *options],
        input=json.dumps(suites).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False,
    )


@pytest.mark.parametrize("shard", range(1, 7))
def test_actual_isolated_cli_binds_and_validates_every_source_vm(tmp_path, shard):
    _, suites = workflow_inventory()
    identity = ["--shard", str(shard), "--shards", "6", "--source-commit", SOURCE_COMMIT]
    plan_result = source_cli("bulk", suites, *identity)
    assert plan_result.returncode == 0, plan_result.stderr.decode("utf-8", errors="replace")
    plan = json.loads(plan_result.stdout)
    assert plan == source_plan(shard)
    report = tmp_path / "completed.txt"
    raw = "\ufeff" + "".join(source_report(plan, ".")).replace("\n", "\r\n")
    report.write_bytes(raw.encode("utf-8"))
    result = source_cli("bulk-verdict", suites, *identity,
                        "--report", str(report), "--runner-exit", "0")
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    receipt = json.loads(result.stdout)
    assert receipt["source_commit"] == SOURCE_COMMIT
    assert receipt["completed_occurrences"] == plan["selected_occurrences"]
    assert receipt["status"] == "PASS" and receipt["runner_exit"] == 0
    assert receipt["occurrences"] == plan["occurrences"]


@pytest.mark.parametrize("damage", ["missing-report", "missing-exit", "not-run-77", "missing-last-suite"])
def test_actual_bulk_cli_fails_closed_before_a_pass_receipt(tmp_path, damage):
    _, suites = workflow_inventory()
    plan = source_plan(1)
    report = tmp_path / "completed.txt"
    lines = source_report(plan)
    if damage == "missing-last-suite":
        lines.pop()
    report.write_bytes("".join(lines).encode("utf-8"))
    options = ["--shard", "1", "--shards", "6", "--source-commit", SOURCE_COMMIT]
    if damage != "missing-report":
        options.extend(["--report", str(report)])
    if damage != "missing-exit":
        options.extend(["--runner-exit", "77" if damage == "not-run-77" else "0"])
    result = source_cli("bulk-verdict", suites, *options)
    assert result.returncode != 0
    assert result.stdout == b""
    assert len(result.stderr) < 2048


def test_source_matrix_has_independent_required_runtime_and_receipt_gates():
    value, _ = workflow_inventory()
    job = value.split("\n  integrated-source:\n", 1)[1].split("\n  keyboard-native:\n", 1)[0]
    assert "runs-on: windows-2022" in job
    assert "timeout-minutes: 60" in job
    assert "fail-fast: false" in job
    assert "shard: [1, 2, 3, 4, 5, 6]" in job
    assert "continue-on-error" not in job and "\n    needs:" not in job
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in job
    assert "persist-credentials: false" in job
    assert "CARR_SOURCE_EXPECTED_COMMIT:" in job
    assert "packaging/windows-cp311.lock" in job
    assert "packaging/windows-semantic-cp311.lock" in job
    assert "--require-hashes" in job and "packaging/sbom_inventory.py" in job
    assert "packaging/acquire_audio_runtime.py" in job
    assert "packaging/prepare_audio_runtime.py" in job
    assert "packaging/prepare_windows_ocr.py" in job
    assert "assert commit == os.environ['CARR_SOURCE_EXPECTED_COMMIT']" in job
    assert "assert (installed / relative).read_bytes() == raw" in job
    assert "assert not installed.is_relative_to(checkout)" in job
    assert "source test prerequisites; no installed native or audio-device qualification" in job
    assert "--shards 6 --source-commit $env:CARR_SOURCE_EXPECTED_COMMIT" in job
    assert job.index("$suiteExit = $LASTEXITCODE") < job.index("bulk-verdict --shard")
    assert job.index("$receiptExit = $LASTEXITCODE") < job.index(
        "if ($suiteExit -ne 0 -or $receiptExit -ne 0) { exit 1 }")
    assert job.index("if ($suiteExit -ne 0 -or $receiptExit -ne 0) { exit 1 }") < job.index(
        "$receiptRaw | Set-Content 'ci-results/integrated-source-bulk-receipt.json'")
    assert "if: always()" in job and "if-no-files-found: error" in job
    assert "name: carr-windows-source-" in job and "${{ matrix.shard }}" in job
