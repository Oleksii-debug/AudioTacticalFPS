"""Run explicitly required pytest suites; missing or skipped tests are not green.

Use only on a trusted synthetic test checkout, never on imported documents.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]

# JUnit alone cannot distinguish a non-strict XPASS from a genuine pass.
# Use pytest's own report classification in the isolated subprocess.
PYTEST_CHILD = """
import sys
import socket
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'scripts'))
from ci_watchdog import CaseWatchdog, without_scheduled_faulthandler

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

    def __init__(self):
        self.watchdog = CaseWatchdog()
        self.case_token = None

    def pytest_sessionstart(self, session):
        self.reporter = session.config.pluginmanager.get_plugin('terminalreporter')

    def pytest_runtest_logstart(self, nodeid, location):
        # Windows can reject huge parameter IDs in environment variables;
        # diagnostics also need a bounded label when a native call stalls.
        self.reporter.write_line('CASE_START ' + nodeid.partition('[')[0][:240])
        self.case_token = self.watchdog.start(nodeid)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.watchdog.finish(self.case_token)
        self.case_token = None

    def pytest_sessionfinish(self, session, exitstatus):
        self.watchdog.close()

    def pytest_runtest_logreport(self, report):
        if hasattr(report, 'wasxfail'):
            self.seen = True

policy = RejectExpectedFailures()
try:
    status = pytest.main(without_scheduled_faulthandler(sys.argv[1:]), plugins=[policy])
finally:
    policy.watchdog.close()
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
    with tempfile.TemporaryDirectory(prefix='carr-ci-') as tmp:
        report = Path(tmp) / 'junit.xml'
        try:
            proc = subprocess.run([
                sys.executable, '-c', PYTEST_CHILD, '-o', 'addopts=',
                '-o', 'xfail_strict=true',
                '--durations=15', '--junitxml', str(report),
                '--basetemp', str(Path(tmp) / 'pytest'),
                '--', str(path),
            ], cwd=ROOT, env=env, timeout=600, check=False)
            cases = list(ET.parse(report).iter('testcase'))
        except (OSError, subprocess.TimeoutExpired, ET.ParseError) as exc:
            print(f'FAIL: {suite}: required suite did not produce a valid report: '
                  f'{type(exc).__name__}', flush=True)
            return False
        failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
        skipped = sum(c.find('skipped') is not None for c in cases)
        passed = len(cases) - failed - skipped
        ok = proc.returncode == 0 and passed > 0 and failed == skipped == 0
        print(f'{"PASS" if ok else "FAIL"}: {suite}: junit_passed={passed}, failed={failed}, '
              f'skipped={skipped}, pytest_exit={proc.returncode}', flush=True)
        return ok


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
