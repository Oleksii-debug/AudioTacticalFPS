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
    def run_fixture(self, source, *suites, extra_env=None, fixture_path='test_sample.py'):
        with tempfile.TemporaryDirectory(prefix='Research Ž ') as temp:
            root = Path(temp) / 'Дослідження'
            (root / 'scripts').mkdir(parents=True)
            shutil.copyfile(SCRIPT, root / 'scripts' / SCRIPT.name)
            # The standalone runner reuses these exact stdlib-only helpers;
            # it never imports a source carr_library over an installed package.
            owners = root / 'src/carr_library/audio'
            owners.mkdir(parents=True)
            for name in ('capture_process.py', 'capture_types.py', 'cleanup.py', 'windows_job.py'):
                shutil.copyfile(SCRIPT.parents[1] / 'src/carr_library/audio' / name,
                                owners / name)
            fixture = root / fixture_path
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(source, encoding='utf-8')
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
                          types.SimpleNamespace(TemporaryDirectory=report_directory,
                                                TemporaryFile=tempfile.TemporaryFile)),
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

    def _assert_real_owned_suite_family(self, *, timed_out):
        spec = importlib.util.spec_from_file_location('reviewed_owned_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        owner = runner._process_owner()
        original_start = owner.start_owned_process
        original_temp = tempfile.TemporaryDirectory
        processes, directories, observed = [], [], {}
        kernel, libc, previous_subreaper = None, None, None
        if os.name == 'nt':
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
        elif sys.platform.startswith('linux'):
            libc = ctypes.CDLL(None, use_errno=True)
            previous_subreaper = ctypes.c_int()
            self.assertEqual(libc.prctl(37, ctypes.byref(previous_subreaper), 0, 0, 0), 0)
        with original_temp(prefix='CARR owned suite Україна ') as temporary:
            root = Path(temporary).resolve()
            marker = root / 'descendant.pid'
            (root / 'descendant.py').write_text(
                "from pathlib import Path\nimport os,sys,time\n"
                "held = open(sys.argv[1], 'wb')\n"
                "Path(sys.argv[2]).write_text(str(os.getpid()), encoding='ascii')\n"
                "while True: time.sleep(1)\n", encoding='utf-8')
            wait = ("    while True: time.sleep(1)\n" if timed_out else
                    "    while not (Path.cwd()/'descendant.pid').exists(): time.sleep(.01)\n")
            (root / 'test_owned.py').write_text(
                "from pathlib import Path\nimport subprocess,sys,time\n"
                "def test_worker(tmp_path):\n"
                "    subprocess.Popen([sys.executable, str(Path.cwd()/'descendant.py'),\n"
                "        str(tmp_path/'held-library.lock'), str(Path.cwd()/'descendant.pid')],\n"
                "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                + wait, encoding='utf-8')
            (root / 'test_ok.py').write_text('def test_ok(): assert True\n', encoding='utf-8')

            def observe_started_descendant(command, **kwargs):
                process = original_start(command, **kwargs)
                processes.append(process)
                try:
                    if Path(command[-1]).name != 'test_owned.py':
                        return process
                    deadline = time.monotonic() + 30
                    while not marker.exists():
                        self.assertIsNone(process.poll(), 'Pytest exited before its descendant started')
                        self.assertLess(time.monotonic(), deadline, 'Synthetic descendant did not start')
                        time.sleep(.01)
                    pid = int(marker.read_text(encoding='ascii'))
                    observed['pid'] = pid
                    if kernel is not None:
                        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE only
                        self.assertTrue(handle, ctypes.get_last_error())
                        observed['handle'] = handle
                        self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                    else:
                        self.assertEqual(os.getpgid(pid), process.pid)
                        self.assertEqual(os.getpgid(process.pid), process.pid)
                    return process
                except BaseException:
                    owner.stop_owned_process(process)
                    raise

            def tracked_temporary(*args, **kwargs):
                directory = original_temp(*args, dir=root, **kwargs)
                directories.append(Path(directory.name))
                return directory

            real_run_suite = runner.run_suite

            def with_test_deadline(suite, **kwargs):
                if suite == 'test_owned.py' and timed_out:
                    # The observer acknowledges a live real descendant before
                    # this wait starts; cancellation cannot preempt admission.
                    with mock.patch.object(runner, 'SUITE_TIMEOUT_SECONDS', .15):
                        return real_run_suite(suite, **kwargs)
                return real_run_suite(suite, **kwargs)

            observed_owner = types.SimpleNamespace(
                start_owned_process=observe_started_descendant,
                stop_owned_process=owner.stop_owned_process)
            output = io.StringIO()
            try:
                if previous_subreaper is not None:
                    self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
                with (mock.patch.object(runner, 'ROOT', root),
                      mock.patch.object(runner, '_process_owner', return_value=observed_owner),
                      mock.patch.object(runner, 'tempfile',
                          types.SimpleNamespace(TemporaryDirectory=tracked_temporary,
                                                TemporaryFile=tempfile.TemporaryFile)),
                      mock.patch.object(runner, 'run_suite', side_effect=with_test_deadline),
                      mock.patch.object(sys, 'argv', ['ci_check.py', 'test_owned.py', 'test_ok.py']),
                      contextlib.redirect_stdout(output)):
                    result = runner.main()
                self.assertEqual(result, 1 if timed_out else 0, output.getvalue())
                if timed_out:
                    self.assertIn('TimeoutExpired', output.getvalue())
                    self.assertIn('Required suites complete: 2; passed=1; failed=1', output.getvalue())
                else:
                    self.assertIn('Required suites complete: 2; passed=2; failed=0', output.getvalue())
                self.assertEqual(len(directories), 2)
                self.assertTrue(all(not directory.exists() for directory in directories))
                self.assertTrue(all(process.poll() is not None for process in processes))
                self.assertTrue(all(process._carr_family_quiescent for process in processes))
                if kernel is not None:
                    # Cleanup promised quiescence before returning, not eventual
                    # disappearance after the report/library has been removed.
                    self.assertEqual(kernel.WaitForSingleObject(observed['handle'], 0), 0)
                else:
                    self.assertTrue(owner._posix_group_quiescent(processes[0].pid))
                    if previous_subreaper is not None:
                        pid, status = os.waitpid(observed['pid'], 0)
                        observed['reaped'] = True
                        self.assertEqual(pid, observed['pid'])
                        self.assertTrue(os.WIFSIGNALED(status))
            finally:
                # Use the same private-family owners on assertion failure too.
                for process in processes:
                    owner.stop_owned_process(process)
                if kernel is not None and observed.get('handle'):
                    kernel.CloseHandle(observed['handle'])
                if previous_subreaper is not None:
                    try:
                        if observed.get('pid') and not observed.get('reaped'):
                            os.waitpid(observed['pid'], 0)
                    finally:
                        libc.prctl(36, previous_subreaper.value, 0, 0, 0)

    def test_timeout_terminates_real_descendant_before_temp_cleanup_and_stays_failed(self):
        self._assert_real_owned_suite_family(timed_out=True)

    def test_completed_pytest_reaps_its_actual_remaining_descendant_before_report_cleanup(self):
        self._assert_real_owned_suite_family(timed_out=False)

    def test_numeric_loopback_requires_the_explicit_fixture_path(self):
        result = self.run_fixture('def test_ok(): assert True\n',
                                  '--allow-loopback', 'test_sample.py')
        self.assertEqual(result.returncode, 1)
        self.assertIn('limited to the two explicit local planning/translation HTTP fixtures', result.stdout)

    def test_inherited_loopback_environment_does_not_change_default_offline_policy(self):
        source = """import socket,pytest
def test_offline():
    with pytest.raises(OSError, match='disabled during offline verification'):
        socket.create_connection(('127.0.0.1', 1))
"""
        result = self.run_fixture(source, 'test_sample.py', extra_env={'CARR_CI_ALLOW_LOOPBACK': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_numeric_loopback_uses_real_tcp_but_refuses_external_names_and_bindings(self):
        source = """import socket,pytest
def test_local_only():
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2) as client:
            with server.accept()[0] as accepted:
                client.sendall(b'fixture')
                assert accepted.recv(7) == b'fixture'
    for denied in (
            lambda: socket.getaddrinfo('example.invalid', 443),
            lambda: socket.getaddrinfo('localhost', 80),
            lambda: socket.gethostbyname('127.0.0.1'),
            lambda: socket.socket().bind(('0.0.0.0', 0)),
            lambda: socket.create_connection(('192.0.2.1', 80), timeout=.1)):
        with pytest.raises(OSError, match='disabled during offline verification'):
            denied()
"""
        for path in ('tests/loopback/test_local_planning.py',
                     'tests/loopback/test_translation_local_adapter.py'):
            with self.subTest(path=path):
                result = self.run_fixture(source, '--allow-loopback', path, fixture_path=path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


    def test_owned_runner_accepts_probe_directory_and_explicit_timeout(self):
        spec = importlib.util.spec_from_file_location('explicit_probe_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        owner = runner._process_owner()
        observed, processes = {}, []
        with tempfile.TemporaryDirectory(prefix='CARR probe Україна ') as temporary:
            root = Path(temporary).resolve()
            marker = root / 'actual-working-directory.txt'
            def started(command, **options):
                observed['cwd'] = options['cwd']
                process = owner.start_owned_process(command, **options)
                processes.append(process)
                def wait(*, timeout):
                    observed['timeout'] = timeout
                    return process.wait(timeout=timeout)
                return types.SimpleNamespace(process=process, wait=wait)
            def stopped(wrapper):
                owner.stop_owned_process(wrapper.process)
            instrumented = types.SimpleNamespace(start_owned_process=started,
                                                  stop_owned_process=stopped)
            command = [sys.executable, '-I', '-B', '-c',
                       'from pathlib import Path; import sys; '
                       'Path(sys.argv[1]).write_text(str(Path.cwd().resolve()), encoding="utf-8")',
                       str(marker)]
            original_deadline = runner.SUITE_TIMEOUT_SECONDS
            with mock.patch.object(runner, '_process_owner', return_value=instrumented):
                result = runner._run_test_process(command, env=dict(os.environ),
                                                  cwd=root, timeout=20)
            self.assertEqual(result, 0)
            self.assertEqual(marker.read_text(encoding='utf-8'), str(root))
            self.assertEqual(observed['cwd'], root)
            self.assertGreater(observed['timeout'], 0)
            self.assertLessEqual(observed['timeout'], runner.PROCESS_LOG_POLL_SECONDS)
            self.assertEqual(runner.SUITE_TIMEOUT_SECONDS, original_deadline)
            self.assertEqual(len(processes), 1)
            self.assertTrue(processes[0]._carr_family_quiescent)



    def _actual_pipe_parent(self, mode):
        """The outer real child has PIPE handles, as pwsh Tee/GitHub do."""
        spec = importlib.util.spec_from_file_location('spool_parent_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        owner = runner._process_owner()
        program = r"""
import importlib.util, json, os, stat, subprocess, sys, time, types
from pathlib import Path
spec = importlib.util.spec_from_file_location('actual_pipe_ci_check', sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
root, mode = Path(sys.argv[2]).resolve(), sys.argv[3]
assert not stat.S_ISREG(os.fstat(1).st_mode)
assert not stat.S_ISREG(os.fstat(2).st_mode)
actual = runner._process_owner()
marker = root / 'worker-ready'
spools, children, relays = [], [], []
quiet = [False]
if mode.startswith('close-'):
    create_spool = runner.tempfile.TemporaryFile
    class CloseFailure:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def close(self):
            self.stream.close()
            raise PermissionError('fixture after actual spool close')
    runner.tempfile = types.SimpleNamespace(
        TemporaryFile=lambda *args, **kwargs: CloseFailure(create_spool(*args, **kwargs)))
child = root / 'worker.py'
child.write_text(
    "import os,stat,sys,time\nfrom pathlib import Path\n"
    "assert stat.S_ISREG(os.fstat(1).st_mode)\n"
    "assert stat.S_ISREG(os.fstat(2).st_mode)\n"
    "Path(sys.argv[1]).write_text('ready', encoding='ascii')\n"
    "mode = sys.argv[2]\n"
    "if mode in ('success', 'close-success'):\n"
    "    os.write(1, b'owned stdout\\n'); os.write(2, b'owned stderr\\n')\n"
    "elif mode.startswith('grow-'):\n"
    "    descriptor = 1 if mode == 'grow-stdout' else 2\n"
    "    while True:\n"
    "        os.write(descriptor, b'x' * 64); time.sleep(.001)\n"
    "else:\n"
    "    while True: time.sleep(.05)\n",
    encoding='utf-8')

def started(command, **options):
    selected = (options['stdout'], options['stderr'])
    assert all(stat.S_ISREG(os.fstat(item.fileno()).st_mode) for item in selected)
    spools.extend(selected)
    process = actual.start_owned_process(command, **options)
    children.append(process)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.005)
    except BaseException:
        actual.stop_owned_process(process)
        raise
    if mode in ('keyboard', 'exit', 'close-keyboard', 'close-exit'):
        def interrupted(*, timeout):
            if mode in ('keyboard', 'close-keyboard'):
                raise KeyboardInterrupt('fixture interrupted wait')
            raise SystemExit(73)
        return types.SimpleNamespace(real=process, wait=interrupted)
    return process

def stopped(process):
    real = getattr(process, 'real', process)
    assert all(not item.closed for item in spools)
    actual.stop_owned_process(real)
    assert real._carr_family_quiescent
    assert all(not item.closed for item in spools)
    quiet[0] = True

relay = runner._relay_process_log
def relayed(stream, descriptor):
    assert quiet[0], 'Output was read before the owned family settled.'
    assert not stream.closed
    relays.append(descriptor)
    return relay(stream, descriptor)

runner._process_owner = lambda: types.SimpleNamespace(
    start_owned_process=started, stop_owned_process=stopped)
runner._relay_process_log = relayed
runner.PROCESS_LOG_LIMIT_BYTES = 512
runner.PROCESS_LOG_POLL_SECONDS = .01
expected_error = {'grow-stdout': RuntimeError, 'grow-stderr': RuntimeError,
                  'deadline': subprocess.TimeoutExpired,
                  'keyboard': KeyboardInterrupt, 'exit': SystemExit,
                  'close-deadline': subprocess.TimeoutExpired,
                  'close-keyboard': KeyboardInterrupt, 'close-exit': SystemExit,
                  'close-success': PermissionError}.get(mode)
try:
    try:
        result = runner._run_test_process(
            [sys.executable, '-I', '-B', str(child), str(marker), mode],
            env=dict(os.environ), cwd=root, timeout=.15 if mode in ('deadline', 'close-deadline') else 10)
    except BaseException as error:
        assert expected_error is not None and isinstance(error, expected_error), repr(error)
        if mode.startswith('grow-'):
            assert mode[5:] in str(error) and 'bounded output' in str(error)
        if mode in ('deadline', 'close-deadline'):
            assert error.timeout == .15
        if mode in ('exit', 'close-exit'):
            assert error.code == 73
        if mode.startswith('close-') and mode != 'close-success':
            assert any('log close also failed' in note for note in error.__notes__)
    else:
        assert expected_error is None and result == 0
    assert quiet[0] and len(children) == 1
    assert all(item.closed for item in spools)
    assert children[0].poll() is not None and children[0]._carr_family_quiescent
    assert relays == ([] if mode in ('keyboard', 'exit', 'close-keyboard', 'close-exit') else [1, 2])
    print('\nSPOOL_RESULT=' + json.dumps({'mode': mode, 'quiet': True, 'closed': True}))
finally:
    for process in children:
        actual.stop_owned_process(process)
"""
        with tempfile.TemporaryDirectory(prefix='CARR pipe parent Україна ') as temporary:
            try:
                result = owner.run_process(
                    [sys.executable, '-I', '-B', '-c', program, str(SCRIPT),
                     str(Path(temporary).resolve()), mode],
                    env=dict(os.environ), cwd=temporary, owned_tree=True,
                    allow_failure=True,
                    budget=owner.AudioBudget(timeout=45, max_log_bytes=1024 * 1024))
            except BaseException as error:
                if getattr(error, 'cleanup_owner', None) is not None:
                    owner.wait_for_cleanup(error)
                raise
            self.assertEqual(result.returncode, 0,
                             result.stdout.decode('utf-8', 'backslashreplace') +
                             result.stderr.decode('utf-8', 'backslashreplace'))
            self.assertIn(b'SPOOL_RESULT=', result.stdout)
            self.assertLess(len(result.stdout) + len(result.stderr), 4096)
            return result

    def test_regular_spools_work_under_actual_pipe_parent_and_relay_only_after_quiescence(self):
        result = self._actual_pipe_parent('success')
        self.assertIn(b'owned stdout\n', result.stdout)
        self.assertIn(b'owned stderr\n', result.stderr)

    def test_actual_live_stdout_and_stderr_growth_fail_before_owned_spools_close(self):
        for mode in ('grow-stdout', 'grow-stderr'):
            with self.subTest(mode=mode):
                self._actual_pipe_parent(mode)

    def test_explicit_spooled_child_deadline_preserves_timeout_after_real_cleanup(self):
        self._actual_pipe_parent('deadline')

    def test_control_signals_propagate_only_after_real_spooled_child_quiescence(self):
        for mode in ('keyboard', 'exit'):
            with self.subTest(mode=mode):
                self._actual_pipe_parent(mode)



    def test_actual_spool_close_errors_preserve_timeout_and_control_signals(self):
        for mode in ('close-deadline', 'close-keyboard', 'close-exit'):
            with self.subTest(mode=mode):
                self._actual_pipe_parent(mode)

    def test_actual_spool_close_error_cannot_turn_success_into_a_pass(self):
        self._actual_pipe_parent('close-success')


class InstalledIdentityTests(unittest.TestCase):
    def setUp(self):
        import runpy
        self.temporary = tempfile.TemporaryDirectory(prefix='CARR identity Україна ')
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.checkout, self.installed = root / 'checkout', root / 'installed'
        self.checkout.mkdir()
        self.selected = {'carr_library': self.installed / 'carr_library',
                         'bootstrap': self.installed / 'bootstrap'}
        (self.checkout / 'pyproject.toml').write_text(
            '[tool.setuptools]\npackages = ["carr_library", "carr_library.ingest", '
            '"bootstrap", "bootstrap.vendor"]\n', encoding='utf-8')
        modules = {'src/carr_library/__init__.py': '',
                   'src/carr_library/ingest/__init__.py': '',
                   'src/carr_library/ingest/actual.py': 'VALUE = 7\n',
                   'bootstrap/__init__.py': '',
                   'bootstrap/vendor/__init__.py': ''}
        for name, content in modules.items():
            source = self.checkout / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(content, encoding='utf-8')
            relative = name.removeprefix('src/')
            target = self.installed / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        probe = self.checkout / 'bootstrap/semantic_compat/probe.py'
        probe.parent.mkdir()
        probe.write_text('raise AssertionError("developer probe is not shipped")\n', encoding='utf-8')
        namespace = {}
        exec(runpy.run_path(str(SCRIPT))['INSTALLED_IDENTITY'], namespace)
        self.verify = namespace['verify_packaged_python']

    def test_declared_wheel_python_excludes_unshipped_developer_probe(self):
        self.assertEqual(self.verify(self.checkout, self.selected), 5)

    def test_changed_shipped_python_is_rejected(self):
        path = self.selected['carr_library'] / 'ingest/actual.py'
        path.write_text('VALUE = 8\n', encoding='utf-8')
        with self.assertRaisesRegex(SystemExit, 'differs from the tested source'):
            self.verify(self.checkout, self.selected)

    def test_missing_shipped_python_is_rejected(self):
        (self.selected['carr_library'] / 'ingest/actual.py').unlink()
        with self.assertRaisesRegex(SystemExit, 'inventory differs'):
            self.verify(self.checkout, self.selected)

    def test_unexpected_installed_python_is_rejected(self):
        extra = self.selected['bootstrap'] / 'semantic_compat/probe.py'
        extra.parent.mkdir()
        extra.write_text('VALUE = 7\n', encoding='utf-8')
        with self.assertRaisesRegex(SystemExit, 'unexpected='):
            self.verify(self.checkout, self.selected)

    def test_source_shadowing_is_rejected(self):
        self.selected['carr_library'] = self.checkout / 'src/carr_library'
        with self.assertRaisesRegex(SystemExit, 'source-shadowed'):
            self.verify(self.checkout, self.selected)

    def test_missing_application_root_cannot_shrink_installed_verification(self):
        del self.selected['bootstrap']
        with self.assertRaisesRegex(SystemExit, 'both application package roots'):
            self.verify(self.checkout, self.selected)


    def _sync_probe_child(self, *, change_installed=False):
        import runpy
        import xml.etree.ElementTree as ET
        namespace = runpy.run_path(str(SCRIPT.with_name('installed_sync_probe.py')))
        runner = runpy.run_path(str(SCRIPT))['PYTEST_CHILD']
        target = self.checkout.parent / 'isolated-sync-Україна'
        (target / 'tests').mkdir(parents=True)
        (target / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
        (target / 'tests/test_origin.py').write_text(
            "import socket, pytest\n"
            "def test_actual_installed_offline_contract():\n"
            "    with pytest.raises(OSError, match='disabled during offline verification'):\n"
            "        socket.create_connection(('example.invalid', 443))\n",
            encoding='utf-8')
        if change_installed:
            (self.selected['carr_library'] / 'ingest/actual.py').write_text('VALUE = 8\n', encoding='utf-8')
        report = target / 'results.xml'
        # Only these explicit synthetic installed roots are placed before the
        # real child policy. Source roots remain distinct and fully hashed by
        # the unchanged INSTALLED_IDENTITY code before pytest can run.
        runner = 'import sys\nsys.path.insert(0, ' + repr(str(self.installed)) + ')\n' + runner
        command = namespace['_command'](self.checkout, target, report, runner)
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in ('PYTHONPATH', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'CARR_CI_ALLOW_LOOPBACK')}
        env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        result = subprocess.run(command, cwd=target, env=env, capture_output=True,
                                text=True, encoding='utf-8', errors='backslashreplace',
                                timeout=60, check=False)
        cases = list(ET.parse(report).iter('testcase')) if report.is_file() else []
        return result, cases

    def test_installed_sync_caller_runs_real_origin_and_offline_child_contract(self):
        result, cases = self._sync_probe_child()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('isolated installed package bytes match this exact source checkout', result.stdout)
        self.assertEqual(len(cases), 1)
        self.assertFalse(any(case.find(tag) is not None
                             for case in cases for tag in ('failure', 'error', 'skipped')))

    def test_installed_sync_caller_cannot_bypass_changed_installed_bytes(self):
        result, cases = self._sync_probe_child(change_installed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('differs from the tested source', result.stdout + result.stderr)
        self.assertEqual(cases, [])


if __name__ == '__main__':
    unittest.main()
