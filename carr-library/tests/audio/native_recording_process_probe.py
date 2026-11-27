"""Actual Windows capture-process ownership, independent of microphone devices.

Run explicitly with the installed interpreter: python -I -B this_file --report FILE.
Ordinary portable suites do not collect this native probe.
"""
import argparse
import ctypes
import faulthandler
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import traceback


def run(report):
    if os.name != 'nt':
        raise RuntimeError('Actual Windows process handles are required.')
    from ctypes import wintypes
    from carr_library.audio import process, windows_process
    from carr_library.audio.models import AudioBudget, AudioCancelled
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessId.argtypes = (wintypes.HANDLE,)
    kernel.GetProcessId.restype = wintypes.DWORD
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report['checks'].append(name)
        print('PASS: ' + name, flush=True)

    def retain(pid):
        handle = kernel.OpenProcess(0x00100000 | 0x0001, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def release(handle):
        if handle:
            if kernel.WaitForSingleObject(handle, 0) == 0x102:
                if not kernel.TerminateProcess(handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                if kernel.WaitForSingleObject(handle, 5000) != 0:
                    raise TimeoutError('Native fixture child did not stop.')
            kernel.CloseHandle(handle)

    with TemporaryDirectory(prefix='carr-native-recording-process-') as temporary:
        root = Path(temporary)
        receipt = root / 'child.json'
        child_code = (
            "import json,subprocess,sys;from pathlib import Path;"
            "child=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(30)'],"
            "stdout=sys.stdout,stderr=sys.stderr,creationflags=subprocess.CREATE_NO_WINDOW);"
            "p=Path(sys.argv[1]);q=p.with_suffix('.tmp');"
            "q.write_text(json.dumps({'pid':child.pid}),encoding='utf-8');q.replace(p)"
        )
        started, held = [], []
        original_start = process._start_process
        def observed_start(*args, **kwargs):
            child = original_start(*args, **kwargs)
            started.append(child)
            return child
        def cancelled():
            if not started or not receipt.exists():
                return False
            if not held:
                held.append(retain(json.loads(receipt.read_text(encoding='utf-8'))['pid']))
            if started[0].poll() is None:
                return False
            check('descendant alive after its original leader exited',
                  kernel.WaitForSingleObject(held[0], 0) == 0x102)
            return True
        process._start_process = observed_start
        try:
            try:
                process.run_process([sys.executable, '-I', '-B', '-c', child_code, str(receipt)],
                    owned_tree=True, budget=AudioBudget(timeout=10), cancelled=cancelled)
            except AudioCancelled:
                pass
            else:
                raise AssertionError('The explicitly cancelled capture returned success.')
            check('owned cancellation waits for the actual descendant exit',
                  bool(held) and kernel.WaitForSingleObject(held[0], 0) == 0)
            check('owned job and native pipes are closed after cancellation',
                  started[0]._carr_job.handle is None
                  and started[0].stdout.closed and started[0].stderr.closed)
        finally:
            process._start_process = original_start
            for handle in held:
                release(handle)

        # A native child which never entered the job still belongs to this
        # launch attempt. It may not run its marker before assignment succeeds.
        original_assign = windows_process._Job.assign
        unassigned = []
        marker = root / 'must-not-run'
        def fail_assignment(self, handle):
            pid = kernel.GetProcessId(handle)
            unassigned.append(retain(pid))
            raise OSError('Explicit native assignment-failure fixture')
        windows_process._Job.assign = fail_assignment
        try:
            try:
                process.run_process([sys.executable, '-I', '-B', '-c',
                    'import sys,time;from pathlib import Path;Path(sys.argv[1]).write_text("bad");time.sleep(30)',
                    str(marker)], owned_tree=True, budget=AudioBudget(timeout=10))
            except OSError as exc:
                check('native assignment failure remains explicit', 'assignment-failure' in str(exc))
            else:
                raise AssertionError('The failed assignment returned a worker result.')
            check('unassigned suspended child is killed and waited before return',
                  len(unassigned) == 1 and kernel.WaitForSingleObject(unassigned[0], 0) == 0
                  and not marker.exists())
        finally:
            windows_process._Job.assign = original_assign
            for handle in unassigned:
                release(handle)

        assigned, resume, cancelled = threading.Event(), threading.Event(), threading.Event()
        outcomes, unrelated = [], None
        def paused_assignment(self, handle):
            original_assign(self, handle)
            assigned.set()
            if not resume.wait(10):
                raise TimeoutError('Native fixture did not release the suspended launch.')
        windows_process._Job.assign = paused_assignment
        def owned_worker():
            try:
                process.run_process([sys.executable, '-I', '-B', '-c', 'import time;time.sleep(30)'],
                    owned_tree=True, cancelled=cancelled.is_set, budget=AudioBudget(timeout=15))
            except BaseException as exc:
                outcomes.append(exc)
        worker = threading.Thread(target=owned_worker)
        worker.start()
        try:
            check('concurrent owned launch reaches suspended assignment', assigned.wait(10))
            unrelated = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time;time.sleep(30)'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            cancelled.set()
            resume.set()
            worker.join(10)
            check('concurrent owned cancellation completes',
                  not worker.is_alive() and len(outcomes) == 1 and isinstance(outcomes[0], AudioCancelled))
            check('unrelated concurrent native process remains outside capture ownership',
                  unrelated.poll() is None)
        finally:
            windows_process._Job.assign = original_assign
            resume.set()
            cancelled.set()
            worker.join(10)
            if unrelated is not None:
                unrelated.kill()
                unrelated.wait(timeout=5)
    report['status'] = 'PASS'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = {'status': 'FAILED', 'checks': [], 'scope': 'actual Windows owned process lifecycle',
              'physical_microphone': 'NOT_RUN', 'human_listening': 'NOT_RUN'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    faulthandler.dump_traceback_later(60, exit=True)
    try:
        run(report)
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2), flush=True)
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    main()
