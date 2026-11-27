"""Run explicitly required pytest suites; missing or skipped tests are not green.

Use only on a trusted synthetic test checkout, never on imported documents.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITE_TIMEOUT_SECONDS = 600


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """Stop the owned suite tree before removing its temporary library files."""
    cleanup_error = None
    try:
        if os.name == 'nt':
            # Keep the parent alive until taskkill has enumerated descendants.
            # Killing pytest first loses the ancestry needed by /T.
            taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
            result = subprocess.run(
                [str(taskkill), '/PID', str(process.pid), '/T', '/F'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=15, check=False,
            )
            if result.returncode:
                cleanup_error = OSError('Windows could not terminate the complete suite process tree.')
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (OSError, subprocess.TimeoutExpired) as exc:
        cleanup_error = exc
    if cleanup_error is not None:
        # Reap our direct child even when tree termination failed; still fail
        # closed and never report that the complete tree was cleaned up.
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if cleanup_error is not None:
        raise OSError('Required-suite process-tree cleanup failed.') from cleanup_error


def _run_test_process(command, *, env) -> int:
    isolation = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == 'nt' else {'start_new_session': True})
    process = subprocess.Popen(command, cwd=ROOT, env=env, **isolation)
    try:
        return process.wait(timeout=SUITE_TIMEOUT_SECONDS)
    except BaseException:
        _terminate_process_tree(process)
        raise

# JUnit alone cannot distinguish a non-strict XPASS from a genuine pass.
# Use pytest's own report classification in the isolated subprocess.
PYTEST_CHILD = """
import sys
import socket

def offline_network(*args, **kwargs):
    raise OSError('Network access is disabled during offline verification')

socket.socket.connect = offline_network
socket.socket.connect_ex = offline_network
socket.create_connection = offline_network
socket.getaddrinfo = offline_network

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


def run_suite(suite: str) -> bool:
    path = (ROOT / suite).resolve()
    if not path.is_relative_to(ROOT) or not path.exists():
        print(f'FAIL: missing or out-of-tree required suite: {suite}', flush=True)
        return False
    env = dict(os.environ)
    for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH'):
        env.pop(key, None)
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    env['PYTHONPATH'] = str(ROOT / 'src')
    # Native ONNX initialization can precede Python socket mocks or API opt-outs.
    # Apply the documented opt-out before starting any test interpreter.
    env.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_TELEMETRY='1',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    env['HF_HUB_OFFLINE'] = '1'
    env['HF_HUB_DISABLE_TELEMETRY'] = '1'
    env['DO_NOT_TRACK'] = '1'
    try:
        with tempfile.TemporaryDirectory(prefix='carr-ci-') as tmp:
            report = Path(tmp) / 'junit.xml'
            returncode = _run_test_process([
                sys.executable, '-c', PYTEST_CHILD, '-o', 'addopts=',
                '-o', 'xfail_strict=true', '--junitxml', str(report),
                '--basetemp', str(Path(tmp) / 'pytest'),
                '--', str(path),
            ], env=env)
            cases = list(ET.parse(report).iter('testcase'))
            failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
            skipped = sum(c.find('skipped') is not None for c in cases)
            passed = len(cases) - failed - skipped
            ok = returncode == 0 and passed > 0 and failed == skipped == 0
            print(f'{"PASS" if ok else "FAIL"}: {suite}: junit_passed={passed}, failed={failed}, '
                  f'skipped={skipped}, pytest_exit={returncode}', flush=True)
            return ok
    except (OSError, subprocess.TimeoutExpired, ET.ParseError) as exc:
        # Cleanup failures also remain failed required suites, rather than
        # escaping the aggregator and hiding other independently run verdicts.
        print(f'FAIL: required suite did not produce a valid report: {type(exc).__name__}', flush=True)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, choices=range(1, 5), default=1,
                        help='Run up to four isolated required suites concurrently; default: one')
    parser.add_argument('suites', nargs='+', help='Explicit required test paths relative to carr-library')
    args = parser.parse_args()
    if args.jobs == 1:
        results = [run_suite(suite) for suite in args.suites]
    else:
        # Every suite retains its own interpreter, offline policy, deadline and
        # JUnit verdict. Consume every future: one failing suite cannot hide
        # another missing, skipped or unfinished required suite.
        with ThreadPoolExecutor(max_workers=args.jobs, thread_name_prefix='required-suite') as executor:
            results = list(executor.map(run_suite, args.suites))
    print(f'Required suites complete: {len(results)}; passed={sum(results)}; '
          f'failed={len(results) - sum(results)}', flush=True)
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
