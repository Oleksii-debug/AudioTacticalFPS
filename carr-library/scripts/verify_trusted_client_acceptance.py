"""Strict installed Windows proof for the optional signed client, without authority."""
import argparse
from hashlib import file_digest
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
ORIGINS = [
    'carr_library', 'carr_library.delivery_contract', 'carr_library.assistant.credentials',
    'carr_library.security', 'carr_library.security._files', 'carr_library.security.anchors',
    'carr_library.security.entitlements', 'carr_library.security.updates',
    'carr_library.security.refresh', 'carr_library.security.polling',
    'carr_library.security.boundary', 'carr_library.security.controller',
    'carr_library.security.__main__', 'carr_library.ui.trust_status',
    'carr_library.ui.desktop_app',
]
SUITES = [
    'tests/security/test_anchor_locking.py', 'tests/security/test_canonical_boundary.py',
    'tests/security/test_controller.py', 'tests/security/test_entitlements.py',
    'tests/security/test_idle_renewal.py', 'tests/security/test_independent_review_boundaries.py',
    'tests/security/test_native_lifetime.py', 'tests/security/test_publication_integrity.py',
    'tests/security/test_refresh.py', 'tests/security/test_update_polling.py',
    'tests/security/test_updates.py', 'tests/ui/test_desktop_handlers.py',
]
PINS = {'cryptography': '50.0.2', 'tuf': '7.0.1', 'securesystemslib': '1.6.0',
        'urllib3': '2.8.0', 'cffi': '2.1.1', 'pycparser': '3.12'}


def digest(path):
    with Path(path).open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def source_identity():
    def git(*arguments):
        return subprocess.check_output(['git', *arguments], cwd=ROOT,
            text=True, encoding='utf-8', timeout=15).strip()
    if git('status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('Tracked acceptance source changed after checkout.')
    return {'commit': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}')}


def installed_origins():
    import carr_library
    package = Path(carr_library.__file__).resolve().parent
    if package.is_relative_to(ROOT.parent):
        raise RuntimeError('Acceptance imported the checkout instead of the installed package.')
    result = {}
    for name in ORIGINS:
        module = importlib.import_module(name)
        actual = Path(module.__file__).resolve(strict=True)
        relative = actual.relative_to(package)
        expected = ROOT / 'src' / 'carr_library' / relative
        if not expected.is_file() or digest(actual) != digest(expected):
            raise RuntimeError('Installed module differs from the exact checked-out source: ' + name)
        result[name] = {'path': str(actual), 'sha256': digest(actual)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('This strict gate requires actual Windows, including the OS credential vault.')
    if Path.cwd().resolve() != ROOT:
        raise RuntimeError('Run this gate from the exact carr-library checkout.')
    dependencies = {name: version(name) for name in PINS}
    if dependencies != PINS:
        raise RuntimeError('Optional signed-client dependencies differ from their exact pins.')
    for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONPATH'):
        os.environ.pop(key, None)
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    args.results.mkdir(parents=True, exist_ok=True)
    report = args.results / 'installed-trusted-client.xml'
    evidence = args.results / 'installed-trusted-client.json'
    result = {'status': 'RUNNING', 'source': source_identity(), 'python': sys.version,
              'dependencies': dependencies, 'origins': installed_origins(), 'suites': SUITES,
              'production_authority': 'NOT_CONFIGURED', 'private_issuer_interoperability': 'NOT_RUN',
              'managed_activation_and_rollback': 'NOT_IMPLEMENTED', 'manual_nvda': 'NOT_RUN'}
    evidence.write_text(json.dumps(result, indent=2), encoding='utf-8')
    import pytest

    class RejectExpectedFailures:
        seen = False

        def pytest_runtest_logreport(self, report):
            if hasattr(report, 'wasxfail'):
                self.seen = True

    policy = RejectExpectedFailures()
    status = pytest.main(['-q', '-p', 'no:cacheprovider', '-o', 'addopts=', '-o', 'pythonpath=',
                         '-o', 'xfail_strict=true', '-o', 'faulthandler_timeout=120',
                         '--import-mode=importlib', '--junitxml=' + str(report),
                         *SUITES], plugins=[policy])
    cases = list(ET.parse(report).iter('testcase')) if report.exists() else []
    failed = sum(any(case.find(tag) is not None for tag in ('failure', 'error')) for case in cases)
    skipped = sum(case.find('skipped') is not None for case in cases)
    passed = len(cases) - failed - skipped
    observed = {case.get('classname', '').rsplit('.', 1)[-1] for case in cases}
    missing_suites = sorted({Path(path).stem for path in SUITES} - observed)
    vault = [case for case in cases if case.get('name') == 'test_real_windows_vault_anchor_roundtrip']
    vault_exact = len(vault) == 1 and not any(vault[0].find(tag) is not None
                                           for tag in ('failure', 'error', 'skipped'))
    after = installed_origins()
    origins_unchanged = after == result['origins']
    ok = (status == 0 and passed > 0 and failed == skipped == 0 and not policy.seen
          and vault_exact and not missing_suites and origins_unchanged)
    result.update(status='PASS' if ok else 'FAIL', passed=passed, failed=failed, skipped=skipped,
                  pytest_exit=int(status), expected_failures=policy.seen, actual_windows_vault=vault_exact,
                  missing_suites=missing_suites, origins_after=after, origins_unchanged=origins_unchanged)
    evidence.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
