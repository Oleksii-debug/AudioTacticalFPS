"""Reproduce the unchanged installed native library workflow probe.

This diagnostic uses the current locked Windows environment, source-bound wheel,
original DesktopFrame and original synthetic probe. It does not substitute a
service, waive a native assertion or qualify the complete integrated workflow.
"""
import argparse
from collections import Counter
from hashlib import sha1, sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PINS = {
    "tests/ui/native_library_workflow_smoke.py": "2bc30526bfb15076f168a2706bb79cc6b5744f66",
    "src/carr_library/library_assistant.py": "f96aa939066c16105c3398e507f43ef1e438d571",
    "src/carr_library/ui/library_assistant.py": "c8e9e0ca148d929ead1b44f7cef17efbb76f6568",
    "src/carr_library/ui/desktop_app.py": "85c0991f744bd521ca247a0e15da93aa585ea30d",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "packaging/windows-cp311.lock": "8dad99c0ea80470b7253873aee78e08857affec6",
    "packaging/windows-semantic-cp311.lock": "70dbd10a1dd6282027820f82207ccc617cc9a740",
    "packaging/trusted-client.lock": "6c4b48ac09572dc259d62d0d801057ab47c04583",
    "packaging/windows-sync-test-cp311.lock": "e3f6eda5976e867abe7267c04e0b867b1fd16f7a",
    "pyproject.toml": "0e691bab81b90bf9b1543b454014c47b987f11ce"
}
MODULES = ["carr_library.application","carr_library.library_assistant","carr_library.ui.library_assistant","carr_library.ui.desktop_app","carr_library.native_runtime","carr_library.ingest.batch_scan","carr_library.ingest.writer_guard","carr_library.research.export_file","bootstrap"]
REQUIRED = [
    "real named native menu dialog and read-only reviewed plan before approval",
    "native Pause checkpoints the real import before topic or export publication",
    "fresh native dialog reloads the exact scoped job and reviewed folder permissions",
    "real resumed worker publishes exactly one topic and export without repeating import",
    "scoped source results exclude the other library and preserve quotations, handles and notebook",
    "native status reports stay on the GUI thread while real tool execution uses the worker"
]
PROBE = 'tests/ui/native_library_workflow_smoke.py'


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


ORIGIN_CHILD = r"""
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
origins = verify()
receipt.write_text(json.dumps({'status': 'PASS', 'qualification': 'identity-only',
    'origins': origins, 'wheel_sha256': proof['wheel_sha256'],
    'wheel_payload_files_verified': len(payload), 'isolated': True,
    'source_injection': False}, indent=2, sort_keys=True) + '\n', encoding='utf-8')
"""


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    args = parser.parse_args()
    require(sys.platform == 'win32' and __debug__
            and re.fullmatch('[0-9a-f]{40}', args.expected_head),
            'Windows, assertions and an exact commit are required.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Exact event head differs.')
    evidence = args.evidence.resolve()
    evidence.mkdir(mode=0o700, exist_ok=False)
    before = sources()
    own_relative = Path(__file__).resolve().relative_to(ROOT.parent).as_posix()
    own_before = Path(__file__).read_bytes()
    require(git('show', args.expected_head + ':' + own_relative)
            == own_before.decode('utf-8').strip(), 'Diagnostic helper differs from Git.')
    identity = dict(source_commit=args.expected_head,
        source_tree=git('rev-parse', 'HEAD^{tree}'), inputs=before,
        helper_sha256=sha256(own_before).hexdigest(),
        python=sys.version, platform=sys.platform)
    write(evidence / 'identity-before.json', identity)
    ci = load('library_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('library_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    work_parent = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='carr-library-workflow-', dir=work_parent) as tmp:
        work = Path(tmp)
        wheels = work / 'wheel'
        wheels.mkdir()
        require(child([sys.executable, '-B', '-m', 'pip', 'wheel', '--no-index',
            '--no-deps', '--no-build-isolation', '--wheel-dir', str(wheels), str(ROOT)],
            evidence / 'wheel-build.txt', cwd=work, ci=ci, timeout=300) == 0,
            'Current application wheel build failed.')
        found = list(wheels.glob('*.whl'))
        require(len(found) == 1, 'Exactly one source application wheel is required.')
        wheel = found[0]
        proof = proof_module.verify_application_wheel(wheel, ROOT.parent, args.expected_head)
        write(evidence / 'source-wheel-proof.json', proof)
        require(child([sys.executable, '-I', '-B', '-m', 'pip', 'install', '--no-index',
            '--no-deps', '--force-reinstall', str(wheel)],
            evidence / 'wheel-install.txt', cwd=work, ci=ci, timeout=180) == 0,
            'Verified application wheel installation failed.')
        require(child([sys.executable, '-I', '-B', '-m', 'pip', 'check'],
            evidence / 'installed-pip-check.txt', cwd=work, ci=ci, timeout=60) == 0,
            'Installed dependency closure is inconsistent.')
        wrapper = work / 'native-origin-child.py'
        wrapper.write_text(ORIGIN_CHILD, encoding='utf-8')
        contract = work / 'child-contract.json'
        write(contract, dict(modules=MODULES))
        require(len(MODULES) == len(set(MODULES)) == 9, 'Origin inventory differs.')

        def snapshot(name):
            receipt, log = evidence / (name + '.json'), evidence / (name + '.txt')
            require(child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
                str(evidence / 'source-wheel-proof.json'), str(ROOT.parent), str(receipt),
                str(contract)], log, cwd=work, ci=ci, timeout=60) == 0,
                'Installed native origin snapshot failed.')
            return json.loads(receipt.read_text(encoding='utf-8'))

        first = snapshot('native-origins-before')
        native_log = evidence / 'native-library-workflow.txt'
        result = None
        failures = []
        source_unchanged = False
        installed_unchanged = False
        try:
            # Same isolated installed argv as integrated-desktop step26.
            # The unchanged probe retains its40s deadline and60s watchdog.
            result = child([sys.executable, '-I', '-B', '-X', 'utf8',
                str(ROOT / PROBE)], native_log, cwd=ROOT, ci=ci, timeout=180)
        except BaseException as exc:
            failures.append(type(exc).__name__ + ': ' + str(exc)[:1024])
        finally:
            try:
                last = snapshot('native-origins-after')
                require(last == first, 'Installed wheel or module origins changed.')
                installed_unchanged = True
            except BaseException as exc:
                failures.append('Post-origin proof: ' + type(exc).__name__ + ': ' + str(exc)[:1024])
            try:
                after = sources()
                require(after == before and Path(__file__).read_bytes() == own_before,
                        'Source/probe/helper changed during actual execution.')
                require(git('rev-parse', 'HEAD') == args.expected_head, 'Source head changed.')
                write(evidence / 'identity-after.json', dict(**identity,
                    inputs_unchanged=True, helper_unchanged=True))
                source_unchanged = True
            except BaseException as exc:
                failures.append('Post-source proof: ' + type(exc).__name__ + ': ' + str(exc)[:1024])

        reports = []
        if native_log.is_file():
            for line in native_log.read_text(encoding='utf-8', errors='replace').splitlines():
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if type(value) is dict and 'status' in value and 'windows_native' in value:
                    reports.append(value)
        report = reports[-1] if reports else None
        if report is not None:
            write(evidence / 'native-library-workflow.json', report)
        try:
            require(result == 0, 'Original native probe exited unsuccessfully.')
            require(len(reports) == 1 and report['status'] == report['windows_native'] == 'PASS'
                    and report['windows_nvda'] == 'NOT_RUN'
                    and report['native_platform'] == 'Windows',
                    'Original native report is incomplete or does not qualify Windows controls.')
            require(type(report.get('checks')) is list
                    and Counter(report['checks']) == Counter(REQUIRED),
                    'All six original native checks are required exactly once.')
            for field, module in (
                    ('service_sha256', 'carr_library.library_assistant'),
                    ('ui_sha256', 'carr_library.ui.library_assistant'),
                    ('desktop_sha256', 'carr_library.ui.desktop_app')):
                require(report[field] == first['origins'][module]['sha256'],
                        'Reported installed source differs: ' + module)
        except BaseException as exc:
            failures.append('Native verdict: ' + type(exc).__name__ + ': ' + str(exc)[:1024])
        summary = dict(status='FAIL' if failures else 'PASS', **identity,
            native_exit=result, required_checks=6, required_installed_origins=9,
            original_probe_unchanged=source_unchanged,
            installed_payload_unchanged=installed_unchanged,
            original_native_timeout_seconds=180,
            errors=failures, windows_nvda='NOT_RUN',
            complete_integrated_qualification='NOT_RUN',
            qualification='Exact installed native library workflow diagnostic only.')
        write(evidence / 'diagnostic-summary.json', summary)
        print(json.dumps({key: summary[key] for key in (
            'status', 'source_commit', 'native_exit', 'required_checks',
            'windows_nvda', 'complete_integrated_qualification', 'errors')},
            sort_keys=True), flush=True)
        require(not failures, 'Original native library workflow diagnostic failed.')


if __name__ == '__main__':
    main()
