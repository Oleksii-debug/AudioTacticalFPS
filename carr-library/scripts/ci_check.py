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
from time import monotonic, sleep
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITE_TIMEOUT_SECONDS = 600



class _ProcessCleanupError(OSError):
    """The owned process tree could not be proved stopped; keep its files."""


class _WindowsJob:
    """Own the suite before it runs, including nested interpreter jobs.

    Windows 8 / Server 2012 and later nest jobs. No breakaway flag is enabled
    here, so a venv redirector's inner job cannot release descendants from our
    job. TerminateJobObject includes nested jobs; its return is not the wait
    barrier. We additionally require the job's active-process count to be zero.
    See Microsoft Learn: procthread/nested-jobs and jobapi2/TerminateJobObject.
    """

    def __init__(self):
        import ctypes
        self.ctypes = ctypes
        # Explicit Windows integer widths also make the ABI independently
        # testable on the portable CI lane. HANDLE/SIZE_T remain pointer-sized.
        dword, size, handle = ctypes.c_uint32, ctypes.c_size_t, ctypes.c_void_p
        boolean = ctypes.c_int32

        class Limits(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', dword),
                ('MinimumWorkingSetSize', size), ('MaximumWorkingSetSize', size),
                ('ActiveProcessLimit', dword), ('Affinity', size),
                ('PriorityClass', dword), ('SchedulingClass', dword)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Limits),
                ('IoInfo', ctypes.c_uint64 * 6), ('ProcessMemoryLimit', size),
                ('JobMemoryLimit', size), ('PeakProcessMemoryUsed', size),
                ('PeakJobMemoryUsed', size)]

        class Accounting(ctypes.Structure):
            _fields_ = [('TotalUserTime', ctypes.c_int64),
                ('TotalKernelTime', ctypes.c_int64),
                ('ThisPeriodTotalUserTime', ctypes.c_int64),
                ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                ('TotalPageFaultCount', dword), ('TotalProcesses', dword),
                ('ActiveProcesses', dword), ('TotalTerminatedProcesses', dword)]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [('dwSize', dword), ('cntUsage', dword),
                ('th32ThreadID', dword), ('th32OwnerProcessID', dword),
                ('tpBasePri', ctypes.c_int32), ('tpDeltaPri', ctypes.c_int32),
                ('dwFlags', dword)]

        self.Accounting, self.ThreadEntry = Accounting, ThreadEntry
        self.kernel = kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ((handle, ctypes.c_wchar_p), handle),
            'SetInformationJobObject': ((handle, ctypes.c_int32, handle, dword), boolean),
            'AssignProcessToJobObject': ((handle, handle), boolean),
            'TerminateJobObject': ((handle, dword), boolean),
            'QueryInformationJobObject': ((handle, ctypes.c_int32, handle, dword,
                                           ctypes.POINTER(dword)), boolean),
            'CreateToolhelp32Snapshot': ((dword, dword), handle),
            'Thread32First': ((handle, ctypes.POINTER(ThreadEntry)), boolean),
            'Thread32Next': ((handle, ctypes.POINTER(ThreadEntry)), boolean),
            'OpenThread': ((dword, boolean, dword), handle),
            'GetProcessIdOfThread': ((handle,), dword),
            'ResumeThread': ((handle,), dword),
            'CloseHandle': ((handle,), boolean),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes, function.restype = argtypes, restype
        self.handle = kernel.CreateJobObjectW(None, None)  # private, non-inheritable
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE only
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits),
                                                   ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def assign(self, process):
        # Popen retains the exact CreateProcess handle. Do not reopen by PID or
        # allow the process to run between creation and job assignment.
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def resume(self, process):
        # CPython closes CreateProcess's thread handle. With CREATE_SUSPENDED
        # the sole initial thread has not run; recover it through documented
        # Toolhelp/OpenThread APIs, then verify its owner before resuming it.
        c, kernel = self.ctypes, self.kernel
        snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        threads = []
        try:
            entry = self.ThreadEntry()
            entry.dwSize = c.sizeof(entry)
            more = kernel.Thread32First(snapshot, c.byref(entry))
            count = 0
            while more:
                count += 1
                if count > 65536 or entry.dwSize < self.ThreadEntry.th32OwnerProcessID.offset + 4:
                    raise OSError('The initial suite thread snapshot was invalid or exceeded its bound.')
                if entry.th32OwnerProcessID == process.pid:
                    threads.append(entry.th32ThreadID)
                entry.dwSize = c.sizeof(entry)
                more = kernel.Thread32Next(snapshot, c.byref(entry))
            if c.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise c.WinError(c.get_last_error())
        finally:
            kernel.CloseHandle(snapshot)
        if len(threads) != 1:
            raise OSError('The suspended suite must have exactly one initial thread.')
        thread = kernel.OpenThread(0x802, False, threads[0])  # SUSPEND_RESUME | QUERY_LIMITED_INFORMATION
        if not thread:
            raise c.WinError(c.get_last_error())
        try:
            if kernel.GetProcessIdOfThread(thread) != process.pid or process.poll() is not None:
                raise OSError('The suspended suite thread owner changed before resume.')
            previous = kernel.ResumeThread(thread)
            if previous == 0xffffffff:
                raise c.WinError(c.get_last_error())
            if previous != 1:
                raise OSError('The initial suite thread did not have its expected suspend count.')
        finally:
            kernel.CloseHandle(thread)

    def terminate_and_wait(self, process, timeout=10):
        c, kernel = self.ctypes, self.kernel
        deadline = monotonic() + timeout
        if not kernel.TerminateJobObject(self.handle, 1):
            raise c.WinError(c.get_last_error())
        process.wait(timeout=max(0, deadline - monotonic()))
        while True:
            accounting = self.Accounting()
            if not kernel.QueryInformationJobObject(self.handle, 1, c.byref(accounting),
                                                     c.sizeof(accounting), None):
                raise c.WinError(c.get_last_error())
            if accounting.ActiveProcesses == 0:
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise OSError('A required-suite job still has active descendants after termination.')
            sleep(min(.02, remaining))

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            self.kernel.CloseHandle(handle)


def _start_test_process(command, *, env):
    if os.name != 'nt':
        return subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
    job, process = _WindowsJob(), None
    try:
        # No CREATE_BREAKAWAY_FROM_JOB: retain any runner job and nest our job.
        process = subprocess.Popen(command, cwd=ROOT, env=env, creationflags=0x204)
        job.assign(process)
        process._carr_job = job
        job.resume(process)
        return process
    except BaseException as start_error:
        try:
            if process is not None:
                # This also handles failed assignment: that child is still
                # suspended and has never been allowed to create descendants.
                try:
                    process.kill()
                finally:
                    job.terminate_and_wait(process)
        except BaseException as cleanup_error:
            raise _ProcessCleanupError(
                f'{type(start_error).__name__}: {start_error}; '
                'required-suite startup cleanup failed.') from cleanup_error
        finally:
            job.close()
        raise


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """Stop and reap the owned suite tree before removing its temporary files."""
    if os.name == 'nt':
        job = getattr(process, '_carr_job', None)
        if job is None:
            raise _ProcessCleanupError('Required-suite process has no Windows job owner.')
        try:
            job.terminate_and_wait(process)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _ProcessCleanupError('Required-suite process-tree cleanup failed.') from exc
        return
    cleanup_error = None
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        cleanup_error = exc
    if cleanup_error is not None and process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if cleanup_error is not None:
        raise _ProcessCleanupError('Required-suite process-tree cleanup failed.') from cleanup_error


def _run_test_process(command, *, env) -> int:
    process = _start_test_process(command, env=env)
    try:
        try:
            returncode = process.wait(timeout=SUITE_TIMEOUT_SECONDS)
        except BaseException as failure:
            try:
                _terminate_process_tree(process)
            except _ProcessCleanupError as cleanup_error:
                raise _ProcessCleanupError(
                    f'{type(failure).__name__}: {failure}; {cleanup_error}') from cleanup_error
            raise
        else:
            # Successful pytest exit must not leave a background descendant
            # holding its basetemp either. The verdict still uses pytest's code.
            if os.name == 'nt':
                _terminate_process_tree(process)
            return returncode
    finally:
        job = getattr(process, '_carr_job', None)
        if job is not None:
            job.close()

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
        directory = tempfile.TemporaryDirectory(prefix='carr-ci-')
        retain = False
        try:
            tmp = directory.name
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
        except _ProcessCleanupError as exc:
            # A failed ownership barrier must never start deleting a directory
            # that might still be in use. Keep it for runner diagnostics; the
            # suite remains failed and the aggregator still runs other suites.
            retain = True
            directory._finalizer.detach()
            raise _ProcessCleanupError(
                f'{exc} Temporary suite files retained: {directory.name}') from exc
        finally:
            if not retain:
                directory.cleanup()
    except (OSError, subprocess.TimeoutExpired, ET.ParseError) as exc:
        # Cleanup failures also remain failed required suites, rather than
        # escaping the aggregator and hiding other independently run verdicts.
        print(f'FAIL: required suite did not produce a valid report: {suite}: '
              f'{type(exc).__name__}: {exc}', flush=True)
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
