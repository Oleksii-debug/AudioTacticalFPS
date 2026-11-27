from pathlib import Path
import contextlib
import ctypes
import io
import importlib.util
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import threading
from unittest import mock
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'ci_check.py'
AUDIO_SCRIPT = SCRIPT.with_name('ci_audio_check.py')
WATCHDOG_SCRIPT = SCRIPT.with_name('ci_watchdog.py')
WINDOWS_OWNER = SCRIPT.parents[1] / 'src' / 'carr_library' / 'audio' / 'windows_process.py'


class CIEntrypointTests(unittest.TestCase):
    def run_fixture(self, source, *suites, extra_env=None, fixture_path='test_sample.py'):
        with tempfile.TemporaryDirectory(prefix='Research Ž ') as temp:
            root = Path(temp) / 'Дослідження'
            (root / 'scripts').mkdir(parents=True)
            shutil.copyfile(SCRIPT, root / 'scripts' / SCRIPT.name)
            shutil.copyfile(WATCHDOG_SCRIPT, root / 'scripts' / WATCHDOG_SCRIPT.name)
            owner = root / 'src' / 'carr_library' / 'audio' / 'windows_process.py'
            owner.parent.mkdir(parents=True)
            shutil.copyfile(WINDOWS_OWNER, owner)
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

    def test_normal_fatal_handler_stays_enabled_without_scheduled_tracebacks(self):
        source = '''import faulthandler
def test_diagnostics(request):
    assert faulthandler.is_enabled()
    assert float(request.config.getini('faulthandler_timeout')) == 0
'''
        result = self.run_fixture(source, 'test_sample.py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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


    def test_numeric_loopback_requires_the_explicit_fixture_path(self):
        result = self.run_fixture('def test_ok(): assert True\n',
                                  '--allow-loopback', 'test_sample.py')
        self.assertEqual(result.returncode, 1)
        self.assertIn('limited to the explicit local-planning HTTP fixture', result.stdout)

    def test_numeric_loopback_rejects_directory_and_lexical_aliases(self):
        path = 'tests/loopback/test_local_planning.py'
        for selector in ('tests/loopback', './' + path, 'tests/loopback/../loopback/test_local_planning.py'):
            with self.subTest(selector=selector):
                result = self.run_fixture('def test_ok(): assert True\n',
                    '--allow-loopback', selector, fixture_path=path)
                self.assertEqual(result.returncode, 1)
                self.assertIn('limited to the explicit local-planning HTTP fixture', result.stdout)

    def test_inherited_loopback_environment_does_not_change_default_offline_policy(self):
        source = """import os,socket,pytest
def test_offline():
    assert 'CARR_CI_ALLOW_LOOPBACK' not in os.environ
    with pytest.raises(OSError, match='disabled during offline verification'):
        socket.create_connection(('127.0.0.1', 1))
"""
        result = self.run_fixture(source, 'test_sample.py', extra_env={'CARR_CI_ALLOW_LOOPBACK': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_numeric_loopback_uses_real_tcp_but_refuses_external_names_and_bindings(self):
        source = """import faulthandler,socket,pytest
def test_local_only(request):
    assert faulthandler.is_enabled()
    assert float(request.config.getini('faulthandler_timeout')) == 0
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2) as client:
            with server.accept()[0] as accepted:
                client.sendall(b'fixture')
                assert accepted.recv(7) == b'fixture'
    with socket.socket() as stream, socket.socket(type=socket.SOCK_DGRAM) as datagram:
        for denied in (
                lambda: socket.getaddrinfo('example.invalid', 443),
                lambda: socket.getaddrinfo('localhost', 80),
                lambda: socket.gethostbyname('127.0.0.1'),
                lambda: stream.bind(('0.0.0.0', 0)),
                lambda: stream.connect(('192.0.2.1', 80)),
                lambda: datagram.bind(('127.0.0.1', 0))):
            with pytest.raises(OSError, match='disabled during offline verification'):
                denied()
"""
        path = 'tests/loopback/test_local_planning.py'
        result = self.run_fixture(source, '--allow-loopback', path, fixture_path=path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CASE_START ', result.stdout)


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

            def completed_child(command, *, env, timeout):
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
            # Windows TEMP may use an 8.3 alias; the runner resolves candidates
            # before checking that they remain inside its canonical root.
            root = Path(temporary).resolve()
            marker = root / 'descendant.pid'
            (root / 'descendant.py').write_text(
                "from pathlib import Path\nimport os,sys,time\n"
                "held = open(sys.argv[1], 'wb')\n"
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
                process = original_start(*args, **kwargs)
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



    def test_posix_settlement_waits_for_owned_descendant_confirmation(self):
        spec = importlib.util.spec_from_file_location('settlement_ci_check', SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        events = []
        process = types.SimpleNamespace(pid=12345, wait=lambda **options: events.append('leader-exited'))
        checks = iter((False, True))
        def quiescent(group):
            self.assertEqual(group, process.pid)
            answer = next(checks)
            events.append(('family-empty', answer))
            return answer
        # A completed leader is intentionally not sufficient evidence.
        with (mock.patch.object(runner, 'os', types.SimpleNamespace(name='posix',
                    killpg=lambda group, signal: events.append(('kill-group', group)))),
              mock.patch.object(runner, '_posix_group_quiescent', side_effect=quiescent),
              mock.patch.object(runner, 'sleep', side_effect=lambda delay: events.append(('wait', delay)))):
            runner._settle_test_process(process)
        self.assertEqual(events, [('kill-group', 12345), 'leader-exited',
                                  ('family-empty', False), ('wait', .02), ('family-empty', True)])


class RelocatedCIChildTests(unittest.TestCase):
    """The installed replay reuses the exact child policy from another cwd."""
    def run_relocated_child(self, *, missing_helper=False):
        with tempfile.TemporaryDirectory(prefix='carr-isolated-child-') as temporary:
            root = Path(temporary)
            trusted = root / 'Перевірений checkout'
            scripts = trusted / 'scripts'
            scripts.mkdir(parents=True)
            script = scripts / SCRIPT.name
            helper = scripts / WATCHDOG_SCRIPT.name
            shutil.copyfile(SCRIPT, script)
            shutil.copyfile(WATCHDOG_SCRIPT, helper)
            child = runpy.run_path(str(script))['PYTEST_CHILD']
            replay = root / 'installed-replay'
            (replay / 'scripts').mkdir(parents=True)
            (replay / 'scripts' / WATCHDOG_SCRIPT.name).write_text(
                "raise AssertionError('Untrusted cwd watchdog must not execute')\n",
                encoding='utf-8')
            (replay / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
            sample = replay / 'test_installed_replay.py'
            sample.write_text(f'''import faulthandler
import socket
import sys
import pytest

def test_installed_replay(request):
    assert {str(trusted)!r} not in sys.path
    assert {str(scripts)!r} not in sys.path
    assert faulthandler.is_enabled()
    assert float(request.config.getini('faulthandler_timeout')) == 0
    with pytest.raises(OSError, match='disabled during offline verification'):
        socket.getaddrinfo('example.invalid', 443)
''', encoding='utf-8')
            if missing_helper:
                helper.unlink()
            environment = {key: value for key, value in os.environ.items()
                           if key not in ('PYTHONPATH', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS')}
            environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
            report = replay / 'results.xml'
            result = subprocess.run([sys.executable, '-I', '-B', '-c', child,
                '-c', str(replay / 'pytest.ini'), '-q', '-o', 'addopts=',
                '-o', 'faulthandler_timeout=90', '--junitxml', str(report),
                '--', str(sample)], cwd=replay, env=environment,
                capture_output=True, text=True, timeout=60)
            return result, report.exists()

    def test_isolated_replay_loads_exact_sibling_helper_without_checkout_imports(self):
        result, reported = self.run_relocated_child()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(reported)
        self.assertIn('CASE_START ', result.stdout)
        self.assertIn('1 passed', result.stdout)
        self.assertNotIn('Untrusted cwd watchdog', result.stdout + result.stderr)

    def test_missing_exact_helper_cannot_fall_back_to_working_directory(self):
        result, reported = self.run_relocated_child(missing_helper=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(reported)
        self.assertIn('FileNotFoundError', result.stderr)
        self.assertIn('ci_watchdog.py', result.stderr)
        self.assertNotIn('Untrusted cwd watchdog', result.stdout + result.stderr)


class AudioCIEntrypointTests(unittest.TestCase):
    """Real wrapper subprocesses over synthetic cases, not native/PCM evidence."""

    def run_fixture(self, source, *arguments, extra_env=None, relative='test_sample.py', configured=''):
        with tempfile.TemporaryDirectory(prefix='carr-audio-gate-') as temp:
            root = Path(temp)
            (root / 'scripts').mkdir()
            script = root / 'scripts' / AUDIO_SCRIPT.name
            shutil.copyfile(AUDIO_SCRIPT, script)
            shutil.copyfile(WATCHDOG_SCRIPT, root / 'scripts' / WATCHDOG_SCRIPT.name)
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding='utf-8')
            if configured:
                (root / 'pytest.ini').write_text('[pytest]\naddopts=' + configured + '\n', encoding='utf-8')
            env = dict(os.environ, CARR_LONG_AUDIO_TESTS='0')
            env.update(extra_env or {})
            return subprocess.run([sys.executable, str(script), '-q', relative, *arguments],
                cwd=root, env=env, capture_output=True, text=True, timeout=60)

    def test_normal_fatal_handler_stays_enabled_and_old_dump_option_cannot_win(self):
        source = '''import faulthandler
def test_diagnostics(request):
    assert faulthandler.is_enabled()
    assert float(request.config.getini('faulthandler_timeout')) == 0
'''
        result = self.run_fixture(source, '-o', 'faulthandler_timeout=.001')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_progress_and_deliberate_optional_skip_are_preserved(self):
        result = self.run_fixture('import pytest\ndef test_ok(): pass\n'
                                 '@pytest.mark.skip(reason="optional engine")\ndef test_optional(): pass\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CASE_START', result.stdout)
        self.assertIn('CASE_END', result.stdout)
        self.assertIn('1 passed, 1 skipped', result.stdout)

    def test_inherited_addopts_cannot_turn_failure_into_collection_success(self):
        result = self.run_fixture('def test_bad(): assert False\n',
                                 extra_env={'PYTEST_ADDOPTS': '--collect-only'})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('1 failed', result.stdout)

    def test_configured_addopts_cannot_replace_execution(self):
        result = self.run_fixture('def test_bad(): assert False\n', configured='--collect-only')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('1 failed', result.stdout)

    def test_inherited_plugin_selection_is_not_loaded(self):
        result = self.run_fixture('def test_ok(): pass\n',
                                 extra_env={'PYTEST_PLUGINS': 'missing_synthetic_test_plugin'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_xfail_and_explicit_nonstrict_xpass_cannot_be_green(self):
        for outcome in ('False', 'True'):
            with self.subTest(outcome=outcome):
                result = self.run_fixture('import pytest\ndef test_ok(): pass\n'
                    '@pytest.mark.xfail(reason="unfinished", strict=False)\n'
                    f'def test_expected(): assert {outcome}\n')
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('XFAIL or XPASS', result.stdout)

    def test_inactive_xfail_marker_allows_a_real_pass(self):
        result = self.run_fixture('import pytest\n@pytest.mark.xfail(False, reason="inactive")\n'
                                 'def test_ok(): pass\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_collection_only_cannot_claim_execution(self):
        result = self.run_fixture('def test_ok(): pass\n', '--collect-only')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('did not execute a passing test', result.stdout)

    def long_fixture(self, hours=(1, 3, 10), body='pass', *arguments):
        source = (f'import pytest\n@pytest.mark.parametrize("hours", {list(hours)!r})\n'
                  f'def test_exact_multi_hour_streamed_pcm(hours):\n    {body}\n')
        return self.run_fixture(source, *arguments,
            relative='tests/audio/test_long_streaming_integrity.py',
            extra_env={'CARR_LONG_AUDIO_TESTS': '1'})

    def test_all_enabled_requested_long_cases_must_pass(self):
        result = self.long_fixture()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_enabled_long_case_is_not_hidden_by_other_passes(self):
        result = self.long_fixture(hours=(1, 3))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('test_exact_multi_hour_streamed_pcm[10]', result.stdout)

    def test_skipped_enabled_long_case_is_not_hidden_by_other_passes(self):
        result = self.long_fixture(body='if hours == 3: pytest.skip("missing prerequisite")')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('requested native/long-PCM cases did not pass:', result.stdout)
        self.assertIn('test_exact_multi_hour_streamed_pcm[3]', result.stdout)

    def test_explicit_deselection_cannot_hide_requested_long_case(self):
        result = self.long_fixture((1, 3, 10), 'pass', '-k', 'not 10')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('test_exact_multi_hour_streamed_pcm[10]', result.stdout)

    def test_required_selection_binds_native_paths_and_exact_parameter_cases(self):
        spec = importlib.util.spec_from_file_location('audio_gate_probe', AUDIO_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        native = 'tests/audio/test_windows_process.py'
        self.assertEqual(module.required_cases(['tests/audio'], platform='win32', long_audio=True),
                         module.WINDOWS_CASES | module.LONG_PCM_CASES)
        self.assertEqual(module.required_cases(['tests/audio'], platform='linux', long_audio=False), set())
        expected = {case for case in module.WINDOWS_CASES if case.startswith(native + '::')}
        self.assertEqual(module.required_cases([str(module.ROOT / native)], platform='win32', long_audio=False), expected)
        case = native + '::test_windows_cancellation_stops_descendant_after_its_helper_exits[run]'
        self.assertEqual(module.required_cases([case], platform='win32', long_audio=False), {case})
        self.assertEqual(module.required_cases(['test_sample.py'], platform='win32', long_audio=True), set())



class CaseWatchdogTests(unittest.TestCase):
    def watchdog_module(self):
        spec = importlib.util.spec_from_file_location('case_watchdog_probe', WATCHDOG_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_real_short_timer_observes_an_active_child_without_private_data(self):
        module = self.watchdog_module()
        child = subprocess.Popen(
            [sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        received = threading.Event()
        messages = []
        private_local = 'PRIVATE_LOCAL_CONTENT'
        def emit(text):
            messages.append((child.poll(), text))
            received.set()
        watchdog = module.CaseWatchdog(timeout=.02, emit=emit)
        try:
            token = watchdog.start('test_synthetic.py::test_child[PRIVATE_PARAMETER_CONTENT]')
            self.assertTrue(received.wait(5), 'The real timer did not deliver a diagnostic.')
            self.assertIsNone(messages[0][0], 'The child must still be active when diagnosed.')
            text = messages[0][1]
            self.assertIn('CASE_DIAGNOSTIC', text)
            self.assertIn('test_synthetic.py::test_child', text)
            self.assertIn('Thread main:', text)
            self.assertNotIn('PRIVATE_PARAMETER_CONTENT', text)
            self.assertNotIn(private_local, text)
            self.assertLessEqual(len(text.encode('ascii')), module.MAX_DIAGNOSTIC_BYTES)
            self.assertTrue(watchdog.finish(token))
        finally:
            watchdog.close()
            try:
                child.communicate(input=b'x', timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0)

    def test_completed_case_cancels_its_real_timer(self):
        module = self.watchdog_module()
        emitted = threading.Event()
        watchdog = module.CaseWatchdog(timeout=.15, emit=lambda text: emitted.set())
        try:
            token = watchdog.start('test_synthetic.py::test_complete')
            self.assertTrue(watchdog.finish(token))
            self.assertFalse(emitted.wait(.25))
        finally:
            watchdog.close()

    def test_stale_snapshot_and_old_finish_cannot_affect_the_new_case(self):
        module = self.watchdog_module()
        entered, release, received = threading.Event(), threading.Event(), threading.Event()
        messages = []
        original = module._thread_snapshot
        def snapshot(label, timeout):
            if label.endswith('test_old'):
                entered.set()
                release.wait(5)
            return original(label, timeout)
        def emit(text):
            messages.append(text)
            received.set()
        watchdog = module.CaseWatchdog(timeout=.02, emit=emit)
        with patch.object(module, '_thread_snapshot', snapshot):
            old_thread = None
            try:
                old = watchdog.start('test_synthetic.py::test_old')
                old_thread = watchdog._timer
                self.assertTrue(entered.wait(5))
                self.assertTrue(watchdog.finish(old))
                current = watchdog.start('test_synthetic.py::test_current')
                self.assertFalse(watchdog.finish(old))
                release.set()
                self.assertTrue(received.wait(5))
                old_thread.join(5)
                self.assertFalse(old_thread.is_alive())
                self.assertEqual(len(messages), 1)
                self.assertIn('test_current', messages[0])
                self.assertNotIn('test_old', messages[0])
                watchdog.finish(current)
            finally:
                release.set()
                watchdog.close()
                if old_thread is not None:
                    old_thread.join(5)

    def test_session_close_suppresses_an_inflight_snapshot(self):
        module = self.watchdog_module()
        entered, release = threading.Event(), threading.Event()
        messages = []
        def snapshot(label, timeout):
            entered.set()
            release.wait(5)
            return 'CASE_DIAGNOSTIC stale\n'
        watchdog = module.CaseWatchdog(timeout=.02, emit=messages.append)
        with patch.object(module, '_thread_snapshot', snapshot):
            thread = None
            try:
                watchdog.start('test_synthetic.py::test_closing')
                thread = watchdog._timer
                self.assertTrue(entered.wait(5))
                watchdog.close()
                release.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(messages, [])
            finally:
                release.set()
                watchdog.close()
                if thread is not None:
                    thread.join(5)

    def test_diagnostic_capture_and_write_errors_do_not_raise_or_print_data(self):
        module = self.watchdog_module()
        messages = []
        watchdog = module.CaseWatchdog(timeout=30, emit=messages.append)
        try:
            token = watchdog.start('test_synthetic.py::test_error[PRIVATE_PARAMETER_CONTENT]')
            with patch.object(module, '_thread_snapshot',
                              side_effect=RuntimeError('PRIVATE_EXCEPTION_CONTENT')):
                watchdog._fire(token)
            self.assertEqual(len(messages), 1)
            self.assertIn('snapshot unavailable (RuntimeError)', messages[0])
            self.assertNotIn('PRIVATE_', messages[0])
            watchdog.finish(token)
            def failed_write(text):
                raise OSError('synthetic unavailable diagnostic stream')
            watchdog._emit = failed_write
            token = watchdog.start('test_synthetic.py::test_write_error')
            watchdog._fire(token)
            watchdog.finish(token)
        finally:
            watchdog.close()

    def test_disabling_scheduled_dump_keeps_arguments_and_end_of_options(self):
        module = self.watchdog_module()
        original = ['-q', '-o', 'faulthandler_timeout=90', '--',
                    'test_synthetic.py::test_case[exact-parameter]']
        arguments = module.without_scheduled_faulthandler(original)
        self.assertEqual(arguments, original[:3] +
                         ['-o', 'faulthandler_timeout=0'] + original[3:])
        self.assertEqual(original[2], 'faulthandler_timeout=90')


if __name__ == '__main__':
    unittest.main()
