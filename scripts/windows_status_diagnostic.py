"""Bounded measurements of the unchanged Windows worker/status publication path.

This is a diagnostic, not an acceptance test or a publication retry. No models,
audio, private inputs, or dependency downloads are used by this script.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time


SOURCE_PARENT = '8fa68b9d9041dd2f4f501f4084a5c9c50998c6f9'
SOURCE_PINS = {
    'carr-library/src/carr_library/audio/process.py':
        'd9c5be71768b235831a45ff88e7aa1f37d660ef9b2c649ee3be7d4d534465fba',
    'carr-library/tests/audio/test_audio_process.py':
        '2e9237f7759e3cff2e2c98b50349a41f902f81ed16ba94cb1cde2494d0fb8c7b',
}


def child_code(stage, release):
    # The real regression's child, unchanged: closed writes and one-shot rename.
    return f'''
import os, time
from pathlib import Path
root, release = Path({str(stage)!r}), Path({str(release)!r})
status = root / 'job_status.json'
status.write_bytes(b'initial')
print('out_time_us=1000', flush=True)
deadline = time.monotonic() + 4
while not release.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('The real parent did not observe worker progress.')
    time.sleep(.005)
for index in range(50):
    temporary = root / 'status.pending'
    temporary.write_bytes(('generation-' + str(index)).encode('ascii'))
    os.replace(temporary, status)
    time.sleep(.01)
'''


def measure(runner, AudioBudget, AudioError, root, condition, number):
    case = root / f'{number:02d}-{condition}'
    stage = case / 'worker'
    stage.mkdir(parents=True)
    status, release = stage / 'job_status.json', case / 'release-worker'
    original_popen = runner.subprocess.Popen
    original_live = runner._live_file_info
    original_lstat = Path.lstat
    children, notices = [], []
    counters = {'live_queries': 0, 'status_queries': 0, 'pending_queries': 0,
                'stress_queries': 0, 'live_lstat': 0}
    query_errors = []
    stop, start = threading.Event(), threading.Event()

    def spawn(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    def observe_live(path):
        counters['live_queries'] += 1
        counters['status_queries'] += path == status
        counters['pending_queries'] += path == stage / 'status.pending'
        return original_live(path)

    def observe_lstat(path, *args, **kwargs):
        if path == status and children and children[-1].poll() is None:
            counters['live_lstat'] += 1
            raise AssertionError('The budget watcher opened changing worker status on Windows.')
        return original_lstat(path, *args, **kwargs)

    def stress_reader():
        while not start.wait(.01):
            if stop.is_set():
                return
        while not stop.is_set():
            for path in (status, stage / 'status.pending'):
                if stop.is_set():
                    return
                counters['stress_queries'] += 1
                try:
                    original_live(path)
                except FileNotFoundError:
                    pass
                except Exception as error:
                    query_errors.append({'type': type(error).__name__, 'error': str(error)[:3000]})
                    return

    def release_after_budget_check(value):
        notices.append(value)
        start.set()
        release.write_bytes(b'Continue the real synthetic worker.')

    reader = (threading.Thread(target=stress_reader, daemon=True)
              if condition == 'direct_metadata_stress' else None)
    row = {'condition': condition, 'iteration': number, 'outcome': 'COMPLETED'}
    started = time.monotonic()
    runner.subprocess.Popen = spawn
    runner._live_file_info = observe_live
    Path.lstat = observe_lstat
    try:
        if reader is not None:
            reader.start()
        runner.run_process(
            [str(Path(sys.executable).resolve()), '-I', '-B', '-c', child_code(stage, release)],
            watch=(stage,) if condition == 'current_watcher' else (),
            progress=release_after_budget_check, budget=AudioBudget(timeout=8))
    except Exception as error:
        row.update(outcome='CHILD_FAILURE' if isinstance(error, AudioError) else 'DIAGNOSTIC_ERROR',
                   error_type=type(error).__name__, error=str(error)[:8192])
    finally:
        stop.set()
        if reader is not None:
            reader.join(timeout=3)
        runner.subprocess.Popen = original_popen
        runner._live_file_info = original_live
        Path.lstat = original_lstat
    row['elapsed_seconds'] = round(time.monotonic() - started, 6)
    row['observations'] = counters
    row['progress'] = notices
    row['query_errors'] = query_errors
    row['reader_finished'] = reader is None or not reader.is_alive()
    row['child_returncodes'] = [child.poll() for child in children]
    for child in children:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
            row.update(outcome='DIAGNOSTIC_ERROR', cleanup_required=True)
    if not row['reader_finished']:
        row.update(outcome='DIAGNOSTIC_ERROR', error='Metadata reader did not finish.')
    try:
        with status.open('rb') as stream:
            row['final_status'] = stream.read(128).decode('ascii', errors='backslashreplace')
    except OSError as error:
        row['final_status_error'] = str(error)
    row['winerror_5'] = '[WinError 5]' in row.get('error', '')
    if row['outcome'] == 'COMPLETED':
        valid = (row.get('final_status') == 'generation-49' and notices == [.001]
                 and row['child_returncodes'] == [0] and not query_errors
                 and row['reader_finished'] and not counters['live_lstat'])
        if condition == 'no_watch':
            valid = valid and counters['live_queries'] == counters['stress_queries'] == 0
        elif condition == 'current_watcher':
            valid = valid and counters['status_queries'] > 0
        else:
            valid = valid and counters['stress_queries'] > 0
        if not valid:
            row.update(outcome='DIAGNOSTIC_ERROR', error='A diagnostic invariant was not satisfied.')
    print('STATUS_CASE ' + json.dumps(row, ensure_ascii=True, sort_keys=True), flush=True)
    return row


def main():
    if os.name != 'nt':
        raise RuntimeError('This diagnostic requires actual Windows.')
    root = Path(__file__).resolve().parents[1]
    actual = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_PINS}
    if actual != SOURCE_PINS:
        raise RuntimeError('The diagnostic must use the exact unchanged production and test sources.')
    source = root / 'carr-library' / 'src'
    sys.path.insert(0, str(source))
    from carr_library.audio import process as runner
    from carr_library.audio.models import AudioBudget, AudioError
    if Path(runner.__file__).resolve() != source / 'carr_library' / 'audio' / 'process.py':
        raise RuntimeError('Unexpected production source origin.')
    if not runner._USE_WINDOWS_LIVE_FILE_STATS:
        raise RuntimeError('The actual Windows metadata path is required.')
    plan = []
    for number in range(20):
        conditions = ('no_watch', 'current_watcher')
        if number % 2:
            conditions = tuple(reversed(conditions))
        plan.extend((condition, number) for condition in conditions)
    plan.extend(('direct_metadata_stress', number) for number in range(10))
    rows = []
    deadline = time.monotonic() + 360
    with tempfile.TemporaryDirectory(prefix='CARR status diagnostic ') as temporary:
        for condition, number in plan:
            if time.monotonic() + 12 > deadline:
                break
            rows.append(measure(runner, AudioBudget, AudioError, Path(temporary), condition, number))
    counts = {}
    for condition in ('no_watch', 'current_watcher', 'direct_metadata_stress'):
        selected = [row for row in rows if row['condition'] == condition]
        counts[condition] = {'attempted': len(selected),
                             'completed': sum(row['outcome'] == 'COMPLETED' for row in selected),
                             'child_failures': sum(row['outcome'] == 'CHILD_FAILURE' for row in selected),
                             'winerror_5': sum(row['winerror_5'] for row in selected),
                             'diagnostic_errors': sum(row['outcome'] == 'DIAGNOSTIC_ERROR' for row in selected)}
    failed = any(row['outcome'] != 'COMPLETED' for row in rows)
    summary = {'status': ('INCOMPLETE' if len(rows) != len(plan) else
                          'FAILURES_OBSERVED' if failed else 'NO_FAILURE_OBSERVED'),
               'source_parent': SOURCE_PARENT, 'diagnostic_commit': os.environ.get('GITHUB_SHA'),
               'source_sha256': actual, 'counts': counts, 'planned_cases': len(plan),
               'completed_measurements': len(rows), 'one_shot_replacements_per_child': 50,
               'replacement_interval_seconds': .01, 'handshake_deadline_seconds': 4,
               'child_budget_seconds': 8, 'overall_budget_seconds': 360,
               'publication_retries': 0, 'acceptance_claim': False,
               'acoustic_inference': 'NOT_RUN', 'model_downloads': 'NOT_RUN',
               'causal_handle_owner': 'NOT_ESTABLISHED_BY_THIS_DIAGNOSTIC'}
    print('STATUS_DIAGNOSTIC ' + json.dumps(summary, ensure_ascii=True, sort_keys=True), flush=True)
    return int(failed or len(rows) != len(plan))


if __name__ == '__main__':
    raise SystemExit(main())
