"""Require exact source/installed JUnit inventories and actual Windows native tools."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


PYTEST_DRIVER = r"""
import importlib, hashlib, json, os, pathlib, sys
source, phase, oracle_path, origins_path, *arguments = sys.argv[1:]
source = pathlib.Path(source).resolve()
oracle = json.loads(pathlib.Path(oracle_path).read_text(encoding='utf-8'))
prefix = pathlib.Path(sys.prefix).resolve()
origins = {}
for name, relative in oracle['modules'].items():
    module = importlib.import_module(name)
    path = pathlib.Path(module.__file__).resolve()
    expected = source / relative
    assert path.is_relative_to(source / 'src') if phase == 'source' else path.is_relative_to(prefix)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(expected.read_bytes()).hexdigest()
    origins[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
if phase == 'installed':
    assert sys.flags.isolated
pathlib.Path(origins_path).write_text(json.dumps(dict(phase=phase, isolated=bool(sys.flags.isolated),
    prefix=str(prefix), module_origins=origins), sort_keys=True, indent=2), encoding='utf-8')
import pytest
raise SystemExit(pytest.main(arguments))
"""


def run_command(command, *, cwd, env, output, label, timeout=600):
    with (output / (label + '.stdout.txt')).open('wb') as stdout, (output / (label + '.stderr.txt')).open('wb') as stderr:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(label + ' failed with exit status ' + str(result.returncode))


def check_junit(path, selectors):
    root = ET.parse(path).getroot()
    if any(root.findall('.//' + tag) for tag in ('failure', 'error', 'skipped')):
        raise ValueError('Required preparation tests failed, errored or skipped.')
    cases = root.findall('.//testcase')
    expected = {}
    for selector, value in selectors.items():
        module = Path(selector).stem
        expected.update({(module, name): count for name, count in value['functions'].items()})
    actual = Counter()
    for case in cases:
        module = case.attrib.get('classname', '').split('.')[-1]
        name = case.attrib.get('name', '').split('[', 1)[0]
        actual[(module, name)] += 1
    if actual != Counter(expected):
        raise ValueError('Preparation JUnit function inventory differs from its exact oracle: ' + repr(actual))
    return len(cases)


def qualify(source, output, work):
    source, output, work = Path(source).resolve(), Path(output).resolve(), Path(work).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(mode=0o700)
    if sys.platform != 'win32' or not sys.flags.isolated:
        raise ValueError('The qualification runner requires actual isolated Windows execution.')
    for optional in ('num2words', 'Babel'):
        try:
            importlib.metadata.version(optional)
        except importlib.metadata.PackageNotFoundError:
            pass
        else:
            raise ValueError('Optional preparation libraries must not be installed in the host core profile.')
    oracle_path = source / 'scripts/reading_preparation_acceptance_oracle.json'
    oracle = json.loads(oracle_path.read_text(encoding='utf-8'))
    normalizer = Path(os.environ['CARR_READING_PREPARATION_MANIFEST']).resolve()
    hunspell = Path(os.environ['CARR_HUNSPELL_MANIFEST']).resolve()
    if not normalizer.is_file() or not hunspell.is_file():
        raise ValueError('The gate must provision both exact reviewed optional manifests.')
    environment = dict(os.environ)
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    report = {'schema': 1, 'status': 'FAIL', 'source_cases': None, 'installed_cases': None,
              'native_checks': None, 'host_core_changed': False,
              'native_speaker_quality': 'NOT_RUN', 'nvda': 'NOT_RUN', 'languages_135': 'NOT_RUN'}
    try:
        for phase in ('source', 'installed'):
            selected = list(oracle['selectors'])
            if phase == 'source':
                cwd = source
                environment['PYTHONPATH'] = os.pathsep.join((str(source / 'src'), str(source)))
                prefix = [sys.executable, '-B', '-X', 'utf8']
                arguments = selected
            else:
                cwd = work / 'installed-tests'
                cwd.mkdir()
                for name in [*selected, *oracle['helpers']]:
                    target = cwd / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / name, target)
                (cwd / 'pytest.ini').write_text('[pytest]\naddopts = --strict-markers\n', encoding='utf-8')
                environment.pop('PYTHONPATH', None)
                prefix = [sys.executable, '-I', '-B', '-X', 'utf8']
                arguments = ['-c', str(cwd / 'pytest.ini'), '--rootdir', str(cwd), '--import-mode=importlib', *selected]
            junit = output / ('reading-preparation-' + phase + '.xml')
            command = [*prefix, '-c', PYTEST_DRIVER, str(source), phase, str(oracle_path),
                       str(output / ('reading-preparation-' + phase + '-origins.json')),
                       '-q', '--junitxml=' + str(junit), *arguments]
            run_command(command, cwd=cwd, env=environment, output=output,
                        label='reading-preparation-' + phase)
            report[phase + '_cases'] = check_junit(junit, oracle['selectors'])
            if report[phase + '_cases'] != oracle['cases_per_source_or_installed']:
                raise ValueError('Required preparation test count differs.')
        command = [sys.executable, '-I', '-B', '-X', 'utf8',
            str(source / 'scripts/windows_reading_preparation_smoke.py'), str(work / 'native'),
            '--normalizer', str(normalizer), '--hunspell', str(hunspell), '--native']
        run_command(command, cwd=work, env=environment, output=output,
                    label='reading-preparation-native', timeout=180)
        native = json.loads((output / 'reading-preparation-native.stdout.txt').read_text(encoding='utf-8'))
        for key, expected in oracle['native']['required_status'].items():
            if native.get(key) != expected or type(native.get(key)) is not type(expected):
                raise ValueError('Native preparation status differs: ' + key)
        checks = native.get('checks')
        if (type(checks) is not list or any(type(row) is not dict or set(row) != {'name', 'passed'}
                                          or row['passed'] is not True for row in checks)
                or Counter(row['name'] for row in checks) != Counter(oracle['native']['checks'])):
            raise ValueError('Native preparation check inventory differs or includes a failure.')
        origins = native.get('module_origins')
        if type(origins) is not dict or set(origins) != set(oracle['modules']):
            raise ValueError('Native preparation did not report all actual module origins.')
        prefix = Path(sys.prefix).resolve()
        for name, relative in oracle['modules'].items():
            row = origins[name]
            path = Path(row['path']).resolve()
            expected = sha256((source / relative).read_bytes()).hexdigest()
            if not path.is_relative_to(prefix) or row['sha256'] != expected or sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('Native preparation did not use the exact installed current source.')
        if native['optional_manifests'] != {
                'normalizer': sha256(normalizer.read_bytes()).hexdigest(),
                'hunspell': sha256(hunspell.read_bytes()).hexdigest()}:
            raise ValueError('Native preparation used different optional manifests.')
        report.update(status='PASS', native_checks=len(checks),
                      optional_manifests=native['optional_manifests'])
        (output / 'reading-preparation-native.json').write_text(json.dumps(native, ensure_ascii=False,
                sort_keys=True, indent=2) + '\n', encoding='utf-8')
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        (output / 'reading-preparation-summary.json').write_text(json.dumps(report, ensure_ascii=False,
                sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--work', required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.source, args.output, args.work), ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
