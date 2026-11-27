"""Run explicitly required pytest suites; missing or skipped tests are not green.

Use only on a trusted synthetic test checkout, never on imported documents.
"""
import argparse
from contextlib import contextmanager
from functools import partial
import hashlib
import importlib
import math
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Lock
import time
from time import sleep
import types
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITE_TIMEOUT_SECONDS = 600
# Sampled live-file caps; each suite can retain at most this admitted prefix
# per stream. Overshoot between polls is refused, never accepted as a pass.
PROCESS_LOG_LIMIT_BYTES = 16 * 1024 * 1024
PROCESS_LOG_POLL_SECONDS = .05
_PROCESS_SOURCE = ROOT / 'src/carr_library/audio'
_PROCESS_IMPORT_LOCK = Lock()
_PROCESS_OWNER_MODULE = None


def _process_owner():
    """Reuse the application's exact stdlib-only native-family owner.

    Load it under a private tooling namespace: source helpers must not shadow
    the carr_library package whose installed bytes the child verifies.
    """
    global _PROCESS_OWNER_MODULE
    with _PROCESS_IMPORT_LOCK:
        if _PROCESS_OWNER_MODULE is None:
            directory = _PROCESS_SOURCE.resolve(strict=True)
            namespace = '_carr_required_process_' + hashlib.sha256(
                os.fsencode(directory)).hexdigest()[:20]
            if namespace not in sys.modules:
                package = types.ModuleType(namespace)
                package.__path__ = [str(directory)]
                sys.modules[namespace] = package
            _PROCESS_OWNER_MODULE = importlib.import_module(namespace + '.capture_process')
        return _PROCESS_OWNER_MODULE


@contextmanager
def _process_log_file(prefix):
    stream = tempfile.TemporaryFile(mode='w+b', prefix=prefix)
    primary = None
    try:
        yield stream
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            stream.close()
        except Exception as close_error:
            if primary is None:
                raise
            primary.add_note('Required test log close also failed: ' + type(close_error).__name__)


def _check_process_logs(streams):
    for label, stream in streams:
        if os.fstat(stream.fileno()).st_size > PROCESS_LOG_LIMIT_BYTES:
            raise RuntimeError(f'Required test {label} exceeded its bounded output allowance.')


def _relay_process_log(stream, descriptor):
    # Read only after the owned family is quiet. Child handles share the
    # regular-file offset, so reading a live spool would disturb its writes.
    stream.seek(0)
    remaining = PROCESS_LOG_LIMIT_BYTES
    while remaining:
        block = stream.read(min(64 * 1024, remaining))
        if not block:
            return
        remaining -= len(block)
        view = memoryview(block)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError('Required test output relay made no progress.')
            view = view[written:]


def _run_test_process(command, *, env, cwd=None, timeout=None) -> int:
    directory = ROOT if cwd is None else cwd
    wait_seconds = SUITE_TIMEOUT_SECONDS if timeout is None else timeout
    if (type(wait_seconds) not in (int, float) or
            not math.isfinite(wait_seconds) or wait_seconds <= 0):
        raise ValueError('Required test process timeout must be positive and finite.')
    owner = _process_owner()
    # GitHub/pwsh Tee standard handles can be pipes. The shared Windows owner
    # deliberately admits only explicit regular output files. Private spools
    # remain open through family cleanup; no ambient parent handle is inherited.
    with (_process_log_file('carr-ci-stdout-') as output,
          _process_log_file('carr-ci-stderr-') as errors):
        streams = (('stdout', output), ('stderr', errors))
        process, failure, returncode = None, None, None
        try:
            process = owner.start_owned_process(command, cwd=directory, env=env,
                                                stdout=output, stderr=errors)
            deadline = time.monotonic() + wait_seconds
            while True:
                _check_process_logs(streams)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, wait_seconds)
                try:
                    returncode = process.wait(timeout=min(PROCESS_LOG_POLL_SECONDS, remaining))
                except subprocess.TimeoutExpired:
                    continue
                _check_process_logs(streams)
                break
        except BaseException as exc:
            failure = exc
        finally:
            if process is not None:
                # Includes an exited pytest leader with surviving descendants.
                # The shared helper defers interruption and retains cleanup
                # until quiescence before either stream can close or be read.
                owner.stop_owned_process(process)
        if failure is None:
            try:
                _check_process_logs(streams)
            except Exception as exc:
                failure = exc
        if failure is not None and not isinstance(failure, Exception):
            raise failure
        try:
            _relay_process_log(output, 1)
            _relay_process_log(errors, 2)
        except Exception as relay_error:
            if failure is None:
                raise
            failure.add_note('Required test output relay also failed: ' + type(relay_error).__name__)
        if failure is not None:
            raise failure
        return returncode


def _cleanup_report_directory(directory) -> None:
    for attempt in range(5):
        try:
            directory.cleanup()
            return
        except PermissionError:
            if attempt == 4:
                raise
            sleep(.05)


@contextmanager
def _report_directory(suite):
    directory = tempfile.TemporaryDirectory(prefix='carr-ci-')
    original_error = None
    try:
        yield directory.name
    except BaseException as exc:
        original_error = exc
        raise
    finally:
        try:
            _cleanup_report_directory(directory)
        except OSError as exc:
            if original_error is None:
                raise
            print(f'FAIL: {suite}: temporary report cleanup failed after '
                  f'{type(original_error).__name__}: {type(exc).__name__}: {exc}; '
                  f'directory={directory.name}', flush=True)

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
    try:
        with _report_directory(suite) as tmp:
            report = Path(tmp) / 'junit.xml'
            returncode = _run_test_process([
                sys.executable, *(['-I', '-B'] if installed else []), '-c', PYTEST_CHILD,
                'numeric-loopback' if allow_loopback else 'offline',
                'installed' if installed else 'source', str(ROOT),
                *(['-o', 'pythonpath=.', '--import-mode=importlib'] if installed else []),
                '-o', 'addopts=',
                '-o', 'xfail_strict=true', '-vv', '--durations=5', '--junitxml', str(report),
                '--basetemp', str(Path(tmp) / 'pytest'),
                '--', str(path),
            ], env=env)
            cases = list(ET.parse(report).iter('testcase'))
            failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
            skipped = sum(c.find('skipped') is not None for c in cases)
            passed = len(cases) - failed - skipped
            ok = returncode == 0 and passed > 0 and failed == skipped == 0
        # Report PASS only after native-family and temporary-data cleanup.
        print(f'{"PASS" if ok else "FAIL"}: {suite}: junit_passed={passed}, failed={failed}, '
              f'skipped={skipped}, pytest_exit={returncode}, elapsed_seconds={time.monotonic() - started:.2f}', flush=True)
        return ok
    except (OSError, RuntimeError, ImportError, subprocess.TimeoutExpired, ET.ParseError) as exc:
        print(f'FAIL: {suite}: required suite did not complete cleanly: '
              f'{type(exc).__name__}: {exc}', flush=True)
        return False


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
