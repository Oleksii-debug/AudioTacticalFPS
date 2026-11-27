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
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]

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


def _stop_process_tree(process):
    """Reap timed-out trusted test interpreters before deleting their fixtures.

    Ingest starts separate sessions/process groups. Killing only pytest (as
    subprocess.run(timeout=...) does) can leave its native worker holding the
    Windows writer lock. Stop descendants while the parent still exists.
    """
    if process.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        return

    # The suite has its own session, but native workers may create another.
    # Freeze each discovered generation before scanning its children so the
    # trusted test tree cannot keep growing during bounded teardown.
    known = [process.pid]
    deadline = time.monotonic() + 5
    try:
        process.send_signal(signal.SIGSTOP)
        for _ in range(32):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Test process-tree cleanup exceeded its deadline.')
            listing = subprocess.run(['ps', '-e', '-o', 'pid=,ppid='],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=remaining, check=True)
            if len(listing.stdout) > 1024 * 1024:
                raise OSError('Test process inventory exceeded its limit.')
            parents = set(known)
            children = [int(pid) for row in listing.stdout.splitlines()
                        for pid, parent in [row.split()]
                        if int(parent) in parents and int(pid) not in parents]
            if not children:
                break
            for child in children:
                try:
                    os.kill(child, signal.SIGSTOP)
                except ProcessLookupError:
                    continue
                known.append(child)
        else:
            raise OSError('Test process tree exceeded its depth limit.')
    finally:
        for pid in reversed(known):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def run_suite(suite: str, *, timeout=600) -> bool:
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
    print(f'RUN: {suite}', flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='carr-ci-') as tmp:
            report = Path(tmp) / 'junit.xml'
            proc = None
            try:
                options = ({'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)}
                           if os.name == 'nt' else {'start_new_session': True})
                proc = subprocess.Popen([
                    sys.executable, '-c', PYTEST_CHILD, '-o', 'addopts=',
                    '-o', 'xfail_strict=true', '--junitxml', str(report),
                    '--basetemp', str(Path(tmp) / 'pytest'),
                    '--', str(path),
                ], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, **options)
                proc.wait(timeout=timeout)
                cases = list(ET.parse(report).iter('testcase'))
            except (OSError, subprocess.SubprocessError, ET.ParseError) as exc:
                print(f'FAIL: {suite}: required suite did not produce a valid report: '
                      f'{type(exc).__name__}', flush=True)
                return False
            finally:
                if proc is not None and proc.poll() is None:
                    _stop_process_tree(proc)
            failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
            skipped = sum(c.find('skipped') is not None for c in cases)
            passed = len(cases) - failed - skipped
            ok = proc.returncode == 0 and passed > 0 and failed == skipped == 0
            print(f'{"PASS" if ok else "FAIL"}: {suite}: junit_passed={passed}, failed={failed}, '
                  f'skipped={skipped}, pytest_exit={proc.returncode}', flush=True)
            return ok
    except (OSError, subprocess.SubprocessError) as exc:
        # Cleanup failure is a failed named suite, never an exception which
        # prevents independent parallel results from reaching their summary.
        print(f'FAIL: {suite}: required suite cleanup failed: {type(exc).__name__}', flush=True)
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
