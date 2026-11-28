"""Run Studio timeline contracts through source, verified wheel and native UI.

Reuse the existing strict runner and exact source-wheel proof. Actual synthetic
PCM, temporary projects and wheel/test payloads remain runner-owned; only bounded
JSON/text/XML evidence is retained. Physical playback and listening are not run.
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
    "tests/studio/test_clip_preview_loop.py": 49,
    "tests/studio/test_project_markers.py": 48,
    "tests/studio/test_marker_consumers.py": 31,
    "tests/studio/test_clip_operations.py": 90,
    "tests/studio/test_clip_editing.py": 34,
    "tests/studio/test_clip_history_dialog.py": 50,
    "tests/studio/test_studio_controller.py": 18
}
PINS = {
    "src/carr_library/studio/clips.py": "598d0c450e3697c699a03c74d806783173fc3ab5",
    "src/carr_library/studio/managed_inputs.py": "fdc163215c53240dd6058e8f4235d2e8aa03db10",
    "src/carr_library/studio/render.py": "3fafc206b592fedd3cf5c836c1b1bc7a0387e5ab",
    "src/carr_library/studio/project.py": "63a20c64909db77b9958e9c16627b78f08050d95",
    "src/carr_library/audio/process.py": "480272d0224770f6b0ec9c8efc8e70d9aab42b0b",
    "src/carr_library/audio/windows_job.py": "27aeb47c39660ac74335d791e22e58b44058db3b",
    "src/carr_library/ui/studio_clips.py": "632da6e1a8f08b4fc6f4ecebe8acaeb98203861a",
    "src/carr_library/studio/clip_operations.py": "a9b2856c445f6d9f67105b754c23deb23fe3e60d",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "src/carr_library/ui/desktop_app.py": "85c0991f744bd521ca247a0e15da93aa585ea30d",
    "src/carr_library/speech/player.py": "6f758c6073e239f771a7b8627713aca8bff51d7f",
    "src/carr_library/speech/playlist.py": "544f845fa425ff643620f68fad777421a85004f4",
    "src/carr_library/speech/progress_speech.py": "40fc50742fb2c50a1bfb2920e25af692017c0aaf",
    "src/carr_library/durable_state.py": "d2b95bde04aa72cdd21ee6b4f0092d3350f84fcb",
    "src/carr_library/operations/broker.py": "f09561333d33ae68480260668ec05b617cd72213",
    "src/carr_library/studio/project_history.py": "ad9dd72a8101639cf03a02420dbb0c2b0c0cc7be",
    "src/carr_library/ui/worker_lifetime.py": "ed62603159bc095724c3b240d4754f989141a2c9",
    "src/carr_library/ui/studio_dialog.py": "ce11e96477841cebe30702001a1b5f4d6107178b",
    "src/carr_library/library_backup.py": "30ed3b152fc1cd26b338200b270e73f2e193a3f9",
    "src/carr_library/backup_producers.py": "0fb5e91a440ded8b62b806b944bdd4722f288e65",
    "src/carr_library/backup_sidecars.py": "5c2ec87913e8ad252dce9aa8cff7ccc22dca3525",
    "src/carr_library/backup_book_audio.py": "4fd92f122d2907c0530fee912c18b45457748682",
    "src/carr_library/state_backup.py": "73e2450f8a912d0fcf70b547fe2cfe4d4750c219",
    "src/carr_library/speech/source.py": "cef32840ecbb5b07c0313e206d9bf622ee20db87",
    "src/carr_library/studio/book_audio.py": "d1e54a397ea5ad22737f34aa1c7160e56759f2bb",
    "src/carr_library/audio/__init__.py": "2346d1b0b458b392f0289da2cc692aa5da7ea87e",
    "src/carr_library/audio/envelopes.py": "52bbc4b960784ac774c77071ba8682b2be5ca60c",
    "src/carr_library/audio/recording.py": "7a71d0975cba5ada05772b43b881998e87b61326",
    "src/carr_library/runtime_settings.py": "00df790d393f9d54f87d0dcf891e6506916437a1",
    "src/carr_library/operations/__init__.py": "7929cba7686f36f15c212833396b5dc9cf464791",
    "src/carr_library/operations/models.py": "1b8e354327d20dc134a30783c648b88f37cfa337",
    "src/carr_library/native_runtime.py": "50ca93914325753f7d9cb91f1bc4f48fc9ae230f",
    "src/carr_library/audio/dsp.py": "f7833b5f600e3a822a3fa2f2068661c922576230",
    "src/carr_library/speech/__init__.py": "f3a90829d0e3780e4fd88512f200e64a947c83a8",
    "bootstrap/__init__.py": "6981b7faa1cca8d29f37fe85751c110c693dae55",
    "tests/studio/test_clip_preview_loop.py": "c2690ed5b36b2860238e6dc1d5d9420f355baf54",
    "tests/studio/test_project_markers.py": "088083c06c452fa5fbac9e14df1786aba524d3d7",
    "tests/studio/test_marker_consumers.py": "0877334ad369cc03b3d48edecfdd00b1a8202c9b",
    "tests/studio/test_clip_operations.py": "a47af2be53416f2a9588a20381063f63422f0940",
    "tests/studio/test_clip_editing.py": "27a3d1d3a156acd48090a08ca7f6264050f2c771",
    "tests/studio/test_clip_history_dialog.py": "d5675f91c0b6285ea9188fa90d4ca416cd6967dc",
    "tests/studio/test_studio_controller.py": "93a093e395388b69d6b7a76f22d7432cc8b60c92",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "packaging/prepare_windows_audio.py": "65b172ac432f28e865200aed2f8e610dda1ac7cf",
    "packaging/windows-audio.lock.json": "2bc706ffc6208f5596a10abee61d3da33f20e6c6",
    "packaging/windows-cp311.lock": "8dad99c0ea80470b7253873aee78e08857affec6",
    "packaging/windows-sync-cp311.lock": "cd9c18a24d7a7ac203d81cf6a8d82d8bcb8f8ab3",
    "packaging/trusted-client.lock": "6c4b48ac09572dc259d62d0d801057ab47c04583",
    "packaging/windows-sync-test-cp311.lock": "e3f6eda5976e867abe7267c04e0b867b1fd16f7a",
    "scripts/windows_clip_editor_smoke.py": "e202ebe7f61490d2745d315f31330f97372ec705",
    "scripts/ci_studio_timeline_cases.json": "5d6c9fa8eed10b037bdb056ce0d942a0ce21de00",
    "scripts/check_studio_timeline_native.py": "cc67469e59aee71504b60640b61619171e7566f1",
    "docs/STUDIO_CLIP_HISTORY.md": "b1ced7ecd98cebfdb9b0b817d9617e3bee2ca625"
}
COMPANIONS = ["scripts/windows_clip_editor_smoke.py"]
MODULES = ["carr_library.studio.clips","carr_library.studio.managed_inputs","carr_library.studio.render","carr_library.studio.project","carr_library.audio.process","carr_library.audio.windows_job","carr_library.ui.studio_clips","carr_library.studio.clip_operations","carr_library.application","carr_library.ui.desktop_app","carr_library.speech.player","carr_library.speech.playlist","carr_library.speech.progress_speech","carr_library.durable_state","carr_library.operations.broker","carr_library.studio.project_history","carr_library.ui.worker_lifetime","carr_library.ui.studio_dialog","carr_library.library_backup","carr_library.backup_producers","carr_library.backup_sidecars","carr_library.backup_book_audio","carr_library.state_backup","carr_library.speech.source","carr_library.studio.book_audio","carr_library.audio","carr_library.audio.envelopes","carr_library.audio.recording","carr_library.runtime_settings","carr_library.operations","carr_library.operations.models","carr_library.native_runtime","carr_library.audio.dsp","carr_library.speech","bootstrap"]



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

NATIVE_CHECKER = 'scripts/check_studio_timeline_native.py'


def audio_inputs(selected):
    require(os.name == 'nt', 'This exact native Studio contract needs actual Windows.')
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
    def checker(phase, log, *extra):
        # Bind the same reviewed helper before both independent pre/post calls.
        path = ROOT / NATIVE_CHECKER
        require(path.is_file() and not path.is_symlink(),
                'Shared native checker is not an ordinary source file.')
        raw = path.read_bytes()
        require(sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
                == PINS[NATIVE_CHECKER], 'Shared native checker changed.')
        return child([sys.executable, '-I', '-B', '-X', 'utf8', str(path), phase,
                      '--log', str(native_log), '--audio-directory', str(audio), *extra],
                     log, cwd=ROOT, ci=ci, timeout=60)
    first = snapshot(before, evidence / 'native-origins-before.txt')
    native_log = evidence / 'native-studio-timeline.txt'
    try:
        require(checker('pre', evidence / 'native-report-before.txt') == 0,
                'Native preflight did not bind the actual installed source and audio.')
        pre = native_log.with_suffix('.pre.json')
        require(pre.is_file() and pre.stat().st_size <= 8 * 1024**2,
                'Native preflight receipt is missing or exceeds its finite bound.')
        proof_digest = sha256(pre.read_bytes()).hexdigest()
        result = child([sys.executable, '-I', '-B', '-X', 'utf8',
            str(ROOT / 'scripts/windows_clip_editor_smoke.py'), '--audio-directory', str(audio),
            '--audio-lock', str(ROOT / 'packaging/windows-audio.lock.json')],
            native_log, cwd=ROOT, ci=ci, timeout=330)
    finally:
        last = snapshot(after, evidence / 'native-origins-after.txt')
        require(last == first, 'Installed wheel or native origin changed during actual Studio work.')
    require(result == 0, 'Actual native Studio process failed; no synthetic transport is admitted.')
    require(checker('post', evidence / 'native-report-validation.txt',
                    '--proof-sha256', proof_digest) == 0,
            'Native report does not match its original preflight and current source.')
    report = native_log.with_suffix('.json')
    require(report.is_file(), 'Actual native Studio report is incomplete.')
    require(len(first['origins']) == 35, 'Studio installed identity inventory differs.')
    return dict(status='PASS', required_checks=92, native_module_origins=15,
                wheel_origin_inventory=35, report_sha256=sha256(report.read_bytes()).hexdigest(),
                preflight_sha256=proof_digest, installed_payload_unchanged=True,
                windows11_qualification='NOT_RUN', bundled_runtime_qualification='NOT_RUN',
                repeat_playback='NOT_RUN', physical_audio_output='NOT_RUN',
                acoustic_quality='NOT_RUN', nvda='NOT_RUN')


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
    ci = load('studio_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('studio_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    source_log = evidence / 'source-contracts.txt'
    # Seven complete reviewed suites keep the existing 600 s child limits.
    # The same two-worker source runner and aggregate bound remain unchanged.
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
    with tempfile.TemporaryDirectory(prefix='carr-studio-contracts-', dir=work_parent) as tmp:
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
        require(len(MODULES) == len(set(MODULES)) == 35,
                'Studio proof requires all reviewed native/test origins and bootstrap.')
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
        qualification='Exact Studio source/installed contracts, native crossfade/markers and passive loop lifecycle only; actual repeat playback, physical output and listening are NOT_RUN.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'all_children_reaped')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
