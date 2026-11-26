"""Run explicitly required pytest suites; missing or skipped tests are not green.

Use only on a trusted synthetic test checkout, never on imported documents.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def run_suite(suite: str) -> bool:
    path = (ROOT / suite).resolve()
    if not path.is_relative_to(ROOT) or not path.exists():
        print(f'FAIL: missing or out-of-tree required suite: {suite}', flush=True)
        return False
    env = dict(os.environ)
    for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH'):
        env.pop(key, None)
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    env['PYTHONPATH'] = str(ROOT / 'src')
    with tempfile.TemporaryDirectory(prefix='carr-ci-') as tmp:
        report = Path(tmp) / 'junit.xml'
        try:
            proc = subprocess.run([
                sys.executable, '-m', 'pytest', '-o', 'addopts=',
                '-o', 'xfail_strict=true', '--junitxml', str(report),
                '--', str(path),
            ], cwd=ROOT, env=env, timeout=600, check=False)
            cases = list(ET.parse(report).iter('testcase'))
        except (OSError, subprocess.TimeoutExpired, ET.ParseError) as exc:
            print(f'FAIL: required suite did not produce a valid report: {type(exc).__name__}', flush=True)
            return False
        failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
        skipped = sum(c.find('skipped') is not None for c in cases)
        passed = len(cases) - failed - skipped
        ok = proc.returncode == 0 and passed > 0 and failed == skipped == 0
        print(f'{"PASS" if ok else "FAIL"}: {suite}: passed={passed}, failed={failed}, '
              f'skipped={skipped}, pytest_exit={proc.returncode}', flush=True)
        return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suites', nargs='+', help='Explicit required test paths relative to carr-library')
    args = parser.parse_args()
    results = [run_suite(suite) for suite in args.suites]
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
