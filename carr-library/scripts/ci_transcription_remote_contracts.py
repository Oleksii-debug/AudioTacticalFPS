"""Strict selected-audio STT source/wheel/Windows acceptance.

Reuse the existing whole-suite runner, source-wheel proof and installed policy.
Only public synthetic recordings and local protocol replies are used. Provider
acoustics, paid calls and manual assistive-technology qualification stay separate.
"""
import argparse
from hashlib import sha1, sha256
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
    "tests/assistant/test_transcription_provider.py": 39,
    "tests/transcription/test_remote_service.py": 29,
    "tests/ui/test_transcription_remote.py": 17
}
PINS = {
    "src/carr_library/assistant/transcription_provider.py": "122c20bb4a442abdefb652ec2e4e9c98ecd74b2f",
    "src/carr_library/assistant/profiles.py": "a3adbe7cc693bc8a3972144007ee15cc85b3b200",
    "src/carr_library/assistant/providers.py": "caf3b41d5ec7f7e7abe28d33b44cf0307d48f1be",
    "src/carr_library/integrations/transcription_remote_values.py": "25e09a95bac8f079a9e3f0d4b7016a6aa027c7b2",
    "src/carr_library/integrations/transcription_remote.py": "bc0c45a8c0edc8dcf8fc7e99476647e3eb8fb8dd",
    "src/carr_library/assistant/transcription_worker.py": "1651009d71e720ddad1793b35f8fad1464af7422",
    "src/carr_library/transcription.py": "641ea1c9c2e983b1cc2c4042db33d42e13bcae1e",
    "src/carr_library/backup_transcription.py": "98161afe7d432a81e169602f61b8443b639ccea6",
    "src/carr_library/backup_producers.py": "c8596f4cb9792a89d3f76d08347b9ebbc963439e",
    "src/carr_library/ui/transcription_remote_dialog.py": "625500b8b51d4fbc37891218b5e1b0acbb9a5898",
    "src/carr_library/ui/transcription_dialog.py": "83425f992811d3560cb53cc191f61dfbe54b8ec5",
    "src/carr_library/ui/assistant_dialog.py": "ec81cbf478a225c482b3e74cd5bc9e9deadd3d9a",
    "tests/transcription/remote_fixture.py": "49b5a4b63c16f8ebada412864f7ce66c4474f0b7",
    "tests/assistant/test_transcription_provider.py": "cbc8f4c706c0da053de2adc8691aff0c06d1e2ae",
    "tests/transcription/test_remote_service.py": "cbe6fcba6064e7f66ffb0d8303d2b8bfe364a000",
    "tests/ui/test_transcription_remote.py": "7cdb3a5f08cfb92c268ff25f7e82724aa2800f29",
    "tests/assistant/test_transcription_loopback.py": "6baa4675771066aba1a7ce40a276419eac4606f9",
    "scripts/translation_loopback_check.py": "360a669baaa8d60b41acc265111d2fa0f68108ee",
    "scripts/windows_transcription_remote_smoke.py": "62e7d85daf58c7aeadbc83fa8cc2227e09825103",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "packaging/windows-cp311.lock": "8dad99c0ea80470b7253873aee78e08857affec6",
    "packaging/windows-sync-cp311.lock": "cd9c18a24d7a7ac203d81cf6a8d82d8bcb8f8ab3",
    "packaging/trusted-client.lock": "6c4b48ac09572dc259d62d0d801057ab47c04583",
    "packaging/windows-sync-test-cp311.lock": "e3f6eda5976e867abe7267c04e0b867b1fd16f7a",
    "packaging/linux-sync-core-cp311.lock": "08f945066be748972a10d5b0ce46480fff722959",
    "src/carr_library/__init__.py": "ee09576cfebe843794b34e7609dd3bb1b86b59a0",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "src/carr_library/assistant/models.py": "1700d0191b4a27b09d18bb6e231b822cf183bd8a",
    "src/carr_library/assistant/credentials.py": "12601a8e6ba34b791b524fa14934144f07dfd493",
    "src/carr_library/integrations/transcription_values.py": "e744e7c0c0b7cbaf47d2cb926f9fca81690c54ab",
    "src/carr_library/ui/desktop_app.py": "85c0991f744bd521ca247a0e15da93aa585ea30d",
    "src/carr_library/ui/worker_lifetime.py": "ed62603159bc095724c3b240d4754f989141a2c9",
    "src/carr_library/operations/broker.py": "f09561333d33ae68480260668ec05b617cd72213",
    "src/carr_library/audio/process.py": "480272d0224770f6b0ec9c8efc8e70d9aab42b0b",
    "src/carr_library/audio/windows_job.py": "27aeb47c39660ac74335d791e22e58b44058db3b",
    "src/carr_library/audio/voice_transport.py": "f4a9f626688cd26e91f5ed0ea2c1813c9a68897e",
    "src/carr_library/native_runtime.py": "50ca93914325753f7d9cb91f1bc4f48fc9ae230f",
    "src/carr_library/durable_state.py": "d2b95bde04aa72cdd21ee6b4f0092d3350f84fcb",
    "src/carr_library/library_backup.py": "30ed3b152fc1cd26b338200b270e73f2e193a3f9",
    "src/carr_library/backup_sidecars.py": "5c2ec87913e8ad252dce9aa8cff7ccc22dca3525",
    "src/carr_library/file_publication.py": "2ce3bdaa2c489cfeff301907e29ca6b552a2f90f",
    "src/carr_library/operations/_locks.py": "ae161b8483f84b2a4aa2f153b4b11b08f76b9693",
    "src/carr_library/catalog/intake.py": "5a55fa8b1326a43ea2b6a4773f4072886e436031",
    "src/carr_library/audio/models.py": "35694ae7cb9ad00e6a37ee11e18cdf60c6b2d7f6",
    "src/carr_library/assistant/state.py": "e8657e7b18075c193b05b71769310b1616c452dd",
    "bootstrap/__init__.py": "6981b7faa1cca8d29f37fe85751c110c693dae55"
}
COMPANIONS = ["tests/transcription/remote_fixture.py"]
BASE_MODULES = ["carr_library.application","carr_library.transcription","carr_library.assistant.models","carr_library.assistant.profiles","carr_library.assistant.providers","carr_library.assistant.credentials","carr_library.assistant.transcription_provider","carr_library.assistant.transcription_worker","carr_library.integrations.transcription_remote","carr_library.integrations.transcription_remote_values","carr_library.integrations.transcription_values","carr_library.backup_transcription","carr_library.backup_producers","carr_library.ui.transcription_dialog","carr_library.ui.transcription_remote_dialog","carr_library.operations.broker","carr_library.audio.process","carr_library.audio.windows_job","carr_library.audio.voice_transport","carr_library.native_runtime","carr_library.durable_state","carr_library.ui.assistant_dialog","carr_library.library_backup","carr_library.backup_sidecars","carr_library.file_publication","carr_library.operations._locks","carr_library.catalog.intake","carr_library.audio.models","carr_library.assistant.state","bootstrap"]
WINDOWS_MODULES = ["carr_library.application","carr_library.transcription","carr_library.assistant.models","carr_library.assistant.profiles","carr_library.assistant.providers","carr_library.assistant.credentials","carr_library.assistant.transcription_provider","carr_library.assistant.transcription_worker","carr_library.integrations.transcription_remote","carr_library.integrations.transcription_remote_values","carr_library.integrations.transcription_values","carr_library.backup_transcription","carr_library.backup_producers","carr_library.ui.transcription_dialog","carr_library.ui.transcription_remote_dialog","carr_library.ui.desktop_app","carr_library.ui.worker_lifetime","carr_library.operations.broker","carr_library.audio.process","carr_library.audio.windows_job","carr_library.audio.voice_transport","carr_library.native_runtime","carr_library.durable_state","carr_library.ui.assistant_dialog","carr_library.library_backup","carr_library.backup_sidecars","carr_library.file_publication","carr_library.operations._locks","carr_library.catalog.intake","carr_library.audio.models","carr_library.assistant.state","bootstrap"]
MODULES = WINDOWS_MODULES if os.name == 'nt' else BASE_MODULES
NATIVE_CONTRACT = json.loads(r'''{"required_checks":["owned failure answers native No before closing only the active inner modal","transfer confirmation defaults to the actual owned No button","registered transcription keeps its existing local default controls","remote child borrows exact local parent and original shared executor","opening remote transcription grants no transfer consent or implicit work","actual local review binds original bytes and normal retention without a child call","review alone leaves actual native consent unchecked and Send disabled","actual native Space grants only the reviewed selected-recording checkbox","observed default No retains no remote job or usage reservation","one explicit Yes runs one current Gateway child on the shared host worker","actual retained transcript has exact source and inactive public provider snapshot","real usage and retained remote receipt expose provider timestamps without local inference","completed child settles its actual Windows Job before returning the transcript","native completion clears review and permission before another transfer","existing transcription editor displays the verified remote text","native edit keeps original audio and recognition text unchanged","busy native Close requests cancellation and retains the current modal until settlement","cancelled queued review creates no second job or provider attempt","all explicit picker answers remain bound to the selected original","actual remote transcription controls use native Windows wx","existing host registry retains its explicit transcription command","one existing host menu opens the shared transcription workflow","actual host Close drains original shared worker and finalizers","all actual STT Job handles are settled before fallback cleanup","no STT resource lease survives actual host cleanup","synthetic STT fixture remains byte-identical through owner cleanup","actual remote-STT nested failure control completed","exact installed source: carr_library.application","exact installed source: carr_library.transcription","exact installed source: carr_library.assistant.models","exact installed source: carr_library.assistant.profiles","exact installed source: carr_library.assistant.providers","exact installed source: carr_library.assistant.credentials","exact installed source: carr_library.assistant.transcription_provider","exact installed source: carr_library.assistant.transcription_worker","exact installed source: carr_library.integrations.transcription_remote","exact installed source: carr_library.integrations.transcription_remote_values","exact installed source: carr_library.integrations.transcription_values","exact installed source: carr_library.backup_transcription","exact installed source: carr_library.backup_producers","exact installed source: carr_library.ui.transcription_dialog","exact installed source: carr_library.ui.transcription_remote_dialog","exact installed source: carr_library.ui.desktop_app","exact installed source: carr_library.ui.worker_lifetime","exact installed source: carr_library.operations.broker","exact installed source: carr_library.audio.process","exact installed source: carr_library.audio.windows_job","exact installed source: carr_library.audio.voice_transport","exact installed source: carr_library.native_runtime","exact installed source: carr_library.durable_state","unchanged installed source after cleanup: carr_library.application","unchanged installed source after cleanup: carr_library.transcription","unchanged installed source after cleanup: carr_library.assistant.models","unchanged installed source after cleanup: carr_library.assistant.profiles","unchanged installed source after cleanup: carr_library.assistant.providers","unchanged installed source after cleanup: carr_library.assistant.credentials","unchanged installed source after cleanup: carr_library.assistant.transcription_provider","unchanged installed source after cleanup: carr_library.assistant.transcription_worker","unchanged installed source after cleanup: carr_library.integrations.transcription_remote","unchanged installed source after cleanup: carr_library.integrations.transcription_remote_values","unchanged installed source after cleanup: carr_library.integrations.transcription_values","unchanged installed source after cleanup: carr_library.backup_transcription","unchanged installed source after cleanup: carr_library.backup_producers","unchanged installed source after cleanup: carr_library.ui.transcription_dialog","unchanged installed source after cleanup: carr_library.ui.transcription_remote_dialog","unchanged installed source after cleanup: carr_library.ui.desktop_app","unchanged installed source after cleanup: carr_library.ui.worker_lifetime","unchanged installed source after cleanup: carr_library.operations.broker","unchanged installed source after cleanup: carr_library.audio.process","unchanged installed source after cleanup: carr_library.audio.windows_job","unchanged installed source after cleanup: carr_library.audio.voice_transport","unchanged installed source after cleanup: carr_library.native_runtime","unchanged installed source after cleanup: carr_library.durable_state"],"unique_required_checks":73,"origin_modules":["carr_library.application","carr_library.transcription","carr_library.assistant.models","carr_library.assistant.profiles","carr_library.assistant.providers","carr_library.assistant.credentials","carr_library.assistant.transcription_provider","carr_library.assistant.transcription_worker","carr_library.integrations.transcription_remote","carr_library.integrations.transcription_remote_values","carr_library.integrations.transcription_values","carr_library.backup_transcription","carr_library.backup_producers","carr_library.ui.transcription_dialog","carr_library.ui.transcription_remote_dialog","carr_library.ui.desktop_app","carr_library.ui.worker_lifetime","carr_library.operations.broker","carr_library.audio.process","carr_library.audio.windows_job","carr_library.audio.voice_transport","carr_library.native_runtime","carr_library.durable_state"],"origin_count":23,"pass_fields":["status","native_keyboard","explicit_approval","retained_remote_receipt","queued_cancel_close","native_host_cleanup","native_failure_cleanup"],"not_run_fields":["paid_provider_call","acoustic_quality","language_coverage","manual_nvda"],"multiplicity":{"transfer confirmation defaults to the actual owned No button":2},"synthetic_transport":true,"picker":"explicit fixture file selection; system picker not automated"}''')

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


def windows_contract(*, work, wheel, contract, evidence, firewall, ci):
    if os.name != 'nt':
        return {'status': 'NOT_RUN', 'reason': 'Required native and firewall acceptance runs in the separate Windows job.'}
    require(firewall is not None, 'The existing loopback-only Windows firewall receipt is required.')
    wrapper = work / 'native-origin-child.py'
    wrapper.write_text(ORIGIN_CHILD, encoding='utf-8')
    before, after = evidence / 'native-origins-before.json', evidence / 'native-origins-after.json'
    def snapshot(path, log):
        require(child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
            str(evidence / 'source-wheel-proof.json'), str(ROOT.parent), str(path), str(contract)],
            log, cwd=work, ci=ci, timeout=60) == 0, 'Installed native origin snapshot failed.')
        return json.loads(path.read_text(encoding='utf-8'))
    first = snapshot(before, evidence / 'native-origins-before.txt')
    loopback = {}
    try:
        for mode in ('source', 'installed'):
            code = child([sys.executable, '-I', '-B', '-X', 'utf8',
                str(ROOT / 'scripts/translation_loopback_check.py'),
                '--mode', mode, '--suite', 'transcription', '--source-root', str(ROOT / 'src'),
                '--firewall-receipt', str(firewall), '--output-dir', str(evidence)],
                evidence / ('transcription-loopback-' + mode + '.txt'), cwd=ROOT, ci=ci, timeout=600)
            report_path = evidence / ('transcription-loopback-' + mode + '.json')
            require(code == 0 and report_path.is_file(), 'Exact real-loopback STT acceptance failed: ' + mode)
            value = json.loads(report_path.read_text(encoding='utf-8'))
            require(value['status'] == 'PASS' and value['cases'] == 6 and value['mode'] == mode
                    and value['suite'] == 'transcription' and value['pytest_exit'] == 0
                    and value['xfail_or_xpass'] is False and len(value['source_origins']) == 11
                    and value['actual_paid_provider_call'] == 'NOT_RUN'
                    and value['companion_sha256'] == {'tests/transcription/remote_fixture.py':
                        sha256((ROOT / 'tests/transcription/remote_fixture.py').read_bytes()).hexdigest()},
                    'Real-loopback STT report does not prove the complete exact contract.')
            loopback[mode] = dict(status='PASS', cases=6,
                report_sha256=sha256(report_path.read_bytes()).hexdigest())
        raw_path = evidence / 'native-remote-stt.txt'
        result = child([sys.executable, '-I', '-B', '-X', 'utf8',
            str(ROOT / 'scripts/windows_transcription_remote_smoke.py'),
            '--source-root', str(ROOT / 'src')],
            raw_path, cwd=ROOT, ci=ci, timeout=330)
        require(result == 0, 'Actual native remote-STT process failed.')
        raw = raw_path.read_text(encoding='utf-8-sig')
        reports = []
        for match in re.finditer(r'(?m)^\{', raw):
            try:
                item, _ = json.JSONDecoder().raw_decode(raw[match.start():])
            except ValueError:
                continue
            if isinstance(item, dict) and 'synthetic_transport' in item:
                reports.append(item)
        require(len(reports) == 1, 'Native remote-STT report is missing or ambiguous.')
        report = reports[0]
        require(all(report.get(field) == 'PASS' for field in NATIVE_CONTRACT['pass_fields']),
                'A required native remote-STT outcome failed.')
        require(all(report.get(field) == 'NOT_RUN' for field in NATIVE_CONTRACT['not_run_fields']),
                'Synthetic remote-STT acceptance cannot claim paid, acoustic, language or manual qualification.')
        required = NATIVE_CONTRACT['required_checks']
        require(len(set(required)) == NATIVE_CONTRACT['unique_required_checks'] == 73
                and type(report.get('checks')) is list
                and all(type(value) is str for value in report['checks'])
                and set(required) <= set(report['checks']),
                'The native remote-STT required check inventory is incomplete.')
        for name, count in NATIVE_CONTRACT['multiplicity'].items():
            require(report['checks'].count(name) == count, 'Both actual default-No observations are required.')
        expected = {name: first['origins'][name]['sha256'] for name in NATIVE_CONTRACT['origin_modules']}
        require(len(expected) == 23 and report['origins'] == expected,
                'Native source identities differ from the full verified installed payload.')
        require(report.get('synthetic_transport') is True
                and report.get('picker') == NATIVE_CONTRACT['picker']
                and report.get('fixture_sha256') == sha256((ROOT / 'tests/transcription/remote_fixture.py').read_bytes()).hexdigest(),
                'Native synthetic boundary or fixture identity differs.')
        write(evidence / 'native-remote-stt.json', report)
    finally:
        last = snapshot(after, evidence / 'native-origins-after.txt')
        require(last == first, 'Installed wheel or source origin changed during STT loopback/native work.')
    return dict(status='PASS', loopback=loopback, native_required_checks=73,
                native_module_origins=23, installed_payload_unchanged=True,
                paid_provider_call='NOT_RUN', acoustic_quality='NOT_RUN',
                language_coverage='NOT_RUN', manual_nvda='NOT_RUN')


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--firewall-receipt', type=Path)
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
        inputs=before, python=sys.version, platform=sys.platform)
    write(evidence / 'identity-before.json', identity)
    ci = load('stt_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('stt_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    source_log = evidence / 'source-contracts.txt'
    # Three whole modules, two serial waves at the existing 600-second maximum.
    code = child([sys.executable, '-B', str(ROOT / 'scripts/ci_check.py'),
                  '--jobs', '2', *SUITES], source_log, cwd=ROOT, ci=ci, timeout=1300)
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
    with tempfile.TemporaryDirectory(prefix='carr-remote-stt-contracts-', dir=work_parent) as tmp:
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
        require(len(MODULES) == len(set(MODULES)) == (32 if os.name == 'nt' else 30),
                'STT identity inventory differs from its reviewed platform map.')
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
        windows = windows_contract(work=work, wheel=wheel, contract=contract, evidence=evidence,
            firewall=args.firewall_receipt.resolve(strict=True) if args.firewall_receipt else None, ci=ci)
    require(sources() == before, 'Pinned sources changed during actual contracts.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Source commit changed.')
    report = dict(status='PASS', **identity, source_passed=sum(SUITES.values()),
        installed_passed=sum(SUITES.values()), installed=installed, inputs_unchanged=True,
        application_wheel_sha256=proof['wheel_sha256'], all_children_reaped=True,
        expected_failures=False, windows=windows,
        qualification='Exact STT source/installed contracts; Windows additionally requires synthetic real-loopback and actual native controls. Paid service/acoustic/language/manual qualification is separate.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'all_children_reaped')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
