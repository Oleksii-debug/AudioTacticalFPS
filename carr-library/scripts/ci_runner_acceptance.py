"""Exact source-only runner qualification on real Linux and Windows hosts.

The existing ci_check owner launches and settles the complete 48-case population.
Linux adds the actual fork regression; Windows receives no skip credit for it.
"""
from collections import Counter
from hashlib import sha256
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def source_files():
    names = git('ls-files', '--', '*.py', 'pyproject.toml',
                'scripts/ci_runner_oracle.json').splitlines()
    if not names or len(names) > 20000:
        raise ValueError('The tracked qualification source inventory is absent or unbounded.')
    return {name: digest(ROOT / name) for name in names}


def population(path, expected):
    cases = list(ET.parse(path).iter('testcase'))
    wanted = Counter({(module, name): count for module, names in expected.items()
                      for name, count in names.items()})
    observed = Counter((row.get('classname'), row.get('name', '').partition('[')[0])
                       for row in cases)
    if observed != wanted or any(row.find(tag) is not None for row in cases
                                for tag in ('failure', 'error', 'skipped')):
        raise ValueError('The executed runner case Counter is not the complete required PASS population.')
    return {'cases': len(cases), 'counter': expected}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 1, 'status': 'RUNNING', 'source_only': True,
              'platform': sys.platform, 'source_commit': args.source_commit,
              'fork': 'NOT_APPLICABLE_ON_WINDOWS', 'runtime': 'NOT_RUN'}
    try:
        if sys.version_info[:2] != (3, 11) or not sys.flags.isolated:
            raise ValueError('Use the fixed isolated CPython 3.11 qualification host.')
        if os.name != 'nt' and not sys.platform.startswith('linux'):
            raise ValueError('Only the declared actual Linux and Windows lanes are supported.')
        if git('rev-parse', 'HEAD') != args.source_commit:
            raise ValueError('The actual checkout differs from the frozen source commit.')
        report['source_tree'] = git('rev-parse', args.source_commit + '^{tree}')
        git('diff', '--exit-code', 'HEAD', '--')
        oracle_path = ROOT / 'scripts/ci_runner_oracle.json'
        oracle = json.loads(oracle_path.read_bytes())
        expected = dict(oracle['ci_counter'])
        selectors = ['tests/packaging/test_ci_check.py']
        if sys.platform.startswith('linux'):
            expected.update(oracle['linux_fork_counter'])
            selectors.append('tests/operations/test_resource_fork.py')
            report['fork'] = 'REQUIRED_ACTUAL_LINUX_FORK'
        required = oracle['windows_cases'] if os.name == 'nt' else oracle['linux_cases']
        if sum(sum(names.values()) for names in expected.values()) != required:
            raise ValueError('The declared exact counter does not match its required population.')
        versions = {name: importlib.metadata.version(name) for name in oracle['required_pins']}
        if versions != oracle['required_pins']:
            raise ValueError('The qualification dependency pins differ from the reviewed runtime.')
        report.update(runtime=versions, python=sys.version, executable_sha256=digest(sys.executable),
                      oracle_sha256=digest(oracle_path), source_files_sha256=source_files())
        (output / 'before.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n',
                                           encoding='utf-8')
        spec = importlib.util.spec_from_file_location('_carr_exact_ci_runner', ROOT / 'scripts/ci_check.py')
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        if runner.ROOT != ROOT:
            raise ValueError('The existing suite owner resolved a foreign checkout.')
        raw = output / 'pytest.txt'
        # Redirect inside the one owned child. The parent runner keeps the
        # original job/session owner and settles all descendants before return.
        redirect = (
            'import os,sys\n'
            'log=open(' + repr(str(raw)) + ',"wb",buffering=0)\n'
            'os.dup2(log.fileno(),1); os.dup2(log.fileno(),2); log.close()\n')
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONUNBUFFERED='1')
        for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'CARR_CI_ALLOW_LOOPBACK'):
            env.pop(key, None)
        env['PYTHONPATH'] = os.pathsep.join((str(ROOT / 'src'), str(ROOT)))
        command = [sys.executable, '-B', '-X', 'utf8', '-c', redirect + runner.PYTEST_CHILD,
                   'offline', '-o', 'addopts=', '-o', 'pythonpath=', '-o', 'xfail_strict=true',
                   '--import-mode=importlib', '--durations=20', '--junitxml',
                   str(output / 'junit.xml'), '--basetemp', str(output / 'pytest-tmp'),
                   '--', *(str(ROOT / name) for name in selectors)]
        code = runner._run_test_process(command, env=env, timeout=900)
        if code != 0:
            raise ValueError('The actual runner regression process failed with exit ' + str(code) + '.')
        report['execution'] = population(output / 'junit.xml', expected)
        if report['execution']['cases'] != required:
            raise ValueError('The complete declared population was not executed.')
        if source_files() != report['source_files_sha256']:
            raise ValueError('Tracked source changed during qualification.')
        if {name: importlib.metadata.version(name) for name in versions} != versions:
            raise ValueError('The pinned qualification runtime changed during execution.')
        git('diff', '--exit-code', 'HEAD', '--')
        report.update(status='PASS', required_cases=required, process_family='CONFIRMED_QUIESCENT')
    except BaseException as error:
        report.update(status='FAIL', reason=type(error).__name__ + ': ' + str(error)[:2000])
        traceback.print_exc()
        raw = output / 'pytest.txt'
        if raw.is_file():
            with raw.open('rb') as stream:
                stream.seek(max(0, raw.stat().st_size - 32000))
                print(stream.read(32000).decode('utf-8', 'replace'), flush=True)
    finally:
        (output / 'gate.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n',
                                         encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('status', 'platform', 'source_commit')},
                     sort_keys=True), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
