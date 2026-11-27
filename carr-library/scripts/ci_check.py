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
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]

# This controlled Word suite deliberately keeps real CSL/DOCX rendering in
# every authority/publication case. The measured Windows r5 run exhausted
# 600 seconds after 10 cases; retain all 36 cases and their per-render bounds.
# Every other required suite keeps the existing ten-minute process deadline.
SUITE_SECONDS = {'tests/citations/test_word_com.py': 1800}


# JUnit alone cannot distinguish a non-strict XPASS from a genuine pass.
# Use pytest's own report classification in the isolated subprocess.
NETWORK_POLICY = """
import sys
import socket

# Existing installed replays consume PYTEST_CHILD directly, with pytest options
# first. Such callers stay offline; only an explicit recognized prefix changes
# this policy. run_suite admits numeric loopback for one exact fixture below.
network_mode = 'offline'
if len(sys.argv) > 1 and sys.argv[1] in ('offline', 'numeric-loopback'):
    network_mode = sys.argv.pop(1)

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

PYTEST_CHILD = NETWORK_POLICY + """
import importlib.util
import sys
import socket

# The child may run under -I from an installed-test staging directory.
# Load only this runner's exact helper; never add a checkout to import paths.
_watchdog_spec = importlib.util.spec_from_file_location(
    '_carr_ci_watchdog', __CARR_CI_WATCHDOG_PATH__)
_watchdog = importlib.util.module_from_spec(_watchdog_spec)
sys.modules[_watchdog_spec.name] = _watchdog
_watchdog_spec.loader.exec_module(_watchdog)
CaseWatchdog = _watchdog.CaseWatchdog
without_scheduled_faulthandler = _watchdog.without_scheduled_faulthandler

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
""".replace("__CARR_CI_WATCHDOG_PATH__", repr(str(Path(__file__).resolve().with_name("ci_watchdog.py"))))


def run_suite(suite: str, *, allow_loopback=False) -> bool:
    path = (ROOT / suite).resolve()
    if not path.is_relative_to(ROOT) or not path.exists():
        print(f'FAIL: missing or out-of-tree required suite: {suite}', flush=True)
        return False
    fixture = 'tests/loopback/test_local_planning.py'
    if allow_loopback and (suite != fixture or path != ROOT / fixture):
        print('FAIL: numeric loopback is limited to the explicit local-planning HTTP fixture.', flush=True)
        return False
    env = dict(os.environ)
    for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH', 'CARR_CI_ALLOW_LOOPBACK'):
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
                sys.executable, '-c', PYTEST_CHILD,
                'numeric-loopback' if allow_loopback else 'offline', '-o', 'addopts=',
                '-o', 'xfail_strict=true',
                '--durations=15', '--junitxml', str(report),
                '--basetemp', str(Path(tmp) / 'pytest'),
                '--', str(path),
            ], cwd=ROOT, env=env, timeout=SUITE_SECONDS.get(suite, 600), check=False)
            cases = list(ET.parse(report).iter('testcase'))
        except (OSError, subprocess.TimeoutExpired, ET.ParseError) as exc:
            print(f'FAIL: {suite}: required suite did not produce a valid report: '
                  f'{type(exc).__name__}: {exc}', flush=True)
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
    parser.add_argument('--allow-loopback', action='store_true',
                        help='Only the explicit synthetic local-planning fixture may use numeric loopback TCP')
    parser.add_argument('suites', nargs='+', help='Explicit required test paths relative to carr-library')
    args = parser.parse_args()
    runner = partial(run_suite, allow_loopback=args.allow_loopback)
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
