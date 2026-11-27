"""Actual collection, exact source/installed execution and native assistant receipts."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


PYTEST_DRIVER = r"""
from hashlib import sha256
import importlib, json, os, pathlib, sys
source, root, phase, mode, oracle_path, evidence_path, collection_path, *arguments = sys.argv[1:]
source, root = pathlib.Path(source).resolve(), pathlib.Path(root).resolve()
oracle = json.loads(pathlib.Path(oracle_path).read_text(encoding='utf-8'))
prefix = pathlib.Path(sys.prefix).resolve()
def identities():
    result = {}
    for name, relative in oracle['modules'].items():
        path = pathlib.Path(importlib.import_module(name).__file__).resolve()
        expected = source / relative
        assert (path.is_relative_to(source / 'src') if phase == 'source'
                else path.is_relative_to(prefix) and 'site-packages' in {part.casefold() for part in path.parts}), name
        value = sha256(path.read_bytes()).hexdigest()
        assert value == sha256(expected.read_bytes()).hexdigest(), name
        result[name] = dict(path=str(path), sha256=value)
    return result
if phase == 'installed':
    assert sys.flags.isolated
before = identities()
# Only the exact copied test package is exposed for Windows spawn targets.
# It contains no application source; installed identities remain verified.
if phase == 'installed':
    sys.path.insert(0, str(root))
import pytest
class Inventory:
    def __init__(self):
        self.rows, self.deselected = [], 0
    def pytest_deselected(self, items):
        self.deselected += len(items)
    def pytest_collection_finish(self, session):
        for item in session.items:
            relative = pathlib.Path(item.path).resolve().relative_to(root).as_posix()
            assert relative in oracle['suites'], relative
            node = item.nodeid.split('::')
            module = relative[:-3].replace('/', '.')
            classname = '.'.join((module, *node[1:-1]))
            self.rows.append(dict(path=relative, nodeid=item.nodeid,
                                  classname=classname, name=node[-1]))
        assert self.rows and not self.deselected, 'Required assistant collection was empty or deselected.'
        assert {row['path'] for row in self.rows} == set(oracle['suites'])
        pathlib.Path(collection_path).write_text(json.dumps(dict(
            phase=phase, mode=mode, actual_collection=True, deselected=self.deselected,
            tests=self.rows), sort_keys=True, indent=2) + '\n', encoding='utf-8')
inventory = Inventory()
code = pytest.main(arguments, plugins=[inventory])
after = identities()
assert after == before, 'Installed/source modules changed during assistant tests.'
pathlib.Path(evidence_path).write_text(json.dumps(dict(phase=phase, mode=mode,
    isolated=bool(sys.flags.isolated), prefix=str(prefix), module_origins=before,
    collected=len(inventory.rows), deselected=inventory.deselected, pytest_exit=int(code)),
    sort_keys=True, indent=2) + '\n', encoding='utf-8')
raise SystemExit(code)
"""

NATIVE_DRIVER = r"""
import runpy, sys
from carr_library.native_runtime import activate_bundled_runtime
handle = activate_bundled_runtime()
try:
    probe, work = sys.argv[1:]
    sys.argv = [probe, work, '--native']
    runpy.run_path(probe, run_name='__main__')
finally:
    if handle is not None:
        handle.close()
"""


def run(command, cwd, environment, output, label, *, timeout):
    with (output / (label + '.stdout.txt')).open('wb') as stdout, (output / (label + '.stderr.txt')).open('wb') as stderr:
        result = subprocess.run(command, cwd=cwd, env=environment, stdout=stdout, stderr=stderr,
                                timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(label + ' failed with exit status ' + str(result.returncode))


def collection(path, oracle):
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('actual_collection') is not True or value.get('deselected') != 0:
        raise ValueError('Actual complete assistant collection is required.')
    rows = value['tests']
    if type(rows) is not list or not rows or {row['path'] for row in rows} != set(oracle['suites']):
        raise ValueError('Actual assistant collection omitted a required complete module.')
    ids = Counter(row['nodeid'] for row in rows)
    if any(count != 1 for count in ids.values()):
        raise ValueError('Assistant collection repeats a test node.')
    for path, expected in oracle['required_functions'].items():
        actual = Counter(row['name'].split('[', 1)[0] for row in rows if row['path'] == path)
        if actual != Counter(expected):
            raise ValueError('Required assistant function Counter differs: ' + path + ' ' + repr(actual))
    return rows


def verify_junit(path, rows):
    root = ET.parse(path).getroot()
    cases = root.findall('.//testcase')
    if any(root.findall('.//' + tag) for tag in ('failure', 'error', 'skipped')):
        raise ValueError('A required assistant case failed, errored, skipped or xfailed.')
    expected = Counter((row['classname'], row['name']) for row in rows)
    actual = Counter((row.get('classname'), row.get('name')) for row in cases)
    if actual != expected:
        raise ValueError('Actual assistant JUnit inventory differs from complete collection: ' + repr((actual, expected)))
    return len(cases)


def qualify(source, output, work):
    source, output, work = Path(source).resolve(), Path(output).resolve(), Path(work).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(mode=0o700)
    if sys.platform != 'win32' or not sys.flags.isolated:
        raise ValueError('The assistant qualification requires isolated actual Windows execution.')
    oracle_path = source / 'scripts/assistant_tools_gate_oracle.json'
    oracle = json.loads(oracle_path.read_text(encoding='utf-8'))
    interactive = json.loads((source / 'scripts/assistant_interactive_acceptance_oracle.json').read_text(encoding='utf-8'))
    selected_files = [*oracle['suites'], *oracle.get('helpers', [])]
    source_files = {name: sha256((source / name).read_bytes()).hexdigest() for name in selected_files}
    source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    source_tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=source, text=True).strip()
    environment = dict(os.environ)
    for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        environment.pop(key, None)
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    ffmpeg = Path(environment['CARR_FFMPEG']).resolve(strict=True)
    discovered = shutil.which('ffmpeg')
    if discovered is None or Path(discovered).resolve(strict=True) != ffmpeg:
        raise ValueError('Old real-engine assistant cases must discover the exact gate-verified FFmpeg.')
    report = dict(schema=1, status='FAIL', source_commit=source_commit, source_tree=source_tree,
        actual_windows=True, isolated=True, installed_package=True, source_cases=None, installed_cases=None,
        native_checks=None, test_files=source_files, ffmpeg_sha256=sha256(ffmpeg.read_bytes()).hexdigest(),
        nvda='NOT_RUN', physical_audio='NOT_RUN', model_quality='NOT_RUN', full_platform='NOT_CLAIMED')
    baseline = None
    try:
        for phase in ('source', 'installed'):
            if phase == 'source':
                cwd = source
                environment['PYTHONPATH'] = os.pathsep.join((str(source / 'src'), str(source)))
                python = [sys.executable, '-B', '-X', 'utf8']
                config = []
            else:
                cwd = work / 'installed-tests'
                cwd.mkdir()
                for name in selected_files:
                    target = cwd / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / name, target)
                    if sha256(target.read_bytes()).hexdigest() != source_files[name]:
                        raise ValueError('Installed test copy differs from current candidate source.')
                (cwd / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
                environment.pop('PYTHONPATH', None)
                python = [sys.executable, '-I', '-B', '-X', 'utf8']
                config = ['-c', str(cwd / 'pytest.ini')]
            common = ['-q', '-o', 'addopts=', '-o', 'pythonpath=', '-o', 'xfail_strict=true',
                      '--rootdir', str(cwd), '--import-mode=importlib', *config, *oracle['suites']]
            for mode in ('collect', 'execute'):
                label = 'assistant-' + phase + '-' + mode
                inventory_path = output / (label + '-collection.json')
                junit = output / ('assistant-' + phase + '.xml')
                arguments = [*common, '--collect-only'] if mode == 'collect' else [*common, '--junitxml=' + str(junit)]
                command = [*python, '-c', PYTEST_DRIVER, str(source), str(cwd), phase, mode,
                           str(oracle_path), str(output / (label + '-origins.json')),
                           str(inventory_path), *arguments]
                run(command, cwd, environment, output, label, timeout=900)
                rows = collection(inventory_path, oracle)
                identities = Counter((row['path'], row['nodeid'], row['classname'], row['name']) for row in rows)
                if baseline is None:
                    baseline = identities
                    (output / 'assistant-executed-oracle.json').write_text(json.dumps(dict(
                        schema=1, source_commit=source_commit, source_tree=source_tree,
                        actual_pytest_collection=True, tests=rows, test_files=source_files),
                        sort_keys=True, indent=2) + '\n', encoding='utf-8')
                elif identities != baseline:
                    raise ValueError('Source/installed collection or execution changed the exact case population.')
                if mode == 'execute':
                    report[phase + '_cases'] = verify_junit(junit, rows)
        probe = source / 'scripts/windows_assistant_interactive_smoke.py'
        probe_sha256 = sha256(probe.read_bytes()).hexdigest()
        run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', NATIVE_DRIVER,
             str(probe), str(work / 'native')], work, environment, output,
            'assistant-native', timeout=240)
        raw = (output / 'assistant-native.stdout.txt').read_text(encoding='utf-8')
        terminal = [json.loads(line) for line in raw.splitlines() if line.lstrip().startswith('{')]
        terminal = [value for value in terminal if value.get('status') in ('PASS', 'FAIL', 'NOT_RUN')]
        if len(terminal) != 1:
            raise ValueError('The native assistant probe must produce exactly one terminal receipt.')
        native = terminal[0]
        for key, expected in interactive['native']['required_status'].items():
            if type(native.get(key)) is not type(expected) or native[key] != expected:
                raise ValueError('Native assistant qualification field differs: ' + key)
        checks = native.get('checks')
        if (type(checks) is not list or any(type(row) is not dict or set(row) != {'name', 'passed'}
                or row['passed'] is not True for row in checks)
                or Counter(row['name'] for row in checks) != Counter(interactive['native']['checks'])):
            raise ValueError('The native assistant check Counter differs or includes failure.')
        origins = native.get('module_origins')
        if type(origins) is not dict or set(origins) != set(interactive['native']['modules']):
            raise ValueError('The native probe omitted an actual installed source identity.')
        prefix = Path(sys.prefix).resolve()
        for name, relative in interactive['native']['modules'].items():
            record = origins[name]
            path = Path(record['path']).resolve(strict=True)
            expected = sha256((source / relative).read_bytes()).hexdigest()
            if (not path.is_relative_to(prefix) or 'site-packages' not in {part.casefold() for part in path.parts}
                    or record['sha256'] != expected or sha256(path.read_bytes()).hexdigest() != expected):
                raise ValueError('The native assistant used different or non-installed source.')
        metrics = native['metrics']
        if any(type(metrics.get(name)) is not int or metrics[name] <= 0 for name in ('frames', 'sample_rate')):
            raise ValueError('The native assistant did not record actual positive PCM measurements.')
        source_text = 'Prefix 😀.\r\nSelected source sentence for actual speech preparation. Another sentence.'
        if metrics['source_sha256'] != sha256(source_text.encode('utf-8')).hexdigest():
            raise ValueError('The native assistant source differs from its exact Unicode fixture.')
        if type(metrics.get('pcm_sha256')) is not str or not re.fullmatch('[0-9a-f]{64}', metrics['pcm_sha256']):
            raise ValueError('The native assistant has no prepared PCM digest.')
        if sha256(probe.read_bytes()).hexdigest() != probe_sha256:
            raise ValueError('The native probe changed during execution.')
        for name, expected in source_files.items():
            if sha256((source / name).read_bytes()).hexdigest() != expected:
                raise ValueError('A tracked assistant test changed during qualification.')
        report.update(status='PASS', native_checks=len(checks), native_probe_sha256=probe_sha256,
                      native=native, actual_collection_cases=sum(baseline.values()))
        (output / 'assistant-native.json').write_text(json.dumps(native, ensure_ascii=False,
            sort_keys=True, indent=2) + '\n', encoding='utf-8')
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        (output / 'assistant-summary.json').write_text(json.dumps(report, ensure_ascii=False,
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
