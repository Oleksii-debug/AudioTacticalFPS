"""Exact recorded-publication acceptance using the existing batch test harness.

The shared harness owns isolated pytest, source-wheel inventory, complete
tracked-source attestation, watchdogs and bounded actual failure diagnostics.
This adapter supplies only its exact population and native receipt contract.
"""
from argparse import ArgumentParser
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    '_recorded_reader_shared_acceptance', ROOT / 'scripts' / 'audio_batch_acceptance.py')
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)
gate.ORACLE_PATH = ROOT / 'scripts' / 'media_overlay_oracles.json'
gate.ORACLE = gate.read(gate.ORACLE_PATH)
gate.SUPPORT = ('tests/__init__.py', 'scripts/windows_media_overlay_smoke.py')


def finalize(results):
    oracle = gate.ORACLE
    report = {'schema': 1, 'status': 'FAIL', 'source_cases': oracle['total_cases'],
              'installed_cases': oracle['total_cases'], 'native_checks': oracle['native']['total_checks'],
              'oracle_sha256': gate.file_hash(gate.ORACLE_PATH), 'nvda': 'NOT_RUN',
              'physical_playback': 'NOT_RUN', 'word_alignment': 'NOT_CLAIMED',
              'third_party_interoperability': 'NOT_RUN'}
    try:
        source = gate.source_identity(results)
        installed = gate.installed_identity(results)
        runtime = gate.runtime_identity()
        report.update(source)
        for mode in ('source', 'installed'):
            phase = gate.read(results / (mode + '-gate.json'))
            gate.require(os.environ.get(mode.upper() + '_OUTCOME') == 'success'
                and phase['status'] == 'PASS' and phase['pytest_exit'] == 0
                and phase['actual_cases'] == oracle['total_cases'],
                'A required reader test phase failed, timed out, skipped or did not run.')
            gate.require(all(phase[key] == source[key] for key in source)
                and phase['wheel_sha256'] == installed['wheel_sha256']
                and phase['oracle_sha256'] == report['oracle_sha256']
                and phase['actual_ffmpeg'] == runtime,
                'Reader test evidence belongs to another source, wheel, oracle or runtime.')
            gate.require(gate.population(results / (mode + '-junit.xml')) == phase['case_counter'],
                'The retained exact reader JUnit Counter changed.')
            interpreter = results / (mode + '-interpreter.json')
            gate.require(gate.file_hash(interpreter) == phase['interpreter_sha256'],
                'The actual reader test interpreter identity receipt changed.')
            executed = gate.read(interpreter)
            checkout_path = results / (mode + '-checkout.json')
            gate.require(gate.file_hash(checkout_path) == phase['checkout_manifest_sha256'],
                'The complete tracked-source receipt changed.')
            checkout = gate.read(checkout_path)
            gate.require(checkout['source_commit'] == source['source_commit']
                and checkout['source_tree'] == source['source_tree']
                and checkout['files'] == gate.checkout_inventory()
                and len(checkout['files']) == phase['tracked_source_files'],
                'The complete tracked checkout differs from the executed reader source.')
            expected = gate.source_python_inventory(checkout['files'], installed['installed_python'])
            gate.require(phase['source_only_python'] == expected['source_only']
                and executed['status'] == 'PASS' and executed['before'] == executed['after']
                and executed['before']['mode'] == mode and executed['before']['isolated'] is True
                and executed['before']['files'] == expected['installed']
                and executed['before']['complete_python'] == expected['source' if mode == 'source' else 'installed'],
                'Actual reader test origins or complete application bytes changed.')
            for name, digest in phase['test_source_sha256'].items():
                gate.require(gate.file_hash(ROOT / name) == digest, 'A reader test or fixture changed.')
            for name, digest in phase['runner_sha256'].items():
                gate.require(gate.file_hash(ROOT / 'scripts' / name) == digest, 'A shared runner changed.')
        gate.require(os.environ.get('NATIVE_OUTCOME') == 'success',
            'The required installed native recorded-reader probe failed or did not run.')
        lines = (results / 'native.txt').read_text(encoding='utf-8-sig').splitlines()
        receipts = [json.loads(line) for line in lines if line.startswith('{')]
        gate.require(len(receipts) == 1, 'Expected one unambiguous actual native reader receipt.')
        native = receipts[0]
        labels = Counter(oracle['native']['expected_label_counter'])
        gate.require(native['status'] == 'PASS' and native['installed_mode'] is True
            and native['isolated'] is True and native['native_controls'] == 'EXECUTED'
            and native['source_commit'] == source['source_commit']
            and Counter(native['checks']) == labels
            and Counter(line[6:] for line in lines if line.startswith('PASS: ')) == labels,
            'Native reader checks differ from the exact declared Counter.')
        gate.require(set(native['module_origins']) == set(oracle['native']['probe_modules']),
            'A native reader implementation origin is missing.')
        prefix = Path(sys.prefix).resolve(strict=True)
        for name, value in native['module_origins'].items():
            path = Path(value['path']).resolve(strict=True)
            gate.require(path.is_relative_to(prefix)
                and 'site-packages' in {part.casefold() for part in path.parts}
                and gate.file_hash(path) == value['sha256']
                == installed['installed_python'].get(name.replace('.', '/') + '.py'),
                'Native reader code differs from the exact source-bound installed wheel.')
        gate.require(native['actual_ffmpeg'] == runtime,
            'Native recorded reading used another FFmpeg runtime.')
        gate.require(set(native['actual_publication_pcm']) == {'epub', 'daisy'},
            'Both actual publication formats must be prepared.')
        for kind, measured in native['actual_publication_pcm'].items():
            gate.require(measured['frames'] == 8000 and measured['sample_rate'] == 16000
                and measured['channels'] == 2 and measured['controlled_frames'] == 4000
                and measured['source_time_accuracy'] == 'estimated_after_time_stretch'
                and all(re.fullmatch('[0-9a-f]{64}', measured[key]) is not None
                        for key in ('pcm_sha256', 'publication_sha256', 'controlled_pcm_sha256')),
                'Actual recorded publication/derivative PCM evidence is incomplete: ' + kind)
        gate.require(all(native[key] == value for key, value in {
            'physical_playback': 'NOT_RUN', 'nvda': 'NOT_RUN', 'word_alignment': 'NOT_CLAIMED',
            'language_quality': 'NOT_CLAIMED', 'third_party_interoperability': 'NOT_RUN'}.items()),
            'The native control probe overstates its measured scope.')
        report.update(status='PASS', wheel_sha256=installed['wheel_sha256'], actual_ffmpeg=runtime,
                      native_receipt_sha256=gate.file_hash(results / 'native.txt'))
        gate.write(results / 'native.json', native)
    except Exception as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:], flush=True)
        report['reason'] = type(error).__name__ + ': ' + str(error)
    gate.write(results / 'media-overlay-gate.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


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
        parser.error('Actual test staging requires this run\'s explicit temporary work directory.')
    return gate.tests(args.phase, results, args.work.resolve(strict=True))


if __name__ == '__main__':
    raise SystemExit(main())
