"""Exact-source and isolated installed Windows qualification for scoped translation."""
from collections import Counter
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def installed_identity(wheel):
    import carr_library
    import bootstrap
    prefix = Path(sys.prefix).resolve(strict=True)
    actual, origins = {}, {}
    for package in (carr_library, bootstrap):
        origin = Path(package.__file__).resolve(strict=True)
        if (not origin.is_relative_to(prefix)
                or 'site-packages' not in {part.casefold() for part in origin.parts}):
            raise ValueError('Application resolved outside the isolated installed environment.')
        origins[package.__name__] = str(origin)
        for path in origin.parent.rglob('*.py'):
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(origin.parent):
                raise ValueError('Installed application source escaped its package.')
            actual[package.__name__ + '/' + path.relative_to(origin.parent).as_posix()] = digest(path)
    with ZipFile(wheel) as archive:
        expected = {name: sha256(archive.read(name)).hexdigest()
                    for name in archive.namelist()
                    if name.endswith('.py') and name.split('/')[0] in ('carr_library', 'bootstrap')}
    if not expected or actual != expected:
        raise ValueError('Installed application source differs from the exact wheel.')
    return dict(isolated=bool(sys.flags.isolated), prefix=str(prefix),
                module_origins=origins, files=actual, wheel_sha256=digest(wheel))



def strict_junit(path, expected_functions):
    cases = list(ET.parse(path).iter('testcase'))
    expected = Counter({(module, name): count for module, names in expected_functions.items()
                        for name, count in names.items()})
    observed = Counter((row.get('classname'), row.get('name', '').partition('[')[0]) for row in cases)
    if observed != expected or any(row.find(tag) is not None for row in cases
                                   for tag in ('failure', 'error', 'skipped')):
        raise ValueError('Required scoped and selected translation cases did not pass exactly.')
    return {'cases': len(cases), 'functions': expected_functions}



def run_population(mode, checkout, output, expected_functions):
    selectors = tuple(name.replace('.', '/') + '.py' for name in expected_functions)
    child = runpy.run_path(str(checkout / 'scripts/ci_check.py'))['PYTEST_CHILD']
    origins = output / (mode + '-origins.json')
    binding = r'''
import hashlib, json, pathlib, sys
import carr_library, bootstrap
expected_root = pathlib.Path(__ROOT__).resolve(strict=True)
origins = {}
for package in (carr_library, bootstrap):
    origin = pathlib.Path(package.__file__).resolve(strict=True)
    if not origin.is_relative_to(expected_root):
        raise ValueError('The test process imported a foreign application.')
    if __INSTALLED__ and ('site-packages' not in {p.casefold() for p in origin.parts} or not sys.flags.isolated):
        raise ValueError('The installed test process lost isolation.')
    origins[package.__name__] = {'path': str(origin), 'sha256': hashlib.sha256(origin.read_bytes()).hexdigest()}
pathlib.Path(__OUTPUT__).write_text(json.dumps({'isolated': bool(sys.flags.isolated), 'module_origins': origins}, sort_keys=True), encoding='utf-8')
'''
    binding = binding.replace('__ROOT__', repr(str(sys.prefix if mode == 'installed' else checkout)))
    binding = binding.replace('__INSTALLED__', repr(mode == 'installed')).replace('__OUTPUT__', repr(str(origins)))
    arguments = [sys.executable]
    if mode == 'installed':
        arguments.append('-I')
    arguments += ['-B', '-X', 'utf8', '-c', binding + '\n' + child, 'offline',
                  '-o', 'addopts=', '-o', 'pythonpath=', '-o', 'xfail_strict=true',
                  '--import-mode=importlib', '--durations=13', '--junitxml',
                  str(output / (mode + '-junit.xml')), '--basetemp', str(output / (mode + '-tmp')),
                  '--', *(str(checkout / path) for path in selectors)]
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        env.pop(key, None)
    if mode == 'source':
        env['PYTHONPATH'] = os.pathsep.join((str(checkout / 'src'), str(checkout)))
    with (output / (mode + '-pytest.txt')).open('wb') as log:
        process = subprocess.run(arguments, cwd=checkout, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, timeout=900, check=False)
    if process.returncode:
        print((output / (mode + '-pytest.txt')).read_text(encoding='utf-8', errors='replace')[-48000:], flush=True)
        raise ValueError(mode + ' translation pytest failed: ' + str(process.returncode))
    result = strict_junit(output / (mode + '-junit.xml'), expected_functions)
    result['origins'] = json.loads(origins.read_text(encoding='utf-8'))
    return result



def native(checkout, output, identity, *, mode=None):
    script = 'windows_translation_scope_smoke.py' if mode else 'windows_translation_v3_smoke.py'
    label = mode or 'selected-advanced'
    result_path = output / ('native-' + label + '.json')
    arguments = [sys.executable, '-I', '-B', '-X', 'utf8', str(checkout / 'scripts' / script),
                 '--output', str(result_path)]
    if mode:
        arguments += ['--mode', mode]
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        env.pop(key, None)
    log = output / ('native-' + label + '.txt')
    process = None
    try:
        with log.open('wb') as stream:
            process = subprocess.run(arguments, cwd=checkout, env=env, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=subprocess.STDOUT, timeout=180, check=False)
    except subprocess.TimeoutExpired:
        # Preserve the last actual native phase and make the parent deadline
        # an explicit failed receipt, including a child that emitted PASS early.
        try:
            partial = json.loads(result_path.read_text(encoding='utf-8')) if result_path.is_file() else {}
        except (ValueError, OSError):
            partial = {}
        partial.update(status='FAIL', parent_timeout_seconds=180,
                       error='The native child did not exit within its unchanged parent deadline.')
        result_path.write_text(json.dumps(partial, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        raise
    finally:
        if process is None or process.returncode:
            if log.is_file():
                with log.open('rb') as stream:
                    stream.seek(max(0, log.stat().st_size - 48000))
                    print(stream.read(48000).decode('utf-8', errors='replace'), flush=True)
    if process.returncode:
        raise ValueError('Native translation flow failed: ' + label)
    result = json.loads(result_path.read_text(encoding='utf-8'))
    if (result.get('status') != 'PASS' or result.get('native_controls') is not True
            or result.get('source_immutable') is not True or result.get('quotation_authority') is not False
            or result.get('actual_credential_vault') != 'PASS' or result.get('actual_google_api') != 'NOT_RUN'
            or result.get('nvda') != 'NOT_RUN'
            or result.get('vault_cleanup') != 'PASS' or result.get('runtime_cleanup') != 'PASS'
            or result.get('native_event_loop') != 'owned-modal-unwound'):
        raise ValueError('Required actual native translation evidence is incomplete.')
    if mode:
        if (result.get('schema') != 'carr-native-scoped-translation/1' or result.get('mode') != mode
                or result.get('windows') is not True or result.get('isolated') is not True
                or result.get('keyboard_focus') is not True or result.get('per_item_approval') is not True
                or result.get('structure_preserved') is not True
                or result.get('documents') != (2 if mode == 'batch' else 1)
                or result.get('fixture_helper_sha256') != digest(checkout / 'tests/reader/test_ebook_reader.py')):
            raise ValueError('Required scoped native flow or fixture identity differs.')
        modules = result.get('module_origins', {})
        expected_modules = {
            'carr_library.application', 'carr_library.providers',
            'carr_library.assistant.translation', 'carr_library.assistant.translation_state',
            'carr_library.assistant.translation_selection', 'carr_library.assistant.translation_scope_state',
            'carr_library.assistant.translation_scope', 'carr_library.ui.translation_scope_dialog',
            'carr_library.library_backup'}
        if set(modules) != expected_modules:
            raise ValueError('Native scoped module-origin inventory differs.')
        for name, value in modules.items():
            path = Path(value['path']).resolve(strict=True)
            if (not path.is_relative_to(Path(identity['prefix']))
                    or value['sha256'] != identity['files'].get(name.replace('.', '/') + '.py')
                    or digest(path) != value['sha256']):
                raise ValueError('Native scoped module differs from the installed candidate.')
        expected_blocks = {'document': 2, 'chapter': 1, 'batch': 4}[mode]
        if result.get('translated_blocks') != expected_blocks or result.get('synthetic_provider_requests') != expected_blocks + 1:
            raise ValueError('Native scoped request or structural population differs.')
    else:
        expected = {'schema': 'carr-native-translation-v3-smoke/1', 'synthetic_provider_requests': 2,
                    'exact_reviewed_pair': 'en->uk', 'explicit_advanced_model': True,
                    'glossary_verified': True, 'directional_languages': True}
        if any(result.get(key) != value for key, value in expected.items()):
            raise ValueError('Existing selected-text Advanced native qualification regressed.')
        path = Path(result['installed_module']).resolve(strict=True)
        if not path.is_relative_to(Path(identity['prefix'])) or digest(path) != identity['files']['carr_library/providers.py']:
            raise ValueError('Selected-text native flow imported foreign provider source.')
    return result


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
    oracle_path = checkout / 'scripts/translation_scope_oracles.json'
    plan = json.loads(oracle_path.read_text(encoding='utf-8'))
    expected = plan['functions']
    count = sum(sum(functions.values()) for functions in expected.values())
    if plan['schema'] != 1 or count != plan['required_cases_per_population'] or count != 268:
        raise ValueError('The finite translation case manifest differs.')
    paths = [module.replace('.', '/') + '.py' for module in expected]
    paths += ['scripts/windows_translation_v3_smoke.py', 'scripts/windows_translation_scope_smoke.py',
              'tests/reader/test_ebook_reader.py', 'scripts/translation_scope_oracles.json',
              'scripts/translation_scope_acceptance.py', 'packaging/source_wheel.py', 'scripts/ci_check.py']
    report = dict(schema=1, status='RUNNING', source_commit=args.source_commit, source_tree=args.source_tree,
                  source='NOT_RUN', installed='NOT_RUN',
                  native={name: 'NOT_RUN' for name in ('selected-advanced', 'document', 'chapter', 'batch')},
                  actual_google_api='NOT_RUN', translation_quality='NOT_QUALIFIED', nvda='NOT_RUN')
    def checkpoint():
        (output / 'translation-scope-gate.json').write_text(
            json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    checkpoint()
    try:
        if sys.platform != 'win32' or not sys.flags.isolated or sys.version_info[:2] != (3, 11):
            raise ValueError('This gate requires actual isolated Windows CPython3.11.')
        spec = importlib.util.spec_from_file_location('_carr_source_wheel', checkout / 'packaging/source_wheel.py')
        source_wheel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(source_wheel)
        report['wheel_source'] = source_wheel.verify_application_wheel(args.wheel, checkout.parent, args.source_commit)
        tree = subprocess.run(['git', 'rev-parse', args.source_commit + '^{tree}'], cwd=checkout,
                              check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        if tree != args.source_tree:
            raise ValueError('The actual candidate tree differs.')
        report['installed_identity'] = installed_identity(args.wheel)
        report['fixture_and_harness_sha256'] = {path: digest(checkout / path) for path in paths}
        (output / 'before-execution.json').write_text(json.dumps(report, sort_keys=True), encoding='utf-8')
        report['source'] = run_population('source', checkout, output, expected)
        report['source_cases'] = report['source']['cases']
        checkpoint()
        print(json.dumps({'phase': 'source', 'status': 'PASS', 'cases': report['source_cases']}), flush=True)
        report['installed'] = run_population('installed', checkout, output, expected)
        report['installed_cases'] = report['installed']['cases']
        checkpoint()
        print(json.dumps({'phase': 'installed', 'status': 'PASS', 'cases': report['installed_cases']}), flush=True)
        for label, mode in (('selected-advanced', None), ('document', 'document'),
                            ('chapter', 'chapter'), ('batch', 'batch')):
            try:
                report['native'][label] = native(checkout, output, report['installed_identity'], mode=mode)
                print(json.dumps({'phase': label, 'status': 'PASS'}), flush=True)
            except BaseException as error:
                report['native'][label] = {'status': 'FAIL', 'error': type(error).__name__ + ': ' + str(error)[:2000],
                                           'receipt': 'native-' + label + '.json'}
                raise
            finally:
                checkpoint()
        if installed_identity(args.wheel) != report['installed_identity']:
            raise ValueError('Installed product source changed during qualification.')
        if {path: digest(checkout / path) for path in paths} != report['fixture_and_harness_sha256']:
            raise ValueError('Exact fixture or harness source changed during qualification.')
        subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--'], cwd=checkout, check=True, timeout=20)
        report.update(status='PASS', source_cases=count, installed_cases=count, native_flows=4)
    except BaseException as error:
        report.update(status='FAIL', error_type=type(error).__name__, error=str(error)[:2000])
        raise
    finally:
        (output / 'translation-scope-gate.json').write_text(
            json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
                                                  'source_cases', 'installed_cases', 'native_flows')}, sort_keys=True))


if __name__ == '__main__':
    main()
