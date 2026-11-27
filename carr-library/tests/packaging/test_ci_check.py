from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'ci_check.py'


class CIEntrypointTests(unittest.TestCase):
    def run_fixture(self, source, *suites, extra_env=None, driver=None):
        with tempfile.TemporaryDirectory(prefix='Research Ž ') as temp:
            root = Path(temp) / 'Дослідження'
            (root / 'scripts').mkdir(parents=True)
            shutil.copyfile(SCRIPT, root / 'scripts' / SCRIPT.name)
            (root / 'test_sample.py').write_text(source, encoding='utf-8')
            env = dict(os.environ)
            env.update(extra_env or {})
            command = [sys.executable, str(root / 'scripts' / SCRIPT.name), *suites]
            if driver is not None:
                (root / 'driver.py').write_text(driver, encoding='utf-8')
                command = [sys.executable, str(root / 'driver.py')]
            return subprocess.run(command,
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

    def test_timeout_stops_detached_descendants_before_fixture_cleanup_and_keeps_other_results(self):
        leaf = '''import json,os,sys,time
from pathlib import Path
held, ready, heartbeat = map(Path, sys.argv[1:4])
with held.open('wb') as stream:
    ready.write_text(json.dumps(dict(leaf=os.getpid(), middle=int(sys.argv[4]), held=str(held))))
    for index in range(400):
        heartbeat.write_text(str(index))
        time.sleep(.05)
'''
        middle = f'''import os,subprocess,sys,time
options = (dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt'
           else dict(start_new_session=True))
subprocess.Popen([sys.executable, '-c', {leaf!r}, *sys.argv[1:4], str(os.getpid())],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, **options)
time.sleep(20)
'''
        source = f'''import os,subprocess,sys,time
from pathlib import Path
def test_stall(tmp_path):
    options = (dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt'
               else dict(start_new_session=True))
    subprocess.Popen([sys.executable, '-c', {middle!r}, str(tmp_path/'held.bin'),
                      str(Path.cwd()/'ready.json'), str(Path.cwd()/'heartbeat')],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **options)
    time.sleep(20)
'''
        driver = '''import importlib.util,json,os,signal,subprocess,sys,time
from pathlib import Path
script = Path.cwd()/'scripts'/'ci_check.py'
spec = importlib.util.spec_from_file_location('bounded_ci_fixture', script)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
original = runner.run_suite
runner.run_suite = lambda suite: original(suite, timeout=5)
Path('test_ok.py').write_text('def test_ok(): assert True\\n')
sys.argv = [str(script), '--jobs', '2', 'test_sample.py', 'test_ok.py']
result = runner.main()
record = json.loads(Path('ready.json').read_text())
stopped = False
try:
    assert result == 1
    before = Path('heartbeat').read_bytes()
    time.sleep(.25)
    assert Path('heartbeat').read_bytes() == before, 'Detached native descendant is still running'
    if sys.platform.startswith('linux'):
        for pid in (record['leaf'], record['middle']):
            status = Path(f'/proc/{pid}/stat')
            if status.exists():
                assert status.read_text().split(') ', 1)[1].startswith('Z '), 'Descendant was paused, not killed'
    assert not Path(record['held']).parents[2].exists(), 'The timed-out suite fixture remains'
    stopped = True
    print('DESCENDANTS_STOPPED_AND_FIXTURES_REMOVED', flush=True)
finally:
    if not stopped:
        for pid in (record['middle'], record['leaf']):
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            else:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
raise SystemExit(result)
'''
        result = self.run_fixture(source, driver=driver)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('FAIL: test_sample.py: required suite did not produce a valid report: TimeoutExpired',
                      result.stdout)
        self.assertIn('Required suites complete: 2; passed=1; failed=1', result.stdout)
        self.assertIn('DESCENDANTS_STOPPED_AND_FIXTURES_REMOVED', result.stdout)
        self.assertNotIn('cleanup failed', result.stdout)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
