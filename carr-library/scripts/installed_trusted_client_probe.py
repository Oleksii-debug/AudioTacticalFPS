"""Check the installed trust runtime while retaining real packaging fixture paths.

Packaging cases read reviewed source helpers and Git history. Application imports
must still come from the selected installed environment, before and after pytest.
"""
from argparse import ArgumentParser
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

SUITES = ["tests/packaging/test_managed_launch_boundary.py","tests/packaging/test_managed_update_native_policy.py","tests/packaging/test_native_launcher_bundle.py","tests/packaging/test_standard_installer.py","tests/security/test_anchor_locking.py","tests/security/test_canonical_boundary.py","tests/security/test_controller.py","tests/security/test_entitlements.py","tests/security/test_idle_renewal.py","tests/security/test_independent_review_boundaries.py","tests/security/test_installation.py","tests/security/test_managed_installation.py","tests/security/test_managed_registration.py","tests/security/test_publication_integrity.py","tests/security/test_refresh.py","tests/security/test_updates.py","tests/ui/test_trust_status_handlers.py"]
WINDOWS_SUITES = ["tests/security/test_windows_vault.py"]
ORIGINS = ["carr_library.security.anchors","carr_library.security.boundary","carr_library.security.controller","carr_library.security.entitlements","carr_library.security.installation","carr_library.security.managed_installation","carr_library.security.refresh","carr_library.security.updates","carr_library.security.windows_registration","carr_library.installation_lease","carr_library.managed_installation","carr_library.ui.trust_status"]


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
    result = run_tests(*args, **kwargs)
    after = helper['checked_origins'](root)
    if after != before:
        raise RuntimeError('Installed trust identity changed during pytest.')
    output.write_text(json.dumps(dict(status='PASS', modules=after), indent=2), encoding='utf-8')
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
    cases = list(ET.parse(report).iter('testcase'))
    failures = sum(any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')) for case in cases)
    counts = {Path(name).stem: sum(case.get('classname', '').rsplit('.', 1)[-1] == Path(name).stem
                                  for case in cases) for name in required}
    if completed.returncode or failures or not all(counts.values()) or counts['test_trust_status_handlers'] != 11:
        raise RuntimeError('All required installed trust cases must pass without skips or expected failures.')
    proof = json.loads(origin_file.read_text(encoding='utf-8'))
    print(json.dumps(dict(status='PASS', cases=len(cases), failed_or_skipped=failures,
        suite_counts=counts, installed_origins=proof['modules'],
        packaging_evidence='Reviewed source helpers; separate actual native setup gates are required',
        production_issuer='UNCONFIGURED', manual_nvda='NOT_RUN')), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
