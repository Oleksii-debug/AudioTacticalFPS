"""Exact source/installed/native qualification of inert named assistant workflows."""
from argparse import ArgumentParser
from collections import Counter
import subprocess
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / 'scripts' / 'workflow_template_oracle.json'
ORACLE = json.loads(ORACLE_PATH.read_text(encoding='utf-8'))
GATE_NAME = 'workflow-template-gate.json'
NATIVE_WORK = 'native-workflow-fixtures'
NATIVE_SECONDS = 240
CASE_FIELD = 'new_template_cases'
DISPATCH_PATH = Path(__file__)
spec = importlib.util.spec_from_file_location(
    '_workflow_template_base', ROOT / 'scripts' / 'audio_batch_acceptance.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
base.ORACLE_PATH, base.ORACLE = ORACLE_PATH, ORACLE
base.SUPPORT = tuple(ORACLE['helpers'])
base.PHASE_SECONDS = 1500
read, write, require, file_hash = base.read, base.write, base.require, base.file_hash
source_identity, installed_identity = base.source_identity, base.installed_identity
runtime_identity, population = base.runtime_identity, base.population
checkout_inventory, source_python_inventory = base.checkout_inventory, base.source_python_inventory


def adapter_identity():
    return {'qualification_adapter_sha256': file_hash(__file__),
            'qualification_dispatch_sha256': file_hash(DISPATCH_PATH)}


def tests(mode, results, work):
    require(sys.platform == 'win32', 'These regressions require actual native Windows execution.')
    result = base.tests(mode, results, work)
    path = results / (mode + '-gate.json')
    report = read(path)
    report.update(adapter_identity())
    write(path, report)
    return result



def native(results, work):
    report = {'schema': 1, 'status': 'FAIL', 'mode': 'native',
              'oracle_sha256': file_hash(ORACLE_PATH),
              **adapter_identity()}

    log = results / 'native-runner.txt'
    try:
        report.update(source_identity(results))
        installed = installed_identity(results)
        probe = ROOT / ORACLE['native']['probe']
        report['probe_sha256'] = file_hash(probe)
        report['wheel_sha256'] = installed['wheel_sha256']
        with log.open('wb') as output:
            result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', str(probe),
                '--work', str(work / NATIVE_WORK)], cwd=work,
                stdout=output, stderr=subprocess.STDOUT, timeout=NATIVE_SECONDS, check=False)
        report['native_exit'] = result.returncode
        require(result.returncode == 0, 'The actual native workflow probe failed.')
        values = [json.loads(line) for line in log.read_text(encoding='utf-8-sig').splitlines()
                  if line.startswith('{')]
        require(len(values) == 1, 'The native probe did not produce exactly one receipt.')
        value = values[0]
        expected = Counter(ORACLE['native']['expected_label_counter'])
        require(value['status'] == 'PASS' and value['actual_windows'] is True
                and value['isolated'] is True and Counter(value['checks']) == expected,
                'Native fields or actual check population differ from the declared oracle.')
        require(all(key in value and type(value[key]) is type(expected) and value[key] == expected
                    for key, expected in ORACLE['native'].get('required_fields', {}).items()),
                'The probe omitted a required exact typed native observation.')
        require(value['metrics'] == ORACLE['native']['expected_metrics'],
                'Actual workflow save/run/restore measurements differ.')
        require(set(value['module_origins']) == set(ORACLE['native']['probe_modules']),
                'The native probe omitted a required implementation origin.')
        prefix = Path(sys.prefix).resolve(strict=True)
        for name, actual in value['module_origins'].items():
            path = Path(actual['path']).resolve(strict=True)
            key = name.replace('.', '/') + '.py'
            require(path.is_relative_to(prefix)
                    and 'site-packages' in {part.casefold() for part in path.parts}
                    and file_hash(path) == actual['sha256'] == installed['installed_python'][key],
                    'The native control imported different or uninstalled implementation bytes.')
        require(installed_identity(results) == installed and file_hash(probe) == report['probe_sha256'],
                'The native probe or complete installed source changed during execution.')
        require(source_identity(results) == {key: report[key] for key in ('source_commit', 'source_tree')},
                'The native phase no longer belongs to the same candidate.')
        report.update(status='PASS', actual_checks=sum(expected.values()), receipt=value,
                      native_output_sha256=file_hash(log))
    except Exception as error:
        base.failure_diagnostics(log, error)
        report['reason'] = type(error).__name__ + ': ' + str(error)
    write(results / 'native-gate.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1

def finalize(results):
    gate = {'schema': 1, 'status': 'FAIL', 'source_cases': ORACLE['total_cases'],
            'installed_cases': ORACLE['total_cases'], 'oracle_sha256': file_hash(ORACLE_PATH),
            **adapter_identity(),
            'native_checks': ORACLE['native']['total_checks'],
            'nvda': 'NOT_RUN', 'language_model_quality': 'NOT_QUALIFIED', 'physical_audio': 'NOT_RUN'}
    try:
        require(sys.platform == 'win32', 'Actual Windows qualification is required.')
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
        for mode in ('source', 'installed'):
            require(all(read(results / (mode + '-gate.json'))[key] == value
                        for key, value in adapter_identity().items()),
                    'The qualification adapter or selected dispatcher changed.')
        native_phase = read(results / 'native-gate.json')
        require(os.environ.get('NATIVE_OUTCOME') == 'success' and native_phase['status'] == 'PASS'
                and native_phase['native_exit'] == 0
                and native_phase['actual_checks'] == ORACLE['native']['total_checks'],
                'The required native controls failed, timed out or did not run.')
        require(all(native_phase[key] == source[key] for key in source)
                and native_phase['wheel_sha256'] == installed['wheel_sha256']
                and native_phase['oracle_sha256'] == gate['oracle_sha256']
                and all(native_phase[key] == value for key, value in adapter_identity().items())
                and native_phase['probe_sha256'] == file_hash(ROOT / ORACLE['native']['probe'])
                and native_phase['native_output_sha256'] == file_hash(results / 'native-runner.txt'),
                'The native receipt belongs to a different source, wheel, probe or oracle.')
        firewall = read(results / 'offline-firewall.json')
        require(firewall['status'] == 'PASS' and firewall['cleanup_status'] == 'PASS'
                and os.environ.get('FIREWALL_OUTCOME') == 'success'
                and os.environ.get('CLEANUP_OUTCOME') == 'success',
                'The actual outbound block or restoration did not complete.')
        require(len(firewall['actual_rules']) == 2
                and {str(Path(row['program']).resolve(strict=True)).casefold() for row in firewall['actual_rules']}
                    == {str(Path(sys.executable).resolve(strict=True)).casefold(),
                        str(Path(os.environ['CARR_FFMPEG']).resolve(strict=True)).casefold()}
                and all(row['direction'] == 'Outbound' and row['action'] == 'Block'
                        and row['enabled'] == 'True' for row in firewall['actual_rules'])
                and {row['name'] for row in firewall['profiles_enabled']}
                    == {'Domain', 'Private', 'Public'}
                and all(row['enabled'] == 'True' for row in firewall['profiles_enabled'])
                and firewall['profiles_restored'] == firewall['profiles_before']
                and firewall['owned_rules_absent'] is True,
                'The exact Python/FFmpeg firewall rules or saved profile readback differs.')
        gate.update(status='PASS', wheel_sha256=installed['wheel_sha256'],
                    actual_ffmpeg=runtime, native_gui='PASS', actual_outbound_block='PASS',
                    firewall_cleanup='PASS', existing_cases_retained=ORACLE['retained_cases'],
                    native_receipt_sha256=file_hash(results / 'native-gate.json'))
        gate[CASE_FIELD] = ORACLE['new_cases']
    except Exception as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__)),
              file=sys.stderr, flush=True)
        gate['reason'] = type(error).__name__ + ': ' + str(error)
    write(results / GATE_NAME, gate)
    print(json.dumps(gate, ensure_ascii=False), flush=True)
    return 0 if gate['status'] == 'PASS' else 1


def main():
    parser = ArgumentParser()
    parser.add_argument('phase', choices=('source', 'installed', 'native', 'finalize'))
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    results = args.results.resolve(strict=True)
    if args.phase == 'finalize':
        return finalize(results)
    require(args.work is not None, 'The exact fresh qualification workspace is required.')
    work = args.work.resolve(strict=True)
    return native(results, work) if args.phase == 'native' else tests(args.phase, results, work)


if __name__ == '__main__':
    raise SystemExit(main())
