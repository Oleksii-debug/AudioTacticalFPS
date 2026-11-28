"""Configured updater portable source and installed contracts.

Four whole modules: handoff53, external-host preparation40, actual UI handlers15,
and explicit controller14. The HTTPS15 loopback suite and actual Windows A/B
activation are separate required qualifications; no offline policy is weakened.
"""
import argparse
from hashlib import file_digest, sha1, sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "tests/security/test_update_handoff.py": 53,
    "tests/security/test_update_runtime.py": 40,
    "tests/security/test_update_ui.py": 15,
    "tests/security/test_update_controller_actions.py": 14
}
PINS = {
    "tests/security/test_update_handoff.py": "057be5e81624330f63d0519569247e1129444781",
    "tests/security/test_update_runtime.py": "91bab7040584adfb1a23dcd84d92047d44f1bf0c",
    "tests/security/test_update_ui.py": "4b7c1e0b0d379654d769da667b11e453cb55a221",
    "tests/security/test_update_controller_actions.py": "1daad6582495e049952e9c36404214059453ada1",
    "tests/security/__init__.py": "8b137891791fe96927ad78e64b0aad7bded08bdc",
    "tests/security/conftest.py": "b8e01dcd3d4145e9e9c433fc177fadc0f3304e18",
    "tests/security/test_managed_installation.py": "6bb9d65a1335c54a9af67b4038c62158b307486f",
    "packaging/windows_installer/build_setup.py": "3c38afc75e22931090c0585dd5b6901c6c74087c",
    "packaging/windows_installer/create_update_target.py": "347846c571030fb30ee809562985ab643c644b6b",
    "packaging/windows_installer/maintenance.py": "76af3bf9a2be2c61b890094ecaa7142cc3d0c8a8",
    "packaging/windows_installer/setup.iss.in": "18f9544ed3d26763f955f14ed8d984a8e2a2558e",
    "packaging/create_delivery.py": "b9041dd6a09156458d4991a7a8635954f516d21f",
    "packaging/install_windows.py": "e4c30a7600c7afdb6771d9ae58b1d16558df0eba",
    "src/carr_library/__main__.py": "a1772425f4cd1b1d923ac06c96a71e75a2afdd72",
    "src/carr_library/installation_lease.py": "0114cc74398d3846d64babf8eb8cf3996b46e99d",
    "src/carr_library/managed_installation.py": "b68013ba422d40dc0daa5c9be68bd5447af2d0bf",
    "src/carr_library/audio/windows_job.py": "27aeb47c39660ac74335d791e22e58b44058db3b",
    "src/carr_library/audio/cleanup.py": "77d69f8406db93dc0ff6110b9597ad805bd92dc5",
    "src/carr_library/audio/models.py": "35694ae7cb9ad00e6a37ee11e18cdf60c6b2d7f6",
    "src/carr_library/audio/process.py": "480272d0224770f6b0ec9c8efc8e70d9aab42b0b",
    "src/carr_library/security/_files.py": "eecd5c75f606741d3457ed8aef57f86556230538",
    "src/carr_library/security/anchors.py": "2b821c8ec8b6a68a2a969f35b4d09d9d0f3d8b15",
    "src/carr_library/security/boundary.py": "fcc03e8fd9eb754a1991925ce9711016ec14ea39",
    "src/carr_library/security/controller.py": "1d550366b08a00aed8b13af4923f47a9c94c5dde",
    "src/carr_library/security/entitlements.py": "9cd523f81580c04cff85024354a68c4d9967c32a",
    "src/carr_library/security/installation.py": "35da4203d610a4eaa23dc51af65a430bfa9e4b00",
    "src/carr_library/security/managed_installation.py": "2c203580e3ff0ea60da9262052e2cf9283632839",
    "src/carr_library/security/refresh.py": "a3f8ace81f8e0b872e74246474f9adf4ab3fa69b",
    "src/carr_library/security/updates.py": "d39fc6c7979ee198c8e84041aa4d3594b00c577c",
    "src/carr_library/security/windows_registration.py": "26c0438eaeb094d67d6f4de2006785d059ffe9c4",
    "src/carr_library/security/update_http_worker.py": "d998af37e81fb047d1f5802a0162d93dd627f314",
    "src/carr_library/security/update_transport.py": "73d05451e7f15e4310ea1d2ed18e35355602ad87",
    "src/carr_library/security/update_handoff.py": "72ec774f28f195589498d95a76da788dbcc3416c",
    "src/carr_library/security/update_host.py": "10ef4bbcf783e6beeeccea7f1eeb5fce33f273ff",
    "src/carr_library/security/update_runtime.py": "b511efb6bf5ff6d38d1e6df42854501150b20065",
    "src/carr_library/delivery_contract.py": "90163967acd50760bf15e8a98b74e0bd76aa63f2",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "src/carr_library/library_backup.py": "30ed3b152fc1cd26b338200b270e73f2e193a3f9",
    "src/carr_library/ui/worker_lifetime.py": "ed62603159bc095724c3b240d4754f989141a2c9",
    "src/carr_library/operations/broker.py": "f09561333d33ae68480260668ec05b617cd72213",
    "src/carr_library/operations/_locks.py": "ae161b8483f84b2a4aa2f153b4b11b08f76b9693",
    "src/carr_library/durable_state.py": "d2b95bde04aa72cdd21ee6b4f0092d3350f84fcb",
    "src/carr_library/catalog/intake.py": "8518f012491b542a57593af75993907983ddda0d",
    "bootstrap/__init__.py": "6981b7faa1cca8d29f37fe85751c110c693dae55",
    "src/carr_library/ui/trust_status.py": "fa58a344dd6d601942876cc5b427315a2cc78aa2",
    "tests/security/test_update_https.py": "561bd843532445010fad409bc24c71c8ada6b0e1",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "packaging/windows-cp311.lock": "8dad99c0ea80470b7253873aee78e08857affec6",
    "packaging/windows-sync-cp311.lock": "cd9c18a24d7a7ac203d81cf6a8d82d8bcb8f8ab3",
    "packaging/windows-sync-test-cp311.lock": "e3f6eda5976e867abe7267c04e0b867b1fd16f7a",
    "packaging/linux-sync-core-cp311.lock": "08f945066be748972a10d5b0ce46480fff722959",
    "packaging/trusted-client.lock": "6c4b48ac09572dc259d62d0d801057ab47c04583"
}
COMPANIONS = ["tests/security/__init__.py","tests/security/conftest.py","tests/security/test_managed_installation.py","packaging/windows_installer/build_setup.py","packaging/windows_installer/create_update_target.py","packaging/windows_installer/maintenance.py","packaging/windows_installer/setup.iss.in","packaging/create_delivery.py","packaging/install_windows.py","src/carr_library/__main__.py","src/carr_library/installation_lease.py","src/carr_library/managed_installation.py","src/carr_library/audio/windows_job.py","src/carr_library/audio/cleanup.py","src/carr_library/audio/models.py","src/carr_library/audio/process.py"]
BASE_MODULES = ["carr_library.security._files","carr_library.security.anchors","carr_library.security.boundary","carr_library.security.controller","carr_library.security.entitlements","carr_library.security.installation","carr_library.security.managed_installation","carr_library.security.refresh","carr_library.security.updates","carr_library.security.windows_registration","carr_library.security.update_http_worker","carr_library.security.update_transport","carr_library.security.update_handoff","carr_library.security.update_host","carr_library.security.update_runtime","carr_library.installation_lease","carr_library.managed_installation","carr_library.__main__","carr_library.delivery_contract","carr_library.application","carr_library.library_backup","carr_library.ui.worker_lifetime","carr_library.audio.models","carr_library.audio.process","carr_library.audio.windows_job","carr_library.audio.cleanup","carr_library.operations.broker","carr_library.operations._locks","carr_library.durable_state","carr_library.catalog.intake","bootstrap"]
WINDOWS_MODULES = ["carr_library.security._files","carr_library.security.anchors","carr_library.security.boundary","carr_library.security.controller","carr_library.security.entitlements","carr_library.security.installation","carr_library.security.managed_installation","carr_library.security.refresh","carr_library.security.updates","carr_library.security.windows_registration","carr_library.security.update_http_worker","carr_library.security.update_transport","carr_library.security.update_handoff","carr_library.security.update_host","carr_library.security.update_runtime","carr_library.installation_lease","carr_library.managed_installation","carr_library.__main__","carr_library.delivery_contract","carr_library.application","carr_library.library_backup","carr_library.ui.worker_lifetime","carr_library.audio.models","carr_library.audio.process","carr_library.audio.windows_job","carr_library.audio.cleanup","carr_library.operations.broker","carr_library.operations._locks","carr_library.durable_state","carr_library.catalog.intake","bootstrap","carr_library.ui.trust_status"]
MODULES = WINDOWS_MODULES if os.name == 'nt' else BASE_MODULES


def require(value, message):
    if not value:
        raise ValueError(message)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def sources():
    result = {}
    for relative, expected in PINS.items():
        path = ROOT / relative
        require(path.is_file() and not path.is_symlink(), 'Missing ordinary source: ' + relative)
        raw = path.read_bytes()
        actual = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        require(actual == expected, 'Changed pinned source: ' + relative)
        result[relative] = dict(git_blob=actual, sha256=sha256(raw).hexdigest(), bytes=len(raw))
    return result


def git(*args):
    return subprocess.run(['git', '--no-replace-objects', '-C', str(ROOT.parent), *args],
        check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def child(command, output, *, cwd, ci, timeout=600):
    environment = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTEST_PLUGINS', 'PYTEST_ADDOPTS'):
        environment.pop(key, None)
    environment.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
        PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', HF_HUB_OFFLINE='1',
        TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
        ORT_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1')
    isolation = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == 'nt' else {'start_new_session': True})
    with output.open('xb') as stream:
        process = subprocess.Popen(command, cwd=cwd, env=environment,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, **isolation)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            ci._terminate_process_tree(process)
            raise
    require(output.stat().st_size <= 8 * 1024**2, 'Required-suite log exceeds its finite bound.')
    print(output.read_text(encoding='utf-8', errors='replace'), flush=True)
    return code


INSTALLED_CHILD = r"""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

wheel, proof_path, checkout, receipt, contract_path = map(Path, sys.argv[1:6])
arguments = sys.argv[6:]
contract = json.loads(contract_path.read_text(encoding='utf-8'))
proof = json.loads(proof_path.read_text(encoding='utf-8'))
assert sys.flags.isolated and sys.flags.dont_write_bytecode and __debug__
assert sha256(wheel.read_bytes()).hexdigest() == proof['wheel_sha256']
import carr_library, bootstrap
package = Path(carr_library.__file__).resolve(strict=True).parent
helper = Path(bootstrap.__file__).resolve(strict=True).parent
assert package.parent == helper.parent
assert not package.is_relative_to(checkout) and not helper.is_relative_to(checkout)
assert all(not Path(p).resolve().is_relative_to(checkout) for p in sys.path if p)
payload = {}
with ZipFile(wheel) as archive:
    for name in archive.namelist():
        if name.startswith(('carr_library/', 'bootstrap/')) and not name.endswith('/'):
            payload[name] = sha256(archive.read(name)).hexdigest()
assert payload
def verify():
    rows = {}
    for relative, digest in payload.items():
        path = package.parent / relative
        assert path.is_file() and not path.is_symlink()
        assert sha256(path.read_bytes()).hexdigest() == digest, relative
    for name in contract['modules']:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve(strict=True)
        assert path.is_relative_to(package if name.startswith('carr_library') else helper)
        rows[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return rows
before = verify()
sys.argv = ['pytest', *arguments]
try:
    exec(compile(contract['pytest_child'], '<existing-ci-check-policy>', 'exec'),
         {'__name__': '__main__'})
except SystemExit as result:
    code = result.code
else:
    raise AssertionError('Existing strict pytest child did not exit explicitly.')
after = verify()
assert after == before
receipt.write_text(json.dumps({'status': 'PASS', 'origins': before,
    'wheel_sha256': proof['wheel_sha256'], 'wheel_payload_files_verified': len(payload),
    'isolated': True, 'source_injection': False, 'payload_unchanged': True},
    indent=2, sort_keys=True) + '\n', encoding='utf-8')
raise SystemExit(code)
"""



def main():
    # Isolated Python ignores PYTHONUTF8; display the retained UTF-8 child log.
    # This changes only this orchestrator's stream, not child/test encodings.
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    args = parser.parse_args()
    require(__debug__ and re.fullmatch('[0-9a-f]{40}', args.expected_head), 'Exact commit and assertions required.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Checkout differs from exact event head.')
    evidence = args.evidence.resolve()
    evidence.mkdir(mode=0o700, exist_ok=False)
    before = sources()
    own_relative = Path(__file__).resolve().relative_to(ROOT.parent).as_posix()
    require(git('show', args.expected_head + ':' + own_relative) == Path(__file__).read_text(encoding='utf-8').strip(),
            'Orchestrator differs from its committed source.')
    identity = dict(source_commit=args.expected_head, source_tree=git('rev-parse', 'HEAD^{tree}'),
        inputs=before,
        python=sys.version, platform=sys.platform)
    write(evidence / 'identity-before.json', identity)
    ci = load('update_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('update_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    source_log = evidence / 'source-contracts.txt'
    # Four whole suites use two waves with unchanged 600-second child bounds.
    # The 1500-second parent allowance includes owned cleanup and report handling.
    code = child([sys.executable, '-B', str(ROOT / 'scripts/ci_check.py'),
                  '--jobs', '3', *SUITES], source_log, cwd=ROOT, ci=ci, timeout=1500)
    lines = source_log.read_text(encoding='utf-8').splitlines()
    expected = [f'PASS: {suite}: junit_passed={count}, failed=0, skipped=0, pytest_exit=0'
                for suite, count in SUITES.items()]
    require(code == 0 and all(lines.count(line) == 1 for line in expected)
            and lines[-1] == f'Required suites complete: {len(SUITES)}; passed={len(SUITES)}; failed=0',
            'Every source whole suite must pass without skips or expected failures.')
    write(evidence / 'source-summary.json', dict(status='PASS', suites=SUITES,
          passed=sum(SUITES.values()), failed=0, skipped=0, expected_failures=False,
          log_sha256=sha256(source_log.read_bytes()).hexdigest()))
    work_parent = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='carr-configured-updater-', dir=work_parent) as tmp:
        work = Path(tmp)
        wheels = work / 'wheel'
        wheels.mkdir()
        build_code = child([sys.executable, '-B', '-m', 'pip', 'wheel', '--no-index',
            '--no-deps', '--no-build-isolation', '--wheel-dir', str(wheels), str(ROOT)],
            evidence / 'wheel-build.txt', cwd=work, ci=ci, timeout=300)
        require(build_code == 0, 'Current source wheel build failed.')
        found = list(wheels.glob('*.whl'))
        require(len(found) == 1, 'Exactly one current application wheel is required.')
        wheel = found[0]
        proof = proof_module.verify_application_wheel(wheel, ROOT.parent, args.expected_head)
        write(evidence / 'source-wheel-proof.json', proof)
        install_code = child([sys.executable, '-I', '-B', '-m', 'pip', 'install', '--no-index',
            '--no-deps', '--force-reinstall', str(wheel)],
            evidence / 'wheel-install.txt', cwd=work, ci=ci, timeout=180)
        require(install_code == 0, 'Verified current wheel was not installed.')
        require(child([sys.executable, '-I', '-B', '-m', 'pip', 'check'],
            evidence / 'installed-pip-check.txt', cwd=work, ci=ci, timeout=60) == 0,
            'Installed dependency closure is inconsistent.')
        staged = work / 'isolated-tests'
        staged.mkdir()
        for relative in (*SUITES, *COMPANIONS):
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
            require(sha256(target.read_bytes()).hexdigest() == before[relative]['sha256'],
                    'Companion changed during staging.')
        ini = staged / 'pytest.ini'
        ini.write_text('[pytest]\naddopts =\n', encoding='utf-8')
        wrapper = work / 'installed_child.py'
        wrapper.write_text(INSTALLED_CHILD, encoding='utf-8')
        contract = work / 'child-contract.json'
        require(len(MODULES) == len(set(MODULES)) == (32 if os.name == 'nt' else 31),
                'Every updater, existing transaction/process owner and bootstrap origin is required.')
        write(contract, dict(pytest_child=ci.PYTEST_CHILD, modules=MODULES))
        installed = []
        for number, (suite, expected_count) in enumerate(SUITES.items(), 1):
            xml = evidence / f'installed-{number}.xml'
            receipt = evidence / f'installed-{number}-origins.json'
            log = evidence / f'installed-{number}.txt'
            result = child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
                str(evidence / 'source-wheel-proof.json'), str(ROOT.parent), str(receipt), str(contract),
                '-v', '-c', str(ini), '--rootdir', str(staged), '--confcutdir', str(staged),
                '--import-mode=importlib', '-o', 'pythonpath=', '-o', 'addopts=',
                '-o', 'xfail_strict=true', '-o', 'faulthandler_timeout=120',
                '-o', 'faulthandler_exit_on_timeout=false', '--junitxml', str(xml),
                '--basetemp', str(work / f'pytest-{number}'), '--', str(staged / suite)],
                log, cwd=staged, ci=ci)
            cases = list(ET.parse(xml).iter('testcase'))
            failed = sum(any(c.find(tag) is not None for tag in ('failure', 'error')) for c in cases)
            skipped = sum(c.find('skipped') is not None for c in cases)
            passed = len(cases) - failed - skipped
            require(result == 0 and passed == expected_count and failed == skipped == 0
                    and receipt.is_file(), 'Installed whole contract failed: ' + suite)
            installed.append(dict(suite=suite, passed=passed, failed=failed, skipped=skipped,
                                  exit=result, origins=json.loads(receipt.read_text(encoding='utf-8'))))
        for relative in (*SUITES, *COMPANIONS):
            require(sha256((staged / relative).read_bytes()).hexdigest() == before[relative]['sha256'],
                    'An installed test or companion changed.')
    require(sources() == before, 'Pinned sources changed during actual contracts.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Source commit changed.')
    report = dict(status='PASS', **identity, source_passed=sum(SUITES.values()),
        installed_passed=sum(SUITES.values()), installed=installed, inputs_unchanged=True,
        application_wheel_sha256=proof['wheel_sha256'], all_children_reaped=True,
        expected_failures=False, selected_whole_suites=SUITES, native='NOT_RUN', actual_owner='NOT_RUN',
        qualification='Exact four whole configured-updater source and isolated installed contracts. Native setup is explicitly substituted in portable cases. HTTPS15 loopback, real external-host A/B activation, production TLS/publisher rollout and manual NVDA remain separate.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'all_children_reaped')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
