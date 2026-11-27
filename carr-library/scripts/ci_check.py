"""Run explicitly required pytest suites; missing or skipped tests are not green.

Use only on a trusted synthetic test checkout, never on imported documents.
"""
import argparse
from contextlib import contextmanager
import ctypes
from functools import lru_cache, partial
import importlib.util
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from time import monotonic, sleep
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_OWNER = ROOT / 'src' / 'carr_library' / 'audio' / 'windows_process.py'
SUITE_TIMEOUT_SECONDS = 600

# Exact measured native suites retain every case and the existing per-case
# watchdog. Word r5 reached 10 cases at 600 s; the 7959 duration run made
# continuous progress through 34/51 cases at 600 s, including all 10 codecs.
# All other required suites keep the existing ten-minute process deadline.
SUITE_SECONDS = {'tests/citations/test_word_com.py': 1800,
                 'tests/audio/test_book_duration.py': 1200}


# JUnit alone cannot distinguish a non-strict XPASS from a genuine pass.
# Use pytest's own report classification in the isolated subprocess.
NETWORK_POLICY = """
import sys
import socket

# Existing installed replays consume PYTEST_CHILD directly, with pytest options
# first. Such callers stay offline; only an explicit recognized prefix changes
# this policy. run_suite admits numeric loopback only for its exact fixtures below.
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



class _SuiteCleanupPending(RuntimeError):
    """The actual process owner and its private reports must remain retained."""
    def __init__(self, process, error):
        super().__init__('Required-suite process cleanup is unconfirmed: '
                         + type(error).__name__ + ': ' + str(error)[:1000])
        self.process = process


_PENDING_SUITES = []


@lru_cache(maxsize=1)
def _windows_launcher():
    # Reuse the existing standalone launcher at this exact trusted checkout.
    # No PATH lookup, taskkill ancestry scan or second native process family.
    spec = importlib.util.spec_from_file_location('_carr_ci_windows_process', WINDOWS_OWNER)
    if spec is None or spec.loader is None:
        raise OSError('The current owned Windows process helper is unavailable.')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.WindowsJobProcess


def _start_test_process(command, *, env):
    options = dict(cwd=ROOT, env=env, stdin=subprocess.DEVNULL, shell=False)
    if os.name == 'nt':
        return _windows_launcher()(command, **options)
    return subprocess.Popen(command, start_new_session=True, **options)


def _posix_group_quiescent(group):
    """Confirm no live member remains; Linux zombies cannot retain writers.

    A container's PID 1 may leave an orphan zombie unreaped indefinitely. It
    owns no descriptors and cannot execute. Only an actual bounded /proc scan
    of this exact killed group can distinguish that from a live descendant.
    Other POSIX platforms keep the stronger group-disappearance check.
    """
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    if not sys.platform.startswith('linux'):
        return False
    seen = False
    try:
        with os.scandir('/proc') as entries:
            for count, entry in enumerate(entries, 1):
                if count > 100000:
                    return False
                if not entry.name.isdecimal():
                    continue
                try:
                    if os.getpgid(int(entry.name)) != group:
                        continue
                    with open('/proc/' + entry.name + '/stat', 'rb') as stream:
                        raw = stream.read(4097)
                    if len(raw) > 4096:
                        return False
                    fields = raw.rsplit(b')', 1)[1].split()
                    if len(fields) < 3 or int(fields[2]) != group:
                        return False
                    seen = True
                    if fields[0] not in (b'Z', b'X'):
                        return False
                except ProcessLookupError:
                    continue
                except FileNotFoundError:
                    continue
    except (OSError, IndexError, ValueError):
        return False
    if seen:
        return True
    # No visible member is not proof under hidepid or a changing process table.
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    return False


def _settle_test_process(process):
    """Keep the current Windows job until its entire owned family is empty."""
    if os.name != 'nt':
        # The existing process-family contract uses a private POSIX session.
        # Its descendants can outlive a successfully exited pytest leader.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
        deadline = monotonic() + 2
        while not _posix_group_quiescent(process.pid):
            if monotonic() >= deadline:
                raise TimeoutError('The required-suite process group did not become quiescent.')
            sleep(.02)
        return
    job = getattr(process, '_carr_job', None)
    if job is None or job.handle is None:
        raise OSError('The required suite has no retained Windows job owner.')
    deadline = monotonic() + 10
    handle = job.handle
    if not job.api.kernel.TerminateJobObject(handle, 1):
        raise job.api.error()
    process.wait(timeout=max(0, deadline - monotonic()))
    retained = getattr(process, '_handle', None)
    if retained is not None:
        retained.Close()
    # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, as used by the current capture
    # settlement boundary. The current launcher remains the sole job owner.
    class Accounting(ctypes.Structure):
        _fields_ = [('user_time', ctypes.c_int64), ('kernel_time', ctypes.c_int64),
                    ('period_user', ctypes.c_int64), ('period_kernel', ctypes.c_int64),
                    ('page_faults', ctypes.c_uint32), ('total_processes', ctypes.c_uint32),
                    ('active_processes', ctypes.c_uint32), ('terminated_processes', ctypes.c_uint32)]
    query = job.api.kernel.QueryInformationJobObject
    query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                      ctypes.c_uint32, ctypes.c_void_p]
    query.restype = ctypes.c_int
    while True:
        accounting = Accounting()
        if not query(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise job.api.error()
        if accounting.active_processes == 0:
            break
        if monotonic() >= deadline:
            raise TimeoutError('The required-suite Windows job did not become empty.')
        sleep(min(.02, max(0, deadline - monotonic())))
    # Empty-job confirmation precedes the existing owner's final close.
    job.close()


def _run_test_process(command, *, env, timeout):
    process = _start_test_process(command, env=env)
    failure = None
    try:
        return process.wait(timeout=timeout)
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            _settle_test_process(process)
        except BaseException as error:
            if failure is not None:
                print('FAIL: required-suite process cleanup after '
                      + type(failure).__name__ + ': ' + type(error).__name__
                      + ': ' + str(error)[:1000], flush=True)
            raise _SuiteCleanupPending(process, error) from failure or error


def _cleanup_report_directory(directory):
    # Retry only transient Windows sharing/permission failures, for 0.2 s.
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
    original = None
    try:
        yield directory.name
    except BaseException as error:
        original = error
        raise
    finally:
        if isinstance(original, _SuiteCleanupPending):
            # Do not delete paths while their native owner may still use them.
            # Retain both objects and disable automatic destructive cleanup;
            # this failed run never emits PASS. OS job ownership also survives
            # until interpreter exit if explicit settlement could not finish.
            directory._finalizer.detach()
            _PENDING_SUITES.append((original.process, directory))
        else:
            try:
                _cleanup_report_directory(directory)
            except OSError as error:
                if original is None:
                    raise
                print(f'FAIL: {suite}: temporary report cleanup failed after '
                      f'{type(original).__name__}: {type(error).__name__}: {error}',
                      flush=True)


def run_suite(suite: str, *, allow_loopback=False) -> bool:
    path = (ROOT / suite).resolve()
    if not path.is_relative_to(ROOT) or not path.exists():
        print(f'FAIL: missing or out-of-tree required suite: {suite}', flush=True)
        return False
    fixtures = ('tests/loopback/test_local_planning.py', 'tests/loopback/test_translation_local_adapter.py')
    if allow_loopback and (suite not in fixtures or path != ROOT / suite):
        print('FAIL: numeric loopback is limited to the explicit local planning/translation HTTP fixtures.', flush=True)
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
    try:
        with _report_directory(suite) as tmp:
            report = Path(tmp) / 'junit.xml'
            returncode = _run_test_process([
                sys.executable, '-c', PYTEST_CHILD,
                'numeric-loopback' if allow_loopback else 'offline', '-o', 'addopts=',
                '-o', 'xfail_strict=true',
                '--durations=15', '--junitxml', str(report),
                '--basetemp', str(Path(tmp) / 'pytest'),
                '--', str(path),
            ], env=env, timeout=SUITE_SECONDS.get(suite, SUITE_TIMEOUT_SECONDS))
            cases = list(ET.parse(report).iter('testcase'))
            failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
            skipped = sum(c.find('skipped') is not None for c in cases)
            passed = len(cases) - failed - skipped
            ok = returncode == 0 and passed > 0 and failed == skipped == 0
        # Both the owned process family and private report files are settled.
        print(f'{"PASS" if ok else "FAIL"}: {suite}: junit_passed={passed}, failed={failed}, '
              f'skipped={skipped}, pytest_exit={returncode}', flush=True)
        return ok
    except (OSError, subprocess.TimeoutExpired, ET.ParseError, _SuiteCleanupPending) as error:
        print(f'FAIL: {suite}: required suite did not complete cleanly: '
              f'{type(error).__name__}: {str(error)[:2000]}', flush=True)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, choices=range(1, 5), default=1,
                        help='Run up to four isolated required suites concurrently; default: one')
    parser.add_argument('--allow-loopback', action='store_true',
                        help='Only explicit synthetic local planning/translation fixtures may use numeric loopback TCP')
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
