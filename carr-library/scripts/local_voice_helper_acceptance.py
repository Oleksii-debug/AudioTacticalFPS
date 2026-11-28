"""Exact local voice-helper acceptance using the existing batch test harness.

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
    '_local_voice_helper_shared_acceptance', ROOT / 'scripts' / 'audio_batch_acceptance.py')
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)
gate.ORACLE_PATH = ROOT / 'scripts' / 'local_voice_helper_oracles.json'
gate.ORACLE = gate.read(gate.ORACLE_PATH)
gate.SUPPORT = ('tests/__init__.py', 'tests/ui/test_transcription_v43_dialog.py',
                'scripts/local_voice_contract_fixture.py', 'scripts/windows_local_voice_helper_smoke.py')
# The explicit public helper fixture pins its own copied interpreter and script.
# No FFmpeg or private vendor inference runtime is selected by this gate.
gate.runtime_identity = lambda: None


def finalize(results):
    oracle = gate.ORACLE
    report = {'schema': 1, 'status': 'FAIL', 'source_cases': oracle['total_cases'],
              'installed_cases': oracle['total_cases'], 'native_checks': oracle['native']['total_checks'],
              'oracle_sha256': gate.file_hash(gate.ORACLE_PATH), 'nvda': 'NOT_RUN',
              'physical_playback': 'NOT_RUN', 'speech_quality': 'NOT_CLAIMED',
              'native_vendor_execution': 'NOT_RUN'}
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
                'A required local voice-helper test phase failed, timed out, skipped or did not run.')
            gate.require(all(phase[key] == source[key] for key in source)
                and phase['wheel_sha256'] == installed['wheel_sha256']
                and phase['oracle_sha256'] == report['oracle_sha256']
                and phase['actual_ffmpeg'] == runtime,
                'Local voice-helper test evidence belongs to another source, wheel, oracle or runtime.')
            gate.require(gate.population(results / (mode + '-junit.xml')) == phase['case_counter'],
                'The retained exact local voice-helper JUnit Counter changed.')
            interpreter = results / (mode + '-interpreter.json')
            gate.require(gate.file_hash(interpreter) == phase['interpreter_sha256'],
                'The actual local voice-helper test interpreter identity receipt changed.')
            executed = gate.read(interpreter)
            checkout_path = results / (mode + '-checkout.json')
            gate.require(gate.file_hash(checkout_path) == phase['checkout_manifest_sha256'],
                'The complete tracked-source receipt changed.')
            checkout = gate.read(checkout_path)
            gate.require(checkout['source_commit'] == source['source_commit']
                and checkout['source_tree'] == source['source_tree']
                and checkout['files'] == gate.checkout_inventory()
                and len(checkout['files']) == phase['tracked_source_files'],
                'The complete tracked checkout differs from the executed local voice-helper source.')
            expected = gate.source_python_inventory(checkout['files'], installed['installed_python'])
            gate.require(phase['source_only_python'] == expected['source_only']
                and executed['status'] == 'PASS' and executed['before'] == executed['after']
                and executed['before']['mode'] == mode and executed['before']['isolated'] is True
                and executed['before']['files'] == expected['installed']
                and executed['before']['complete_python'] == expected['source' if mode == 'source' else 'installed'],
                'Actual local voice-helper test origins or complete application bytes changed.')
            for name, digest in phase['test_source_sha256'].items():
                gate.require(gate.file_hash(ROOT / name) == digest, 'A local voice-helper test or fixture changed.')
            for name, digest in phase['runner_sha256'].items():
                gate.require(gate.file_hash(ROOT / 'scripts' / name) == digest, 'A shared runner changed.')
        gate.require(os.environ.get('NATIVE_OUTCOME') == 'success',
            'The required installed local voice-helper probe failed or did not run.')
        lines = (results / 'native.txt').read_text(encoding='utf-8-sig').splitlines()
        receipts = [json.loads(line) for line in lines if line.startswith('{')]
        gate.require(len(receipts) == 1, 'Expected one unambiguous actual local voice-helper receipt.')
        native = receipts[0]
        labels = Counter(oracle['native']['expected_label_counter'])
        gate.require(native['status'] == 'PASS' and native['installed_mode'] is True
            and native['isolated'] is True and native['native_controls'] == 'EXECUTED'
            and native['source_commit'] == source['source_commit']
            and Counter(native['checks']) == labels
            and Counter(line[6:] for line in lines if line.startswith('PASS: ')) == labels,
            'Local voice-helper checks differ from the exact declared Counter.')
        gate.require(set(native['module_origins']) == set(oracle['native']['probe_modules']),
            'A local voice-helper implementation origin is missing.')
        prefix = Path(sys.prefix).resolve(strict=True)
        for name, value in native['module_origins'].items():
            path = Path(value['path']).resolve(strict=True)
            gate.require(path.is_relative_to(prefix)
                and 'site-packages' in {part.casefold() for part in path.parts}
                and gate.file_hash(path) == value['sha256']
                == installed['installed_python'].get(name.replace('.', '/') + '.py'),
                'Local voice-helper code differs from the exact source-bound installed wheel.')
        gate.require(runtime is None, 'Local voice-helper qualification does not select an FFmpeg or private vendor inference runtime.')
        firewall = gate.read(results / 'native-firewall.json')
        released = gate.read(results / 'native-firewall-cleanup.json')
        gate.require(released['status'] == 'PASS' and released['qualified_applied'] is True
            and released['absent'] is True and released['rule_name'] == firewall['name'],
            'The exact run-owned native firewall rule was not safely removed and verified absent.')
        gate.require(firewall['status'] == 'PASS' and firewall['enabled'] is True
            and firewall['profiles_enabled'] is True and firewall['direction'] == 'Outbound'
            and firewall['action'] == 'Block'
            and Path(firewall['program']).resolve(strict=True) == Path(sys.executable).resolve(strict=True),
            'Actual local voice-helper review did not retain its scoped outgoing firewall rule.')
        fixture = native['fixture']
        gate.require(native['physical_playback'] == 'NOT_RUN' and native['nvda'] == 'NOT_RUN'
            and native['speech_quality'] == 'NOT_CLAIMED'
            and native['native_vendor_execution'] == 'NOT_RUN'
            and native['synthetic_helper_execution'] == 'EXECUTED'
            and native['sample_rate'] == 16000 and native['frames'] == 512
            and native['all_children_settled'] is True
            and type(native['actual_processes']) is int and native['actual_processes'] >= 6
            and native['fixture_source_sha256'] == gate.file_hash(ROOT / 'scripts' / 'local_voice_contract_fixture.py')
            and fixture == native['fixture_after']
            and fixture['launch'] == 'python' and fixture['protocol'] == 'carr.local-voice.v1'
            and type(fixture['files']) is int and fixture['files'] >= 3
            and type(fixture['bytes']) is int and fixture['bytes'] > 0
            and all(re.fullmatch('[0-9a-f]{64}', fixture[key]) is not None for key in (
                'manifest_sha256', 'fixture_script_sha256', 'selected_python_sha256', 'host_python_sha256'))
            and re.fullmatch('[0-9a-f]{64}', native['output_sha256']) is not None
            and re.fullmatch('[0-9a-f]{64}', native['retained_schema1_sha256']) is not None,
            'Actual helper launch, output, profile preservation or cleanup proof is incomplete.')
        report.update(status='PASS', wheel_sha256=installed['wheel_sha256'], actual_ffmpeg=runtime,
                      native_receipt_sha256=gate.file_hash(results / 'native.txt'))
        gate.write(results / 'native.json', native)
    except Exception as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:], flush=True)
        report['reason'] = type(error).__name__ + ': ' + str(error)
    gate.write(results / 'local-voice-helper-gate.json', report)
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
