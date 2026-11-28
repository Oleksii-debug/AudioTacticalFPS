"""Keep the original broad pytest selection complete across independent jobs."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'v3_service_shards.py'
SPEC = importlib.util.spec_from_file_location('reviewed_v3_service_coverage', SCRIPT)
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)

ORIGINAL = (
    "tests/audio",
    "tests/speech",
    "tests/assistant",
    "tests/studio",
    "tests/transcription",
    "tests/academic_integration",
    "tests/test_delivery_contract.py",
    "tests/storage/test_pending_backup.py",
    "tests/storage/test_complete_pending_backup.py",
    "tests/storage/test_backup_v4_review.py",
    "tests/e2e/test_import_worker_publication.py",
    "tests/e2e/test_import_publication_receipt_integrity.py",
    "tests/ingest/test_import_replay_identity.py",
    "tests/packaging/test_worker_bytecode.py",
    "tests/storage/test_v3_state_backup.py",
    "tests/storage/test_state_snapshot_identity.py",
    "tests/language_qualification",
    "tests/conversion",
    "tests/extract/test_language_manager.py",
    "tests/packaging/test_prepare_windows_audio.py",
)


def test_two_shards_preserve_every_original_selector_in_order():
    first, second = (coverage.selection(shard) for shard in (1, 2))
    assert coverage.required_selectors() == ORIGINAL
    assert first['selectors'] + second['selectors'] == list(ORIGINAL)
    assert first['selectors'] == list(ORIGINAL[:4])
    assert second['selectors'] == list(ORIGINAL[4:])
    assert first['shard_counts'] == second['shard_counts'] == [4, 16]
    assert not set(first['selectors']) & set(second['selectors'])
    assert len(set(first['selectors']) | set(second['selectors'])) == 20
    assert first['manifest_git_blob'] == second['manifest_git_blob'] == '784b5543881199f1117ebe9a4be1fef4223665c2'
    assert first['required_sha256'] == second['required_sha256'] == '8c37b1e6d1edf7027550ac31d9d91e55f47879faf38c89244018a4cf0589c8a2'
    assert first['shard_count'] == second['shard_count'] == 2
    assert first['required_count'] == second['required_count'] == 20
    assert first['status'] == second['status'] == 'COVERAGE_VALID'
    assert all('pytest_exit' not in row for row in (first, second))


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'reordered', 'replaced',
                                    'extra', 'schema', 'nested'])
def test_changed_manifest_cannot_change_reviewed_coverage(tmp_path, change):
    value = json.loads(coverage.MANIFEST.read_bytes())
    if change == 'missing':
        value['selectors'].pop()
    elif change == 'duplicate':
        value['selectors'][-1] = value['selectors'][0]
    elif change == 'reordered':
        value['selectors'][0], value['selectors'][1] = value['selectors'][1], value['selectors'][0]
    elif change == 'replaced':
        value['selectors'][-1] = 'tests/packaging/test_ci_check.py'
    elif change == 'extra':
        value['allow_missing'] = True
    elif change == 'nested':
        value['selectors'][-1] = 'tests/audio/test_dsp.py'
    else:
        value['schema'] = 'carr-v3-broad-service/0'
    manifest = tmp_path / 'changed.json'
    manifest.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        coverage.selection(1, manifest=manifest)


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'overlap', 'reordered',
                                    'missing-job', 'moved'])
def test_partial_or_changed_assignments_never_claim_complete_coverage(change):
    required = coverage.required_selectors()
    shards = [list(part) for part in coverage._reviewed_assignment(required)]
    if change == 'missing':
        shards[1].pop()
    elif change == 'duplicate':
        shards[1][-1] = shards[1][0]
    elif change == 'overlap':
        shards[1][-1] = shards[0][0]
    elif change == 'missing-job':
        shards.pop()
    elif change == 'moved':
        shards[0][-1], shards[1][0] = shards[1][0], shards[0][-1]
    else:
        shards.reverse()
    with pytest.raises(ValueError):
        coverage.validate_partition(required, shards)


def test_duplicate_fields_are_not_an_alternate_manifest(tmp_path):
    original = coverage.MANIFEST.read_text(encoding='utf-8')
    manifest = tmp_path / 'duplicate.json'
    manifest.write_text(original.replace('{', '{"schema":"carr-v3-broad-service/0",', 1),
                        encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate'):
        coverage.required_selectors(manifest)


def test_manifest_limit_is_applied_before_decoding(tmp_path):
    manifest = tmp_path / 'too-large.json'
    manifest.write_bytes(b'x' * (coverage.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ValueError, match='too large'):
        coverage.required_selectors(manifest)


def test_crlf_and_json_presentation_do_not_change_the_reviewed_identity(tmp_path):
    value = json.loads(coverage.MANIFEST.read_bytes())
    manifest = tmp_path / 'windows.json'
    raw = json.dumps(dict(reversed(tuple(value.items()))), ensure_ascii=True, indent=4)
    manifest.write_bytes(raw.replace('\n', '\r\n').encode('ascii'))
    assert coverage.required_selectors(manifest) == ORIGINAL


def _checkout(root):
    for selector in ORIGINAL:
        path = root / selector
        if selector.endswith('.py'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# Existing selected test file.\n', encoding='utf-8')
        else:
            path.mkdir(parents=True, exist_ok=True)


def test_directory_selectors_keep_future_contained_tests_in_the_runner_arguments(tmp_path):
    _checkout(tmp_path)
    future = tmp_path / 'tests/audio/test_future_case.py'
    future.write_text('def test_future_case():\n    assert True\n', encoding='utf-8')
    first, second = (coverage.selection(shard, root=tmp_path) for shard in (1, 2))
    assert first['selectors'] + second['selectors'] == list(ORIGINAL)
    assert 'tests/audio' in first['selectors'] + second['selectors']
    assert 'tests/audio/test_future_case.py' not in first['selectors'] + second['selectors']
    assert future.read_text(encoding='utf-8').endswith('    assert True\n')


@pytest.mark.parametrize('selector', ['tests/audio', 'tests/test_delivery_contract.py'])
def test_wrong_kind_selector_refuses_before_pytest_can_start(tmp_path, selector):
    _checkout(tmp_path)
    path = tmp_path / selector
    if path.is_dir():
        path.rmdir()
        path.write_bytes(b'Not a selected directory')
    else:
        path.unlink()
        path.mkdir()
    with pytest.raises(ValueError, match='wrong-kind'):
        coverage.selection(1, root=tmp_path)


def test_missing_checkout_refuses_before_pytest_can_start(tmp_path):
    with pytest.raises(FileNotFoundError):
        coverage.selection(1, root=tmp_path)


@pytest.mark.parametrize('shard', [0, 3, True])
def test_only_the_two_required_jobs_are_admitted(shard):
    with pytest.raises(ValueError, match='shard'):
        coverage.selection(shard)


@pytest.mark.parametrize('shard', [1, 2])
def test_actual_isolated_cli_creates_selection_receipt_without_overwriting(tmp_path, shard):
    receipt = tmp_path / 'coverage.json'
    command = [sys.executable, '-I', '-B', '-S', str(SCRIPT),
               '--shard', str(shard), '--output', str(receipt)]
    result = subprocess.run(command, cwd=tmp_path, text=True, encoding='utf-8',
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    raw = receipt.read_bytes()
    assert json.loads(raw) == json.loads(result.stdout) == coverage.selection(shard)
    assert json.loads(raw)['status'] == 'COVERAGE_VALID'
    repeated = subprocess.run(command, cwd=tmp_path, text=True, encoding='utf-8',
                              capture_output=True, timeout=15)
    assert repeated.returncode == 1 and 'FAIL: V3 broad-service coverage' in repeated.stderr
    assert receipt.read_bytes() == raw
