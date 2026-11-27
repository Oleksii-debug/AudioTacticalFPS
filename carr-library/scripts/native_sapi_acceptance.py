"""Exact native SAPI acceptance using the existing batch test harness.

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
    '_native_sapi_shared_acceptance', ROOT / 'scripts' / 'audio_batch_acceptance.py')
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)
gate.ORACLE_PATH = ROOT / 'scripts' / 'native_sapi_oracles.json'
gate.ORACLE = gate.read(gate.ORACLE_PATH)
gate.SUPPORT = ('tests/__init__.py', 'tests/storage/__init__.py', 'scripts/windows_native_sapi_smoke.py')
# Native COM uses the already pinned pywin32 client and installed SAPI voices.
# No recognizer, model or FFmpeg audio processing is selected by this gate.
# The shared harness retains its compatibility field actual_ffmpeg as null.
gate.runtime_identity = lambda: None


def finalize(results):
    oracle = gate.ORACLE
    report = {'schema': 1, 'status': 'FAIL', 'source_cases': oracle['total_cases'],
              'installed_cases': oracle['total_cases'], 'native_checks': oracle['native']['total_checks'],
              'oracle_sha256': gate.file_hash(gate.ORACLE_PATH), 'nvda': 'NOT_RUN',
              'physical_playback': 'NOT_RUN'}
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
                'A required native SAPI test phase failed, timed out, skipped or did not run.')
            gate.require(all(phase[key] == source[key] for key in source)
                and phase['wheel_sha256'] == installed['wheel_sha256']
                and phase['oracle_sha256'] == report['oracle_sha256']
                and phase['actual_ffmpeg'] == runtime,
                'Native SAPI test evidence belongs to another source, wheel, oracle or runtime.')
            gate.require(gate.population(results / (mode + '-junit.xml')) == phase['case_counter'],
                'The retained exact native SAPI JUnit Counter changed.')
            interpreter = results / (mode + '-interpreter.json')
            gate.require(gate.file_hash(interpreter) == phase['interpreter_sha256'],
                'The actual native SAPI test interpreter identity receipt changed.')
            executed = gate.read(interpreter)
            checkout_path = results / (mode + '-checkout.json')
            gate.require(gate.file_hash(checkout_path) == phase['checkout_manifest_sha256'],
                'The complete tracked-source receipt changed.')
            checkout = gate.read(checkout_path)
            gate.require(checkout['source_commit'] == source['source_commit']
                and checkout['source_tree'] == source['source_tree']
                and checkout['files'] == gate.checkout_inventory()
                and len(checkout['files']) == phase['tracked_source_files'],
                'The complete tracked checkout differs from the executed native SAPI source.')
            expected = gate.source_python_inventory(checkout['files'], installed['installed_python'])
            gate.require(phase['source_only_python'] == expected['source_only']
                and executed['status'] == 'PASS' and executed['before'] == executed['after']
                and executed['before']['mode'] == mode and executed['before']['isolated'] is True
                and executed['before']['files'] == expected['installed']
                and executed['before']['complete_python'] == expected['source' if mode == 'source' else 'installed'],
                'Actual native SAPI test origins or complete application bytes changed.')
            for name, digest in phase['test_source_sha256'].items():
                gate.require(gate.file_hash(ROOT / name) == digest, 'A native SAPI test or fixture changed.')
            for name, digest in phase['runner_sha256'].items():
                gate.require(gate.file_hash(ROOT / 'scripts' / name) == digest, 'A shared runner changed.')
        gate.require(os.environ.get('NATIVE_OUTCOME') == 'success',
            'The required installed native SAPI probe failed or did not run.')
        lines = (results / 'native.txt').read_text(encoding='utf-8-sig').splitlines()
        receipts = [json.loads(line) for line in lines if line.startswith('{')]
        gate.require(len(receipts) == 1, 'Expected one unambiguous actual native SAPI receipt.')
        native = receipts[0]
        labels = Counter(oracle['native']['expected_label_counter'])
        gate.require(native['status'] == 'PASS' and native['installed_mode'] is True
            and native['isolated'] is True and native['native_controls'] == 'EXECUTED'
            and native['source_commit'] == source['source_commit']
            and Counter(native['checks']) == labels
            and Counter(line[6:] for line in lines if line.startswith('PASS: ')) == labels,
            'Native SAPI checks differ from the exact declared Counter.')
        gate.require(set(native['module_origins']) == set(oracle['native']['probe_modules']),
            'A native SAPI implementation origin is missing.')
        prefix = Path(sys.prefix).resolve(strict=True)
        for name, value in native['module_origins'].items():
            path = Path(value['path']).resolve(strict=True)
            gate.require(path.is_relative_to(prefix)
                and 'site-packages' in {part.casefold() for part in path.parts}
                and gate.file_hash(path) == value['sha256']
                == installed['installed_python'].get(name.replace('.', '/') + '.py'),
                'Native SAPI code differs from the exact source-bound installed wheel.')
        gate.require(runtime is None, 'Native SAPI qualification does not select an FFmpeg processing or inference runtime.')
        firewall = gate.read(results / 'native-firewall.json')
        released = gate.read(results / 'native-firewall-cleanup.json')
        gate.require(released['status'] == 'PASS' and released['qualified_applied'] is True
            and released['absent'] is True and released['rule_name'] == firewall['name'],
            'The exact run-owned native firewall rule was not safely removed and verified absent.')
        gate.require(firewall['status'] == 'PASS' and firewall['enabled'] is True
            and firewall['profiles_enabled'] is True and firewall['direction'] == 'Outbound'
            and firewall['action'] == 'Block'
            and Path(firewall['program']).resolve(strict=True) == Path(sys.executable).resolve(strict=True),
            'Actual native speech did not retain its scoped outgoing firewall rule.')
        pcm = native['actual_native_pcm']
        gate.require(native['physical_playback'] == 'NOT_RUN' and native['nvda'] == 'NOT_RUN'
            and native['com_client'] == 'pywin32' and native['com_client_version'] == '312'
            and native['private_entrypoint'] == 'EXECUTED'
            and native['compatibility_host_admission'] == 'PASSIVE_ACTUAL_PATH_CHECKS'
            and native['native_cancellation'] == 'OBSERVED' and native['saved_cursor'] == 0
            and type(pcm['frames']) is int and pcm['frames'] > 0
            and pcm['sample_rate'] == 22050 and pcm['channels'] == 1
            and re.fullmatch('sapi5:[0-9a-f]{24}', pcm['voice']) is not None
            and all(re.fullmatch('[0-9a-f]{64}', value) is not None for value in (
                pcm['pcm_sha256'], pcm['text_sha256'], native['reading_state_sha256']))
            and native['reading_state_sha256'] == native['restored_state_sha256'],
            'Actual native SAPI PCM, cancellation, explicit punctuation or passive restore evidence is incomplete.')
        report.update(status='PASS', wheel_sha256=installed['wheel_sha256'], actual_ffmpeg=runtime,
                      native_receipt_sha256=gate.file_hash(results / 'native.txt'))
        gate.write(results / 'native.json', native)
    except Exception as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:], flush=True)
        report['reason'] = type(error).__name__ + ': ' + str(error)
    gate.write(results / 'native-sapi-gate.json', report)
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
