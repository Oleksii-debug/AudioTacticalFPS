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

    def test_cleanup_failure_is_named_and_cannot_publish_pass_or_hide_timeout(self):
        spec = importlib.util.spec_from_file_location('diagnostic_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        original_temp = tempfile.TemporaryDirectory
        with original_temp(prefix='CARR cleanup diagnosis ') as temporary:
            # The runner compares canonical paths. Windows temporary paths can
            # use an 8.3 alias, so bind the fixture to the same resolved root.
            root = Path(temporary).resolve(strict=True)
            (root / 'test_ok.py').write_text('def test_ok(): assert True\n', encoding='utf-8')

            def failing_cleanup(*args, **kwargs):
                # Exercise a real temporary directory and a real passing child.
                # The report-directory owner retries cleanup; every injected
                # failure retains the original diagnostic and strict verdict.
                directory = original_temp(*args, dir=root, **kwargs)
                def cleanup():
                    directory.cleanup()
                    raise PermissionError('Synthetic held-library.lock cleanup failure')
                return types.SimpleNamespace(name=directory.name, cleanup=cleanup)

            for timed_out in (False, True):
                with self.subTest(timed_out=timed_out):
                    output, errors = io.StringIO(), io.StringIO()
                    process = (mock.patch.object(runner, '_run_test_process',
                        side_effect=subprocess.TimeoutExpired(['synthetic pytest'], 600))
                        if timed_out else contextlib.nullcontext())
                    with (mock.patch.object(runner, 'ROOT', root),
                          mock.patch.object(runner, 'tempfile', types.SimpleNamespace(
                              TemporaryDirectory=failing_cleanup)), process,
                          contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
                        result = runner.run_suite('test_ok.py')
                    self.assertFalse(result)
                    self.assertNotIn('PASS: test_ok.py:', output.getvalue())
                    self.assertIn('FAIL: test_ok.py:', output.getvalue())
                    self.assertIn('held-library.lock', output.getvalue())
                    self.assertIn('PermissionError', errors.getvalue())
                    if timed_out:
                        self.assertIn('last phase=pytest process', output.getvalue())
                        self.assertIn('TimeoutExpired', errors.getvalue())
                        self.assertIn('600', errors.getvalue())
                    else:
                        self.assertIn('last phase=temporary directory cleanup', output.getvalue())

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

            # Windows TEMP may use an 8.3 alias; the runner resolves candidates
            # before checking that they remain inside its canonical root.
            root = Path(temporary).resolve()
            marker = root / 'descendant.pid'
            (root / 'descendant.py').write_text(
                "from pathlib import Path\nimport os,sys,time\n"
                "held = open(sys.argv[1], 'wb')\n"
                "marker = Path(sys.argv[2])\n"
                "pending = marker.with_suffix('.pending')\n"
                "pending.write_text(str(os.getpid()), encoding='ascii')\n"
                "pending.replace(marker)\n"

                "Path(sys.argv[2]).write_text(str(os.getpid()), encoding='ascii')\n"
                "while True: time.sleep(1)\n", encoding='utf-8')
            (root / 'test_timeout.py').write_text(
                "from pathlib import Path\nimport subprocess,sys,time\n"
                "def test_wait(tmp_path):\n"
                "    subprocess.Popen([sys.executable, str(Path.cwd()/'descendant.py'),\n"
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
                        # Startup itself may fail before the PID is observed.
                        # Reap the owned process tree even on that error path.
                        runner._terminate_process_tree(process)

                        process.kill()
                    process.wait(timeout=5)
                if previous_subreaper is not None:
                    if observed.get('pid') and not observed.get('reaped'):
                        os.waitpid(observed['pid'], 0)
                    libc.prctl(36, previous_subreaper.value, 0, 0, 0)


if __name__ == '__main__':
    unittest.main()
