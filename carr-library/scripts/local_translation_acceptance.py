"""Exact local adapter acceptance, existing translation regressions and native flows.

Only numeric loopback TCP is allowed for the one public synthetic HTTP fixture.
Native probes use synthetic replies. No translator server, model or private
material is installed, started, discovered or linguistically qualified.
"""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def native_local(checkout, output, identity, mode=None):
    label = mode or 'selected'
    script = 'windows_translation_scope_smoke.py' if mode else 'windows_translation_local_smoke.py'
    destination = output / ('native-local-' + label + '.json')
    command = [sys.executable, '-I', '-B', '-X', 'utf8', str(checkout / 'scripts' / script),
               '--output', str(destination)]
    if mode:
        command += ['--mode', mode, '--local']
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        env.pop(key, None)
    log = output / ('native-local-' + label + '.txt')
    try:
        with log.open('wb') as stream:
            result = subprocess.run(command, cwd=checkout, env=env, stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT, timeout=180, check=False)
        if result.returncode:
            raise ValueError('Local native flow returned ' + str(result.returncode))
        report = json.loads(destination.read_text(encoding='utf-8'))
        expected = dict(status='PASS', native_controls=True, source_immutable=True,
            quotation_authority=False, actual_google_api='NOT_RUN', actual_credential_vault='NOT_USED',
            nvda='NOT_RUN', vault_cleanup='PASS', runtime_cleanup='PASS',
            native_event_loop='owned-modal-unwound', local_adapter='libretranslate-local',
            zero_remote_reservation=True, credential_calls=0, actual_local_engine='NOT_RUN',
            translation_quality='NOT_QUALIFIED')
        if any(report.get(key) != value for key, value in expected.items()):
            raise ValueError('Local native receipt does not prove its declared bounded flow.')
        if mode:
            if (report.get('schema') != 'carr-native-scoped-translation/1' or report.get('mode') != mode
                    or report.get('windows') is not True or report.get('isolated') is not True
                    or report.get('keyboard_focus') is not True or report.get('per_item_approval') is not True
                    or report.get('structure_preserved') is not True
                    or report.get('documents') != (2 if mode == 'batch' else 1)
                    or report.get('fixture_helper_sha256') != sha256(
                        (checkout / 'tests/reader/test_ebook_reader.py').read_bytes()).hexdigest()):
                raise ValueError('Local scoped native structure or retained fixture identity differs.')
            expected_modules = {
                'carr_library.application', 'carr_library.providers',
                'carr_library.assistant.translation', 'carr_library.assistant.translation_state',
                'carr_library.assistant.translation_selection', 'carr_library.assistant.translation_scope_state',
                'carr_library.assistant.translation_scope', 'carr_library.ui.translation_scope_dialog',
                'carr_library.library_backup'}
            modules = report.get('module_origins', {})
            if set(modules) != expected_modules:
                raise ValueError('Local native module inventory differs.')
            for name, origin in modules.items():
                path = Path(origin['path']).resolve(strict=True)
                if (not path.is_relative_to(Path(identity['prefix']))
                        or origin['sha256'] != identity['files'].get(name.replace('.', '/') + '.py')
                        or sha256(path.read_bytes()).hexdigest() != origin['sha256']):
                    raise ValueError('Local native flow imported foreign product bytes.')
            blocks = {'document': 2, 'chapter': 1, 'batch': 4}[mode]
            if report.get('translated_blocks') != blocks or report.get('synthetic_provider_requests') != blocks + 1:
                raise ValueError('Local native request population differs.')
        else:
            path = Path(report['installed_module']).resolve(strict=True)
            if (report.get('schema') != 'carr-native-local-translation/1'
                    or report.get('synthetic_provider_requests') != 2
                    or report.get('exact_reviewed_pair') != 'en->uk'
                    or report.get('directional_languages') is not True
                    or not path.is_relative_to(Path(identity['prefix']))
                    or sha256(path.read_bytes()).hexdigest() != identity['files']['carr_library/providers.py']):
                raise ValueError('Local selected-text native identity differs.')
        return report
    except BaseException as error:
        if log.is_file():
            print(log.read_text(encoding='utf-8', errors='replace')[-48000:], flush=True)
        try:
            partial = json.loads(destination.read_text(encoding='utf-8')) if destination.is_file() else {}
        except (ValueError, OSError):
            partial = {}
        partial.update(status='FAIL', parent_error=type(error).__name__ + ': ' + str(error)[:2000])
        destination.write_text(json.dumps(partial, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        raise


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--source-tree', required=True)
    args = parser.parse_args()
    checkout, output = args.checkout.resolve(strict=True), args.output.resolve(strict=True)
    shared = module(checkout / 'scripts/translation_scope_acceptance.py', '_translation_shared_acceptance')
    oracle = json.loads((checkout / 'scripts/local_translation_oracles.json').read_text(encoding='utf-8'))
    old = json.loads((checkout / 'scripts/translation_scope_oracles.json').read_text(encoding='utf-8'))
    local = oracle['local_functions']
    count = sum(sum(values.values()) for values in local.values())
    if (oracle['schema'] != 1 or count != 38 or old['required_cases_per_population'] != 268
            or sum(sum(values.values()) for values in old['functions'].values()) != 268
            or oracle['required_cases_per_population'] != 306
            or set(local) != {'tests.loopback.test_translation_local_adapter'}):
        raise ValueError('The exact protected local and existing translation population differs.')
    paths = [name.replace('.', '/') + '.py' for name in (*old['functions'], *local)]
    paths += ['scripts/local_translation_oracles.json', 'scripts/translation_scope_oracles.json',
              'scripts/local_translation_acceptance.py', 'scripts/translation_scope_acceptance.py',
              'scripts/windows_translation_v3_smoke.py', 'scripts/windows_translation_local_smoke.py',
              'scripts/windows_translation_scope_smoke.py', 'tests/reader/test_ebook_reader.py',
              'scripts/ci_check.py', 'scripts/ci_watchdog.py', 'packaging/source_wheel.py']
    report = dict(schema=1, status='RUNNING', source_commit=args.source_commit, source_tree=args.source_tree,
        phases={}, native={}, actual_google_api='NOT_RUN', actual_local_engine='NOT_RUN',
        translation_quality='NOT_QUALIFIED', nvda='NOT_RUN', production_models='NOT_RUN')
    failures = []
    def checkpoint():
        (output / 'local-translation-gate.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    def phase(name, operation, target):
        try:
            target[name] = operation()
            print(json.dumps(dict(phase=name, status='PASS')), flush=True)
        except BaseException as error:
            target[name] = dict(status='FAIL', error=type(error).__name__ + ': ' + str(error)[:4000])
            failures.append(name)
            print(json.dumps(dict(phase=name, status='FAIL', error=str(error)[:2000])), flush=True)
        finally:
            checkpoint()
    checkpoint()
    try:
        if sys.platform != 'win32' or not sys.flags.isolated or sys.version_info[:2] != (3, 11):
            raise ValueError('Actual isolated Windows CPython3.11 is required.')
        source = module(checkout / 'packaging/source_wheel.py', '_local_translation_source_wheel')
        report['wheel_source'] = source.verify_application_wheel(args.wheel, checkout.parent, args.source_commit)
        actual_tree = subprocess.run(['git', 'rev-parse', args.source_commit + '^{tree}'], cwd=checkout,
            check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        if actual_tree != args.source_tree:
            raise ValueError('Candidate tree changed.')
        report['installed_identity'] = shared.installed_identity(args.wheel)
        before = {path: shared.digest(checkout / path) for path in paths}
        report['fixture_and_harness_sha256'] = before
        for group, population, policy in (('existing', old['functions'], 'offline'),
                                           ('local', local, 'numeric-loopback')):
            folder = output / group
            folder.mkdir()
            for mode in ('source', 'installed'):
                phase(group + '-' + mode,
                    lambda m=mode, f=folder, p=population, n=policy:
                        shared.run_population(m, checkout, f, p, network_mode=n),
                    report['phases'])
        for label, mode in (('selected-advanced', None), ('document', 'document'),
                            ('chapter', 'chapter'), ('batch', 'batch')):
            phase('existing-' + label, lambda m=mode: shared.native(
                checkout, output, report['installed_identity'], mode=m), report['native'])
        for mode in (None, 'document', 'chapter', 'batch'):
            phase('local-' + (mode or 'selected'), lambda m=mode: native_local(
                checkout, output, report['installed_identity'], m), report['native'])
        if shared.installed_identity(args.wheel) != report['installed_identity']:
            raise ValueError('Installed product source changed during acceptance.')
        if {path: shared.digest(checkout / path) for path in paths} != before:
            raise ValueError('Protected fixture or harness bytes changed during acceptance.')
        subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--'], cwd=checkout, check=True, timeout=20)
        if failures:
            raise ValueError('Required translation phases failed: ' + ', '.join(failures))
        report.update(status='PASS', source_cases=306, installed_cases=306, native_flows=8,
                      local_loopback_cases_per_population=38, existing_cases_per_population=268)
    except BaseException as error:
        report.update(status='FAIL', error=type(error).__name__ + ': ' + str(error)[:4000])
        raise
    finally:
        checkpoint()
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
                                                  'source_cases', 'installed_cases', 'native_flows')}, sort_keys=True))


if __name__ == '__main__':
    main()
