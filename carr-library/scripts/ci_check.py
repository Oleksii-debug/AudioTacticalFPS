"""Run explicitly required pytest suites; missing or skipped tests are not green.

Use only on a trusted synthetic test checkout, never on imported documents.
"""
import argparse
from functools import partial
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]

# JUnit alone cannot distinguish a non-strict XPASS from a genuine pass.
# Use pytest's own report classification in the isolated subprocess.
NETWORK_POLICY = """
import sys
import socket

network_mode = sys.argv.pop(1)
if network_mode not in ('offline', 'numeric-loopback'):
    raise SystemExit('Invalid required-test network policy')

def offline_network(*args, **kwargs):
    raise OSError('Network access is disabled during offline verification')

def local_address(address):
    return (isinstance(address, tuple) and len(address) in (2, 4)
            and address[0] in ('127.0.0.1', '::1')
            and type(address[1]) is int and 0 <= address[1] <= 65535)

def checked_network(event, args):
    if event == 'socket.getaddrinfo':
        if network_mode != 'numeric-loopback' or args[0] not in ('127.0.0.1', '::1'):
            offline_network()
    elif event in ('socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg'):
        if (network_mode != 'numeric-loopback' or not local_address(args[1])
                or args[0].family not in (socket.AF_INET, socket.AF_INET6)
                or args[0].type != socket.SOCK_STREAM):
            offline_network()
    elif event in ('socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'):
        offline_network()

sys.addaudithook(checked_network)
if network_mode == 'offline':
    socket.socket.connect = offline_network
    socket.socket.connect_ex = offline_network
    socket.create_connection = offline_network
    socket.getaddrinfo = offline_network

"""

INSTALLED_IDENTITY = r'''
def verify_packaged_python(source_checkout, selected_packages):
    """Compare every declared/shipped Python file, excluding unshipped probes."""
    import hashlib
    import tomllib
    from pathlib import Path
    source_checkout = Path(source_checkout).resolve(strict=True)
    configuration = tomllib.loads((source_checkout / 'pyproject.toml').read_text(encoding='utf-8'))
    declared = configuration['tool']['setuptools']['packages']
    if (type(declared) is not list or not declared
            or any(type(name) is not str or not all(part.isidentifier() for part in name.split('.'))
                   for name in declared)):
        raise SystemExit('Installed acceptance requires the explicit package inventory')
    if set(selected_packages) != {'carr_library', 'bootstrap'}:
        raise SystemExit('Installed acceptance requires both application package roots')
    total = 0
    for name, relative in (('carr_library', 'src/carr_library'), ('bootstrap', 'bootstrap')):
        source = source_checkout / relative
        selected = Path(selected_packages[name]).resolve(strict=True)
        if not source.is_dir() or selected == source.resolve():
            raise SystemExit('Installed acceptance refused a missing source or source-shadowed package: ' + name)
        expected = {}
        for package in declared:
            if package != name and not package.startswith(name + '.'):
                continue
            directory = source.joinpath(*package.split('.')[1:])
            if not directory.is_dir():
                raise SystemExit('Declared source package is missing: ' + package)
            for original in directory.glob('*.py'):
                expected[original.relative_to(source).as_posix()] = original
        actual = {path.relative_to(selected).as_posix(): path for path in selected.rglob('*.py')}
        if not expected or set(expected) != set(actual):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise SystemExit('Installed Python inventory differs for ' + name +
                             ': missing=' + repr(missing) + '; unexpected=' + repr(extra))
        for relative_path, original in expected.items():
            if (hashlib.sha256(original.read_bytes()).digest() !=
                    hashlib.sha256(actual[relative_path].read_bytes()).digest()):
                raise SystemExit('Installed package differs from the tested source: ' + relative_path)
        total += len(expected)
    return total
'''

PYTEST_CHILD = NETWORK_POLICY + INSTALLED_IDENTITY + """
execution_mode, source_checkout = sys.argv.pop(1), sys.argv.pop(1)
if execution_mode not in ('source', 'installed'):
    raise SystemExit('Invalid required-test package origin')
if execution_mode == 'installed':
    import importlib
    from pathlib import Path
    selected = {}
    for name in ('carr_library', 'bootstrap'):
        package = importlib.import_module(name)
        selected[name] = Path(package.__file__).resolve(strict=True).parent
    checked = verify_packaged_python(source_checkout, selected)
    print('PASS: isolated installed package bytes match this exact source checkout; '
          + str(checked) + ' declared Python files', flush=True)

try:
    import onnxruntime
except ImportError:
    pass
else:
    onnxruntime.disable_telemetry_events()

import pytest

class RejectExpectedFailures:
    seen = False

    def pytest_runtest_logreport(self, report):
        if hasattr(report, 'wasxfail'):
            self.seen = True

policy = RejectExpectedFailures()
status = pytest.main(sys.argv[1:], plugins=[policy])
if policy.seen:
    print('FAIL: pytest reported XFAIL or XPASS', flush=True)
    if status == pytest.ExitCode.OK:
        status = pytest.ExitCode.TESTS_FAILED
raise SystemExit(status)
"""


def run_suite(suite: str, *, allow_loopback=False, installed=False) -> bool:
    path = (ROOT / suite).resolve()
    if not path.is_relative_to(ROOT) or not path.exists():
        print(f'FAIL: missing or out-of-tree required suite: {suite}', flush=True)
        return False
    if allow_loopback and suite not in (
            'tests/loopback/test_local_planning.py',
            'tests/loopback/test_translation_local_adapter.py'):
        print('FAIL: numeric loopback is limited to the two explicit local planning/translation HTTP fixtures.', flush=True)
        return False
    env = dict(os.environ)
    for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH', 'CARR_CI_ALLOW_LOOPBACK'):
        env.pop(key, None)
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    if not installed:
        env['PYTHONPATH'] = str(ROOT / 'src')
    # Native ONNX initialization can precede Python socket mocks or API opt-outs.
    # Apply the documented opt-out before starting any test interpreter.
    env.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_TELEMETRY='1',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    env['HF_HUB_OFFLINE'] = '1'
    env['HF_HUB_DISABLE_TELEMETRY'] = '1'
    env['DO_NOT_TRACK'] = '1'
    print(f'RUN: required suite: {suite}', flush=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='carr-ci-') as tmp:
        report = Path(tmp) / 'junit.xml'
        try:
            proc = subprocess.run([
                sys.executable, *(['-I', '-B'] if installed else []), '-c', PYTEST_CHILD,
                'numeric-loopback' if allow_loopback else 'offline',
                'installed' if installed else 'source', str(ROOT),
                *(['-o', 'pythonpath=.', '--import-mode=importlib'] if installed else []),
                '-o', 'addopts=',
                '-o', 'xfail_strict=true', '-vv', '--durations=5', '--junitxml', str(report),
                '--basetemp', str(Path(tmp) / 'pytest'),
                '--', str(path),
            ], cwd=ROOT, env=env, timeout=600, check=False)
            cases = list(ET.parse(report).iter('testcase'))
        except (OSError, subprocess.TimeoutExpired, ET.ParseError) as exc:
            print(f'FAIL: required suite did not produce a valid report: {suite}: '
                  f'{type(exc).__name__}: {exc}', flush=True)
            return False
        failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
        skipped = sum(c.find('skipped') is not None for c in cases)
        passed = len(cases) - failed - skipped
        ok = proc.returncode == 0 and passed > 0 and failed == skipped == 0
        print(f'{"PASS" if ok else "FAIL"}: {suite}: junit_passed={passed}, failed={failed}, '
              f'skipped={skipped}, pytest_exit={proc.returncode}, elapsed_seconds={time.monotonic() - started:.2f}', flush=True)
        return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, choices=range(1, 5), default=1,
                        help='Run up to four isolated required suites concurrently; default: one')
    parser.add_argument('--allow-loopback', action='store_true',
                        help='Only the explicit synthetic planning/translation fixtures may use numeric loopback TCP')
    parser.add_argument('--installed', action='store_true',
                        help='Use isolated installed packages and verify their Python bytes against this checkout')
    parser.add_argument('suites', nargs='+', help='Explicit required test paths relative to carr-library')
    args = parser.parse_args()
    runner = partial(run_suite, allow_loopback=args.allow_loopback, installed=args.installed)
    if args.jobs == 1:
        results = [runner(suite) for suite in args.suites]
    else:
        # Every suite retains its own interpreter, offline policy, deadline and
        # JUnit verdict. Consume every future: one failing suite cannot hide
        # another missing, skipped or unfinished required suite.
        with ThreadPoolExecutor(max_workers=args.jobs, thread_name_prefix='required-suite') as executor:
            results = list(executor.map(runner, args.suites))
    print(f'Required suites complete: {len(results)}; passed={sum(results)}; '
          f'failed={len(results) - sum(results)}', flush=True)
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
