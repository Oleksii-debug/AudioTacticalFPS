from pathlib import Path
import contextlib
import ctypes
import importlib.util
import io
import json
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
            return subprocess.run([sys.executable, '-B', str(root / 'scripts' / SCRIPT.name), *suites],
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

    def _run_report_cleanup_fixture(self, failures, *, timed_out=False):
        spec = importlib.util.spec_from_file_location('cleanup_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        original_temp = tempfile.TemporaryDirectory
        directories, attempts = [], []
        output = io.StringIO()
        with original_temp(prefix='CARR report cleanup ') as temporary:
            root = Path(temporary).resolve()
            (root / 'test_sample.py').write_text('def test_ok(): assert True\n', encoding='utf-8')

            def report_directory(*args, **kwargs):
                directory = original_temp(*args, dir=root, **kwargs)
                directories.append(directory)

                def cleanup():
                    attempts.append(directory.name)
                    if len(attempts) <= failures:
                        raise PermissionError('Synthetic report handle is still held')
                    directory.cleanup()

                return types.SimpleNamespace(name=directory.name, cleanup=cleanup)

            def completed_child(command, *, env):
                if timed_out:
                    raise subprocess.TimeoutExpired('synthetic required suite', .15)
                report = Path(command[command.index('--junitxml') + 1])
                report.write_text(
                    '<testsuites><testsuite><testcase name="test_ok"/></testsuite></testsuites>',
                    encoding='utf-8')
                return 0

            try:
                with (mock.patch.object(runner, 'ROOT', root),
                      mock.patch.object(runner, 'tempfile',
                          types.SimpleNamespace(TemporaryDirectory=report_directory)),
                      mock.patch.object(runner, '_run_test_process', side_effect=completed_child) as child,
                      mock.patch.object(runner, 'sleep'),
                      contextlib.redirect_stdout(output)):
                    result = runner.run_suite('test_sample.py')
                child.assert_called_once()
            finally:
                for directory in directories:
                    directory.cleanup()
        return result, output.getvalue(), len(attempts)

    def test_transient_report_cleanup_permission_error_is_retried_before_pass(self):
        result, output, attempts = self._run_report_cleanup_fixture(1)
        self.assertTrue(result, output)
        self.assertEqual(attempts, 2)
        self.assertIn('PASS: test_sample.py:', output)
        self.assertNotIn('FAIL:', output)

    def test_persistent_report_cleanup_permission_error_is_named_and_never_passes(self):
        result, output, attempts = self._run_report_cleanup_fixture(5)
        self.assertFalse(result, output)
        self.assertEqual(attempts, 5)
        self.assertIn('FAIL: test_sample.py:', output)
        self.assertIn('PermissionError', output)
        self.assertNotIn('PASS:', output)

    def test_report_cleanup_permission_error_preserves_original_timeout(self):
        result, output, attempts = self._run_report_cleanup_fixture(5, timed_out=True)
        self.assertFalse(result, output)
        self.assertEqual(attempts, 5)
        self.assertIn('FAIL: test_sample.py:', output)
        self.assertIn('TimeoutExpired', output)
        self.assertIn('PermissionError', output)
        self.assertNotIn('PASS:', output)

    def test_windows_delayed_descendant_exit_precedes_temporary_cleanup(self):
        spec = importlib.util.spec_from_file_location('reviewed_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        held = {'locked': True}
        closed = []
        process = types.SimpleNamespace(pid=2468, returncode=None)

        def wait_parent(timeout):
            if process.returncode is None:
                raise subprocess.TimeoutExpired('synthetic-suite', timeout)
            return process.returncode

        def request_termination(*args, **kwargs):
            self.assertIsNone(process.returncode, 'Capture/termination must precede parent exit')
            process.returncode = -9
            # Native termination has been requested, but the descendant still
            # owns the library handle until its process object is signalled.
            self.assertTrue(held['locked'])
            return types.SimpleNamespace(returncode=0)

        def wait_descendant(handle, milliseconds):
            self.assertEqual(handle, 42)
            self.assertGreaterEqual(milliseconds, 0)
            self.assertLessEqual(milliseconds, 10000)
            self.assertIsNotNone(process.returncode)
            held['locked'] = False
            return 0

        process.wait = wait_parent
        process.kill = lambda: setattr(process, 'returncode', -9)
        descendants = runner._WindowsDescendants.__new__(runner._WindowsDescendants)
        descendants._handles = [42]
        descendants._kernel = types.SimpleNamespace(
            WaitForSingleObject=wait_descendant,
            CloseHandle=lambda handle: closed.append(handle) or True,
        )
        api = types.SimpleNamespace(**vars(subprocess))
        api.CREATE_NEW_PROCESS_GROUP = 0x200
        api.Popen = lambda *args, **kwargs: process
        api.run = request_termination
        original_temp = tempfile.TemporaryDirectory
        directories = []

        @contextlib.contextmanager
        def temporary_with_open_file_guard(suite):
            with original_temp(prefix='carr-ci-') as name:
                directories.append(Path(name))
                try:
                    yield name
                finally:
                    if held['locked']:
                        raise PermissionError('A descendant still owns held-library.lock')
                    self.assertEqual(closed, [42], 'Close wait handles before library removal')

        with original_temp(prefix='CARR delayed exit ') as name:
            root = Path(name).resolve(strict=True)
            (root / 'test_wait.py').write_text('def test_wait(): pass\n', encoding='utf-8')
            output = io.StringIO()
            with (mock.patch.object(runner, 'ROOT', root),
                  mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt', environ=os.environ)),
                  mock.patch.object(runner, 'subprocess', api),
                  mock.patch.object(runner, '_WindowsDescendants', return_value=descendants),
                  mock.patch.object(runner, '_report_directory', temporary_with_open_file_guard),
                  contextlib.redirect_stdout(output)):
                self.assertFalse(runner.run_suite('test_wait.py'))
            self.assertIn('TimeoutExpired', output.getvalue())
            self.assertNotIn('PermissionError', output.getvalue())
            self.assertTrue(all(not directory.exists() for directory in directories))

    def test_windows_venv_owns_base_interpreter_without_mutating_launch_inputs(self):
        spec = importlib.util.spec_from_file_location('reviewed_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        selected = r'C:\selected venv\Scripts\python.exe'
        base = r'C:\Python311\python.exe'
        environment = {'PYTHONEXECUTABLE': 'unrelated', 'SELECTED': 'preserved'}
        command = [selected, '-B', '-c', 'pass']
        process = mock.Mock()
        process.wait.return_value = 0
        api = types.SimpleNamespace(**vars(subprocess))
        api.CREATE_NEW_PROCESS_GROUP = 0x200
        api.Popen = mock.Mock(return_value=process)
        with (mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt')),
              mock.patch.object(runner, 'sys', types.SimpleNamespace(executable=selected,
                  _base_executable=base, prefix=r'C:\selected venv', base_prefix=r'C:\Python311')),
              mock.patch.object(runner, 'subprocess', api)):
            self.assertEqual(runner._run_test_process(command, env=environment), 0)
        api.Popen.assert_called_once_with([base, '-B', '-c', 'pass'], cwd=runner.ROOT,
            env={'SELECTED': 'preserved', '__PYVENV_LAUNCHER__': selected}, creationflags=0x200)
        self.assertEqual(command, [selected, '-B', '-c', 'pass'])
        self.assertEqual(environment, {'PYTHONEXECUTABLE': 'unrelated', 'SELECTED': 'preserved'})
        process.wait.assert_called_once_with(timeout=runner.SUITE_TIMEOUT_SECONDS)

    def test_windows_failed_descendant_wait_reaps_parent_closes_all_handles_and_fails(self):
        spec = importlib.util.spec_from_file_location('reviewed_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        for wait_result in (258, 0xFFFFFFFF):  # timeout and failed native wait
            with self.subTest(wait_result=wait_result):
                closed = []
                process = mock.Mock(pid=2468)
                descendants = runner._WindowsDescendants.__new__(runner._WindowsDescendants)
                descendants._handles = [42, 43]
                descendants._ctypes = types.SimpleNamespace(
                    WinError=lambda code: OSError(code, 'Native wait failed'), get_last_error=lambda: 6)
                descendants._kernel = types.SimpleNamespace(
                    WaitForSingleObject=lambda *args: wait_result,
                    CloseHandle=lambda handle: closed.append(handle) or True,
                )
                api = types.SimpleNamespace(**vars(subprocess))
                api.run = mock.Mock(return_value=types.SimpleNamespace(returncode=0))
                with (mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt', environ=os.environ)),
                      mock.patch.object(runner, 'subprocess', api),
                      mock.patch.object(runner, '_WindowsDescendants', return_value=descendants)):
                    with self.assertRaisesRegex(OSError, 'process-tree cleanup failed'):
                        runner._terminate_process_tree(process)
                process.wait.assert_called_once_with(timeout=10)
                self.assertEqual(closed, [42, 43])

    def test_windows_failed_descendant_capture_still_terminates_parent_and_fails(self):
        spec = importlib.util.spec_from_file_location('reviewed_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        process = mock.Mock(pid=2468)
        api = types.SimpleNamespace(**vars(subprocess))
        api.run = mock.Mock(return_value=types.SimpleNamespace(returncode=0))
        with (mock.patch.object(runner, 'os', types.SimpleNamespace(name='nt', environ=os.environ)),
              mock.patch.object(runner, 'subprocess', api),
              mock.patch.object(runner, '_WindowsDescendants', side_effect=OSError('Capture denied'))):
            with self.assertRaisesRegex(OSError, 'process-tree cleanup failed'):
                runner._terminate_process_tree(process)
        api.run.assert_called_once()
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)

    def test_timeout_terminates_real_descendant_before_temp_cleanup_and_stays_failed(self):
        spec = importlib.util.spec_from_file_location('reviewed_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        original_popen = subprocess.Popen
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
                "Path(sys.argv[2]).write_text(str(os.getpid()), encoding='ascii')\n"
                "while True: time.sleep(1)\n", encoding='utf-8')
            (root / 'test_timeout.py').write_text(
                "from pathlib import Path\nimport json,os,pytest,subprocess,sys,time\n"
                "def test_wait(tmp_path):\n"
                "    (Path.cwd()/'suite-owner.json').write_text(json.dumps(dict(pid=os.getpid(),\n"
                "        executable=sys.executable, prefix=sys.prefix, pytest=pytest.__file__)), encoding='utf-8')\n"
                "    subprocess.Popen([sys.executable, '-B', str(Path.cwd()/'descendant.py'),\n"
                "        str(tmp_path/'held-library.lock'), str(Path.cwd()/'descendant.pid')],\n"
                "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "    while True: time.sleep(1)\n", encoding='utf-8')
            (root / 'test_ok.py').write_text('def test_ok(): assert True\n', encoding='utf-8')

            def observe_started_descendant(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                processes.append(process)
                if Path(args[0][-1]).name != 'test_timeout.py':
                    return process
                deadline = time.monotonic() + 30
                while not marker.exists():
                    self.assertIsNone(process.poll(), 'Synthetic pytest exited before starting its child')
                    self.assertLess(time.monotonic(), deadline, 'Synthetic child did not start')
                    time.sleep(.02)
                pid = int(marker.read_text(encoding='ascii'))
                observed['pid'] = pid
                observed['suite'] = json.loads((root / 'suite-owner.json').read_text(encoding='utf-8'))
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

            subprocess_api = types.SimpleNamespace(**vars(subprocess))
            subprocess_api.Popen = observe_started_descendant
            output = io.StringIO()
            try:
                if previous_subreaper is not None:
                    self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
                with (mock.patch.object(runner, 'ROOT', root),
                      mock.patch.object(runner, 'subprocess', subprocess_api),
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
                import pytest
                self.assertEqual(observed['suite']['pid'], processes[0].pid)
                self.assertEqual(os.path.normcase(observed['suite']['executable']), os.path.normcase(sys.executable))
                self.assertEqual(os.path.normcase(observed['suite']['prefix']), os.path.normcase(sys.prefix))
                self.assertEqual(os.path.normcase(observed['suite']['pytest']), os.path.normcase(pytest.__file__))
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


if __name__ == '__main__':
    unittest.main()
