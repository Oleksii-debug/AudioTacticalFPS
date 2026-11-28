from pathlib import Path
import contextlib
import ctypes
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'ci_check.py'


class CIEntrypointTests(unittest.TestCase):
    def run_fixture(self, source, *suites, extra_env=None):
        with tempfile.TemporaryDirectory(prefix='Research Ž ') as temp:
            root = Path(temp) / 'Дослідження'
            (root / 'scripts').mkdir(parents=True)
            shutil.copyfile(SCRIPT, root / 'scripts' / SCRIPT.name)
            (root / 'test_sample.py').write_text(source, encoding='utf-8')
            env = dict(os.environ)
            env.update(extra_env or {})
            return subprocess.run([sys.executable, str(root / 'scripts' / SCRIPT.name), *suites],
                                  cwd=root, env=env, capture_output=True, text=True, timeout=60)

    def test_pass(self):
        result = self.run_fixture('def test_ok(): assert True\n', 'test_sample.py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('passed=1, failed=0, skipped=0', result.stdout)

    def test_offline_gate_blocks_connections_and_name_resolution(self):
        source = '''import socket
import pytest
def test_offline():
    for action in (lambda: socket.create_connection(('example.invalid', 443)),
                   lambda: socket.getaddrinfo('example.invalid', 443)):
        with pytest.raises(OSError, match='disabled during offline verification'):
            action()
'''
        result = self.run_fixture(source, 'test_sample.py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failure_not_green(self):
        result = self.run_fixture('def test_bad(): assert False\n', 'test_sample.py')
        self.assertEqual(result.returncode, 1)
        self.assertIn('failed=1', result.stdout)

    def test_empty_not_green(self):
        result = self.run_fixture('# no tests\n', 'test_sample.py')
        self.assertEqual(result.returncode, 1)
        self.assertIn('pytest_exit=5', result.stdout)

    def test_skipped_not_green(self):
        result = self.run_fixture('import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n', 'test_sample.py')
        self.assertEqual(result.returncode, 1)
        self.assertIn('skipped=1', result.stdout)

    def test_missing_suite_not_hidden_by_passing_suite(self):
        result = self.run_fixture('def test_ok(): assert True\n', 'test_sample.py', 'missing.py')
        self.assertEqual(result.returncode, 1)
        self.assertIn('missing or out-of-tree', result.stdout)

    def test_inherited_pytest_addopts_cannot_turn_failure_green(self):
        result = self.run_fixture('def test_bad(): assert False\n', 'test_sample.py',
                                  extra_env={'PYTEST_ADDOPTS': '--collect-only'})
        self.assertEqual(result.returncode, 1)
        self.assertIn('failed=1', result.stdout)

    def test_outside_tree_rejected(self):
        result = self.run_fixture('def test_ok(): assert True\n', '..')
        self.assertEqual(result.returncode, 1)
        self.assertIn('missing or out-of-tree', result.stdout)

    def test_explicit_nonstrict_xpass_not_green(self):
        result = self.run_fixture(
            "import pytest\n@pytest.mark.xfail(reason='unfinished', strict=False)\ndef test_xpass(): assert True\n",
            'test_sample.py')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_xfail_not_green(self):
        result = self.run_fixture(
            "import pytest\n@pytest.mark.xfail(reason='unfinished')\ndef test_xfail(): assert False\n",
            'test_sample.py')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_mixed_pass_xpass_not_green(self):
        result = self.run_fixture(
            "import pytest\ndef test_ok(): assert True\n@pytest.mark.xfail(reason='unfinished', strict=False)\ndef test_xpass(): assert True\n",
            'test_sample.py')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_inactive_xfail_marker_allows_pass(self):
        result = self.run_fixture(
            "import pytest\n@pytest.mark.xfail(False, reason='inactive')\ndef test_ok(): assert True\n",
            'test_sample.py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_parallel_workers_are_real_independent_processes(self):
        source = '''from pathlib import Path
import os,time
def test_parallel_barrier():
    marker=Path.cwd()/('ready-'+str(os.getpid()))
    marker.write_text('ready')
    deadline=time.monotonic()+15
    while len(tuple(Path.cwd().glob('ready-*'))) < 2:
        assert time.monotonic()<deadline, 'The second isolated suite never started'
        time.sleep(.02)
'''
        result = self.run_fixture(source, '--jobs', '2', 'test_sample.py', 'test_sample.py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Required suites complete: 2; passed=2; failed=0', result.stdout)

    def test_parallel_missing_suite_cannot_hide_passing_suite(self):
        result = self.run_fixture('def test_ok(): assert True\n',
                                  '--jobs', '2', 'missing.py', 'test_sample.py')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Required suites complete: 2; passed=1; failed=1', result.stdout)

    def test_parallel_skips_remain_failed_required_suites(self):
        result = self.run_fixture('import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n',
                                  '--jobs', '2', 'test_sample.py', 'test_sample.py')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Required suites complete: 2; passed=0; failed=2', result.stdout)

    def test_parallel_failures_remain_failed_required_suites(self):
        result = self.run_fixture('def test_bad(): assert False\n',
                                  '--jobs', '2', 'test_sample.py', 'test_sample.py')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Required suites complete: 2; passed=0; failed=2', result.stdout)

    def test_worker_limit_is_explicit_and_bounded(self):
        for workers in ('0', '5', 'not-a-number'):
            with self.subTest(workers=workers):
                result = self.run_fixture('def test_ok(): assert True\n',
                                          '--jobs', workers, 'test_sample.py')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_timeout_terminates_real_descendant_before_temp_cleanup_and_stays_failed(self):
        self._assert_timeout_reaps_real_descendant(orphan_parent=False)

    def test_timeout_owns_real_descendant_even_after_intermediate_parent_exits(self):
        self._assert_timeout_reaps_real_descendant(orphan_parent=True)

    def _assert_timeout_reaps_real_descendant(self, *, orphan_parent):
        spec = importlib.util.spec_from_file_location('reviewed_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        original_start = runner._start_test_process
        original_temp = tempfile.TemporaryDirectory
        processes, directories, observed = [], [], {}
        kernel = None
        previous_subreaper = None
        if os.name == 'nt':
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.TerminateProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
        elif sys.platform.startswith('linux'):
            # Adopt/reap this synthetic grandchild even in containers whose
            # PID1 does not promptly reap orphans. Restore the previous setting.
            libc = ctypes.CDLL(None, use_errno=True)
            previous_subreaper = ctypes.c_int()
            self.assertEqual(libc.prctl(37, ctypes.byref(previous_subreaper), 0, 0, 0), 0)
        with original_temp(prefix='CARR timeout Ž ') as temporary:
            root = Path(temporary).resolve(strict=True)
            marker = root / 'descendant.pid'
            (root / 'descendant.py').write_text(
                "from pathlib import Path\nimport os,sys,time\n"
                "held = open(sys.argv[1], 'wb')\n"
                "marker = Path(sys.argv[2])\n"
                "pending = marker.with_suffix('.pending')\n"
                "pending.write_text(str(os.getpid()), encoding='ascii')\n"
                "os.replace(pending, marker)\n"
                "while True: time.sleep(1)\n", encoding='utf-8')
            if orphan_parent:
                # The direct parent exits before timeout. A terminal PPID
                # snapshot/taskkill traversal cannot discover this orphan;
                # the job established before suite startup must still own it.
                (root / 'spawner.py').write_text(
                    "import subprocess,sys\n"
                    "subprocess.Popen([sys.executable, *sys.argv[1:]],\n"
                    "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n",
                    encoding='utf-8')
            launch = ('subprocess.run' if orphan_parent else 'subprocess.Popen')
            prefix = "str(Path.cwd()/'spawner.py'), " if orphan_parent else ''
            (root / 'test_timeout.py').write_text(
                "from pathlib import Path\nimport subprocess,sys,time\n"
                "def test_wait(tmp_path):\n"
                f"    {launch}([sys.executable, {prefix}str(Path.cwd()/'descendant.py'),\n"
                "        str(tmp_path/'held-library.lock'), str(Path.cwd()/'descendant.pid')],\n"
                "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "    (Path.cwd()/'launch-complete').write_text('parent returned')\n"
                "    while True: time.sleep(1)\n", encoding='utf-8')
            (root / 'test_ok.py').write_text('def test_ok(): assert True\n', encoding='utf-8')

            def observe_started_descendant(*args, **kwargs):
                # Observe only after the production suspended-launch owner
                # has assigned its job and resumed the initial thread.
                process = original_start(*args, **kwargs)
                processes.append(process)
                if Path(args[0][-1]).name != 'test_timeout.py':
                    return process
                deadline = time.monotonic() + 30
                while not marker.exists() or not (root / 'launch-complete').exists():
                    self.assertIsNone(process.poll(), 'Synthetic pytest exited before starting its child')
                    self.assertLess(time.monotonic(), deadline, 'Synthetic child did not start')
                    time.sleep(.02)
                pid = int(marker.read_text(encoding='ascii'))
                observed['pid'] = pid
                if kernel is not None:
                    handle = kernel.OpenProcess(0x100001, False, pid)  # SYNCHRONIZE | TERMINATE
                    self.assertTrue(handle, ctypes.get_last_error())
                    observed['handle'] = handle
                    self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)  # alive
                else:
                    self.assertEqual(os.getpgid(process.pid), process.pid)
                    self.assertEqual(os.getpgid(pid), process.pid)
                return process

            def tracked_temporary(*args, **kwargs):
                directory = original_temp(*args, dir=root, **kwargs)
                directories.append(Path(directory.name))
                return directory

            real_run_suite = runner.run_suite

            def with_test_deadline(suite):
                if suite == 'test_timeout.py':
                    # Startup is acknowledged by the real descendant first;
                    # only the wait budget is shortened, never termination.
                    with mock.patch.object(runner, 'SUITE_TIMEOUT_SECONDS', .15):
                        return real_run_suite(suite)
                return real_run_suite(suite)

            output = io.StringIO()
            try:
                if previous_subreaper is not None:
                    self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
                with (mock.patch.object(runner, 'ROOT', root),
                      mock.patch.object(runner, '_start_test_process', side_effect=observe_started_descendant),
                      mock.patch.object(runner, 'tempfile', types.SimpleNamespace(TemporaryDirectory=tracked_temporary)),
                      mock.patch.object(runner, 'run_suite', side_effect=with_test_deadline),
                      mock.patch.object(sys, 'argv', ['ci_check.py', 'test_timeout.py', 'test_ok.py']),
                      contextlib.redirect_stdout(output)):
                    result = runner.main()
                self.assertEqual(result, 1, output.getvalue())
                self.assertIn('TimeoutExpired', output.getvalue())
                self.assertIn('Required suites complete: 2; passed=1; failed=1', output.getvalue())
                self.assertEqual(len(directories), 2)
                self.assertTrue(all(not directory.exists() for directory in directories))
                self.assertTrue(all(process.returncode is not None for process in processes))
                if kernel is not None:
                    self.assertEqual(kernel.WaitForSingleObject(observed['handle'], 5000), 0)
                elif previous_subreaper is not None:
                    deadline = time.monotonic() + 5
                    while True:
                        pid, status = os.waitpid(observed['pid'], os.WNOHANG)
                        if pid:
                            observed['reaped'] = True
                            self.assertTrue(os.WIFSIGNALED(status))
                            break
                        self.assertLess(time.monotonic(), deadline, 'Descendant survived suite timeout')
                        time.sleep(.02)
                else:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            os.kill(observed['pid'], 0)
                        except ProcessLookupError:
                            break
                        self.assertLess(time.monotonic(), deadline, 'Descendant survived suite timeout')
                        time.sleep(.02)
            finally:
                # A failing regression must not leave its own synthetic worker.
                if kernel is not None and observed.get('handle'):
                    if kernel.WaitForSingleObject(observed['handle'], 0) == 258:
                        kernel.TerminateProcess(observed['handle'], 1)
                        kernel.WaitForSingleObject(observed['handle'], 5000)
                    kernel.CloseHandle(observed['handle'])
                elif observed.get('pid') and not observed.get('reaped'):
                    try:
                        os.kill(observed['pid'], 9)
                    except ProcessLookupError:
                        pass
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                if previous_subreaper is not None:
                    if observed.get('pid') and not observed.get('reaped'):
                        os.waitpid(observed['pid'], 0)
                    libc.prctl(36, previous_subreaper.value, 0, 0, 0)


    def _runner(self):
        spec = importlib.util.spec_from_file_location('ci_job_boundary', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        return runner

    @contextlib.contextmanager
    def _modeled_job(self):
        runner = self._runner()
        kernel = types.SimpleNamespace(**{
            name: mock.Mock(return_value=1) for name in (
                'CreateJobObjectW', 'SetInformationJobObject', 'AssignProcessToJobObject',
                'TerminateJobObject', 'QueryInformationJobObject', 'CreateToolhelp32Snapshot',
                'Thread32First', 'Thread32Next', 'OpenThread', 'GetProcessIdOfThread',
                'ResumeThread', 'CloseHandle')})
        kernel.CreateJobObjectW.return_value = 101
        kernel.CreateToolhelp32Snapshot.return_value = 102
        kernel.OpenThread.return_value = 103
        kernel.GetProcessIdOfThread.return_value = 42
        kernel.Thread32Next.return_value = 0
        def first(snapshot, pointer):
            entry = pointer._obj
            entry.th32OwnerProcessID = 42
            entry.th32ThreadID = 99
            return 1
        kernel.Thread32First.side_effect = first
        with (mock.patch.object(ctypes, 'WinDLL', return_value=kernel, create=True),
              mock.patch.object(ctypes, 'get_last_error', return_value=18, create=True),
              mock.patch.object(ctypes, 'WinError', side_effect=lambda code: OSError(code, 'modeled Win32 failure'), create=True)):
            job = runner._WindowsJob()
            try:
                yield runner, job, kernel
            finally:
                job.close()

    def test_windows_job_abi_and_exact_launch_ownership_before_resume(self):
        with self._modeled_job() as (runner, job, kernel):
            limits = kernel.SetInformationJobObject.call_args.args[2]._obj
            self.assertEqual(limits.BasicLimitInformation.LimitFlags, 0x2000)
            self.assertEqual(ctypes.sizeof(limits), 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112)
            self.assertEqual(ctypes.sizeof(job.Accounting), 48)
            self.assertEqual(ctypes.sizeof(job.ThreadEntry), 28)
            kernel.CreateJobObjectW.assert_called_once_with(None, None)
            events = []
            process = mock.Mock(pid=42, _handle=9876)
            process.poll.return_value = None
            def popen(command, **kwargs):
                self.assertEqual(command, ['python.exe', '-c', 'trusted suite'])
                self.assertEqual(kwargs['creationflags'], 0x204)  # suspended + process group; no breakaway
                self.assertIs(kwargs['env'], environment)
                events.append('created suspended')
                return process
            def assign(job_handle, process_handle):
                self.assertEqual((job_handle, process_handle), (101, 9876))
                events.append('assigned exact process')
                return 1
            kernel.AssignProcessToJobObject.side_effect = assign
            kernel.ResumeThread.side_effect = lambda handle: (events.append('resumed'), 1)[1]
            environment = {'CARR_TEST': 'kept'}
            api = types.SimpleNamespace(**vars(subprocess))
            api.Popen = popen
            with (mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt')),
                  mock.patch.object(runner, '_WindowsJob', return_value=job),
                  mock.patch.object(runner, 'subprocess', api)):
                self.assertIs(runner._start_test_process(['python.exe', '-c', 'trusted suite'], env=environment), process)
            self.assertEqual(events, ['created suspended', 'assigned exact process', 'resumed'])
            self.assertIs(process._carr_job, job)
            kernel.OpenThread.assert_called_once_with(0x802, False, 99)
            self.assertEqual(kernel.CloseHandle.call_args_list, [mock.call(102), mock.call(103)])
            process.kill.assert_not_called()

    def test_failed_windows_assignment_never_resumes_and_reaps_suspended_child(self):
        with self._modeled_job() as (runner, job, kernel):
            kernel.AssignProcessToJobObject.return_value = 0
            process = mock.Mock(pid=42, _handle=9876)
            api = types.SimpleNamespace(**vars(subprocess))
            api.Popen = mock.Mock(return_value=process)
            with (mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt')),
                  mock.patch.object(runner, '_WindowsJob', return_value=job),
                  mock.patch.object(runner, 'subprocess', api)):
                with self.assertRaisesRegex(OSError, 'modeled Win32 failure'):
                    runner._start_test_process(['python.exe'], env={})
            kernel.ResumeThread.assert_not_called()
            process.kill.assert_called_once_with()
            process.wait.assert_called_once()
            kernel.TerminateJobObject.assert_called_once_with(101, 1)
            kernel.CloseHandle.assert_called_once_with(101)
            self.assertIsNone(job.handle)

    def test_windows_launch_failure_closes_unassigned_job(self):
        with self._modeled_job() as (runner, job, kernel):
            api = types.SimpleNamespace(**vars(subprocess))
            api.Popen = mock.Mock(side_effect=OSError('launch refused'))
            with (mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt')),
                  mock.patch.object(runner, '_WindowsJob', return_value=job),
                  mock.patch.object(runner, 'subprocess', api)):
                with self.assertRaisesRegex(OSError, 'launch refused'):
                    runner._start_test_process(['python.exe'], env={})
            kernel.AssignProcessToJobObject.assert_not_called()
            kernel.TerminateJobObject.assert_not_called()
            kernel.CloseHandle.assert_called_once_with(101)

    def test_windows_resume_refuses_changed_thread_owner_or_suspend_count(self):
        for owner, count in ((43, 1), (42, 0), (42, 2), (42, 0xffffffff)):
            with self.subTest(owner=owner, count=count), self._modeled_job() as (runner, job, kernel):
                kernel.GetProcessIdOfThread.return_value = owner
                kernel.ResumeThread.return_value = count
                process = mock.Mock(pid=42)
                process.poll.return_value = None
                with self.assertRaises(OSError):
                    job.resume(process)
                if owner != 42:
                    kernel.ResumeThread.assert_not_called()
                self.assertEqual(kernel.CloseHandle.call_args_list, [mock.call(102), mock.call(103)])

    def test_windows_job_termination_acknowledgement_waits_for_all_active_descendants(self):
        with self._modeled_job() as (runner, job, kernel):
            states, events = iter((2, 1, 0)), []
            kernel.TerminateJobObject.side_effect = lambda *args: (events.append('terminate'), 1)[1]
            def query(handle, kind, pointer, size, returned):
                self.assertEqual((handle, kind, size, returned), (101, 1, 48, None))
                count = next(states)
                pointer._obj.ActiveProcesses = count
                events.append(('active', count))
                return 1
            kernel.QueryInformationJobObject.side_effect = query
            process = mock.Mock(pid=42, _carr_job=job)
            process.wait.side_effect = lambda **kwargs: events.append('root waited')
            with (mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt')),
                  mock.patch.object(runner, 'sleep', side_effect=lambda seconds: events.append('wait descendants'))):
                runner._terminate_process_tree(process)
            self.assertEqual(events, ['terminate', 'root waited', ('active', 2),
                                     'wait descendants', ('active', 1), 'wait descendants', ('active', 0)])
            process.kill.assert_not_called()

    def test_windows_descendant_that_never_finishes_keeps_cleanup_failed(self):
        with self._modeled_job() as (runner, job, kernel):
            def active(handle, kind, pointer, size, returned):
                pointer._obj.ActiveProcesses = 1
                return 1
            kernel.QueryInformationJobObject.side_effect = active
            process = mock.Mock(pid=42, _carr_job=job)
            process.wait.return_value = 1
            with (mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt')),
                  mock.patch.object(runner, 'monotonic', side_effect=[0, 0, 11])):
                with self.assertRaisesRegex(runner._ProcessCleanupError, 'process-tree cleanup failed'):
                    runner._terminate_process_tree(process)
            kernel.QueryInformationJobObject.assert_called_once()
            process.kill.assert_not_called()

    def test_windows_successful_root_exit_still_drains_owned_descendants(self):
        with self._modeled_job() as (runner, job, kernel):
            process = mock.Mock(pid=42, _carr_job=job)
            process.wait.return_value = 0
            with (mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt')),
                  mock.patch.object(runner, '_start_test_process', return_value=process)):
                self.assertEqual(runner._run_test_process(['python.exe'], env={}), 0)
            kernel.TerminateJobObject.assert_called_once_with(101, 1)
            kernel.QueryInformationJobObject.assert_called_once()
            self.assertEqual(process.wait.call_count, 2)
            kernel.CloseHandle.assert_called_once_with(101)

    def test_unproved_windows_cleanup_retains_files_and_preserves_timeout_failure(self):
        runner = self._runner()
        process = mock.Mock(pid=42)
        process.wait.side_effect = subprocess.TimeoutExpired(['synthetic'], .1)
        job = mock.Mock()
        process._carr_job = job
        job.terminate_and_wait.side_effect = OSError('owned descendant still active')
        original_temp = tempfile.TemporaryDirectory
        directories = []
        with original_temp(prefix='CARR retained Ž ') as tmp:
            root = Path(tmp).resolve(strict=True)
            (root / 'suite.py').write_text('def test_ok(): pass', encoding='utf-8')
            def tracked(*args, **kwargs):
                directory = original_temp(*args, dir=root, **kwargs)
                directories.append(directory)
                return directory
            output = io.StringIO()
            with (mock.patch.object(runner, 'ROOT', root),
                  mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt', environ={})),
                  mock.patch.object(runner, '_start_test_process', return_value=process),
                  mock.patch.object(runner, 'tempfile', types.SimpleNamespace(TemporaryDirectory=tracked)),
                  contextlib.redirect_stdout(output)):
                self.assertFalse(runner.run_suite('suite.py'))
            self.assertIn('TimeoutExpired', output.getvalue())
            self.assertIn('Temporary suite files retained', output.getvalue())
            self.assertEqual(len(directories), 1)
            self.assertTrue(Path(directories[0].name).is_dir())
            self.assertFalse(directories[0]._finalizer.alive)
            job.close.assert_called_once_with()
            directories[0].cleanup()  # The synthetic model has no live process.


if __name__ == '__main__':
    unittest.main()
