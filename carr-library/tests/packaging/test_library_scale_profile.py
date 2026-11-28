"""Actual optional profiler receipts; no capacity or unprofiled timing claim."""
import cProfile
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from carr_library import application

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'benchmark_library_scale.py'
SPEC = importlib.util.spec_from_file_location('_library_corpus_profile', SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def case():
    return {'segments_per_book': 40, 'segment_bytes': 128, 'result_page_size': 37,
            'repeats': 1, 'phase_timeout': 120.0,
            'max_disk_bytes': 128 * 1024**2, 'min_free_bytes': 0}


@pytest.mark.parametrize('enabled', [False, True])
def test_actual_profiled_measure_preserves_complete_traversal_restart_and_retained_identity(tmp_path, enabled):
    output = tmp_path / 'measurements'
    executable, env = benchmark.interpreter()
    selected = Path(application.__file__).resolve()
    source_root = SCRIPT.parents[1] / 'src'
    selection = (['--source-root', str(source_root)] if selected.is_relative_to(source_root)
                 else ['--installed'])
    command = [executable, '-I', '-B', str(SCRIPT), '--output-dir', str(output),
        '--sizes', '2', '--construction', 'retained-fixture', '--public-samples', '0',
        '--segments-per-book', '40', '--segment-bytes', '128', '--result-page-size', '37',
        '--repeats', '1', '--phase-timeout', '120', '--min-free-bytes', '0',
        '--max-disk-bytes', str(128 * 1024**2),
        '--expected-application-origin', str(selected), *selection]
    if enabled:
        command.append('--diagnostic-profile')
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=150, env=env, check=False)
    assert completed.returncode == 0, (completed.stdout[-65536:], completed.stderr[-8192:])
    report = benchmark.read_json(output / 'library-corpus-scale.json')
    configuration = benchmark.read_json(output / 'configuration.json')
    phases = report['cases'][0]['phases']
    assert report['status'] == 'PASS' and [row['phase'] for row in phases] == ['build', 'measure', 'restart']
    assert len({row['pid'] for row in phases}) == 3
    assert all(row['status'] == 'PASS' and row['process_reaped'] and row['exit_code'] == 0
               and row['books'] == 2 and row['segments'] == 80 for row in phases)
    assert len({row['corpus_sha256'] for row in phases}) == 1
    assert phases[1]['full_traversal']['results'] == 80
    assert phases[1]['full_traversal']['exact_no_loss_no_duplicates'] is True
    assert phases[1]['full_traversal']['requests'] == 3
    assert phases[1]['full_traversal']['copied_result_samples'] == 3
    assert phases[2]['full_traversal']['status'] == 'NOT_REPEATED'
    assert phases[1]['database_unchanged'] is phases[2]['database_unchanged'] is True
    assert report['harness_sha256'] == sha256(SCRIPT.read_bytes()).hexdigest()
    assert report.get('diagnostic_profile', False) is enabled
    assert configuration.get('diagnostic_profile', False) is enabled
    if enabled:
        recorded = phases[1]['diagnostic_profile']
        assert recorded['qualification'] == 'DIAGNOSTIC_ONLY'
        assert 0 < len(recorded['functions']) <= 80
        assert 'diagnostic_profile' not in phases[0] and 'diagnostic_profile' not in phases[2]
        assert b'CORPUS_PROFILE ' in completed.stdout
        assert 'DIAGNOSTIC_ONLY' in report['claims']['profiled_timing_qualification']
        assert any(row['function'] == 'traverse' for row in recorded['functions'])
    else:
        assert 'diagnostic_profile' not in configuration and 'diagnostic_profile' not in report
        assert all('diagnostic_profile' not in row for row in phases)
        assert 'profiled_timing_qualification' not in report['claims']
        assert b'CORPUS_PROFILE ' not in completed.stdout
    assert not list(output.rglob('*.prof')) and not list(output.rglob('*.pstats'))


def test_real_cprofile_bounds_function_and_caller_rows_redacts_paths_and_preserves_recursive_counts():
    scope = {}
    secret_route = '/PRIVATE_PROFILE_CANARY/customer-recording.wav'
    code = '\n'.join(f'def synthetic_{number}():\n    return sum(range({number + 2}))\n'
                     for number in range(90))
    code += '\ndef recursive(value):\n    return 0 if value == 0 else recursive(value - 1) + 1\n'
    exec(compile(code, secret_route, 'exec'), scope)
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        for number in range(90):
            assert scope[f'synthetic_{number}']() >= 1
        assert scope['recursive'](30) == 30
    finally:
        profiler.disable()
    report = benchmark.diagnostic_profile_report(profiler)
    raw = json.dumps(report, ensure_ascii=True, allow_nan=False)
    assert 'PRIVATE_PROFILE_CANARY' not in raw and 'customer-recording.wav' not in raw
    assert report['qualification'] == 'DIAGNOSTIC_ONLY'
    assert len(report['functions']) == 80 and report['omitted_functions'] > 0
    assert len(raw.encode('ascii')) <= 512 * 1024
    for row in report['functions']:
        assert 0 <= row['primitive_calls'] <= row['total_calls']
        assert row['self_seconds'] >= 0 and row['cumulative_seconds'] >= 0
        assert len(row['callers']) <= 6 and row['omitted_callers'] >= 0
        for caller in row['callers']:
            assert 0 <= caller['primitive_calls'] <= caller['total_calls']
    recursive = next(row for row in report['functions'] if row['function'] == 'recursive')
    assert recursive['file'] == 'external-code'
    assert recursive['primitive_calls'] == 1 and recursive['total_calls'] == 31
    own_edge = next(row for row in recursive['callers'] if row['function'] == 'recursive')
    assert own_edge['primitive_calls'] == 1 and own_edge['total_calls'] == 30


@pytest.mark.parametrize('invalid', [None, 0, 1, 'true'])
def test_diagnostic_profile_requires_explicit_boolean_before_creating_any_output(tmp_path, invalid):
    output = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError, match='diagnostic profiler'):
        benchmark.run_benchmark(output, [2], case(), diagnostic_profile=invalid)
    assert not output.exists()
