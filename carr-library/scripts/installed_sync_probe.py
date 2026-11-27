"""Run the same meaningful transfer regressions against only an installed wheel."""
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET


def main():
    root = Path(__file__).resolve().parents[1]
    import carr_library.sync.service
    origin = Path(carr_library.sync.service.__file__).resolve()
    if origin.is_relative_to(root):
        raise RuntimeError('Installed verification unexpectedly imported source checkout code.')
    runner = runpy.run_path(str(root / 'scripts' / 'ci_check.py'))['PYTEST_CHILD']
    with TemporaryDirectory(prefix='carr-installed-sync-') as temporary:
        target = Path(temporary)
        shutil.copytree(root / 'tests' / 'sync', target / 'tests',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (target / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
        report = target / 'results.xml'
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() not in ('PYTHONPATH', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'CARR_CI_ALLOW_LOOPBACK')}
        environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        result = subprocess.run([sys.executable, '-I', '-B', '-c', runner, 'offline',
            '-c', str(target / 'pytest.ini'), '-o', 'xfail_strict=true', '--junitxml', str(report),
            str(target / 'tests')], cwd=target, env=environment, timeout=480, check=False)
        if not report.is_file():
            raise RuntimeError('Installed transfer verification did not produce its required JUnit report '
                               f'(exit {result.returncode}). See the preceding child diagnostics.')
        cases = list(ET.parse(report).iter('testcase'))
        failed = sum(any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')) for case in cases)
        passed = len(cases) - failed
        if result.returncode or not cases or failed:
            raise RuntimeError('The installed transfer tests did not all pass without skips.')
        print(json.dumps({'status': 'PASS', 'installed_module': str(origin),
            'tests': passed, 'failed_or_skipped': failed, 'nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
