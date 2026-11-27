"""Run the same meaningful transfer regressions against only an installed wheel."""
from argparse import ArgumentParser
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET


def _checked_origins(root):
    names = (
        'carr_library.sync.archive', 'carr_library.sync.crypto',
        'carr_library.sync.service', 'carr_library.sync.vault',
        'carr_library.sync.cli', 'carr_library.ui.portable_sync',
        'carr_library.file_publication', 'carr_library.library_backup',
        'carr_library.pending_backup', 'carr_library.backup_sidecars',
        'carr_library.backup_producers', 'carr_library.state_backup',
    )
    origins = {}
    for name in names:
        module = importlib.import_module(name)
        actual = Path(module.__file__).resolve(strict=True)
        expected = root / 'src' / Path(*name.split('.')).with_suffix('.py')
        if actual.is_relative_to(root) or not actual.is_relative_to(Path(sys.prefix).resolve()):
            raise RuntimeError('Installed verification imported outside its selected environment: ' + name)
        digest = sha256(actual.read_bytes()).hexdigest()
        if digest != sha256(expected.read_bytes()).hexdigest():
            raise RuntimeError('Installed module differs from the checked-out source: ' + name)
        origins[name] = dict(path=str(actual), source_sha256=digest)
    return origins


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Retain the exact installed JUnit evidence.')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    origins = _checked_origins(root)
    origin = origins['carr_library.sync.service']['path']
    runner = runpy.run_path(str(root / 'scripts' / 'ci_check.py'))['PYTEST_CHILD']
    with TemporaryDirectory(prefix='carr-installed-sync-') as temporary:
        target = Path(temporary)
        shutil.copytree(root / 'tests' / 'sync', target / 'tests' / 'sync',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (target / 'tests' / 'ui').mkdir()
        for name in ('test_portable_sync.py', 'probe_portable_sync_native.py'):
            shutil.copyfile(root / 'tests' / 'ui' / name, target / 'tests' / 'ui' / name)
        (target / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
        report = target / 'results.xml'
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() not in ('PYTHONPATH', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'CARR_CI_ALLOW_LOOPBACK')}
        environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run([sys.executable, '-I', '-B', '-c', runner,
            '-c', str(target / 'pytest.ini'), '-o', 'xfail_strict=true',
            '--import-mode=importlib', '--junitxml', str(report),
            str(target / 'tests')], cwd=target, env=environment, timeout=480, check=False)
        if not report.is_file():
            raise RuntimeError('Installed transfer verification did not produce its required JUnit report '
                               f'(exit {result.returncode}). See the preceding child diagnostics.')
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(report, args.report)
        cases = list(ET.parse(report).iter('testcase'))
        failed = sum(any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')) for case in cases)
        passed = len(cases) - failed
        required = {'test_archive', 'test_crypto', 'test_service', 'test_complete_state',
                    'test_cli', 'test_vault', 'test_service_commit_boundaries', 'test_portable_sync'}
        counts = {name: sum(case.get('classname', '').rsplit('.', 1)[-1] == name
                            for case in cases) for name in required}
        if result.returncode or not cases or failed or not all(counts.values()):
            raise RuntimeError('The installed transfer tests did not all pass without skips.')
        if _checked_origins(root) != origins:
            raise RuntimeError('Installed source identity changed during transfer verification.')
        print(json.dumps({'status': 'PASS', 'installed_module': str(origin),
            'tests': passed, 'failed_or_skipped': failed, 'suite_counts': counts,
            'source_origins': origins, 'nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
