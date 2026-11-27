"""Measured audiobook qualification through the existing source/installed engine.

The shared audio-batch engine owns staging, offline policy, complete source and
wheel binding, real FFmpeg pins, exact JUnit Counters and failure diagnostics.
This wrapper retains all 143 queue cases and adds receipt-bound duration, backup, CLI and native UI coverage.
"""
from argparse import ArgumentParser
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / 'scripts' / 'book_duration_oracles.json'
ORACLE = json.loads(ORACLE_PATH.read_text(encoding='utf-8'))
spec = importlib.util.spec_from_file_location(
    '_existing_audio_acceptance', ROOT / 'scripts' / 'audio_batch_acceptance.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
engine.ORACLE_PATH, engine.ORACLE = ORACLE_PATH, ORACLE
require, read, write, file_hash = engine.require, engine.read, engine.write, engine.file_hash
source_identity, installed_identity = engine.source_identity, engine.installed_identity
runtime_identity, population = engine.runtime_identity, engine.population
checkout_inventory, source_python_inventory = engine.checkout_inventory, engine.source_python_inventory


def finalize(results):
    gate = {'schema': 1, 'status': 'FAIL', 'source_cases': ORACLE['total_cases'],
            'installed_cases': ORACLE['total_cases'], 'native_checks': ORACLE['native']['total_checks'],
            'oracle_sha256': file_hash(ORACLE_PATH), 'nvda': 'NOT_RUN',
            'physical_audio': 'NOT_RUN', 'speech_quality': 'NOT_CLAIMED'}
    try:
        source = source_identity(results)
        installed = installed_identity(results)
        runtime = runtime_identity()
        gate.update(source)
        for mode in ('source', 'installed'):
            phase = read(results / (mode + '-gate.json'))
            require(os.environ.get(mode.upper() + '_OUTCOME') == 'success'
                    and phase['status'] == 'PASS' and phase['pytest_exit'] == 0
                    and phase['actual_cases'] == ORACLE['total_cases'],
                    'A required test phase failed, timed out, skipped, or did not run.')
            require(all(phase[key] == source[key] for key in source)
                    and phase['wheel_sha256'] == installed['wheel_sha256']
                    and phase['oracle_sha256'] == gate['oracle_sha256']
                    and phase['actual_ffmpeg'] == runtime,
                    'A test phase belongs to another source, wheel, oracle, or runtime.')
            require(population(results / (mode + '-junit.xml')) == phase['case_counter'],
                    'The retained actual JUnit population changed.')
            require(file_hash(results / (mode + '-interpreter.json')) == phase['interpreter_sha256'],
                    'The test interpreter identity receipt changed.')
            checkout_path = results / (mode + '-checkout.json')
            require(file_hash(checkout_path) == phase['checkout_manifest_sha256'],
                    'The retained complete source inventory receipt changed.')
            checkout = read(checkout_path)
            require(checkout['source_commit'] == source['source_commit']
                    and checkout['source_tree'] == source['source_tree']
                    and checkout['files'] == checkout_inventory()
                    and len(checkout['files']) == phase['tracked_source_files'],
                    'The complete tracked source inventory differs from the executed checkout.')
            expected_python = source_python_inventory(checkout['files'], installed['installed_python'])
            require(expected_python['source_only'] == phase['source_only_python'],
                    'Unshipped source diagnostics changed after execution.')
            for name, expected in phase['test_source_sha256'].items():
                require(file_hash(ROOT / name) == expected, 'A test or fixture changed after execution.')
            for name, expected in phase['runner_sha256'].items():
                require(file_hash(ROOT / 'scripts' / name) == expected, 'A runner changed after execution.')
        require(os.environ.get('NATIVE_OUTCOME') == 'success', 'The required native probe failed or did not run.')
        lines = (results / 'native.txt').read_text(encoding='utf-8-sig').splitlines()
        reports = [json.loads(line) for line in lines if line.startswith('{')]
        require(len(reports) == 1, 'The native probe did not produce one unambiguous receipt.')
        native = reports[0]
        expected = Counter(ORACLE['native']['expected_label_counter'])
        require(native['status'] == 'PASS' and native['installed_mode'] is True
                and native['isolated'] is True and native['native_controls'] == 'PASS'
                and native['native_cleanup'] == 'PASS'
                and Counter(native['checks']) == expected
                and Counter(line[6:] for line in lines if line.startswith('PASS: ')) == expected,
                'The actual native label Counters differ from the exact oracle.')
        require(set(native['module_origins']) == set(ORACLE['native']['probe_modules']),
                'The actual native implementation origin inventory is incomplete.')
        prefix = Path(sys.prefix).resolve(strict=True)
        for name, value in native['module_origins'].items():
            path = Path(value['path']).resolve(strict=True)
            key = name.replace('.', '/') + '.py'
            require(path.is_relative_to(prefix)
                    and 'site-packages' in {part.casefold() for part in path.parts}
                    and file_hash(path) == value['sha256'] == installed['installed_python'].get(key),
                    'The native probe did not execute exact installed candidate bytes.')
        require(native['actual_ffmpeg'] == runtime, 'The native duration probe used another FFmpeg runtime.')
        original = native['verified_output']
        require(original['sample_rate'] == 8000 and original['channels'] == 1
                and original['frames'] == 2000 and original['bytes'] > 0,
                'The preserved native queue conversion receipt is incomplete.')
        duration = native['measured_duration']
        require(duration['status'] == 'complete' and duration['resynthesised'] is False
                and type(duration['source_frames']) is int and duration['source_frames'] > 1
                and type(duration['parts']) is int and duration['parts'] > 1
                and duration['pcm_join_sha256'] == duration['source_pcm_sha256']
                and all(type(duration[key]) is str and len(duration[key]) == 64
                        and all(c in '0123456789abcdef' for c in duration[key])
                        for key in ('review_sha256', 'source_sha256', 'pcm_join_sha256', 'source_pcm_sha256')),
                'The native measured source/part/PCM equality receipt is incomplete.')
        gate.update(status='PASS', wheel_sha256=installed['wheel_sha256'], actual_ffmpeg=runtime,
                    native_receipt_sha256=file_hash(results / 'native.txt'))
        write(results / 'native.json', native)
    except Exception as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:], flush=True)
        gate['reason'] = type(error).__name__ + ': ' + str(error)
    write(results / 'book-duration-gate.json', gate)
    print(json.dumps(gate, ensure_ascii=False), flush=True)
    return 0 if gate['status'] == 'PASS' else 1



def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('source', 'installed', 'finalize'))
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    results = args.results.resolve(strict=True)
    if args.phase == 'finalize':
        return finalize(results)
    if args.work is None:
        parser.error('Actual test staging requires this run\'s explicit temporary directory.')
    return engine.tests(args.phase, results, args.work.resolve(strict=True))


if __name__ == '__main__':
    raise SystemExit(main())
