from pathlib import Path
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import threading
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'ci_check.py'
AUDIO_SCRIPT = SCRIPT.with_name('ci_audio_check.py')
WATCHDOG_SCRIPT = SCRIPT.with_name('ci_watchdog.py')


class CIEntrypointTests(unittest.TestCase):
    def run_fixture(self, source, *suites, extra_env=None):
        with tempfile.TemporaryDirectory(prefix='Research Ž ') as temp:
            root = Path(temp) / 'Дослідження'
            (root / 'scripts').mkdir(parents=True)
            shutil.copyfile(SCRIPT, root / 'scripts' / SCRIPT.name)
            shutil.copyfile(WATCHDOG_SCRIPT, root / 'scripts' / WATCHDOG_SCRIPT.name)
            (root / 'test_sample.py').write_text(source, encoding='utf-8')
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
