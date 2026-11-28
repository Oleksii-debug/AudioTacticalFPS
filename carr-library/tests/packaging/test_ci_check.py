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
            return subprocess.run([sys.executable, '-B', str(root / 'scripts' / SCRIPT.name), *suites],
                                  cwd=root, env=env, capture_output=True, text=True, timeout=60)

    def test_pass(self):
        result = self.run_fixture('def test_ok(): assert True\n', 'test_sample.py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('passed=1, failed=0, skipped=0', result.stdout)

    def test_real_imports_and_ordinary_descendants_preserve_sealed_module_bytes(self):
        source = '''from hashlib import sha256
import importlib
import os
from pathlib import Path
import subprocess
import sys

def test_sealed_imports(tmp_path, monkeypatch):
    sealed = tmp_path / 'sealed module tree Ž'
    package = sealed / 'sealed_runner_fixture'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text("VALUE = 'package'\\n", encoding='utf-8')
    (package / 'parent.py').write_text("VALUE = 'parent imported'\\n", encoding='utf-8')
    (package / 'child.py').write_text("VALUE = 'descendant imported'\\n", encoding='utf-8')
    def inventory():
        return {path.relative_to(sealed).as_posix(): sha256(path.read_bytes()).hexdigest()
                for path in sealed.rglob('*') if path.is_file()}
    before = inventory()
    monkeypatch.syspath_prepend(str(sealed))
    imported = importlib.import_module('sealed_runner_fixture.parent')
    assert imported.VALUE == 'parent imported'
    assert Path(imported.__file__).resolve() == (package / 'parent.py').resolve()
    # This ordinary descendant deliberately has no -B: the runner must carry
    # its bytecode policy across that real process boundary through the environment.
    code = ("from pathlib import Path; import sys; import sealed_runner_fixture.child as m; "
            "assert Path(m.__file__).resolve() == Path(sys.argv[1]).resolve(); "
            "assert m.VALUE == 'descendant imported'; print(m.VALUE)")
    child = subprocess.run([sys.executable, '-c', code, str(package / 'child.py')],
        env=dict(os.environ, PYTHONPATH=str(sealed)), cwd=tmp_path,
        capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stdout + child.stderr
    assert child.stdout.strip() == 'descendant imported'
    assert inventory() == before, 'Imports changed the sealed module inventory'
    assert not list(sealed.rglob('__pycache__')) and not list(sealed.rglob('*.pyc'))
'''
        # An empty value enables bytecode writes; '0' is still a non-empty
        # PYTHONDONTWRITEBYTECODE value and would not exercise this regression.
        result = self.run_fixture(source, 'test_sample.py',
                                  extra_env={'PYTHONDONTWRITEBYTECODE': ''})
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

    def test_parallel_real_child_tails_cannot_swallow_strict_verdict_lines(self):
        source = ("import atexit, os\n"
                  "atexit.register(lambda: os.write(1, b'CHILD_TAIL_WITHOUT_NEWLINE'))\n"
                  "def test_ok(): assert True\n")
        result = self.run_fixture(source, '--jobs', '2', 'test_sample.py', 'test_sample.py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('CHILD_TAIL_WITHOUT_NEWLINE'), 2)
        expected = 'PASS: test_sample.py: junit_passed=1, failed=0, skipped=0, pytest_exit=0'
        self.assertEqual(result.stdout.splitlines().count(expected), 2, result.stdout)
        self.assertEqual(result.stdout.splitlines()[-3:], [
            expected, expected, 'Required suites complete: 2; passed=2; failed=0'])

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

            def with_test_deadline(suite, *, report=None):
                if suite == 'test_timeout.py':
                    # Startup is acknowledged by the real descendant first;
                    # only the wait budget is shortened, never termination.
                    with mock.patch.object(runner, 'SUITE_TIMEOUT_SECONDS', .15):
                        return real_run_suite(suite, report=report)
                return real_run_suite(suite, report=report)

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


    def _run_reviewed_timeout_fixture(self, source, *arguments):
        with tempfile.TemporaryDirectory(prefix='CARR reviewed timeout Ž ') as temporary:
            root = Path(temporary).resolve()
            (root / 'scripts').mkdir()
            shutil.copyfile(SCRIPT, root / 'scripts' / SCRIPT.name)
            reviewed = root / 'tests' / 'transcription' / 'test_admission_boundaries.py'
            reviewed.parent.mkdir(parents=True)
            reviewed.write_text(source, encoding='utf-8')
            (root / 'test_other.py').write_text('def test_other(): assert True\n', encoding='utf-8')
            return subprocess.run(
                [sys.executable, '-B', str(root / 'scripts' / SCRIPT.name), *arguments],
                cwd=root, env=dict(os.environ), capture_output=True, text=True, timeout=60)

    def test_reviewed_timeout_cli_keeps_offline_policy_and_other_selected_suite(self):
        reviewed = 'tests/transcription/test_admission_boundaries.py'
        source = '''import socket
import pytest
def test_still_offline():
    with pytest.raises(OSError, match='disabled during offline verification'):
        socket.getaddrinfo('example.invalid', 443)
    with socket.socket() as connection:
        with pytest.raises(OSError, match='disabled during offline verification'):
            connection.connect(('127.0.0.1', 9))
'''
        result = self._run_reviewed_timeout_fixture(
            source, '--jobs', '2', '--suite-timeout', reviewed + '=900',
            reviewed, 'test_other.py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Required suites complete: 2; passed=2; failed=0', result.stdout)
        for suite in (reviewed, 'test_other.py'):
            self.assertEqual(sum(line.startswith('PASS: ' + suite + ':')
                                 for line in result.stdout.splitlines()), 1)

    def test_reviewed_timeout_reaches_only_the_selected_real_child_wait(self):
        spec = importlib.util.spec_from_file_location('reviewed_timeout_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        original_popen = subprocess.Popen
        waits, processes = [], []
        reviewed = 'tests/transcription/test_admission_boundaries.py'
        with tempfile.TemporaryDirectory(prefix='CARR timeout scope Ž ') as temporary:
            root = Path(temporary).resolve()
            (root / reviewed).parent.mkdir(parents=True)
            for suite in (reviewed, 'test_other.py'):
                (root / suite).write_text('def test_ok(): assert True\n', encoding='utf-8')

            def observe_wait(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                processes.append(process)
                actual_wait = process.wait
                suite = Path(args[0][-1]).relative_to(root).as_posix()

                def wait(*, timeout=None):
                    waits.append((suite, timeout))
                    return actual_wait(timeout=timeout)

                process.wait = wait
                return process

            api = types.SimpleNamespace(**vars(subprocess))
            api.Popen = observe_wait
            output = io.StringIO()
            try:
                with (mock.patch.object(runner, 'ROOT', root),
                      mock.patch.object(runner, 'subprocess', api),
                      mock.patch.object(sys, 'argv', ['ci_check.py', '--suite-timeout',
                          reviewed + '=900', reviewed, 'test_other.py']),
                      contextlib.redirect_stdout(output)):
                    result = runner.main()
                self.assertEqual(result, 0, output.getvalue())
                self.assertEqual(waits, [(reviewed, 900), ('test_other.py', 600)])
                self.assertTrue(all(process.returncode == 0 for process in processes))
            finally:
                for process in processes:
                    if process.poll() is None:
                        runner._terminate_process_tree(process)

    def test_invalid_reviewed_timeout_arguments_launch_no_suite(self):
        spec = importlib.util.spec_from_file_location('timeout_arguments_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        reviewed = 'tests/transcription/test_admission_boundaries.py'
        with tempfile.TemporaryDirectory(prefix='CARR timeout validation Ž ') as temporary:
            root = Path(temporary).resolve()
            target = root / reviewed
            target.parent.mkdir(parents=True)
            target.write_text('def test_ok(): assert True\n', encoding='utf-8')
            (root / 'test_other.py').write_text('def test_ok(): assert True\n', encoding='utf-8')
            trials = [
                ('missing value', ['--suite-timeout', reviewed], False),
                ('missing equals', ['--suite-timeout', reviewed, reviewed], False),
                ('double equals', ['--suite-timeout', reviewed + '==900', reviewed], False),
                ('unselected whole suite', ['--suite-timeout', reviewed + '=900', 'test_other.py'], False),
                ('node is not the selected whole suite', [
                    '--suite-timeout', reviewed + '=900', reviewed + '::test_ok'], False),
                ('unreviewed suite', ['--suite-timeout', 'test_other.py=900', 'test_other.py'], False),
                ('missing reviewed file', ['--suite-timeout', reviewed + '=900', reviewed], True),
                ('duplicate override', ['--suite-timeout', reviewed + '=900',
                    '--suite-timeout', reviewed + '=900', reviewed], False),
                ('validate every override before launch', ['--suite-timeout', reviewed + '=900',
                    '--suite-timeout', 'test_other.py=900', reviewed, 'test_other.py'], False),
            ]
            for label, value in (
                    ('zero', '0'), ('negative', '-1'), ('below reviewed', '899'),
                    ('above reviewed', '901'), ('float', '900.0'), ('nan', 'NaN'),
                    ('exponent', '9e2'), ('leading zero', '0900'), ('Unicode digits', '９００'),
                    ('whitespace', ' 900'), ('boolean', 'True'), ('empty', ''),
                    ('unbounded integer', '9' * 8000)):
                trials.append((label, ['--suite-timeout', reviewed + '=' + value, reviewed], False))
            for label, arguments, remove in trials:
                with self.subTest(case=label):
                    if remove:
                        target.unlink()
                    try:
                        with (mock.patch.object(runner, 'ROOT', root),
                              mock.patch.object(runner, 'run_suite') as child,
                              mock.patch.object(sys, 'argv', ['ci_check.py', *arguments]),
                              contextlib.redirect_stderr(io.StringIO())):
                            with self.assertRaises(SystemExit) as failure:
                                runner.main()
                            self.assertEqual(failure.exception.code, 2)
                            child.assert_not_called()
                    finally:
                        if remove:
                            target.write_text('def test_ok(): assert True\n', encoding='utf-8')

    def test_direct_suite_call_cannot_expand_unreviewed_or_invalid_timeouts(self):
        spec = importlib.util.spec_from_file_location('direct_timeout_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        reviewed = 'tests/transcription/test_admission_boundaries.py'
        with tempfile.TemporaryDirectory(prefix='CARR direct timeout Ž ') as temporary:
            root = Path(temporary).resolve()
            (root / reviewed).parent.mkdir(parents=True)
            for suite in (reviewed, 'test_other.py'):
                (root / suite).write_text('def test_ok(): assert True\n', encoding='utf-8')
            trials = [('test_other.py', 900)]
            trials.extend((reviewed, value) for value in (0, -1, 899, 901, 900.0, True, '900', 10**100))
            for suite, seconds in trials:
                with self.subTest(suite=suite, seconds=seconds):
                    messages = []
                    with (mock.patch.object(runner, 'ROOT', root),
                          mock.patch.object(runner, '_run_test_process') as child):
                        self.assertFalse(runner.run_suite(
                            suite, report=messages.append, timeout_seconds=seconds))
                        child.assert_not_called()
                    self.assertEqual(len(messages), 1)
                    self.assertTrue(messages[0].startswith('FAIL: ' + suite + ':'))

    def test_reviewed_timeout_never_accepts_failure_skip_xfail_or_xpass(self):
        reviewed = 'tests/transcription/test_admission_boundaries.py'
        fixtures = {
            'failure': 'def test_bad(): assert False\n',
            'skip': 'import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n',
            'xfail': 'import pytest\n@pytest.mark.xfail\ndef test_expected(): assert False\n',
            'xpass': 'import pytest\n@pytest.mark.xfail(strict=False)\ndef test_unexpected(): pass\n',
        }
        for label, source in fixtures.items():
            with self.subTest(case=label):
                result = self._run_reviewed_timeout_fixture(
                    source, '--suite-timeout', reviewed + '=900', reviewed)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('Required suites complete: 1; passed=0; failed=1', result.stdout)
                self.assertFalse(any(line.startswith('PASS: ' + reviewed + ':')
                                     for line in result.stdout.splitlines()))


    def _run_gil_diagnostic_fixture(self, source, preamble, *, interval='.05'):
        """A real isolated child exercises the Windows311 policy on every host."""
        import xml.etree.ElementTree as ET
        spec = importlib.util.spec_from_file_location('gil_diagnostic_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        child = runner.PYTEST_CHILD
        marker = 'if _needs_gil_stack_dump():'
        self.assertEqual(child.count(marker), 1)
        # Only platform selection is injected. The actual plugin, timer, native
        # synchronous dumper, pytest reports and offline policy are unchanged.
        child = child.replace(marker, 'if True:')
        with tempfile.TemporaryDirectory(prefix='CARR timed diagnostic Ž ') as temporary:
            root = Path(temporary)
            target, report = root / 'test_sample.py', root / 'junit.xml'
            target.write_text(source, encoding='utf-8')
            env = dict(os.environ)
            for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH'):
                env.pop(key, None)
            env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
            executable = sys.executable
            if os.name == 'nt' and sys.prefix != sys.base_prefix:
                executable = sys._base_executable
                env.pop('PYTHONEXECUTABLE', None)
                env['__PYVENV_LAUNCHER__'] = sys.executable
            result = subprocess.run([executable, '-B', '-c', preamble + '\n' + child,
                '-s', '-o', 'addopts=', '-o', 'xfail_strict=true',
                '-o', 'faulthandler_timeout=' + interval,
                '-o', 'faulthandler_exit_on_timeout=false', '--junitxml', str(report),
                '--basetemp', str(root / 'pytest'), '--', str(target)],
                cwd=root, env=env, capture_output=True, text=True, timeout=60)
            cases = list(ET.parse(report).iter('testcase')) if report.exists() else []
            return result, cases

    def test_gil_timed_dump_uses_real_safe_traversal_and_keeps_fatal_offline_policy(self):
        preamble = r"""import ctypes
import faulthandler
import threading
_dump_complete = threading.Event()
_inside_test = threading.Event()
_gil_seen = []
_real_dump = faulthandler.dump_traceback
def observed_dump(*args, **kwargs):
    try:
        assert _inside_test.wait(10), 'The test body never started'
        _gil_seen.append(ctypes.pythonapi.PyGILState_Check())
        return _real_dump(*args, **kwargs)
    finally:
        _dump_complete.set()
def forbidden_native_timer(*args, **kwargs):
    raise AssertionError('The unsafe native timed traversal was armed')
faulthandler.dump_traceback = observed_dump
faulthandler.dump_traceback_later = forbidden_native_timer
"""
        source = r"""import __main__
import faulthandler
import socket
import time
import pytest
def frame_churn(depth):
    a, b, c = 1, 2, 3
    return frame_churn(depth - 1) if depth else a + b + c
def test_real_frame_churn_and_fatal_handler():
    __main__._inside_test.set()
    assert faulthandler.is_enabled()
    select = __main__._needs_gil_stack_dump
    assert select('win32', 'cpython', (3, 11))
    for platform, implementation, version in (
            ('linux', 'cpython', (3, 11)), ('win32', 'pypy', (3, 11)),
            ('win32', 'cpython', (3, 12)), ('win32', 'cpython', (3, 10))):
        assert not select(platform, implementation, version)
    with pytest.raises(OSError, match='disabled during offline verification'):
        socket.getaddrinfo('example.invalid', 443)
    deadline = time.monotonic() + 10
    while not __main__._dump_complete.is_set():
        assert time.monotonic() < deadline, 'The real timed diagnostic never ran'
        frame_churn(80)
    assert __main__._gil_seen == [1]
    assert faulthandler.is_enabled()
"""
        result, cases = self._run_gil_diagnostic_fixture(source, preamble)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(cases), 1)
        self.assertFalse(any(c.find(tag) is not None for c in cases
                             for tag in ('failure', 'error', 'skipped')))
        self.assertIn('Timed stack diagnostic (GIL-owned Python thread):', result.stderr)
        self.assertIn('test_real_frame_churn_and_fatal_handler', result.stderr)

    def test_gil_timed_dump_is_cancelled_and_joined_before_the_next_test(self):
        preamble = r"""import faulthandler
_dump_calls = []
def forbidden_dump(*args, **kwargs):
    _dump_calls.append(True)
    raise AssertionError('A cancelled diagnostic ran after its test')
faulthandler.dump_traceback = forbidden_dump
def forbidden_native_timer(*args, **kwargs):
    raise AssertionError('The unsafe native timed traversal was armed')
faulthandler.dump_traceback_later = forbidden_native_timer
"""
        source = r"""import __main__
import faulthandler
import threading
import time
def test_a_cancel_before_deadline(pytestconfig):
    plugin = next(p for p in pytestconfig.pluginmanager.get_plugins()
                  if isinstance(p, __main__.GilTimedStackDump))
    assert plugin.timeout == 30
    # The first diagnostic already owns its captured interval. Disabling the
    # next timer makes the following native thread-lifetime check unambiguous.
    plugin.timeout = 0
def test_b_no_late_callback_or_timer():
    time.sleep(.05)
    assert __main__._dump_calls == []
    assert not [t for t in threading.enumerate()
                if t.name == 'carr-ci-stack-diagnostic']
    assert faulthandler.is_enabled()
"""
        result, cases = self._run_gil_diagnostic_fixture(source, preamble, interval='30')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(cases), 2)
        self.assertFalse(any(c.find(tag) is not None for c in cases
                             for tag in ('failure', 'error', 'skipped')))
        self.assertNotIn('Timed stack diagnostic', result.stderr)

    def test_gil_timed_dump_error_cannot_be_a_successful_pytest_exit(self):
        preamble = r"""import faulthandler
import threading
_dump_complete = threading.Event()
def failed_dump(*args, **kwargs):
    _dump_complete.set()
    raise OSError('Synthetic diagnostic descriptor failure')
faulthandler.dump_traceback = failed_dump
def forbidden_native_timer(*args, **kwargs):
    raise AssertionError('The unsafe native timed traversal was armed')
faulthandler.dump_traceback_later = forbidden_native_timer
"""
        source = r"""import __main__
def test_real_diagnostic_failure():
    assert __main__._dump_complete.wait(10)
"""
        result, _ = self._run_gil_diagnostic_fixture(source, preamble)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('The timed stack diagnostic failed.', result.stdout + result.stderr)
        self.assertIn('Synthetic diagnostic descriptor failure', result.stdout + result.stderr)


    def _run_direct_pytest_diagnostic_fixture(self, source, preamble, *, interval='.05'):
        """Run the public diagnostic factory inside a real isolated pytest child."""
        import json
        import xml.etree.ElementTree as ET
        child = r"""import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
import threading
import types
import pytest

script = Path(sys.argv.pop(1))
script_digest = hashlib.sha256(script.read_bytes()).hexdigest()
spec = importlib.util.spec_from_file_location('direct_ci_diagnostic_factory', script)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
network = (socket.socket.connect, socket.socket.connect_ex,
           socket.create_connection, socket.getaddrinfo)
path_before = list(sys.path)
telemetry_loaded = 'onnxruntime' in sys.modules
actual = runner.pytest_diagnostic_plugins()
wanted = sys.platform == 'win32' and sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 11)
assert len(actual) == int(wanted)
real_sys = runner.sys
# Inject only the known platform predicate for this portable regression. The
# actual process, pytest hooks, synchronous native dumper and timer stay real.
for platform, implementation, version, expected in (
        ('linux', 'cpython', (3, 11), 0),
        ('win32', 'pypy', (3, 11), 0),
        ('win32', 'cpython', (3, 12), 0),
        ('win32', 'cpython', (3, 11), 1)):
    selected = types.SimpleNamespace(**vars(sys))
    selected.platform = platform
    selected.implementation = types.SimpleNamespace(name=implementation)
    selected.version_info = version
    runner.sys = selected
    diagnostics = runner.pytest_diagnostic_plugins()
    assert len(diagnostics) == expected
runner.sys = real_sys
assert len(diagnostics) == 1
assert network == (socket.socket.connect, socket.socket.connect_ex,
                   socket.create_connection, socket.getaddrinfo)
assert sys.path == path_before and ('onnxruntime' in sys.modules) == telemetry_loaded
original_child = runner.PYTEST_CHILD
try:
    runner.PYTEST_CHILD += '\nclass GilTimedStackDump: pass\n'
    try:
        runner.pytest_diagnostic_plugins()
    except RuntimeError as error:
        assert 'definitions changed' in str(error)
    else:
        raise AssertionError('Ambiguous canonical diagnostic definitions were accepted')
finally:
    runner.PYTEST_CHILD = original_child

class CallerPolicy:
    def __init__(self):
        self.calls = []
    def pytest_runtest_logreport(self, report):
        if report.when == 'call':
            self.calls.append((report.nodeid, report.outcome))
caller_policy = CallerPolicy()
"""
        ending = r"""
status = pytest.main(sys.argv[1:], plugins=[caller_policy, *diagnostics])
alive = [thread.name for thread in threading.enumerate()
         if thread.name == 'carr-ci-stack-diagnostic']
assert not alive, 'A direct pytest diagnostic outlived its test run'
assert hashlib.sha256(script.read_bytes()).hexdigest() == script_digest
assert network == (socket.socket.connect, socket.socket.connect_ex,
                   socket.create_connection, socket.getaddrinfo)
Path('direct-receipt.json').write_text(json.dumps({
    'exit': int(status), 'caller_reports': caller_policy.calls,
    'diagnostic_threads': alive, 'source_sha256': script_digest,
    'socket_policy_unchanged': True, 'platform_selection_checked': True,
    'ambiguous_definition_refused': True}), encoding='utf-8')
raise SystemExit(status)
"""
        with tempfile.TemporaryDirectory(prefix='CARR direct diagnostic Ž ') as temporary:
            root = Path(temporary)
            target, report = root / 'test_sample.py', root / 'junit.xml'
            script = root / 'ci_check.py'
            shutil.copyfile(SCRIPT, script)
            target.write_text(source, encoding='utf-8')
            env = dict(os.environ)
            for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH'):
                env.pop(key, None)
            env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
            executable = sys.executable
            if os.name == 'nt' and sys.prefix != sys.base_prefix:
                executable = sys._base_executable
                env.pop('PYTHONEXECUTABLE', None)
                env['__PYVENV_LAUNCHER__'] = sys.executable
            result = subprocess.run([executable, '-I', '-B', '-X', 'utf8', '-c',
                child + '\n' + preamble + '\n' + ending, str(script),
                '-s', '-o', 'addopts=', '-o', 'pythonpath=', '-o', 'xfail_strict=true',
                '-o', 'faulthandler_timeout=' + interval,
                '-o', 'faulthandler_exit_on_timeout=false', '--junitxml', str(report),
                '--basetemp', str(root / 'pytest'), '--', str(target)],
                cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
            cases = list(ET.parse(report).iter('testcase')) if report.exists() else []
            receipt_path = root / 'direct-receipt.json'
            receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else {}
            return result, cases, receipt

    def test_direct_pytest_factory_reuses_real_gil_dump_without_changing_caller_policy(self):
        preamble = r"""import ctypes
import faulthandler
import time
inside_test = threading.Event()
dump_complete = threading.Event()
gil_seen = []
real_dump = faulthandler.dump_traceback
def observed_dump(*args, **kwargs):
    try:
        assert inside_test.wait(10), 'The direct pytest body never started'
        gil_seen.append(ctypes.pythonapi.PyGILState_Check())
        return real_dump(*args, **kwargs)
    finally:
        dump_complete.set()
def forbidden_timer(*args, **kwargs):
    raise AssertionError('Direct pytest armed the unsafe native timer')
faulthandler.dump_traceback = observed_dump
faulthandler.dump_traceback_later = forbidden_timer
"""
        source = r"""import __main__
import faulthandler
import socket
import time
def churn(depth):
    values = (1, 2, 3)
    return churn(depth - 1) if depth else sum(values)
def test_actual_direct_hook(pytestconfig):
    __main__.inside_test.set()
    assert faulthandler.is_enabled()
    assert pytestconfig.getini('faulthandler_timeout') == 0
    assert __main__.network == (socket.socket.connect, socket.socket.connect_ex,
                               socket.create_connection, socket.getaddrinfo)
    deadline = time.monotonic() + 10
    while not __main__.dump_complete.is_set():
        assert time.monotonic() < deadline, 'No real direct diagnostic occurred'
        churn(80)
    assert __main__.gil_seen == [1]
    assert faulthandler.is_enabled()
"""
        result, cases, receipt = self._run_direct_pytest_diagnostic_fixture(source, preamble)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(cases), 1)
        self.assertFalse(any(case.find(tag) is not None for case in cases
                             for tag in ('failure', 'error', 'skipped')))
        self.assertIn('Timed stack diagnostic (GIL-owned Python thread):', result.stderr)
        self.assertIn('test_actual_direct_hook', result.stderr)
        self.assertTrue(receipt['socket_policy_unchanged'])
        self.assertTrue(receipt['platform_selection_checked'])
        self.assertTrue(receipt['ambiguous_definition_refused'])
        self.assertEqual(receipt['diagnostic_threads'], [])
        self.assertEqual([row[1] for row in receipt['caller_reports']], ['passed'])

    def test_direct_pytest_factory_settles_timer_before_the_next_test(self):
        preamble = r"""import faulthandler
dump_calls = []
def forbidden_dump(*args, **kwargs):
    dump_calls.append(True)
    raise AssertionError('Direct diagnostic ran after its cancelled test')
def forbidden_timer(*args, **kwargs):
    raise AssertionError('Direct pytest armed the unsafe native timer')
faulthandler.dump_traceback = forbidden_dump
faulthandler.dump_traceback_later = forbidden_timer
"""
        source = r"""import __main__
import faulthandler
import threading
import time
def test_a_direct_timer_cancelled():
    assert __main__.diagnostics[0].timeout == 30
    __main__.diagnostics[0].timeout = 0
def test_b_direct_owner_has_settled():
    time.sleep(.05)
    assert __main__.dump_calls == []
    assert not [thread for thread in threading.enumerate()
                if thread.name == 'carr-ci-stack-diagnostic']
    assert faulthandler.is_enabled()
"""
        result, cases, receipt = self._run_direct_pytest_diagnostic_fixture(
            source, preamble, interval='30')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(cases), 2)
        self.assertFalse(any(case.find(tag) is not None for case in cases
                             for tag in ('failure', 'error', 'skipped')))
        self.assertNotIn('Timed stack diagnostic', result.stderr)
        self.assertEqual(receipt['diagnostic_threads'], [])
        self.assertEqual([row[1] for row in receipt['caller_reports']], ['passed', 'passed'])

    def test_direct_pytest_diagnostic_failure_preserves_nonzero_exit_after_cleanup(self):
        preamble = r"""import faulthandler
dump_complete = threading.Event()
def failed_dump(*args, **kwargs):
    dump_complete.set()
    raise OSError('Synthetic direct diagnostic descriptor failure')
def forbidden_timer(*args, **kwargs):
    raise AssertionError('Direct pytest armed the unsafe native timer')
faulthandler.dump_traceback = failed_dump
faulthandler.dump_traceback_later = forbidden_timer
"""
        source = r"""import __main__
import faulthandler
def test_direct_diagnostic_failure():
    assert faulthandler.is_enabled()
    assert __main__.dump_complete.wait(10)
"""
        result, _, receipt = self._run_direct_pytest_diagnostic_fixture(source, preamble)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('The timed stack diagnostic failed.', result.stdout + result.stderr)
        self.assertIn('Synthetic direct diagnostic descriptor failure', result.stdout + result.stderr)
        self.assertNotEqual(receipt['exit'], 0)
        self.assertEqual(receipt['diagnostic_threads'], [])
        self.assertTrue(receipt['socket_policy_unchanged'])


if __name__ == '__main__':
    unittest.main()
