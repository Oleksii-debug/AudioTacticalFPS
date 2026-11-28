"""Run the canonical catalog and passive language-evidence acceptance.

Five complete suites retain every old catalog/UI case and all new typed
observation, real backup and current-host callback cases. The actual native
probe uses synthetic recorded observations only; no language-quality, current
device, authenticated reviewer, acoustic, physical-device or NVDA claim follows.
"""
import argparse
from hashlib import sha1, sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "tests/language_qualification/test_catalog.py": 31,
    "tests/language_qualification/test_ui.py": 7,
    "tests/language_qualification/test_evidence.py": 54,
    "tests/language_qualification/test_evidence_backup.py": 27,
    "tests/language_qualification/test_evidence_ui.py": 28
}
PINS = {
    "src/carr_library/language_qualification/__init__.py": "9a6572a3c1a7e46d859d6cb8242c65880b02f285",
    "src/carr_library/language_qualification/catalog.py": "236a61ecb765b544ca4e0005fb94e6b462256996",
    "src/carr_library/language_qualification/evidence.py": "befc09f3b9ac1724baf37d1d4016bb59b27f956c",
    "src/carr_library/language_qualification/backup.py": "45f11351d946bd10587d2842dca80c3e4eb7a39a",
    "src/carr_library/ui/language_matrix.py": "3c5a50d5117f25b58a5333f4165a661db22f80b0",
    "src/carr_library/backup_sidecars.py": "4052234a20b12d26d825463363e2126585f73211",
    "src/carr_library/state_backup.py": "65c75e75f82c78ba3153da01d687669b276428e3",
    "src/carr_library/library_backup.py": "0ccbba5db4c41e78143f0ef11aef51bd3df55339",
    "src/carr_library/durable_state.py": "d2b95bde04aa72cdd21ee6b4f0092d3350f84fcb",
    "src/carr_library/operations/_locks.py": "ae161b8483f84b2a4aa2f153b4b11b08f76b9693",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "src/carr_library/catalog/intake.py": "8518f012491b542a57593af75993907983ddda0d",
    "src/carr_library/storage/revisions.py": "417a9920d4c25eacd36cc81f7441ea321c18d632",
    "src/carr_library/storage/schema_identity.py": "2c72c6132969cb1076035a47ee87c4573ebd93be",
    "src/carr_library/data_paths.py": "4eaa42c376d501f9d0f8f71e27d6a82724a00600",
    "src/carr_library/ingest/writer_guard.py": "2445a40f97cdb45bd590e64da415a18fae31ce54",
    "src/carr_library/ingest/bundle.py": "d664107cebe9704c568220a6752c159e95c70f60",
    "src/carr_library/ingest/restore.py": "7c752b0203eb3d85bf3dfdeb2dea9a8c76013f37",
    "src/carr_library/ingest/catalog_asset_copy.py": "23f32ae91afbf154689c7f56a385f1c062f32f81",
    "src/carr_library/ingest/retained_assets.py": "acbaabd4b5661daadc83dfec6cda9394deb35ce3",
    "src/carr_library/backup_capacity.py": "1fd11ab8d3a2be5434e32bdcba65447c55730ad7",
    "src/carr_library/pending_backup.py": "b2c3fc3a52e9d2fef5c019196ef239381c519fc7",
    "src/carr_library/reader/reading_state.py": "c9b1fe207bfc26d4caa939ebc4d1199a1172837c",
    "src/carr_library/ui/desktop_app.py": "85c0991f744bd521ca247a0e15da93aa585ea30d",
    "src/carr_library/ui/worker_lifetime.py": "ed62603159bc095724c3b240d4754f989141a2c9",
    "src/carr_library/ui/worker_cleanup.py": "2f071d9d2762ef15f5f8ee3da2fcf25de165b15a",
    "src/carr_library/speech/progress_speech.py": "40fc50742fb2c50a1bfb2920e25af692017c0aaf",
    "src/carr_library/native_runtime.py": "50ca93914325753f7d9cb91f1bc4f48fc9ae230f",
    "bootstrap/__init__.py": "6981b7faa1cca8d29f37fe85751c110c693dae55",
    "src/carr_library/audio/cleanup.py": "77d69f8406db93dc0ff6110b9597ad805bd92dc5",
    "src/carr_library/audio/process.py": "480272d0224770f6b0ec9c8efc8e70d9aab42b0b",
    "src/carr_library/audio/windows_job.py": "27aeb47c39660ac74335d791e22e58b44058db3b",
    "src/carr_library/operations/broker.py": "f09561333d33ae68480260668ec05b617cd72213",
    "src/carr_library/runtime_settings.py": "00df790d393f9d54f87d0dcf891e6506916437a1",
    "src/carr_library/speech/__init__.py": "f3a90829d0e3780e4fd88512f200e64a947c83a8",
    "src/carr_library/ui/command_registry.py": "2fe71b6b3b7c46cef384d6fddb56997b30564e08",
    "tests/language_qualification/test_catalog.py": "eeee7e7b0bb1fad0aa7f9baa2209a3fddb9b1642",
    "tests/language_qualification/test_ui.py": "2e263b45440ed8ada0e443998885f69c48f3820c",
    "tests/language_qualification/test_evidence.py": "6c5d52a8102d40842f23838a1eb65630626a2a9d",
    "tests/language_qualification/test_evidence_backup.py": "a40f8b1964fa0a2ccc66e06e46134701484adb38",
    "tests/language_qualification/test_evidence_ui.py": "a367681b16b41b161e9a581444cdb463220d1547",
    "tests/language_qualification/evidence_fixture.py": "72944369f7304650e8742f97372d41415e3e35da",
    "src/carr_library/language_qualification/data/LANGUAGE_COVERAGE_DEV2_UK.md": "8081b24c7c33eff578e7896052d4221cfff8c684",
    "src/carr_library/language_qualification/data/SELECTED_LANGUAGES_UK.md": "caea8c5a8a5698971d75a565cba9a5a9c00b411f",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "scripts/windows_language_evidence_smoke.py": "1a0358e96abb435b6b944c8a69046e8f3f178c1b",
    "scripts/ci_language_evidence_cases.json": "6797df44d708230f38ba999760bbfd3cb8581e40"
}
COMPANIONS = ['tests/language_qualification/evidence_fixture.py']
MODULES = [
    "carr_library.language_qualification",
    "carr_library.language_qualification.catalog",
    "carr_library.language_qualification.evidence",
    "carr_library.language_qualification.backup",
    "carr_library.ui.language_matrix",
    "carr_library.backup_sidecars",
    "carr_library.state_backup",
    "carr_library.library_backup",
    "carr_library.durable_state",
    "carr_library.operations._locks",
    "carr_library.application",
    "carr_library.catalog.intake",
    "carr_library.storage.revisions",
    "carr_library.storage.schema_identity",
    "carr_library.data_paths",
    "carr_library.ingest.writer_guard",
    "carr_library.ingest.bundle",
    "carr_library.ingest.restore",
    "carr_library.ingest.catalog_asset_copy",
    "carr_library.ingest.retained_assets",
    "carr_library.backup_capacity",
    "carr_library.pending_backup",
    "carr_library.reader.reading_state",
    "carr_library.ui.desktop_app",
    "carr_library.ui.worker_lifetime",
    "carr_library.ui.worker_cleanup",
    "carr_library.speech.progress_speech",
    "carr_library.native_runtime",
    "bootstrap",
    "carr_library.audio.cleanup",
    "carr_library.audio.process",
    "carr_library.audio.windows_job",
    "carr_library.operations.broker",
    "carr_library.runtime_settings",
    "carr_library.speech",
    "carr_library.ui.command_registry"
]


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

CASE_FILE = 'scripts/ci_language_evidence_cases.json'
NATIVE_FILE = 'scripts/windows_language_evidence_smoke.py'
NEW_SUITES = (
    'tests/language_qualification/test_evidence.py',
    'tests/language_qualification/test_evidence_backup.py',
    'tests/language_qualification/test_evidence_ui.py',
)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate diagnostic JSON field.')
        result[key] = value
    return result


def checked_json(path, maximum, *, git_blob=None):
    def ordinary(info):
        return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and not getattr(info, 'st_file_attributes', 0) & 1024)
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, stat.S_IFMT(info.st_mode), info.st_nlink)
    before = path.lstat()
    require(ordinary(before) and 0 < before.st_size <= maximum,
            'Diagnostic JSON is not a bounded ordinary single-link file.')
    with path.open('rb') as stream:
        held = os.fstat(stream.fileno())
        require(ordinary(held) and identity(held) == identity(before),
                'Diagnostic JSON changed before its held read.')
        raw = stream.read(maximum + 1)
        final_held = os.fstat(stream.fileno())
        require(ordinary(final_held) and identity(final_held) == identity(held),
                'Diagnostic JSON changed during its held read.')
    after = path.lstat()
    require(len(raw) == before.st_size and ordinary(after)
            and identity(after) == identity(before),
            'Diagnostic JSON changed after its held read.')
    if git_blob is not None:
        require(sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
                == git_blob, 'Reviewed case-data source changed.')
    def invalid_constant(value):
        raise ValueError('Non-finite diagnostic JSON value.')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                       parse_constant=invalid_constant)
    return value, sha256(raw).hexdigest()


def read_contract():
    data, digest = checked_json(ROOT / CASE_FILE, 64 * 1024, git_blob=PINS[CASE_FILE])
    require(type(data) is dict and set(data) == {
        'schema', 'suites', 'native', 'native_script_git_blob'}
        and data['schema'] == 'carr-language-evidence-contracts/1',
        'The fixed language-evidence contract schema differs.')
    suites = data['suites']
    require(type(suites) is dict and set(suites) == set(NEW_SUITES),
            'Only the three reviewed new whole suites belong in shared case data.')
    for name, row in suites.items():
        require(type(row) is dict and set(row) == {'count', 'functions'}
                and type(row['count']) is int and row['count'] == SUITES[name]
                and type(row['functions']) is list and row['functions']
                and len(row['functions']) == len(set(row['functions']))
                and all(type(item) is str and re.fullmatch(r'test_[A-Za-z0-9_]+', item)
                        for item in row['functions']),
                'Reviewed language-evidence case/function inventory differs.')
    require(sum(row['count'] for row in suites.values()) == 109
            and sum(len(row['functions']) for row in suites.values()) == 32,
            'The complete new language-evidence case/function total differs.')
    native = data['native']
    require(type(native) is dict
            and native['schema'] == 'carr-native-language-evidence-contract/1'
            and data['native_script_git_blob'] == PINS[NATIVE_FILE]
            and native['native_script_git_blob'] == PINS[NATIVE_FILE]
            and native['native_script_path'] == NATIVE_FILE
            and native['mode'] == 'installed'
            and native['argv'] == ['--installed', '--report', 'PATH.json']
            and native['watchdog_seconds'] == 300 and native['parent_seconds'] == 330,
            'The exact native language-evidence source or CLI contract differs.')
    required = native['required_checks']
    origins = native['module_origins']
    require(type(required) is list and len(required) == len(set(required)) == 75
            and all(type(item) is str and 0 < len(item) <= 512 for item in required)
            and type(origins) is list and len(origins) == len(set(origins)) == 14
            and set(origins) <= set(MODULES),
            'The required native checks or current installed origins differ.')
    require(native['required_check_multiplicities'] == {
        'passive import confirmation initially focuses native No': 3}
        and type(native['required_fields']) is dict and len(native['required_fields']) == 16
        and all(type(key) is str and type(value) is str
                for key, value in native['required_fields'].items())
        and native['fixture'] == {
            'path': 'tests/language_qualification/evidence_fixture.py',
            'git_blob': PINS['tests/language_qualification/evidence_fixture.py']},
        'Native passive-evidence, default-No or fixture obligations differ.')
    return data


def native_contract(*, work, wheel, contract, evidence, ci, reviewed):
    wrapper = work / 'native-origin-child.py'
    wrapper.write_text(ORIGIN_CHILD, encoding='utf-8')
    before, after = evidence / 'native-origins-before.json', evidence / 'native-origins-after.json'
    def snapshot(path, log):
        require(child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
            str(evidence / 'source-wheel-proof.json'), str(ROOT.parent), str(path), str(contract)],
            log, cwd=work, ci=ci, timeout=60) == 0,
            'Installed native language-evidence origin snapshot failed.')
        return checked_json(path, 2 * 1024**2)[0]
    first = snapshot(before, evidence / 'native-origins-before.txt')
    native_log = evidence / 'native-language-evidence.txt'
    native_report = evidence / 'native-language-evidence.json'
    try:
        result = child([sys.executable, '-I', '-B', '-X', 'utf8',
            str(ROOT / NATIVE_FILE), '--installed', '--report', str(native_report)],
            native_log, cwd=work, ci=ci, timeout=330)
    finally:
        last = snapshot(after, evidence / 'native-origins-after.txt')
        require(last == first,
                'Installed wheel or exact origin changed during actual native language evidence work.')
    require(result == 0, 'Actual native language-evidence process failed.')
    report, report_sha = checked_json(native_report, 2 * 1024**2)
    native = reviewed['native']
    fields = native['required_fields']
    require(type(report) is dict and set(report) == set(fields) | {
        'checks', 'module_origins', 'worker_admissions', 'errors'},
        'Actual native language-evidence report schema differs.')
    require(all(type(report[key]) is type(value) and report[key] == value
                for key, value in fields.items()) and report['errors'] == [],
            'Actual native language-evidence result or qualification differs.')
    checks = report['checks']
    require(type(checks) is list and 75 <= len(checks) <= 2048
            and all(type(item) is str and 0 < len(item) <= 512 for item in checks)
            and set(native['required_checks']) <= set(checks),
            'Actual native language-evidence checks are incomplete.')
    logged_checks = [line[6:] for line in native_log.read_text(encoding='utf-8').splitlines()
                     if line.startswith('PASS: ')]
    require(logged_checks == checks
            and all(checks.count(name) == count
                    for name, count in native['required_check_multiplicities'].items()),
            'Native check output, persisted report or actual default-No observations disagree.')
    origins = report['module_origins']
    require(type(origins) is dict and set(origins) == set(native['module_origins'])
            and all(type(origins[name]) is str
                    and origins[name] == first['origins'][name]['path'] for name in origins)
            and len(first['origins']) == 36,
            'Native language-evidence report does not bind the observed installed origins.')
    admissions = report['worker_admissions']
    require(type(admissions) is list and 1 <= len(admissions) <= 128
            and all(type(row) is dict and set(row) == {'title', 'admitted', 'same_executor'}
                    and type(row['title']) is str and 0 < len(row['title']) <= 256
                    and row['admitted'] is True and row['same_executor'] is True
                    for row in admissions),
            'Native work did not retain its actual original shared worker.')
    require(read_contract() == reviewed, 'Reviewed case/native data changed during actual work.')
    return dict(status='PASS', required_checks=75, native_module_origins=14,
        observed_check_occurrences=len(checks), observed_unique_checks=len(set(checks)),
        default_no_observations=3, wheel_origin_inventory=36,
        report_sha256=report_sha, installed_payload_unchanged=True,
        authenticated_reviewer_identity='NOT_RUN', language_quality='NOT_RUN',
        acoustic_inference='NOT_RUN', physical_devices='NOT_RUN', nvda='NOT_RUN',
        native_file_pickers='NOT_RUN', all_135_languages='NOT_RUN')


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
    require(os.name == 'nt', 'The focused language evidence gate requires actual Windows.')
    before = sources()
    reviewed_contract = read_contract()
    own_relative = Path(__file__).resolve().relative_to(ROOT.parent).as_posix()
    require(git('show', args.expected_head + ':' + own_relative) == Path(__file__).read_text(encoding='utf-8').strip(),
            'Orchestrator differs from its committed source.')
    identity = dict(source_commit=args.expected_head, source_tree=git('rev-parse', 'HEAD^{tree}'),
        inputs=before, case_contract=before[CASE_FILE],
        python=sys.version, platform=sys.platform)
    write(evidence / 'identity-before.json', identity)
    ci = load('language_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('language_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    source_log = evidence / 'source-contracts.txt'
    # Five whole suites use two waves with unchanged 600-second child bounds.
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
    with tempfile.TemporaryDirectory(prefix='carr-language-evidence-', dir=work_parent) as tmp:
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
        require(len(MODULES) == len(set(MODULES)) == 36,
                'Every reviewed language, backup, host and bootstrap origin is required.')
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
        native = native_contract(work=work, wheel=wheel, contract=contract,
            evidence=evidence, ci=ci, reviewed=reviewed_contract)
    after = sources()
    require(after == before, 'Pinned sources changed during actual contracts.')
    write(evidence / 'identity-after.json', dict(source_commit=args.expected_head,
        source_tree=git('rev-parse', 'HEAD^{tree}'), inputs=after))
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Source commit changed.')
    report = dict(status='PASS', **identity, source_passed=sum(SUITES.values()),
        installed_passed=sum(SUITES.values()), installed=installed, inputs_unchanged=True,
        application_wheel_sha256=proof['wheel_sha256'], all_children_reaped=True,
        expected_failures=False, selected_whole_suites=SUITES, native=native,
        qualification='Exact language catalog and passive-evidence source/installed contracts and actual native controls with synthetic observations only. Recorded reviewers are not authenticated; real language quality, all135 languages, current-device capability, acoustic inference, physical devices, native file pickers and NVDA remain NOT_RUN.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'all_children_reaped')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
