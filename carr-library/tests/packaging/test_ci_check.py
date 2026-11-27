from pathlib import Path
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import pytest

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


@pytest.fixture
def runner_module(tmp_path):
    spec = importlib.util.spec_from_file_location('_ci_runner_failure_regression', SCRIPT)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.ROOT = tmp_path
    for name in ('test_affected.py', 'test_following.py'):
        (tmp_path / name).write_text('def test_actual_pass(): assert True\n', encoding='utf-8')
    return runner


@pytest.mark.parametrize('jobs', [1, 2])
@pytest.mark.parametrize('timed_out', [False, True], ids=['passing-worker', 'timed-out-worker'])
def test_locked_cleanup_keeps_failure_and_completes_other_required_suites(
        runner_module, tmp_path, monkeypatch, capfd, jobs, timed_out):
    runner = runner_module
    original_run = subprocess.run
    original_temporary = tempfile.TemporaryDirectory
    workspaces = []
    affected = set()
    launched = []

    class LockedWorkspace:
        def __init__(self, *args, **kwargs):
            self.temporary = original_temporary(*args, **kwargs)
            workspaces.append(self.temporary)

        def __enter__(self):
            return self.temporary.__enter__()

        def __exit__(self, *exc):
            if self.temporary.name in affected:
                raise PermissionError(13, 'Simulated WinError 32: academic.sqlite is still in use',
                                      str(Path(self.temporary.name) / 'academic.sqlite'))
            return self.temporary.__exit__(*exc)

    def run_worker(argv, **kwargs):
        suite = Path(argv[-1]).name
        launched.append(suite)
        assert kwargs['timeout'] == 600
        if suite == 'test_affected.py':
            workspace = Path(argv[argv.index('--junitxml') + 1]).parent
            (workspace / 'academic.sqlite').write_bytes(b'synthetic locked database')
            affected.add(str(workspace))
            if timed_out:
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        # The unaffected suite, and the successful-before-cleanup case, execute
        # actual pytest subprocesses and produce their real JUnit reports.
        return original_run(argv, **kwargs)

    monkeypatch.setattr(runner.tempfile, 'TemporaryDirectory', LockedWorkspace)
    monkeypatch.setattr(runner.subprocess, 'run', run_worker)
    monkeypatch.setattr(sys, 'argv', ['ci_check.py', '--jobs', str(jobs),
                                     'test_affected.py', 'test_following.py'])
    try:
        assert runner.main() == 1
        output = capfd.readouterr().out
        assert sorted(launched) == ['test_affected.py', 'test_following.py']
        assert 'PASS: test_following.py: junit_passed=1, failed=0, skipped=0, pytest_exit=0' in output
        assert 'PASS: test_affected.py:' not in output
        verdicts = [line for line in output.splitlines() if line.startswith('FAIL: test_affected.py:')]
        assert len(verdicts) == 1
        assert 'temporary workspace cleanup failed: PermissionError' in verdicts[0]
        if timed_out:
            assert 'timed out after 600s: TimeoutExpired' in verdicts[0]
        else:
            assert 'junit_passed=1, failed=0, skipped=0, pytest_exit=0' in verdicts[0]
        assert 'Required suites complete: 2; passed=1; failed=1' in output
    finally:
        # Only the cleanup boundary is simulated; release the real synthetic
        # workspace after asserting its failed result and the following verdict.
        for temporary in workspaces:
            temporary.cleanup()


@pytest.mark.parametrize('failure', ['timeout', 'missing-report', 'malformed-report', 'launch-error'])
def test_worker_report_failures_are_named_and_do_not_abort_remaining_suites(
        runner_module, monkeypatch, capfd, failure):
    runner = runner_module
    original_run = subprocess.run

    def run_worker(argv, **kwargs):
        if Path(argv[-1]).name == 'test_affected.py':
            assert kwargs['timeout'] == 600
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            if failure == 'launch-error':
                raise OSError('Synthetic worker launch failed')
            if failure == 'malformed-report':
                Path(argv[argv.index('--junitxml') + 1]).write_text('<testsuite>', encoding='utf-8')
            return subprocess.CompletedProcess(argv, 0)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(runner.subprocess, 'run', run_worker)
    monkeypatch.setattr(sys, 'argv', ['ci_check.py', 'test_affected.py', 'test_following.py'])
    assert runner.main() == 1
    output = capfd.readouterr().out
    assert 'FAIL: test_affected.py:' in output
    assert 'PASS: test_following.py: junit_passed=1, failed=0, skipped=0, pytest_exit=0' in output
    assert 'Required suites complete: 2; passed=1; failed=1' in output


if __name__ == '__main__':
    unittest.main()
