"""Run the same meaningful transfer regressions against only an installed wheel."""
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET


def _command(root, target, report, runner):
    """Use the same explicit network and installed-origin protocol as CI."""
    return [sys.executable, '-I', '-B', '-c', runner,
            'offline', 'installed', str(root),
            '-c', str(target / 'pytest.ini'), '-o', 'addopts=',
            '-o', 'xfail_strict=true', '--junitxml', str(report),
            '--', str(target / 'tests')]


def main():
    root = Path(__file__).resolve().parents[1]
    import carr_library.sync.service
    origin = Path(carr_library.sync.service.__file__).resolve()
    if origin.is_relative_to(root):
        raise RuntimeError('Installed verification unexpectedly imported source checkout code.')
    checked = runpy.run_path(str(root / 'scripts' / 'ci_check.py'))
    runner = checked['PYTEST_CHILD']
    with TemporaryDirectory(prefix='carr-installed-sync-') as temporary:
        target = Path(temporary)
        shutil.copytree(root / 'tests' / 'sync', target / 'tests',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (target / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
        report = target / 'results.xml'
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() not in ('PYTHONPATH', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'CARR_CI_ALLOW_LOOPBACK')}
        environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        returncode = checked['_run_test_process'](_command(root, target, report, runner),
            cwd=target, env=environment, timeout=480)
        if not report.is_file():
            raise RuntimeError('Installed transfer verification did not produce its required JUnit report '
                               f'(exit {returncode}). See the preceding child diagnostics.')
        cases = list(ET.parse(report).iter('testcase'))
        failed = sum(any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')) for case in cases)
        passed = len(cases) - failed
        if returncode or not cases or failed:
            raise RuntimeError('The installed transfer tests did not all pass without skips.')
        print(json.dumps({'status': 'PASS', 'installed_module': str(origin),
            'tests': passed, 'failed_or_skipped': failed, 'nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
