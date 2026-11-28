"""Check the installed trust runtime while retaining real packaging fixture paths.

Packaging cases read reviewed source helpers and Git history. Application imports
must still come from the selected installed environment, before and after pytest.
"""
from argparse import ArgumentParser
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

SUITES = ["tests/packaging/test_managed_launch_boundary.py","tests/packaging/test_managed_update_native_policy.py","tests/packaging/test_native_launcher_bundle.py","tests/packaging/test_standard_installer.py","tests/security/test_anchor_locking.py","tests/security/test_canonical_boundary.py","tests/security/test_controller.py","tests/security/test_entitlements.py","tests/security/test_idle_renewal.py","tests/security/test_independent_review_boundaries.py","tests/security/test_installation.py","tests/security/test_managed_installation.py","tests/security/test_managed_registration.py","tests/security/test_publication_integrity.py","tests/security/test_refresh.py","tests/security/test_updates.py","tests/ui/test_trust_status_handlers.py"]
WINDOWS_SUITES = ["tests/security/test_windows_vault.py"]
ORIGINS = ["carr_library.security.anchors","carr_library.security.boundary","carr_library.security.controller","carr_library.security.entitlements","carr_library.security.installation","carr_library.security.managed_installation","carr_library.security.refresh","carr_library.security.updates","carr_library.security.windows_registration","carr_library.installation_lease","carr_library.managed_installation","carr_library.ui.trust_status"]


SUITE_PROPERTY = 'carr.required_suite'
CASE_PROPERTY = 'carr.required_case'
TRUST_UI = 'tests/ui/test_trust_status_handlers.py'


def _required_names(required):
    if not isinstance(required, (list, tuple)):
        raise ValueError('Required suites must be an explicit sequence.')
    names = tuple(required)
    if not names or any(type(name) is not str or not name for name in names):
        raise ValueError('Required suites must be explicit nonempty relative paths.')
    for name in names:
        path = PurePosixPath(name)
        if (path.is_absolute() or str(path) != name or '\\' in name
                or any(part in ('.', '..') for part in path.parts)):
            raise ValueError('Required suites cannot use path aliases.')
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError('Required suite paths are duplicated or aliased.')
    return names


def _case_identity(suite, case):
    return sha256(json.dumps([suite, case], ensure_ascii=True,
                             separators=(',', ':')).encode('ascii')).hexdigest()


def required_suite_plugin(root, required):
    """Label actual collected items independently of pytest's display rootdir."""
    import pytest
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Required suite root must be an existing directory.')
    required = _required_names(required)
    allowed = {}
    for name in required:
        expected = root / name
        actual = expected.resolve(strict=True)
        if (actual != expected or not actual.is_relative_to(root)
                or not actual.is_file() or actual in allowed):
            raise ValueError('A required suite is outside its exact source path: ' + name)
        allowed[actual] = name

    class RequiredSuiteIdentity:
        def __init__(self):
            self.collected = []
            self._items = {}

        def pytest_collection_finish(self, session):
            seen, counts = set(), dict.fromkeys(required, 0)
            for item in session.items:
                actual = Path(item.path).resolve(strict=True)
                suite = allowed.get(actual)
                if suite is None:
                    raise pytest.UsageError('Collected test is outside the exact required suites.')
                separator, case = item.nodeid.partition('::')[1:]
                if not separator or not case:
                    raise pytest.UsageError('Collected test has no stable local case identity.')
                identity = _case_identity(suite, case)
                if identity in seen:
                    raise pytest.UsageError('A required test was collected more than once.')
                seen.add(identity)
                counts[suite] += 1
                self._items[id(item)] = (suite, identity)
                self.collected.append(dict(suite=suite, case=case, identity=identity))
            if not all(counts.values()):
                raise pytest.UsageError('A required suite did not collect any tests.')

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            report = outcome.get_result()
            try:
                suite, identity = self._items[id(item)]
            except KeyError:
                raise RuntimeError('A test ran without required-suite collection evidence.') from None
            if any(str(name).startswith('carr.required_') for name, _ in report.user_properties):
                raise RuntimeError('A test attempted to replace reserved required-suite evidence.')
            # JUnit takes properties from the teardown report. Each report gets
            # this exact pair once; item/user fixture properties are untouched.
            report.user_properties = [*report.user_properties,
                                      (SUITE_PROPERTY, suite), (CASE_PROPERTY, identity)]

    return RequiredSuiteIdentity()


def verify_required_report(report, required, collected, *, required_counts=None):
    """Require one passing JUnit case for every exact collected case, and no extras."""
    required = _required_names(required)
    if type(collected) is not list or not collected:
        raise RuntimeError('Required-suite collector evidence is missing.')
    expected, collected_counts = {}, dict.fromkeys(required, 0)
    for row in collected:
        if (type(row) is not dict or set(row) != {'suite', 'case', 'identity'}
                or type(row['suite']) is not str or row['suite'] not in collected_counts
                or type(row['case']) is not str or not row['case']
                or row['identity'] != _case_identity(row['suite'], row['case'])
                or row['identity'] in expected):
            raise RuntimeError('Invalid, foreign or duplicate required-suite collector evidence.')
        expected[row['identity']] = row['suite']
        collected_counts[row['suite']] += 1
    if not all(collected_counts.values()):
        raise RuntimeError('Required-suite collector evidence omits a required suite.')
    counts, seen = dict.fromkeys(required, 0), set()
    cases = list(ET.parse(report).iter('testcase'))
    for case in cases:
        properties = case.findall('./properties/property')
        reserved = [(item.get('name'), item.get('value')) for item in properties
                    if (item.get('name') or '').startswith('carr.required_')]
        if (len(reserved) != 2
                or sorted(name for name, _ in reserved) != sorted((SUITE_PROPERTY, CASE_PROPERTY))):
            raise RuntimeError('A JUnit case has missing, duplicate or foreign required-suite labels.')
        values = dict(reserved)
        suite, identity = values[SUITE_PROPERTY], values[CASE_PROPERTY]
        if suite not in counts or expected.get(identity) != suite or identity in seen:
            raise RuntimeError('A JUnit case has foreign, mismatched or duplicate required-suite identity.')
        seen.add(identity)
        counts[suite] += 1
        if any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')):
            raise RuntimeError('A required installed trust case failed, errored or was skipped.')
    if seen != set(expected) or counts != collected_counts:
        raise RuntimeError('JUnit omitted required collected cases.')
    exact = {TRUST_UI: 11} if required_counts is None else dict(required_counts)
    if any(name not in counts or type(count) is not int or count <= 0 or counts[name] != count
           for name, count in exact.items()):
        raise RuntimeError('Required installed trust suite counts differ from the explicit gate.')
    return counts


def checked_origins(root):
    found = {}
    for name in ORIGINS:
        if name == 'carr_library.ui.trust_status' and os.name != 'nt':
            # Linux exercises the real trust service and the retained AST UI cases;
            # wx itself is a required Windows-only native qualification.
            package = Path(importlib.import_module('carr_library').__file__).resolve(strict=True).parent
            actual = (package / 'ui' / 'trust_status.py').resolve(strict=True)
            qualification = 'installed-source-presence-only; native wx NOT_RUN'
        else:
            actual = Path(importlib.import_module(name).__file__).resolve(strict=True)
            qualification = 'imported-installed-runtime'
        expected = root / 'src' / Path(*name.split('.')).with_suffix('.py')
        if not actual.is_relative_to(Path(sys.prefix).resolve()) or actual.is_relative_to(root):
            raise RuntimeError('Trust acceptance imported outside its installed runtime: ' + name)
        digest = sha256(actual.read_bytes()).hexdigest()
        if digest != sha256(expected.read_bytes()).hexdigest():
            raise RuntimeError('Installed trust source differs from this checkout: ' + name)
        found[name] = dict(path=str(actual), sha256=digest, qualification=qualification)
    return found


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    required = list(SUITES) + (list(WINDOWS_SUITES) if os.name == 'nt' else [])
    origin_file = args.report.with_suffix('.origins.json').resolve()
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items()
        if key.upper() not in ('PYTHONPATH', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'CARR_CI_ALLOW_LOOPBACK')}
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    child = r"""
import json
from pathlib import Path
import runpy
import sys
root, output = Path(sys.argv[1]), Path(sys.argv[2])
sys.argv = ['installed-trust', *sys.argv[3:]]
# Only fixture packages/build helpers may use the checkout root; never its src.
sys.path.insert(0, str(root))
helper = runpy.run_path(str(root / 'scripts' / 'installed_trusted_client_probe.py'))
from carr_library.native_runtime import activate_bundled_runtime
runtime_handle = activate_bundled_runtime()
import pytest
run_tests = pytest.main
def checked_main(*args, **kwargs):
    before = helper['checked_origins'](root)
    required = list(helper['SUITES']) + (list(helper['WINDOWS_SUITES']) if sys.platform == 'win32' else [])
    membership = helper['required_suite_plugin'](root, required)
    kwargs['plugins'] = [*kwargs.get('plugins', ()), membership]
    result = run_tests(*args, **kwargs)
    after = helper['checked_origins'](root)
    if after != before:
        raise RuntimeError('Installed trust identity changed during pytest.')
    output.write_text(json.dumps(dict(status='IDENTITY_VERIFIED',
        verification_scope='Installed module identity only; test acceptance is separate.',
        pytest_exit=int(result), modules=after, collected=membership.collected), indent=2), encoding='utf-8')
    return result
pytest.main = checked_main
# Keep the existing unconditional offline socket guard and XFAIL/XPASS policy.
exec(compile(runpy.run_path(str(root / 'scripts' / 'ci_check.py'))['PYTEST_CHILD'],
             str(root / 'scripts' / 'ci_check.py'), 'exec'))
"""
    with TemporaryDirectory(prefix='carr-installed-trust-') as temporary:
        config = Path(temporary) / 'pytest.ini'
        config.write_text('[pytest]\n', encoding='utf-8')
        completed = subprocess.run([sys.executable, '-I', '-B', '-c', child,
            str(root), str(origin_file), '-c', str(config),
            '-o', 'addopts=', '-o', 'pythonpath=', '-o', 'xfail_strict=true',
            '--import-mode=importlib', '--junitxml', str(report),
            *[str(root / name) for name in required]],
            cwd=root, env=environment, timeout=600, check=False)
    if not report.is_file() or not origin_file.is_file():
        raise RuntimeError('Installed trust tests did not produce complete JUnit and origin evidence.')
    proof = json.loads(origin_file.read_text(encoding='utf-8'))
    if proof.get('status') != 'IDENTITY_VERIFIED' or set(proof.get('modules', {})) != set(ORIGINS):
        raise RuntimeError('Installed trust origin evidence is missing or incomplete.')
    counts = verify_required_report(report, required, proof.get('collected'))
    if completed.returncode or proof.get('pytest_exit') != 0:
        raise RuntimeError('All required installed trust cases must pass without skips or expected failures; '
                           f'pytest_exit={completed.returncode}.')
    print(json.dumps(dict(status='PASS', cases=sum(counts.values()), failed_or_skipped=0,
        suite_counts=counts, installed_origins=proof['modules'],
        packaging_evidence='Reviewed source helpers; separate actual native setup gates are required',
        production_issuer='UNCONFIGURED', manual_nvda='NOT_RUN')), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
