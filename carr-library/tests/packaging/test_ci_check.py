from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'ci_check.py'


class CIEntrypointTests(unittest.TestCase):
    def run_fixture(self, source, *suites, extra_env=None, fixture_path='test_sample.py'):
        with tempfile.TemporaryDirectory(prefix='Research Ž ') as temp:
            root = Path(temp) / 'Дослідження'
            (root / 'scripts').mkdir(parents=True)
            shutil.copyfile(SCRIPT, root / 'scripts' / SCRIPT.name)
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


    def test_numeric_loopback_requires_the_explicit_fixture_path(self):
        result = self.run_fixture('def test_ok(): assert True\n',
                                  '--allow-loopback', 'test_sample.py')
        self.assertEqual(result.returncode, 1)
        self.assertIn('limited to the explicit local-planning HTTP fixture', result.stdout)

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
        path = 'tests/loopback/test_local_planning.py'
        result = self.run_fixture(source, '--allow-loopback', path, fixture_path=path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
