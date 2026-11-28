"""Run Book effects and measured-part contracts through source, wheel and native UI.

Reuse the existing strict runner and exact source-wheel proof. All actual audio,
synthetic source and temporary wheel/test payloads remain runner-owned; only
bounded JSON/text/XML evidence is retained. No acoustic/model/release claim.
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
    "tests/studio/test_book_effects.py": 14,
    "tests/studio/test_measured_audio.py": 36,
    "tests/studio/test_measured_pcm.py": 19,
    "tests/studio/test_book_measured_cli.py": 9,
    "tests/ui/test_book_effects.py": 5,
    "tests/ui/test_book_duration_dialog.py": 17,
    "tests/ui/test_native_book_publication_driver.py": 8,
}
PINS = {
    "src/carr_library/studio/book_audio.py": "d1e54a397ea5ad22737f34aa1c7160e56759f2bb",
    "src/carr_library/studio/measured_audio.py": "3e4227b536a5a40edde371c829f44bd20f28685f",
    "src/carr_library/studio/publishing.py": "e369f0d40cbc818d9374e1d4f84bc3e28e09ad63",
    "src/carr_library/ui/book_audio_dialog.py": "0d89734850a1a1954ff0ca8312e4f6575b6c3b30",
    "src/carr_library/ui/book_duration_dialog.py": "0f9daced19fca2bf4fd60cf91f96a493d07cdb75",
    "src/carr_library/studio/book_cli.py": "38a26a21e314a8872b01393edd1801fb5e3ea273",
    "tests/studio/test_book_effects.py": "76453a8a97571696f494258425191236e76205b0",
    "tests/studio/test_measured_audio.py": "717ad80542669ecf0543ea6d971f3e7f2e849444",
    "tests/studio/test_measured_pcm.py": "a8cb8ecfc05508ef33a6b8a0e2f123cc197a6ae0",
    "tests/studio/test_book_measured_cli.py": "07d107d1c10eb341f43cfcd8a0823ba1f93554cb",
    "tests/ui/test_book_effects.py": "12a47c7a05879eeaeab7047dd7eddac17dbf9cdb",
    "tests/ui/test_book_duration_dialog.py": "d9516c2bf87ea38c34149d87185cb201ba3a34f5",
    "scripts/windows_book_audio_smoke.py": "fe4d7f64686da37557e3913805f8d27a4a130724",
    "tests/ui/test_native_book_publication_driver.py": "f9b2475291eae1724fef366ba59ab86e83a02792",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "packaging/prepare_windows_audio.py": "65b172ac432f28e865200aed2f8e610dda1ac7cf",
    "packaging/windows-audio.lock.json": "2bc706ffc6208f5596a10abee61d3da33f20e6c6",
    "packaging/windows-cp311.lock": "8dad99c0ea80470b7253873aee78e08857affec6",
    "packaging/windows-sync-cp311.lock": "cd9c18a24d7a7ac203d81cf6a8d82d8bcb8f8ab3",
    "packaging/trusted-client.lock": "6c4b48ac09572dc259d62d0d801057ab47c04583",
    "packaging/windows-sync-test-cp311.lock": "e3f6eda5976e867abe7267c04e0b867b1fd16f7a",
    "src/carr_library/native_runtime.py": "50ca93914325753f7d9cb91f1bc4f48fc9ae230f",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "src/carr_library/backup_book_audio.py": "4fd92f122d2907c0530fee912c18b45457748682",
    "src/carr_library/library_backup.py": "30ed3b152fc1cd26b338200b270e73f2e193a3f9",
    "src/carr_library/backup_producers.py": "c8596f4cb9792a89d3f76d08347b9ebbc963439e",
    "src/carr_library/backup_sidecars.py": "5c2ec87913e8ad252dce9aa8cff7ccc22dca3525",
    "src/carr_library/ui/worker_lifetime.py": "ed62603159bc095724c3b240d4754f989141a2c9",
    "src/carr_library/studio/render.py": "11925e2f60742a3193d2ae50238ab56ae8889534",
    "src/carr_library/studio/project.py": "72b3260421afbfcfa912481798967e59ce9d13de",
    "src/carr_library/reader/reading_desk.py": "0acd7f25fb17de24b434741ff3b7cf2c634a4c8a",
    "src/carr_library/speech/source.py": "cef32840ecbb5b07c0313e206d9bf622ee20db87",
    "src/carr_library/runtime_settings.py": "00df790d393f9d54f87d0dcf891e6506916437a1",
    "src/carr_library/operations/broker.py": "107e1a600bb4bdf0f3edb7735ee5fcdb68d9fb36",
    "src/carr_library/audio/dsp.py": "f7833b5f600e3a822a3fa2f2068661c922576230",
    "src/carr_library/audio/process.py": "480272d0224770f6b0ec9c8efc8e70d9aab42b0b",
    "src/carr_library/ui/desktop_app.py": "85c0991f744bd521ca247a0e15da93aa585ea30d",
    "src/carr_library/ui/effects_dialog.py": "d48b985ccb38b5a48123bc2a2c39afb2b2f452e4",
    "src/carr_library/audio/effect_presets.py": "8ad8b5730b60fa6531904f83360232e7a697e834",
    "src/carr_library/audio/envelopes.py": "52bbc4b960784ac774c77071ba8682b2be5ca60c",
    "tests/studio/test_book_audio.py": "f98140e413d0fb32784493045c0882011f6a081c",
    "tests/ui/test_book_audio_dialog.py": "180dda968201569a11711f44e406b82443c9945c",
    "tests/reader/test_ebook_reader.py": "39f3837d66bac1dc69ab9b2b05f9e31e6e1be849",
}
COMPANIONS = ["tests/studio/test_book_audio.py","tests/ui/test_book_audio_dialog.py","tests/reader/test_ebook_reader.py","scripts/windows_book_audio_smoke.py"]
MODULES = ["carr_library.studio.book_audio","carr_library.ui.book_audio_dialog","carr_library.ui.worker_lifetime","carr_library.studio.render","carr_library.studio.project","carr_library.reader.reading_desk","carr_library.speech.source","carr_library.runtime_settings","carr_library.operations.broker","carr_library.audio.dsp","carr_library.audio.process","carr_library.ui.desktop_app","carr_library.studio.measured_audio","carr_library.studio.publishing","carr_library.ui.book_duration_dialog","carr_library.ui.effects_dialog","carr_library.audio.effect_presets","carr_library.audio.envelopes","carr_library.studio.book_cli","bootstrap"]


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

NATIVE_VERIFY = r"""import json, pathlib, re, sys
raw = pathlib.Path(sys.argv[2]).read_text(encoding='utf-8-sig')
reports = []
for match in re.finditer(r'(?m)^\{', raw):
    try:
        item, _ = json.JSONDecoder().raw_decode(raw[match.start():])
    except ValueError:
        continue
    if isinstance(item, dict) and 'native_sapi_render' in item:
        reports.append(item)
assert len(reports) == 1, 'Native book narration report is missing or ambiguous.'
report = reports[0]
assert report['status'] == report['native_keyboard'] == report['native_sapi_render'] == report['native_failure_cleanup'] == report['native_role_effects'] == report['native_measured_duration'] == 'PASS'
required = set(["real native Windows controls","actual retained EPUB import completes","existing shared worker owns finalizers","Book audio command is registered in the existing keyboard registry","current Books and Collections exposes the selected retained edition","installed native SAPI voice is actually enumerated","native preview names both real EPUB chapters and original revision","preview and voice discovery do not synthesize","default-No cancellation prepares no jobs or output","Prepare stores one immutable plan and two ordinary schema2 jobs","Prepare still starts no speech or output","native explicit Run completes both source-bound Studio jobs","actual selected SAPI renders the literal source without cues","measured complete chapter PCM: 1","measured complete chapter PCM: 2","published exact non-authorizing source receipt: 1","published exact non-authorizing source receipt: 2","archived source blocks completed-job reuse visibly","source denial preserves every previously committed artifact","original EPUB and retained asset remain byte-identical","book narration never creates quotation approval","native Escape closes the book dialog","native host shutdown has no retained cleanup failure","exact installed source: carr_library.studio.book_audio","exact installed source: carr_library.ui.book_audio_dialog","exact installed source: carr_library.ui.worker_lifetime","exact installed source: carr_library.studio.render","exact installed source: carr_library.studio.project","exact installed source: carr_library.reader.reading_desk","exact installed source: carr_library.speech.source","exact installed source: carr_library.runtime_settings","exact installed source: carr_library.operations.broker","exact installed source: carr_library.audio.dsp","exact installed source: carr_library.audio.process","exact installed source: carr_library.ui.desktop_app","native failure answers only owned question No before unwinding active modal","Book role editor offers existing typed gain and omits timeline-changing tempo","Book effect numeric editor has one actual gain control","Book ordered effect review retains explicit numeric value","applying Book role effects invalidates the previous preparation review","native Book preview displays the exact ordered role effects","explicit Book effects create Plan2 and existing Project3 without changing old Plan1","real SAPI Book jobs render their reviewed current Studio role effects","effect-bearing Book master contains actual PCM","native role gain measurably changes completed masters while reusing SAPI","native measured preview displays actual frames and honest whole-line scope","measured preview starts no additional speech and publishes no file","measured default-No confirmation preserves absent output","native measured part has exact current PCM geometry","actual native measured parts concatenate to every original master frame","measured publication retains the exact effect-bearing Book source receipt","measured publication finishes on shared worker without another speech call","closing measured child keeps the original Book service and resumed modal alive","exact installed source: carr_library.studio.measured_audio","exact installed source: carr_library.studio.publishing","exact installed source: carr_library.ui.book_duration_dialog","exact installed source: carr_library.ui.effects_dialog","exact installed source: carr_library.audio.effect_presets","exact installed source: carr_library.audio.envelopes","unchanged installed source after Book work: carr_library.studio.book_audio","unchanged installed source after Book work: carr_library.ui.book_audio_dialog","unchanged installed source after Book work: carr_library.ui.worker_lifetime","unchanged installed source after Book work: carr_library.studio.render","unchanged installed source after Book work: carr_library.studio.project","unchanged installed source after Book work: carr_library.reader.reading_desk","unchanged installed source after Book work: carr_library.speech.source","unchanged installed source after Book work: carr_library.runtime_settings","unchanged installed source after Book work: carr_library.operations.broker","unchanged installed source after Book work: carr_library.audio.dsp","unchanged installed source after Book work: carr_library.audio.process","unchanged installed source after Book work: carr_library.ui.desktop_app","unchanged installed source after Book work: carr_library.studio.measured_audio","unchanged installed source after Book work: carr_library.studio.publishing","unchanged installed source after Book work: carr_library.ui.book_duration_dialog","unchanged installed source after Book work: carr_library.ui.effects_dialog","unchanged installed source after Book work: carr_library.audio.effect_presets","unchanged installed source after Book work: carr_library.audio.envelopes"])
assert len(required) == 77
assert required <= set(report['checks']), sorted(required - set(report['checks']))
assert all(report[key] == 'NOT_RUN' for key in ('manual_keyboard', 'nvda', 'listening_quality', 'private_model_inference'))
selected = pathlib.Path(sys.argv[1]).resolve(strict=True)
pin = json.loads(pathlib.Path('packaging/windows-audio.lock.json').read_text(encoding='utf-8'))
assert set(report['audio_runtime']) == {'ffmpeg', 'ffprobe'}
for name, evidence in report['audio_runtime'].items():
    assert evidence['sha256'] == pin['output_files']['bin/' + name + '.exe']['sha256']
    assert pathlib.Path(evidence['path']).resolve(strict=True) == selected / 'bin' / (name + '.exe')
pathlib.Path(sys.argv[3]).write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report), flush=True)
"""


def audio_inputs(selected):
    require(os.name == 'nt', 'This exact native Book contract needs actual Windows.')
    pin = json.loads((ROOT / 'packaging/windows-audio.lock.json').read_text(encoding='utf-8'))
    rows = {}
    for name in ('ffmpeg', 'ffprobe'):
        path = (selected / 'bin' / (name + '.exe')).resolve(strict=True)
        found = shutil.which(name)
        require(found and Path(found).resolve(strict=True) == path,
                'Actual codec discovery differs from the explicitly selected runtime: ' + name)
        before = path.stat()
        require(path.is_file() and not path.is_symlink() and before.st_nlink == 1,
                'Selected codec is not an ordinary unlinked file.')
        with path.open('rb') as stream:
            digest = file_digest(stream, 'sha256').hexdigest()
        after = path.stat()
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                'Selected codec changed while hashing.')
        expected = pin['output_files']['bin/' + name + '.exe']
        require(before.st_size == expected['size'] and digest == expected['sha256'],
                'Selected codec bytes differ from the existing lock.')
        rows[name] = dict(path=str(path), size=before.st_size, sha256=digest)
    return rows


def native_contract(*, work, wheel, contract, evidence, audio, ci):
    wrapper = work / 'native-origin-child.py'
    wrapper.write_text(ORIGIN_CHILD, encoding='utf-8')
    before, after = evidence / 'native-origins-before.json', evidence / 'native-origins-after.json'
    def snapshot(path, log):
        require(child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
            str(evidence / 'source-wheel-proof.json'), str(ROOT.parent), str(path), str(contract)],
            log, cwd=work, ci=ci, timeout=60) == 0, 'Installed native origin snapshot failed.')
        return json.loads(path.read_text(encoding='utf-8'))
    first = snapshot(before, evidence / 'native-origins-before.txt')
    raw = evidence / 'native-book-audio.txt'
    try:
        result = child([sys.executable, '-I', '-B', '-X', 'utf8',
            str(ROOT / 'scripts/windows_book_audio_smoke.py'), '--audio-directory', str(audio),
            '--audio-lock', str(ROOT / 'packaging/windows-audio.lock.json')],
            raw, cwd=ROOT, ci=ci, timeout=330)
    finally:
        last = snapshot(after, evidence / 'native-origins-after.txt')
        require(last == first, 'Installed wheel or native origin changed during actual Book work.')
    require(result == 0, 'Actual native Book process failed; no synthetic substitute is allowed.')
    report = evidence / 'native-book-audio.json'
    code = child([sys.executable, '-I', '-B', '-X', 'utf8', '-c', NATIVE_VERIFY,
                  str(audio), str(raw), str(report)],
                  evidence / 'native-report-validation.txt', cwd=ROOT, ci=ci, timeout=60)
    require(code == 0 and report.is_file(), 'Actual native Book report is incomplete.')
    value = json.loads(report.read_text(encoding='utf-8'))
    require(len(first['origins']) == 20, 'Native plus CLI/bootstrap identity inventory differs.')
    return dict(status='PASS', required_checks=77, native_module_origins=18,
                wheel_origin_inventory=20, report_sha256=sha256(report.read_bytes()).hexdigest(),
                installed_payload_unchanged=True,
                windows11_qualification='NOT_RUN', bundled_runtime_qualification='NOT_RUN',
                acoustic_quality='NOT_RUN', private_model_inference='NOT_RUN')


def main():
    # Isolated Python ignores PYTHONUTF8; display the retained UTF-8 child log.
    # This changes only this orchestrator's stream, not child/test encodings.
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--audio-directory', required=True, type=Path)
    args = parser.parse_args()
    require(__debug__ and re.fullmatch('[0-9a-f]{40}', args.expected_head), 'Exact commit and assertions required.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Checkout differs from exact event head.')
    evidence = args.evidence.resolve()
    evidence.mkdir(mode=0o700, exist_ok=False)
    before = sources()
    selected_audio = args.audio_directory.resolve(strict=True)
    audio_before = audio_inputs(selected_audio)
    own_relative = Path(__file__).resolve().relative_to(ROOT.parent).as_posix()
    require(git('show', args.expected_head + ':' + own_relative) == Path(__file__).read_text(encoding='utf-8').strip(),
            'Orchestrator differs from its committed source.')
    identity = dict(source_commit=args.expected_head, source_tree=git('rev-parse', 'HEAD^{tree}'),
        inputs=before, audio_runtime=audio_before, python=sys.version, platform=sys.platform)
    write(evidence / 'identity-before.json', identity)
    ci = load('book_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('book_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    source_log = evidence / 'source-contracts.txt'
    # The six product suites retain their strict 600 s child limits. The
    # portable probe driver is additional; aggregate/reporting stays bounded.
    code = child([sys.executable, '-B', str(ROOT / 'scripts/ci_check.py'),
                  '--jobs', '2', *SUITES], source_log, cwd=ROOT, ci=ci, timeout=2000)
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
    with tempfile.TemporaryDirectory(prefix='carr-book-contracts-', dir=work_parent) as tmp:
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
                'Book proof requires all 18 native modules, Book CLI and bootstrap.')
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
            evidence=evidence, audio=selected_audio, ci=ci)
    require(audio_inputs(selected_audio) == audio_before, 'Selected codec changed during actual contracts.')
    require(sources() == before, 'Pinned sources changed during actual contracts.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Source commit changed.')
    report = dict(status='PASS', **identity, source_passed=sum(SUITES.values()),
        installed_passed=sum(SUITES.values()), installed=installed, inputs_unchanged=True,
        application_wheel_sha256=proof['wheel_sha256'], all_children_reaped=True,
        expected_failures=False, native=native,
        qualification='Exact Book source/installed contracts and required Windows controls/SAPI/PCM only; no acoustic/model or release claim.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'all_children_reaped')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
