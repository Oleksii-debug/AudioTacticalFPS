from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'ci_check.py'


class CIEntrypointTests(unittest.TestCase):
    def run_fixture(self, source, *suites, extra_env=None, script_changes=(), files=None):
        with tempfile.TemporaryDirectory(prefix='Research Ž ') as temp:
            root = Path(temp) / 'Дослідження'
            (root / 'scripts').mkdir(parents=True)
            shutil.copyfile(SCRIPT, root / 'scripts' / SCRIPT.name)
            script = root / 'scripts' / SCRIPT.name
            for original, replacement in script_changes:
                text = script.read_text(encoding='utf-8')
                self.assertIn(original, text)
                script.write_text(text.replace(original, replacement), encoding='utf-8')
            (root / 'test_sample.py').write_text(source, encoding='utf-8')
            for name, content in (files or {}).items():
                (root / name).write_text(content, encoding='utf-8')
            env = dict(os.environ)
            env.update(extra_env or {})
            return subprocess.run([sys.executable, str(root / 'scripts' / SCRIPT.name), *suites],
                                  cwd=root, env=env, capture_output=True, text=True, timeout=60)

    def test_pass(self):
        result = self.run_fixture('def test_ok(): assert True\n', 'test_sample.py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('passed=1, failed=0, skipped=0', result.stdout)
        self.assertIn('START: test_sample.py: timeout=600s', result.stdout)
        self.assertIn('CHILD:', result.stdout)
        self.assertIn('END: test_sample.py: elapsed=', result.stdout)

    def test_startup_watchdog_reports_real_import_stack(self):
        result = self.run_fixture(
            'def test_ok(): assert True\n', 'test_sample.py',
            script_changes=(('STACK_DUMP_SECONDS = 120', 'STACK_DUMP_SECONDS = 0.1'),),
            # Keep the accelerated watchdog inside this deliberately stalled import.
            # It must capture the real import stack, then stop before pytest imports
            # run with the fixture's 100 ms interval. Production retains 120 seconds.
            files={'onnxruntime.py':
                   'import faulthandler\nimport time\n'
                   'try:\n    time.sleep(0.4)\n'
                   'finally:\n    faulthandler.cancel_dump_traceback_later()\n'
                   'def disable_telemetry_events(): pass\n'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Timeout (', result.stderr)
        self.assertIn('onnxruntime.py', result.stderr)
        self.assertIn('passed=1, failed=0, skipped=0', result.stdout)

    def test_timeout_names_the_actual_suite_and_is_not_green(self):
        result = self.run_fixture(
            'import time\ndef test_slow(): time.sleep(20)\n', 'test_sample.py',
            script_changes=(('SUITE_TIMEOUT_SECONDS = 600', 'SUITE_TIMEOUT_SECONDS = 2'),),
            files={'onnxruntime.py': 'def disable_telemetry_events(): pass\n'})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('START: test_sample.py: timeout=2s', result.stdout)
        self.assertIn('FAIL: test_sample.py: required suite did not produce a valid report: TimeoutExpired',
                      result.stdout)
        self.assertIn('elapsed=', result.stdout)
        self.assertIn('Required suites complete: 1; passed=0; failed=1', result.stdout)

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


if __name__ == '__main__':
    unittest.main()
