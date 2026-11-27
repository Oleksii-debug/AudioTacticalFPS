"""The V3 scheduling split must never omit or duplicate required coverage."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'v3_source_shards.py'
SPEC = importlib.util.spec_from_file_location('reviewed_v3_source_coverage', SCRIPT)
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)


def test_original_139_required_suites_are_two_complete_disjoint_shards():
    first, second = coverage.selection(1), coverage.selection(2)
    required = coverage.required_suites()
    assert len(required) == len(set(required)) == 139
    assert first['manifest_git_blob'] == second['manifest_git_blob'] == '37c38b6153c183f8dd5924a97ac8edef35f83108'
    assert first['required_sha256'] == second['required_sha256']
    assert first['required_count'] == second['required_count'] == 139
    assert first['shard_counts'] == second['shard_counts'] == [70, 69]
    assert first['suites'] == list(required[::2]) and second['suites'] == list(required[1::2])
    assert not set(first['suites']) & set(second['suites'])
    assert set(first['suites']) | set(second['suites']) == set(required)
    assert 'tests/studio/test_book_batch.py' in first['suites']
    assert 'tests/studio/test_book_cli.py' in second['suites']
    assert first['status'] == second['status'] == 'COVERAGE_VALID'


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'reordered', 'replaced', 'extra', 'schema'])
def test_changed_manifest_cannot_redefine_successful_coverage(tmp_path, change):
    value = json.loads(coverage.MANIFEST.read_bytes())
    if change == 'missing':
        value['suites'].pop()
    elif change == 'duplicate':
        value['suites'][-1] = value['suites'][0]
    elif change == 'reordered':
        value['suites'][0], value['suites'][1] = value['suites'][1], value['suites'][0]
    elif change == 'replaced':
        value['suites'][-1] = 'tests/packaging/test_ci_check.py'
    elif change == 'extra':
        value['allow_missing'] = True
    else:
        value['schema'] = 'carr-v3-required-source/0'
    manifest = tmp_path / 'changed.json'
    manifest.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        coverage.selection(1, manifest=manifest)


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'overlap', 'reordered'])
def test_coverage_guard_refuses_partial_or_overlapping_worker_assignments(change):
    required = coverage.required_suites()
    shards = [list(required[::2]), list(required[1::2])]
    if change == 'missing':
        shards[1].pop()
    elif change == 'duplicate':
        shards[1][-1] = shards[1][0]
    elif change == 'overlap':
        shards[1][-1] = shards[0][0]
    else:
        shards.reverse()
    with pytest.raises(ValueError):
        coverage.validate_partition(required, shards)


def test_duplicate_json_fields_are_not_a_second_manifest_authority(tmp_path):
    original = coverage.MANIFEST.read_text(encoding='utf-8')
    manifest = tmp_path / 'duplicate.json'
    manifest.write_text(original.replace('{', '{"schema": "carr-v3-required-source/0",', 1),
                        encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate'):
        coverage.required_suites(manifest)


def test_checkout_crlf_does_not_change_the_reviewed_selector_inventory(tmp_path):
    manifest = tmp_path / 'windows.json'
    value = json.loads(coverage.MANIFEST.read_bytes())
    # Harmless JSON presentation changes are separate from the ordered inventory.
    raw = json.dumps(dict(reversed(tuple(value.items()))), ensure_ascii=True, indent=4)
    manifest.write_bytes(raw.replace('\n', '\r\n').encode('ascii'))
    assert coverage.required_suites(manifest) == coverage.required_suites()


def test_missing_required_file_refuses_before_a_runner_can_start(tmp_path):
    with pytest.raises(FileNotFoundError):
        coverage.selection(1, root=tmp_path)


@pytest.mark.parametrize('shard', [0, 3, True])
def test_only_the_two_reviewed_shards_are_admitted(shard):
    with pytest.raises(ValueError, match='shard'):
        coverage.selection(shard)


def test_actual_isolated_coverage_cli_emits_no_execution_claim_and_preserves_prior_receipt(tmp_path):
    report = tmp_path / 'coverage.json'
    command = [sys.executable, '-I', '-B', '-S', str(SCRIPT),
               '--shard', '2', '--output', str(report)]
    result = subprocess.run(command, cwd=tmp_path, text=True, encoding='utf-8',
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    raw = report.read_bytes()
    selected = json.loads(raw)
    assert selected == json.loads(result.stdout) == coverage.selection(2)
    assert selected['status'] == 'COVERAGE_VALID' and 'pytest_exit' not in selected
    repeated = subprocess.run(command, cwd=tmp_path, text=True, encoding='utf-8',
                              capture_output=True, timeout=15)
    assert repeated.returncode == 1 and 'FAIL: V3 required-source coverage' in repeated.stderr
    assert report.read_bytes() == raw
