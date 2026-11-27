"""Run explicitly required pytest suites; missing or skipped tests are not green.

Use only on a trusted synthetic test checkout, never on imported documents.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import traceback
from time import sleep
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITE_TIMEOUT_SECONDS = 600
# One measured, reviewed whole-suite exception; all other suites retain 600 s.
# This changes only the test-process envelope, never ASR/worker deadlines.
REVIEWED_SUITE_TIMEOUT_SECONDS = {
    'tests/transcription/test_admission_boundaries.py': 900,
}


def _reviewed_suite_timeout(suite: str, seconds: int) -> int:
    if (type(seconds) is not int
            or REVIEWED_SUITE_TIMEOUT_SECONDS.get(suite) != seconds):
        raise ValueError('No reviewed timeout allowance matches this exact suite and value.')
    path = (ROOT / suite).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError('A reviewed timeout requires an existing in-tree whole-suite file.')
    return seconds


def _suite_timeout_overrides(values, suites):
    result = {}
    for value in values:
        if value.count('=') != 1:
            raise ValueError('Use --suite-timeout SUITE=SECONDS.')
        suite, raw = value.split('=')
        if suite in result:
            raise ValueError('A suite timeout may be specified only once.')
        if suite not in suites:
            raise ValueError('A suite timeout must name an explicitly selected whole suite.')
        expected = REVIEWED_SUITE_TIMEOUT_SECONDS.get(suite)
        # Compare canonical text before int conversion, including huge or
        # non-decimal input. No caller can request an arbitrary larger budget.
        if expected is None or raw != str(expected):
            raise ValueError('Only the exact reviewed suite timeout value is permitted.')
        result[suite] = _reviewed_suite_timeout(suite, expected)
    return result


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


def _run_test_process(command, *, env, timeout_seconds=None) -> int:
    isolation = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == 'nt' else {'start_new_session': True})
    process = subprocess.Popen(command, cwd=ROOT, env=env, **isolation)
    try:
        return process.wait(timeout=(SUITE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds))
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

def _needs_gil_stack_dump(platform=sys.platform, implementation=sys.implementation.name,
                          version=sys.version_info[:2]):
    return platform == 'win32' and implementation == 'cpython' and tuple(version) == (3, 11)

class GilTimedStackDump:
    # CPython 3.11's native timed dumper walks changing frames without the GIL
    # (python/cpython#140815; pytest-dev/pytest#7022). A Python thread calls the
    # synchronous, bounded dumper while it owns the GIL instead. Fatal exception
    # handling remains pytest's own; the parent's 600/900 s deadline is unchanged.
    timeout = 0.0
    exit_on_timeout = False

    @pytest.hookimpl(tryfirst=True)
    def pytest_configure(self, config):
        timeout = float(config.getini('faulthandler_timeout') or 0.0)
        if timeout <= 0:
            return
        import math
        if not math.isfinite(timeout):
            raise ValueError('A timed stack diagnostic needs a finite interval.')
        self.timeout = timeout
        self.exit_on_timeout = config.getini('faulthandler_exit_on_timeout')
        # Pinned pytest 9.0.2 consults this same ini cache in its protocol hook.
        # Only its unsafe timed traversal is suppressed; enable()/fatal handling
        # and all other configured pytest policies remain untouched.
        config._inicache['faulthandler_timeout'] = 0.0

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        if self.timeout <= 0:
            return (yield)
        import faulthandler
        import os
        import threading
        stopped = threading.Event()
        interval = self.timeout
        errors = []
        fd = os.dup(sys.__stderr__.fileno())

        def diagnostic():
            try:
                if not stopped.wait(interval):
                    os.write(fd, b'Timed stack diagnostic (GIL-owned Python thread):\\n')
                    # This synchronous CPython entry point does not release the
                    # GIL while traversing frames; its usual 100-thread/frame
                    # and 500-character field bounds still apply.
                    faulthandler.dump_traceback(file=fd, all_threads=True)
                    if self.exit_on_timeout:
                        os._exit(1)
            except BaseException as exc:
                errors.append(exc)
            finally:
                os.close(fd)

        worker = threading.Thread(target=diagnostic, name='carr-ci-stack-diagnostic',
                                  daemon=True)
        try:
            worker.start()
        except BaseException:
            os.close(fd)
            raise
        try:
            return (yield)
        finally:
            stopped.set()
            worker.join(timeout=5)
            if worker.is_alive():
                raise RuntimeError('The timed stack diagnostic did not settle.')
            if errors:
                raise RuntimeError('The timed stack diagnostic failed.') from errors[0]

class RejectExpectedFailures:
    seen = False

    def pytest_runtest_logreport(self, report):
        if hasattr(report, 'wasxfail'):
            self.seen = True

policy = RejectExpectedFailures()
plugins = [policy]
if _needs_gil_stack_dump():
    plugins.append(GilTimedStackDump())
status = pytest.main(sys.argv[1:], plugins=plugins)
if policy.seen:
    print('FAIL: pytest reported XFAIL or XPASS', flush=True)
    if status == pytest.ExitCode.OK:
        status = pytest.ExitCode.TESTS_FAILED
raise SystemExit(status)
"""


def _cleanup_report_directory(directory) -> None:
    # Windows may still be releasing a terminated child's file handles.
    # Retry only permission failures, briefly; persistent residue is a failure.
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
            # Do not replace a suite timeout or other primary failure with a
            # secondary cleanup exception; record both, with the suite name.
            print(f'FAIL: {suite}: temporary report cleanup failed after '
                  f'{type(original_error).__name__}: {type(exc).__name__}: {exc}; '
                  f'directory={directory.name}', flush=True)
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def run_suite(suite: str, *, report=None, timeout_seconds=None) -> bool:
    emit = report if report is not None else lambda message: print(message, flush=True)
    process_options = {}
    if timeout_seconds is not None:
        try:
            process_options['timeout_seconds'] = _reviewed_suite_timeout(suite, timeout_seconds)
        except (ValueError, OSError) as exc:
            emit(f'FAIL: {suite}: invalid required-suite timeout: {exc}')
            return False
    path = (ROOT / suite).resolve()
    if not path.is_relative_to(ROOT) or not path.exists():
        emit(f'FAIL: missing or out-of-tree required suite: {suite}')
        return False
    env = dict(os.environ)
    for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH'):
        env.pop(key, None)
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    env['PYTHONPATH'] = str(ROOT / 'src')
    # Ordinary test descendants inherit this; the suite interpreter also has -B.
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # Native ONNX initialization can precede Python socket mocks or API opt-outs.
    # Apply the documented opt-out before starting any test interpreter.
    env.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_TELEMETRY='1',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    env['HF_HUB_OFFLINE'] = '1'
    env['HF_HUB_DISABLE_TELEMETRY'] = '1'
    env['DO_NOT_TRACK'] = '1'
    phase = 'temporary directory setup'
    try:
        with _report_directory(suite) as tmp:
            report = Path(tmp) / 'junit.xml'
            phase = 'pytest process'
            returncode = _run_test_process([
                sys.executable, '-B', '-c', PYTEST_CHILD, '-v', '-o', 'addopts=',
                '-o', 'xfail_strict=true', '-o', 'faulthandler_timeout=120',
                '-o', 'faulthandler_exit_on_timeout=false', '--junitxml', str(report),
                '--basetemp', str(Path(tmp) / 'pytest'),
                '--', str(path),
            ], env=env, **process_options)
            phase = 'JUnit report validation'
            cases = list(ET.parse(report).iter('testcase'))
            failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
            skipped = sum(c.find('skipped') is not None for c in cases)
            passed = len(cases) - failed - skipped
            ok = returncode == 0 and passed > 0 and failed == skipped == 0
            phase = 'temporary directory cleanup'
        # A passing child is not a completed suite until its temporary report
        # and test data have also been cleaned up successfully.
        emit(f'{"PASS" if ok else "FAIL"}: {suite}: junit_passed={passed}, failed={failed}, '
             f'skipped={skipped}, pytest_exit={returncode}')
        return ok
    except (OSError, subprocess.TimeoutExpired, ET.ParseError) as exc:
        # Cleanup failures also remain failed required suites, rather than
        # escaping the aggregator and hiding other independently run verdicts.
        emit(f'FAIL: {suite}: required suite did not complete cleanly; '
             f'last phase={phase}; {type(exc).__name__}: {exc}')
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, choices=range(1, 5), default=1,
                        help='Run up to four isolated required suites concurrently; default: one')
    parser.add_argument('--suite-timeout', action='append', default=[], metavar='SUITE=SECONDS',
                        help='Explicit reviewed allowance for a selected whole suite; default: 600 seconds')
    parser.add_argument('suites', nargs='+', help='Explicit required test paths relative to carr-library')
    args = parser.parse_args()
    try:
        suite_timeouts = _suite_timeout_overrides(args.suite_timeout, args.suites)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    def execute(suite):
        messages = []
        options = {'report': messages.append}
        if suite in suite_timeouts:
            options['timeout_seconds'] = suite_timeouts[suite]
        ok = run_suite(suite, **options)
        if len(messages) != 1:
            return False, f'FAIL: {suite}: missing or ambiguous required-suite verdict'
        return ok, messages[0]

    if args.jobs == 1:
        records = [execute(suite) for suite in args.suites]
    else:
        # Every suite retains its own interpreter, offline policy, deadline and
        # JUnit verdict. Consume every future: one failing suite cannot hide
        # another missing, skipped or unfinished required suite.
        with ThreadPoolExecutor(max_workers=args.jobs, thread_name_prefix='required-suite') as executor:
            records = list(executor.map(execute, args.suites))
    # Children inherit the console and can end a progress line without a newline.
    # Emit machine-consumed verdicts only after all child processes and report
    # cleanup have ended. A leading newline isolates even an unterminated tail;
    # every supplied suite still contributes exactly one strict JUnit verdict.
    results = [ok for ok, _ in records]
    summary = [message for _, message in records]
    summary.append(f'Required suites complete: {len(results)}; passed={sum(results)}; '
                   f'failed={len(results) - sum(results)}')
    print('\n' + '\n'.join(summary), flush=True)
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
