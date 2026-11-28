"""Actual local TLS/TUF updater contracts under an explicit Windows firewall.

The caller blocks external addresses for this interpreter and its base Python.
Only this separate gate removes the four offline socket monkeypatch assignments;
the canonical pytest XFAIL/XPASS/diagnostic policy stays byte-identical otherwise.
Source and isolated installed each require the complete15-case pinned module.
"""
import argparse
from hashlib import file_digest, sha1, sha256
import importlib.util
import ipaddress
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
    "tests/security/test_update_https.py": 15
}
PINS = {
    "tests/security/test_update_https.py": "561bd843532445010fad409bc24c71c8ada6b0e1",
    "tests/security/__init__.py": "8b137891791fe96927ad78e64b0aad7bded08bdc",
    "tests/security/conftest.py": "b8e01dcd3d4145e9e9c433fc177fadc0f3304e18",
    "src/carr_library/security/_files.py": "eecd5c75f606741d3457ed8aef57f86556230538",
    "src/carr_library/security/anchors.py": "2b821c8ec8b6a68a2a969f35b4d09d9d0f3d8b15",
    "src/carr_library/security/entitlements.py": "9cd523f81580c04cff85024354a68c4d9967c32a",
    "src/carr_library/security/boundary.py": "fcc03e8fd9eb754a1991925ce9711016ec14ea39",
    "src/carr_library/security/installation.py": "35da4203d610a4eaa23dc51af65a430bfa9e4b00",
    "src/carr_library/security/updates.py": "d39fc6c7979ee198c8e84041aa4d3594b00c577c",
    "src/carr_library/security/update_http_worker.py": "d998af37e81fb047d1f5802a0162d93dd627f314",
    "src/carr_library/security/update_transport.py": "73d05451e7f15e4310ea1d2ed18e35355602ad87",
    "src/carr_library/delivery_contract.py": "90163967acd50760bf15e8a98b74e0bd76aa63f2",
    "src/carr_library/audio/models.py": "35694ae7cb9ad00e6a37ee11e18cdf60c6b2d7f6",
    "src/carr_library/audio/process.py": "480272d0224770f6b0ec9c8efc8e70d9aab42b0b",
    "src/carr_library/audio/windows_job.py": "27aeb47c39660ac74335d791e22e58b44058db3b",
    "src/carr_library/audio/cleanup.py": "77d69f8406db93dc0ff6110b9597ad805bd92dc5",
    "src/carr_library/operations/broker.py": "f09561333d33ae68480260668ec05b617cd72213",
    "src/carr_library/operations/_locks.py": "ae161b8483f84b2a4aa2f153b4b11b08f76b9693",
    "src/carr_library/durable_state.py": "d2b95bde04aa72cdd21ee6b4f0092d3350f84fcb",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "src/carr_library/library_backup.py": "30ed3b152fc1cd26b338200b270e73f2e193a3f9",
    "src/carr_library/storage/revisions.py": "417a9920d4c25eacd36cc81f7441ea321c18d632",
    "bootstrap/__init__.py": "6981b7faa1cca8d29f37fe85751c110c693dae55",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "packaging/windows-cp311.lock": "8dad99c0ea80470b7253873aee78e08857affec6",
    "packaging/windows-sync-cp311.lock": "cd9c18a24d7a7ac203d81cf6a8d82d8bcb8f8ab3",
    "packaging/windows-sync-test-cp311.lock": "e3f6eda5976e867abe7267c04e0b867b1fd16f7a",
    "packaging/trusted-client.lock": "6c4b48ac09572dc259d62d0d801057ab47c04583"
}
COMPANIONS = ["tests/security/__init__.py","tests/security/conftest.py"]
MODULES = ["carr_library.security._files","carr_library.security.anchors","carr_library.security.entitlements","carr_library.security.boundary","carr_library.security.installation","carr_library.security.updates","carr_library.security.update_http_worker","carr_library.security.update_transport","carr_library.delivery_contract","carr_library.audio.models","carr_library.audio.process","carr_library.audio.windows_job","carr_library.audio.cleanup","carr_library.operations.broker","carr_library.operations._locks","carr_library.durable_state","carr_library.application","carr_library.library_backup","carr_library.storage.revisions","bootstrap"]


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




def firewall_receipt(path):
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if value.get('status') != 'PASS' or value.get('allowed_test_peer') != '127.0.0.1':
        raise ValueError('The calling workflow did not attest the explicit loopback firewall boundary.')
    programs = value.get('blocked_programs')
    if (not isinstance(programs, list) or not programs
            or any(type(item) is not str for item in programs)):
        raise ValueError('The firewall evidence has no bounded executable inventory.')
    programs = {os.path.normcase(str(Path(item).resolve(strict=True))) for item in programs}
    selected = {os.path.normcase(str(Path(item).resolve(strict=True)))
                for item in (sys.executable, sys._base_executable)}
    if not selected.issubset(programs):
        raise ValueError('The firewall does not cover this interpreter and its base interpreter.')
    def ranges(rows):
        if not isinstance(rows, list) or len(rows) != 3:
            raise ValueError('The expected external-address firewall ranges are missing.')
        result = set()
        for row in rows:
            if type(row) is not str or row.count('-') != 1:
                raise ValueError('Invalid external-address firewall range.')
            first, last = row.split('-')
            result.add((str(ipaddress.ip_address(first)), str(ipaddress.ip_address(last))))
        return result
    expected = ranges(['0.0.0.1-126.255.255.255', '128.0.0.0-255.255.255.255',
                       '::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'])
    if ranges(value.get('blocked_remote_ranges')) != expected:
        raise ValueError('The firewall ranges differ from the reviewed loopback-only gate.')
    return {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest(),
            'selected_interpreters': sorted(selected),
            'boundary': 'calling workflow read back active process-scoped external-address blocks'}



SOURCE_CHILD = r"""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys

root, contract_path, receipt = map(Path, sys.argv[1:4])
arguments = sys.argv[4:]
assert sys.flags.isolated and sys.flags.dont_write_bytecode and __debug__
root = root.resolve(strict=True)
contract = json.loads(contract_path.read_text(encoding='utf-8'))
sys.path[:0] = [str(root / 'src'), str(root)]
def verify():
    rows = {}
    for name in contract['modules']:
        relative = ('bootstrap/__init__.py' if name == 'bootstrap'
                    else 'src/' + name.replace('.', '/') + '.py')
        module = importlib.import_module(name)
        actual = Path(module.__file__).resolve(strict=True)
        expected = (root / relative).resolve(strict=True)
        assert actual == expected
        digest = sha256(actual.read_bytes()).hexdigest()
        assert digest == contract['source_hashes'][relative]
        rows[name] = {'path': str(actual), 'sha256': digest}
    return rows
before = verify()
sys.argv = ['pytest', *arguments]
try:
    exec(compile(contract['pytest_child'], '<explicit-loopback-ci-policy>', 'exec'),
         {'__name__': '__main__'})
except SystemExit as result:
    code = result.code
else:
    raise AssertionError('The actual strict pytest child did not exit explicitly.')
after = verify()
assert before == after
receipt.write_text(json.dumps({'status': 'PASS', 'mode': 'source',
    'origins': before, 'unchanged': True}, indent=2, sort_keys=True) + '\n', encoding='utf-8')
raise SystemExit(code)
"""


def loopback_policy(ci):
    blocked = ('socket.socket.connect = offline_network\n'
               'socket.socket.connect_ex = offline_network\n'
               'socket.create_connection = offline_network\n'
               'socket.getaddrinfo = offline_network\n')
    require(ci.PYTEST_CHILD.count(blocked) == 1,
            'The independently reviewed offline policy shape changed.')
    return ci.PYTEST_CHILD.replace(blocked, '')


def source_contract(ci, evidence, before, boundary):
    work_parent = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='carr-update-tls-source-', dir=work_parent) as tmp:
        work = Path(tmp)
        contract = work / 'source-policy.json'
        write(contract, dict(pytest_child=loopback_policy(ci), modules=MODULES,
            source_hashes={name: row['sha256'] for name, row in before.items()}))
        wrapper = work / 'source-child.py'
        wrapper.write_text(SOURCE_CHILD, encoding='utf-8')
        xml = evidence / 'source.xml'
        receipt = evidence / 'source-origins.json'
        result = child([sys.executable, '-I', '-B', str(wrapper), str(ROOT), str(contract), str(receipt),
            '-v', '-c', os.devnull, '--rootdir', str(ROOT), '--confcutdir', str(ROOT),
            '--import-mode=importlib', '-o', 'pythonpath=', '-o', 'addopts=',
            '-o', 'xfail_strict=true', '-o', 'faulthandler_timeout=120',
            '-o', 'faulthandler_exit_on_timeout=false', '--junitxml', str(xml),
            '--basetemp', str(work / 'pytest'), '--', str(ROOT / next(iter(SUITES)))],
            evidence / 'source-contracts.txt', cwd=work, ci=ci)
        cases = list(ET.parse(xml).iter('testcase'))
        require(result == 0 and len(cases) == 15 and receipt.is_file()
                and all(not any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped'))
                        for case in cases), 'All15 real source TLS cases must pass without skips or XFAIL/XPASS.')
        write(evidence / 'source-summary.json', dict(status='PASS', suites=SUITES, passed=15,
            failed=0, skipped=0, expected_failures=False, firewall=boundary,
            junit_sha256=sha256(xml.read_bytes()).hexdigest(),
            log_sha256=sha256((evidence / 'source-contracts.txt').read_bytes()).hexdigest()))

def main():
    # Isolated Python ignores PYTHONUTF8; display the retained UTF-8 child log.
    # This changes only this orchestrator's stream, not child/test encodings.
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--firewall-receipt', required=True, type=Path)
    args = parser.parse_args()
    require(os.name == 'nt', 'This dedicated TLS gate requires actual Windows external-address rules.')
    boundary = firewall_receipt(args.firewall_receipt.resolve(strict=True))
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
    source_contract(ci, evidence, before, boundary)
    work_parent = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='carr-update-https-', dir=work_parent) as tmp:
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
        require(len(MODULES) == len(set(MODULES)) == 20,
                'Every updater, existing transaction/process owner and bootstrap origin is required.')
        write(contract, dict(pytest_child=loopback_policy(ci), modules=MODULES))
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
        expected_failures=False, selected_whole_suites=SUITES, firewall=boundary,
        native_activation='NOT_RUN', production_tls='NOT_RUN',
        qualification='Exact15 actual loopback TLS/TUF cases in source and isolated installed modes under verified external-address firewall. No production endpoint, vendor rollout, native application activation or manual NVDA claim.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'all_children_reaped')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
