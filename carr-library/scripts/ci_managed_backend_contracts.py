"""Focused actual child protocols for managed reference backends.

Five whole modules: OmniVoice/OpenVoice40+6, unchanged legacy40+10,
and fresh current-scope42 regressions.
Both source and isolated installed execution are required. This separate gate
never replaces the complete voice-manager, actual-owner or native UI gates.
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
    "tests/integrations/test_dordiai_reference_backends.py": 40,
    "tests/integrations/test_dordiai_managed_backends.py": 6,
    "tests/integrations/test_dordiai_voices.py": 40,
    "tests/integrations/test_dordiai_speech.py": 10,
    "tests/audio/test_voice_scope_checks.py": 42
}
PINS = {
    "src/carr_library/audio/voice_manifest.py": "2b5cc075286d2892d1262cddb15bcb84a737d007",
    "src/carr_library/audio/voice_transport.py": "f4a9f626688cd26e91f5ed0ea2c1813c9a68897e",
    "src/carr_library/audio/voice_settings.py": "64e745302ea2bffb84dc0d713be9d1e0f70eff1e",
    "src/carr_library/runtime_settings.py": "00df790d393f9d54f87d0dcf891e6506916437a1",
    "src/carr_library/audio/voice_packages.py": "824275dcace7617ce65123d469537593877d51d3",
    "src/carr_library/audio/package_publication.py": "702cb573482265bb7e63d9483ecf2178649a63a4",
    "src/carr_library/audio/voice_lifecycle.py": "0508ffba185e99171d0d2e230a9adb06db05bbae",
    "src/carr_library/audio/voice_reference.py": "8d3265dcc37662046a3bebcf2abacf317c818553",
    "src/carr_library/audio/voice_restore.py": "44844682df24a5d3934830e5ae0d3d78d09a9a6a",
    "src/carr_library/state_backup.py": "7027e3f07f967403d6e523aa23c8233176a43222",
    "src/carr_library/library_backup.py": "30ed3b152fc1cd26b338200b270e73f2e193a3f9",
    "src/carr_library/ui/voice_packages.py": "bb32612b95eaee36bd6d7517dc3369c5a2e4d6da",
    "src/carr_library/ui/voice_reference.py": "53578efe02f434c72ebd3b3acae95eb729671659",
    "src/carr_library/ui/engine_settings.py": "e2ca712b9df71d7808b0fdb620978772c1f4dd65",
    "src/carr_library/integrations/_dordiai_authority.py": "df687d0d557fbc57af8c44ab029b7dd080084721",
    "src/carr_library/integrations/dordiai_voices.py": "b402a6cb86ee150dc67d5654a3495289269f10aa",
    "src/carr_library/integrations/dordiai_speech.py": "85c9248923b6332da9ea30d7b0dc644f90dc77ba",
    "src/carr_library/audio/voice_ownership.py": "2b1dc1d7de15d65bababa7ebf5e3a13191e1c71d",
    "src/carr_library/audio/models.py": "35694ae7cb9ad00e6a37ee11e18cdf60c6b2d7f6",
    "src/carr_library/audio/dsp.py": "f7833b5f600e3a822a3fa2f2068661c922576230",
    "src/carr_library/audio/cleanup.py": "77d69f8406db93dc0ff6110b9597ad805bd92dc5",
    "src/carr_library/audio/process.py": "480272d0224770f6b0ec9c8efc8e70d9aab42b0b",
    "src/carr_library/operations/broker.py": "f09561333d33ae68480260668ec05b617cd72213",
    "src/carr_library/operations/_locks.py": "ae161b8483f84b2a4aa2f153b4b11b08f76b9693",
    "src/carr_library/durable_state.py": "d2b95bde04aa72cdd21ee6b4f0092d3350f84fcb",
    "src/carr_library/file_publication.py": "2ce3bdaa2c489cfeff301907e29ca6b552a2f90f",
    "src/carr_library/research/export_file.py": "335fd4d03ed4e612d628ec2f93b615feba49e97d",
    "src/carr_library/studio/publishing.py": "e369f0d40cbc818d9374e1d4f84bc3e28e09ad63",
    "src/carr_library/sync/crypto.py": "681a08530212f8cb18f600cfaf9314cc36a787e9",
    "src/carr_library/speech/__init__.py": "f3a90829d0e3780e4fd88512f200e64a947c83a8",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "src/carr_library/native_runtime.py": "50ca93914325753f7d9cb91f1bc4f48fc9ae230f",
    "bootstrap/__init__.py": "6981b7faa1cca8d29f37fe85751c110c693dae55",
    "src/carr_library/audio/windows_job.py": "27aeb47c39660ac74335d791e22e58b44058db3b",
    "src/carr_library/ui/desktop_app.py": "85c0991f744bd521ca247a0e15da93aa585ea30d",
    "src/carr_library/ui/worker_lifetime.py": "ed62603159bc095724c3b240d4754f989141a2c9",
    "src/carr_library/ui/recording_dialog.py": "d2447529f7f5ac6ee07de2e06c9da7189ee3c067",
    "tests/audio/test_voice_package_management.py": "65ab95aa77c62224f7a42d7e9453f013cbb77ec5",
    "tests/audio/test_voice_management_state.py": "a3f4372f0454d18e6a249b5a4248bea019aaae80",
    "tests/audio/test_voice_reference_management.py": "48da3169be41c3524ebd5af86e7af686450013a2",
    "tests/audio/test_voice_protected_management.py": "81350202f697d579e1822284c17f0252e2b294db",
    "tests/audio/test_voice_publication_lifetime.py": "1f71856039c5d3e94312714f020021110f1e9b84",
    "tests/audio/test_voice_ownership.py": "078cdf3a83f84c390f6a242b59d4a19b9b9d52c9",
    "tests/integrations/test_dordiai_owner_lease.py": "282bc60cf1d94ce29a32602a6ead0440def271e8",
    "tests/integrations/test_dordiai_managed_authority.py": "7e1522e102826b75eea702d5ddc44eec2afe86f5",
    "tests/ui/test_voice_packages.py": "b179baf1923d9fa96597eba0bf985ab270ec1116",
    "tests/ui/test_voice_reference.py": "ed1c77898b3ff72bc3246c8a8537ca3b5961e945",
    "tests/ui/test_engine_settings_lifetime.py": "fd4674f62ec4a8696e08618b7f30fa72a7127537",
    "tests/integrations/test_dordiai_voices.py": "e46257220ef1af5c129669aa9037dafa788077af",
    "tests/integrations/test_dordiai_speech.py": "1b8191f2a59c9eea4da662423c1b8bd4cd03fb19",
    "tests/integrations/test_dordiai_host_lifecycle.py": "54eea6081c72b32b8295addae25f12d399bd5d9e",
    "tests/integrations/test_dordiai_settings.py": "3f4a49f3ef04d72cd4ad05d7ca4fe170925359a8",
    "tests/integrations/test_dordiai_probe_cancellation.py": "96ec861256678a75010ca3ec9b1620d13876d866",
    "tests/integrations/test_dordiai_convergence.py": "e18e02267f7a3d3a7fdaed909abff8b1c8e78e92",
    "tests/audio/test_bundled_runtime_settings.py": "1e48791b33ae256ff5ede4ac7c24dd64afb78beb",
    "tests/storage/test_v3_state_backup.py": "feb3ab5bc89db85614d1bb947e6f6a34e5578e64",
    "tests/storage/test_library_backup.py": "d14b278b356bb47fe66e822125a257807f2cd895",
    "tests/storage/test_state_backup_portability.py": "27a9bf1ee349b48c557bd6f38f11f7ed15a99930",
    "tests/audio/voice_management_fixtures.py": "ef5be0ea225a4222146edfb05ff03f9037dd0eb5",
    "tests/integrations/__init__.py": "1ca9f3fafe08709103c64426a7c5311eb3f28455",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "packaging/windows-audio.lock.json": "2bc706ffc6208f5596a10abee61d3da33f20e6c6",
    "scripts/windows_voice_management_smoke.py": "7f1f806035ab334713a2b9c13c21bbca59bb09f0",
    "scripts/ci_owner_voice_api_smoke.py": "e22c226ef24cf48957d04c32f25cb472e48c764e",
    "scripts/ci_voice_management_owner_smoke.py": "14bc8f48cd5c5fc4ffeca5a37a973c7860f7efef",
    "src/carr_library/catalog/intake.py": "8518f012491b542a57593af75993907983ddda0d",
    "tests/integrations/test_dordiai_reference_backends.py": "de40ac027c6706886d248edaea14dec7bea36608",
    "tests/integrations/test_dordiai_managed_backends.py": "1f03119d76dbf72b839c004aacc70baea0bb8657",
    "src/carr_library/integrations/_dordiai_probe.py": "44fd0e0a169dc15c759a2d8a020341168b33defc",
    "tests/audio/test_voice_scope_checks.py": "1397674e847d3128ac7a38f2463103ad0b3c06de",
}
COMPANIONS = ["tests/audio/voice_management_fixtures.py","tests/integrations/__init__.py"]
BASE_MODULES = ["carr_library.audio.voice_manifest","carr_library.audio.voice_transport","carr_library.audio.voice_settings","carr_library.runtime_settings","carr_library.audio.voice_packages","carr_library.audio.package_publication","carr_library.audio.voice_lifecycle","carr_library.audio.voice_reference","carr_library.audio.voice_restore","carr_library.state_backup","carr_library.library_backup","carr_library.ui.voice_packages","carr_library.ui.voice_reference","carr_library.ui.engine_settings","carr_library.integrations._dordiai_authority","carr_library.integrations.dordiai_voices","carr_library.integrations.dordiai_speech","carr_library.audio.voice_ownership","carr_library.audio.models","carr_library.audio.dsp","carr_library.audio.cleanup","carr_library.audio.process","carr_library.operations.broker","carr_library.operations._locks","carr_library.durable_state","carr_library.file_publication","carr_library.research.export_file","carr_library.studio.publishing","carr_library.sync.crypto","carr_library.speech","carr_library.application","carr_library.native_runtime","bootstrap","carr_library.catalog.intake","carr_library.audio.windows_job"]
WINDOWS_MODULES = ["carr_library.audio.voice_manifest","carr_library.audio.voice_transport","carr_library.audio.voice_settings","carr_library.runtime_settings","carr_library.audio.voice_packages","carr_library.audio.package_publication","carr_library.audio.voice_lifecycle","carr_library.audio.voice_reference","carr_library.audio.voice_restore","carr_library.state_backup","carr_library.library_backup","carr_library.ui.voice_packages","carr_library.ui.voice_reference","carr_library.ui.engine_settings","carr_library.integrations._dordiai_authority","carr_library.integrations.dordiai_voices","carr_library.integrations.dordiai_speech","carr_library.audio.voice_ownership","carr_library.audio.models","carr_library.audio.dsp","carr_library.audio.cleanup","carr_library.audio.process","carr_library.operations.broker","carr_library.operations._locks","carr_library.durable_state","carr_library.file_publication","carr_library.research.export_file","carr_library.studio.publishing","carr_library.sync.crypto","carr_library.speech","carr_library.application","carr_library.native_runtime","bootstrap","carr_library.audio.windows_job","carr_library.ui.desktop_app","carr_library.ui.worker_lifetime","carr_library.ui.recording_dialog","carr_library.catalog.intake"]
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



def audio_inputs(selected):
    rows = {}
    pin = (json.loads((ROOT / 'packaging/windows-audio.lock.json').read_text(encoding='utf-8'))
           if os.name == 'nt' else None)
    for name in ('ffmpeg', 'ffprobe'):
        path = (selected / 'bin' / (name + ('.exe' if os.name == 'nt' else ''))).resolve(strict=True)
        found = shutil.which(name)
        require(found and Path(found).resolve(strict=True) == path,
                'Actual codec discovery differs from the explicitly selected test runtime: ' + name)
        before = path.stat()
        require(path.is_file() and not path.is_symlink() and before.st_nlink == 1,
                'Selected codec is not an ordinary unlinked file.')
        with path.open('rb') as stream:
            digest = file_digest(stream, 'sha256').hexdigest()
        after = path.stat()
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                'Selected codec changed while hashing.')
        if pin is not None:
            expected = pin['output_files']['bin/' + name + '.exe']
            require(before.st_size == expected['size'] and digest == expected['sha256'],
                    'Selected Windows codec bytes differ from the existing lock.')
        rows[name] = dict(path=str(path), size=before.st_size, sha256=digest,
                         binding='existing locked Windows test runtime' if pin is not None
                                 else 'explicit current Linux system test codec, exact before/after bytes')
    return rows


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
        inputs=before, audio_runtime=audio_before,
        python=sys.version, platform=sys.platform)
    write(evidence / 'identity-before.json', identity)
    ci = load('voice_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('voice_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
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
    with tempfile.TemporaryDirectory(prefix='carr-managed-backends-', dir=work_parent) as tmp:
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
        require(len(MODULES) == len(set(MODULES)) == (38 if os.name == 'nt' else 35),
                'Every selected voice-management/provider origin and bootstrap are required.')
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
    require(audio_inputs(selected_audio) == audio_before, 'Selected codec changed during actual contracts.')
    require(sources() == before, 'Pinned sources changed during actual contracts.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Source commit changed.')
    report = dict(status='PASS', **identity, source_passed=sum(SUITES.values()),
        installed_passed=sum(SUITES.values()), installed=installed, inputs_unchanged=True,
        application_wheel_sha256=proof['wheel_sha256'], all_children_reaped=True,
        expected_failures=False, selected_whole_suites=SUITES, native='NOT_RUN', actual_owner='NOT_RUN',
        qualification='Exact whole managed reference protocol40+6 and unchanged legacy40+10 source and isolated installed contracts. Synthetic actual children establish host protocol and cleanup only; native UI, actual model inference, acoustics, recording, NVDA and release remain separate.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'all_children_reaped')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
