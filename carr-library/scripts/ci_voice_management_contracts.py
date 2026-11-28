"""Run whole current voice-management/provider contracts from source and a verified wheel.

Both current-manager phases use the exact acquired public owner metadata API.
Actual native Windows management is required on Windows. Private model inference,
acoustic quality, physical recording, NVDA and release remain separate.
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
    "tests/audio/test_voice_package_management.py": 30,
    "tests/audio/test_voice_management_state.py": 25,
    "tests/audio/test_voice_reference_management.py": 30,
    "tests/audio/test_voice_protected_management.py": 15,
    "tests/audio/test_voice_publication_lifetime.py": 15,
    "tests/audio/test_voice_ownership.py": 30,
    "tests/integrations/test_dordiai_owner_lease.py": 28,
    "tests/integrations/test_dordiai_managed_authority.py": 18,
    "tests/ui/test_voice_packages.py": 46,
    "tests/ui/test_voice_reference.py": 32,
    "tests/ui/test_engine_settings_lifetime.py": 43,
    "tests/integrations/test_dordiai_voices.py": 40,
    "tests/integrations/test_dordiai_speech.py": 10,
    "tests/integrations/test_dordiai_host_lifecycle.py": 13,
    "tests/integrations/test_dordiai_settings.py": 17,
    "tests/integrations/test_dordiai_probe_cancellation.py": 7,
    "tests/integrations/test_dordiai_convergence.py": 10,
    "tests/audio/test_bundled_runtime_settings.py": 8,
    "tests/storage/test_v3_state_backup.py": 42,
    "tests/storage/test_library_backup.py": 9,
    "tests/storage/test_state_backup_portability.py": 27
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
    "src/carr_library/catalog/intake.py": "8518f012491b542a57593af75993907983ddda0d"
}
COMPANIONS = ["tests/audio/voice_management_fixtures.py","tests/integrations/__init__.py"]
BASE_MODULES = ["carr_library.audio.voice_manifest","carr_library.audio.voice_transport","carr_library.audio.voice_settings","carr_library.runtime_settings","carr_library.audio.voice_packages","carr_library.audio.package_publication","carr_library.audio.voice_lifecycle","carr_library.audio.voice_reference","carr_library.audio.voice_restore","carr_library.state_backup","carr_library.library_backup","carr_library.ui.voice_packages","carr_library.ui.voice_reference","carr_library.ui.engine_settings","carr_library.integrations._dordiai_authority","carr_library.integrations.dordiai_voices","carr_library.integrations.dordiai_speech","carr_library.audio.voice_ownership","carr_library.audio.models","carr_library.audio.dsp","carr_library.audio.cleanup","carr_library.audio.process","carr_library.operations.broker","carr_library.operations._locks","carr_library.durable_state","carr_library.file_publication","carr_library.research.export_file","carr_library.studio.publishing","carr_library.sync.crypto","carr_library.speech","carr_library.application","carr_library.native_runtime","bootstrap","carr_library.catalog.intake","carr_library.audio.windows_job"]
WINDOWS_MODULES = ["carr_library.audio.voice_manifest","carr_library.audio.voice_transport","carr_library.audio.voice_settings","carr_library.runtime_settings","carr_library.audio.voice_packages","carr_library.audio.package_publication","carr_library.audio.voice_lifecycle","carr_library.audio.voice_reference","carr_library.audio.voice_restore","carr_library.state_backup","carr_library.library_backup","carr_library.ui.voice_packages","carr_library.ui.voice_reference","carr_library.ui.engine_settings","carr_library.integrations._dordiai_authority","carr_library.integrations.dordiai_voices","carr_library.integrations.dordiai_speech","carr_library.audio.voice_ownership","carr_library.audio.models","carr_library.audio.dsp","carr_library.audio.cleanup","carr_library.audio.process","carr_library.operations.broker","carr_library.operations._locks","carr_library.durable_state","carr_library.file_publication","carr_library.research.export_file","carr_library.studio.publishing","carr_library.sync.crypto","carr_library.speech","carr_library.application","carr_library.native_runtime","bootstrap","carr_library.audio.windows_job","carr_library.ui.desktop_app","carr_library.ui.worker_lifetime","carr_library.ui.recording_dialog","carr_library.catalog.intake"]
MODULES = WINDOWS_MODULES if os.name == 'nt' else BASE_MODULES
NATIVE_CONTRACT = json.loads(r'''{"unique_required_checks":83,"behavior_checks":["native failure answers owned No before closing only the active inner modal","registration confirmation defaults to the actual owned No button","existing engine settings exposes one explicit package-management action","package child borrows exact engine parent and existing host executor","unregistered package view has no implicit permission or metadata child","default No leaves voice registration and owner files unchanged","explicit manifest registration binds the real current settings map","Holos does not expose unreviewed reference creation","saved explicit OmniVoice configuration enables only its reference creator","reference child starts without recording permission or quality approval","reference close opens no microphone and dispatches no owner operation","inspection returns exact source roles and clears permission and languages","Apply without explicit rights cannot call the owner API","native keyboard explicitly grants rights and the selected production language","real owner installation publishes only the reviewed data and language choice","real installed version listing is visible in the readable status","installed version review resets export and production permission","unencrypted export requires separate explicit permission","actual owner export contains only the reviewed reference version","export result exposes its actual independently checked SHA256","empty removal confirmation cannot mutate the reviewed profile","real deactivation retains files and revokes active production authority","deletion requires a fresh distinct empty confirmation","real deletion removes only the explicitly reviewed profile versions","source package shared models and unrelated voice preferences remain exact","queued management borrows the retained host executor","closed queued child cannot call the owner API or deliver a late review","parent engine view reload preserves the current management binding","existing engine parent retains one native Close control","actual isolated owner metadata operations completed with no live Job handles","current broker has no active voice-management lease after native work","real native Windows voice controls","registered engine action belongs to the existing host command registry","actual host menu contains one existing local-engine command","native host Close drains the original shared executor and finalizers","all actual metadata Job process and thread handles are settled before fallback","no active broker lease survives native host cleanup","synthetic owner companion remains byte-identical after cleanup","native voice failure cleanup control completed"],"required_checks":["native failure answers owned No before closing only the active inner modal","registration confirmation defaults to the actual owned No button","existing engine settings exposes one explicit package-management action","package child borrows exact engine parent and existing host executor","unregistered package view has no implicit permission or metadata child","default No leaves voice registration and owner files unchanged","explicit manifest registration binds the real current settings map","Holos does not expose unreviewed reference creation","saved explicit OmniVoice configuration enables only its reference creator","reference child starts without recording permission or quality approval","reference close opens no microphone and dispatches no owner operation","inspection returns exact source roles and clears permission and languages","Apply without explicit rights cannot call the owner API","native keyboard explicitly grants rights and the selected production language","real owner installation publishes only the reviewed data and language choice","real installed version listing is visible in the readable status","installed version review resets export and production permission","unencrypted export requires separate explicit permission","actual owner export contains only the reviewed reference version","export result exposes its actual independently checked SHA256","empty removal confirmation cannot mutate the reviewed profile","real deactivation retains files and revokes active production authority","deletion requires a fresh distinct empty confirmation","real deletion removes only the explicitly reviewed profile versions","source package shared models and unrelated voice preferences remain exact","queued management borrows the retained host executor","closed queued child cannot call the owner API or deliver a late review","parent engine view reload preserves the current management binding","existing engine parent retains one native Close control","actual isolated owner metadata operations completed with no live Job handles","current broker has no active voice-management lease after native work","real native Windows voice controls","registered engine action belongs to the existing host command registry","actual host menu contains one existing local-engine command","native host Close drains the original shared executor and finalizers","all actual metadata Job process and thread handles are settled before fallback","no active broker lease survives native host cleanup","synthetic owner companion remains byte-identical after cleanup","native voice failure cleanup control completed","exact installed source: carr_library.application","exact installed source: carr_library.runtime_settings","exact installed source: carr_library.durable_state","exact installed source: carr_library.native_runtime","exact installed source: carr_library.audio.voice_settings","exact installed source: carr_library.audio.voice_packages","exact installed source: carr_library.audio.voice_lifecycle","exact installed source: carr_library.audio.voice_reference","exact installed source: carr_library.audio.package_publication","exact installed source: carr_library.audio.voice_manifest","exact installed source: carr_library.audio.voice_ownership","exact installed source: carr_library.audio.voice_transport","exact installed source: carr_library.audio.process","exact installed source: carr_library.audio.windows_job","exact installed source: carr_library.operations.broker","exact installed source: carr_library.operations._locks","exact installed source: carr_library.ui.desktop_app","exact installed source: carr_library.ui.engine_settings","exact installed source: carr_library.ui.voice_packages","exact installed source: carr_library.ui.voice_reference","exact installed source: carr_library.ui.worker_lifetime","exact installed source: carr_library.ui.recording_dialog","unchanged installed source after cleanup: carr_library.application","unchanged installed source after cleanup: carr_library.runtime_settings","unchanged installed source after cleanup: carr_library.durable_state","unchanged installed source after cleanup: carr_library.native_runtime","unchanged installed source after cleanup: carr_library.audio.voice_settings","unchanged installed source after cleanup: carr_library.audio.voice_packages","unchanged installed source after cleanup: carr_library.audio.voice_lifecycle","unchanged installed source after cleanup: carr_library.audio.voice_reference","unchanged installed source after cleanup: carr_library.audio.package_publication","unchanged installed source after cleanup: carr_library.audio.voice_manifest","unchanged installed source after cleanup: carr_library.audio.voice_ownership","unchanged installed source after cleanup: carr_library.audio.voice_transport","unchanged installed source after cleanup: carr_library.audio.process","unchanged installed source after cleanup: carr_library.audio.windows_job","unchanged installed source after cleanup: carr_library.operations.broker","unchanged installed source after cleanup: carr_library.operations._locks","unchanged installed source after cleanup: carr_library.ui.desktop_app","unchanged installed source after cleanup: carr_library.ui.engine_settings","unchanged installed source after cleanup: carr_library.ui.voice_packages","unchanged installed source after cleanup: carr_library.ui.voice_reference","unchanged installed source after cleanup: carr_library.ui.worker_lifetime","unchanged installed source after cleanup: carr_library.ui.recording_dialog"],"origins":["carr_library.application","carr_library.runtime_settings","carr_library.durable_state","carr_library.native_runtime","carr_library.audio.voice_settings","carr_library.audio.voice_packages","carr_library.audio.voice_lifecycle","carr_library.audio.voice_reference","carr_library.audio.package_publication","carr_library.audio.voice_manifest","carr_library.audio.voice_ownership","carr_library.audio.voice_transport","carr_library.audio.process","carr_library.audio.windows_job","carr_library.operations.broker","carr_library.operations._locks","carr_library.ui.desktop_app","carr_library.ui.engine_settings","carr_library.ui.voice_packages","carr_library.ui.voice_reference","carr_library.ui.worker_lifetime","carr_library.ui.recording_dialog"],"origin_count":22,"origin_contract":"Actual installed module origin must be under sys.prefix, outside checkout src, and byte-identical to exact checkout source before and after actual host/native cleanup.","pass_fields":["status","native_keyboard","native_metadata_lifecycle","native_queued_close","native_host_cleanup","native_failure_cleanup"],"not_run_fields":["real_owner_inference","voice_similarity","physical_recording","manual_nvda"],"synthetic_owner_protocol":true,"qualifiers":{"picker":"explicit fixture file selections; system picker not automated","interpreter":"only the inert synthetic owner marker is replaced with this installed Python"},"multiplicity":{"registration confirmation defaults to the actual owned No button":2},"check_policy":"Require the complete named set with no false rows or qualification substitutions. The default-No observation intentionally executes twice, once before No and once before explicit Yes; do not enforce globally unique emitted rows."}''')


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

OWNER_CONTRACT = json.loads(r'''{
  "schema": "carr-current-manager-real-owner/1",
  "required_unique_checks": [
    "actual current registration binds the real owner closure and existing package lock",
    "current configuration explicitly selects the supported OmniVoice package recipe",
    "actual current inspection returns exact generated data roles and languages",
    "unchecked reference rights refuse before another child or settings mutation",
    "real owner installation publishes exact first version and current explicit rights",
    "second actual install preserves immutable first-version bytes and advances binding",
    "explicit current version activation reaches the real owner and refreshes authority",
    "stale saved version review cannot silently select a later authority",
    "export requires its own explicit reference permission before owner access",
    "refused export creates no selected output",
    "actual current export has exact owner document and independently verified receipt",
    "actual current revoke preserves the owner tombstone and exact installed files",
    "revoked real owner identity cannot fall back to an old legacy profile",
    "deletion requires the exact fresh reviewed action",
    "actual current delete removes only the selected retained versions",
    "local revocation survives real owner registration deletion",
    "every original runtime helper worker model marker and legacy reference remains exact",
    "unrelated current voice preferences remain exact across all management commits",
    "all generated source recordings text and original input archives remain exact",
    "all metadata actions use actual current child admission and exact real owner bytes",
    "all actual metadata processes are reaped and Windows handles are closed",
    "no current broker lease survives actual owner management",
    "no model synthesis microphone playback or private runtime was executed",
    "executed application and exact acquisition companions remain unchanged",
    "all probe-owned generated data were retired"
  ],
  "required_check_count": 25,
  "origin_modules": [
    "carr_library.runtime_settings",
    "carr_library.durable_state",
    "carr_library.catalog.intake",
    "carr_library.audio.models",
    "carr_library.audio.cleanup",
    "carr_library.audio.process",
    "carr_library.audio.windows_job",
    "carr_library.audio.voice_manifest",
    "carr_library.audio.voice_ownership",
    "carr_library.audio.voice_transport",
    "carr_library.audio.voice_settings",
    "carr_library.audio.voice_packages",
    "carr_library.audio.voice_lifecycle",
    "carr_library.audio.package_publication",
    "carr_library.operations.broker",
    "carr_library.operations._locks",
    "carr_library.research.export_file",
    "carr_library.studio.publishing"
  ],
  "origin_count": 18,
  "required_PASS": [
    "status",
    "actual_owner_api",
    "current_manager",
    "management.current_broker_cleanup",
    "management.actual_process_cleanup"
  ],
  "mode_values": [
    "source",
    "installed"
  ],
  "required_preacquired": {
    "owner_input": "preacquired-closed-directory",
    "supplied_owner_directory_unchanged": true,
    "probe_downloaded_owner_source_retired": false
  },
  "required_qualifications": {
    "real_engine_inference": "NOT_RUN",
    "acoustic_quality": "NOT_RUN",
    "physical_recording": "NOT_RUN",
    "manual_nvda": "NOT_RUN",
    "release": "NOT_RUN",
    "owner_source_or_data_in_public_artifacts": false
  },
  "required_actions": [
    "prepare_lock",
    "catalog",
    "inspect",
    "install",
    "versions",
    "review_version",
    "activate",
    "export",
    "remove"
  ],
  "children": "All rows returned=True/returncode=0/owner_sources_verified=True/broker_admitted=True/windows_job_required=(actual platform is Windows); positive integer child_count equals children length and total action counts; every required action positive. Exact nine action key set.",
  "identity": "origins_before and origins_after have exact 18 keys and equal hashes; outer gate compares hashes to selected source/wheel. Exact three owner_source_receipts Git SHA/bytes and companion hashes also bound.",
  "bounds": {
    "maximum_json_bytes": 65536,
    "maximum_metadata_children": 128,
    "operation_seconds": 900,
    "source_maximum_bytes_each": 131072,
    "public_artifacts": "Bounded JSON and text only; no owner source/package/PCM/runtime/model binary."
  }
}''')
OWNER_IDENTITY = json.loads(r'''{
  "repository": "Oleksii-debug/ChatGPT-Autopilot-ExtensionChatGPT-Autopilot-Extension",
  "revision": "a1c73a5747bc86eb11f0c037120442e0c2593e91",
  "prefix": "dordiai-voices/source/0.12.2/kx47Runtime",
  "files": {
    "voice_policy.py": "de305d15a955b92dc1a8bfb719b7f9df501a94d8",
    "voice_packages.py": "2abe2a50bcf58aea423764ef15acc04538ee2f9b",
    "voice_assets.py": "6cabd9c44a3979e2b543f2272915fdc83523c87c"
  }
}''')


def owner_inputs(directory):
    # Reuse the exact accepted closed-directory verifier; never acquire here.
    helper = load('voice_exact_owner_acquisition', ROOT / 'scripts/ci_owner_voice_api_smoke.py')
    probe = load('voice_exact_current_manager_owner', ROOT / 'scripts/ci_voice_management_owner_smoke.py')
    selected, _buffers, receipts, identity = probe.existing_owner(directory, helper)
    require(helper.OWNER == OWNER_IDENTITY['repository']
            and helper.REVISION == OWNER_IDENTITY['revision']
            and helper.PREFIX == OWNER_IDENTITY['prefix']
            and helper.SOURCE_BLOBS == OWNER_IDENTITY['files'],
            'Public owner closure differs from the exact reviewed immutable source.')
    return dict(receipts=receipts, ancestry=identity)


def owner_contract(*, work, wheel, contract, evidence, directory, owner_before, inputs, ci):
    wrapper = work / 'owner-origin-child.py'
    wrapper.write_text(ORIGIN_CHILD, encoding='utf-8')
    def snapshot(path, log):
        require(child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
            str(evidence / 'source-wheel-proof.json'), str(ROOT.parent), str(path), str(contract)],
            log, cwd=work, ci=ci, timeout=60) == 0, 'Installed owner-probe origin snapshot failed.')
        return json.loads(path.read_text(encoding='utf-8'))
    first = snapshot(evidence / 'owner-origins-before.json', evidence / 'owner-origins-before.txt')
    results, failures = {}, []
    try:
        for mode in ('source', 'installed'):
            report_path, log = evidence / ('owner-' + mode + '.json'), evidence / ('owner-' + mode + '.txt')
            command = [sys.executable, '-I', '-B', '-X', 'utf8',
                str(ROOT / 'scripts/ci_voice_management_owner_smoke.py'),
                '--source-root', str(ROOT / 'src'), '--owner-api-directory', str(directory),
                '--report', str(report_path)]
            if mode == 'installed':
                command.append('--require-installed')
            try:
                require(owner_inputs(directory) == owner_before, 'Acquired owner source changed before execution.')
                code = child(command, log, cwd=work, ci=ci, timeout=990)
                require(code == 0, 'Current-manager real-owner phase failed: ' + mode)
                require(report_path.is_file() and not report_path.is_symlink()
                        and 0 < report_path.stat().st_size <= 65536,
                        'Current-manager owner receipt is missing or oversized.')
                value = json.loads(report_path.read_text(encoding='utf-8'))
                require(type(value) is dict and value.get('schema') == OWNER_CONTRACT['schema']
                        and value.get('mode') == mode
                        and value.get('platform') == ('Windows' if os.name == 'nt' else 'Linux'),
                        'Current-manager owner receipt identity differs.')
                for field in OWNER_CONTRACT['required_PASS']:
                    observed = value
                    for key in field.split('.'):
                        require(type(observed) is dict, 'Missing owner outcome: ' + field)
                        observed = observed.get(key)
                    require(observed == 'PASS', 'Required owner outcome failed: ' + field)
                checks = value.get('checks')
                require(type(checks) is list and all(type(label) is str for label in checks)
                        and len(checks) == len(set(checks)) == OWNER_CONTRACT['required_check_count']
                        and set(checks) == set(OWNER_CONTRACT['required_unique_checks']),
                        'Every exact current-manager owner check must pass once.')
                for key, expected in OWNER_CONTRACT['required_preacquired'].items():
                    require(type(value.get(key)) is type(expected) and value.get(key) == expected,
                            'Current owner acquisition scope differs: ' + key)
                require(value.get('qualifications') == OWNER_CONTRACT['required_qualifications'],
                        'Metadata work cannot qualify inference, acoustics, recording, NVDA or release.')
                require(value.get('synthetic_model_and_worker_markers') is True
                        and value.get('interpreter') ==
                            'Only the exact frozen fixture inert marker is replaced with this interpreter.',
                        'Owner probe changed its explicit inert-model and interpreter scope.')
                require(value.get('owner_repository') == OWNER_IDENTITY['repository']
                        and value.get('owner_revision') == OWNER_IDENTITY['revision']
                        and value.get('owner_prefix') == OWNER_IDENTITY['prefix']
                        and value.get('owner_source_receipts') == owner_before['receipts'],
                        'Executed real owner bytes differ from the exact acquisition.')
                require(value.get('fixture_sha256') ==
                            inputs['tests/audio/voice_management_fixtures.py']['sha256']
                        and value.get('owner_helper_sha256') ==
                            inputs['scripts/ci_owner_voice_api_smoke.py']['sha256'],
                        'Actual owner companions differ from the pinned checkout.')
                origins = value.get('origins_before')
                require(type(origins) is dict and set(origins) == set(OWNER_CONTRACT['origin_modules'])
                        and value.get('origins_after') == origins,
                        'Every current-manager source origin must remain exact after real owner cleanup.')
                for name, digest in origins.items():
                    relative = 'src/' + name.replace('.', '/') + '.py'
                    require(digest == inputs[relative]['sha256']
                            and digest == first['origins'][name]['sha256'],
                            'Real owner phase used an unselected source or wheel: ' + name)
                management = value['management']
                counts, rows = management.get('action_counts'), management.get('children')
                require(type(counts) is dict and set(counts) == set(OWNER_CONTRACT['required_actions'])
                        and all(type(count) is int and count > 0 for count in counts.values()),
                        'Every actual owner action must have a positive exact count.')
                require(type(rows) is list and type(management.get('child_count')) is int
                        and 0 < len(rows) == management['child_count'] == sum(counts.values()) <= 128,
                        'Every actual owner child must have a bounded retained outcome.')
                observed = dict.fromkeys(counts, 0)
                for row in rows:
                    require(type(row) is dict and row.get('action') in observed
                            and row.get('returned') is True
                            and type(row.get('returncode')) is int and row['returncode'] == 0
                            and row.get('owner_sources_verified') is True
                            and row.get('broker_admitted') is True
                            and row.get('windows_job_required') is (os.name == 'nt')
                            and type(row.get('request_sha256')) is str
                            and re.fullmatch('[0-9a-f]{64}', row['request_sha256']) is not None,
                            'An actual owner request lacks exact source, broker or process proof.')
                    observed[row['action']] += 1
                require(observed == counts, 'Actual child inventory and action counts differ.')
                require(type(management.get('operation_seconds')) in (int, float)
                        and 0 <= management['operation_seconds'] <= 990,
                        'Actual owner operation exceeded its finite cleanup-inclusive bound.')
                require(owner_inputs(directory) == owner_before, 'Acquired owner source changed after execution.')
                results[mode] = dict(status='PASS', checks=len(checks), origins=len(origins),
                    children=len(rows), actions=counts, report_sha256=sha256(report_path.read_bytes()).hexdigest())
            except Exception as error:
                failures.append(dict(mode=mode, type=type(error).__name__, error=str(error)[:2048]))
    finally:
        last = snapshot(evidence / 'owner-origins-after.json', evidence / 'owner-origins-after.txt')
        require(last == first, 'Installed wheel or source origin changed during actual owner work.')
        require(owner_inputs(directory) == owner_before, 'Read-only acquired owner closure changed.')
    result = dict(status='FAIL' if failures else 'PASS', phases=results, failures=failures,
        required_checks_per_mode=25, origins_per_mode=18, installed_payload_unchanged=True,
        acquired_owner_unchanged=True, actual_owner_api=True, current_manager=True,
        qualifications=OWNER_CONTRACT['required_qualifications'])
    write(evidence / 'owner-summary.json', result)
    return result


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


def native_contract(*, work, wheel, contract, evidence, audio, ci):
    if os.name != 'nt':
        value = dict(status='NOT_RUN', reason='Actual native Windows management is required in the paired Windows job.',
                     native_keyboard='NOT_RUN', private_model_inference='NOT_RUN', manual_nvda='NOT_RUN')
        write(evidence / 'native-summary.json', value)
        return value
    wrapper = work / 'native-origin-child.py'
    wrapper.write_text(ORIGIN_CHILD, encoding='utf-8')
    before, after = evidence / 'native-origins-before.json', evidence / 'native-origins-after.json'
    def snapshot(path, log):
        require(child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
            str(evidence / 'source-wheel-proof.json'), str(ROOT.parent), str(path), str(contract)],
            log, cwd=work, ci=ci, timeout=60) == 0, 'Installed native origin snapshot failed.')
        return json.loads(path.read_text(encoding='utf-8'))
    first = snapshot(before, evidence / 'native-origins-before.txt')
    value, failure = None, None
    log = evidence / 'native-voice-management.txt'
    try:
        code = child([sys.executable, '-I', '-B', '-X', 'utf8',
            str(ROOT / 'scripts/windows_voice_management_smoke.py'),
            '--source-root', str(ROOT / 'src')], log, cwd=work, ci=ci, timeout=330)
        require(code == 0, 'Required actual native voice-management phase failed.')
        raw = log.read_text(encoding='utf-8-sig')
        reports = []
        for match in re.finditer(r'(?m)^\{', raw):
            try:
                result, _ = json.JSONDecoder().raw_decode(raw[match.start():])
            except ValueError:
                continue
            if isinstance(result, dict) and 'checks' in result:
                reports.append(result)
        require(len(reports) == 1, 'Required native voice report is missing or ambiguous.')
        value = reports[0]
        require(all(value.get(field) == 'PASS' for field in NATIVE_CONTRACT['pass_fields']),
                'Every native management, keyboard and cleanup outcome is required.')
        require(all(value.get(field) == 'NOT_RUN' for field in NATIVE_CONTRACT['not_run_fields'])
                and value.get('synthetic_owner_protocol') is True,
                'Synthetic native metadata work cannot qualify private inference, recording, similarity or NVDA.')
        require(type(value.get('checks')) is list and all(type(row) is str for row in value['checks']),
                'Native check inventory is invalid.')
        require(set(NATIVE_CONTRACT['required_checks']) <= set(value['checks']),
                'A required native voice-management check is absent.')
        for label in NATIVE_CONTRACT['required_checks']:
            required_count = NATIVE_CONTRACT['multiplicity'].get(label, 1)
            require(value['checks'].count(label) == required_count
                    and raw.splitlines().count('PASS: ' + label) == required_count,
                    'Required native observation differs: ' + label)
        require(all(value.get(key) == expected for key, expected in NATIVE_CONTRACT['qualifiers'].items()),
                'Actual picker/interpreter fixture scope differs.')
        require(set(value['origins']) == set(NATIVE_CONTRACT['origins']),
                'Native management origin inventory differs.')
        require(all(digest == first['origins'][name]['sha256']
                    for name, digest in value['origins'].items()),
                'Native management source differs from the exact installed wheel.')
        require(value['fixture_sha256'] == sha256((ROOT / COMPANIONS[0]).read_bytes()).hexdigest(),
                'Native metadata companion changed.')
        write(evidence / 'native-voice-management.json', value)
    except Exception as error:
        failure = dict(type=type(error).__name__, error=str(error)[:2048])
    finally:
        last = snapshot(after, evidence / 'native-origins-after.txt')
        require(last == first, 'Installed wheel or native origin changed during actual native work.')
    result = dict(status='FAIL' if failure else 'PASS', required_checks=83,
                  native_origins=22, installed_origins=len(first['origins']),
                  installed_payload_unchanged=True, failure=failure,
                  synthetic_owner_protocol=True, real_owner_inference='NOT_RUN',
                  physical_recording='NOT_RUN', manual_nvda='NOT_RUN', release='NOT_RUN')
    write(evidence / 'native-summary.json', result)
    require(failure is None and value is not None, 'Every required native voice-management check must pass.')
    return result


def main():
    # Isolated Python ignores PYTHONUTF8; display the retained UTF-8 child log.
    # This changes only this orchestrator's stream, not child/test encodings.
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--audio-directory', required=True, type=Path)
    parser.add_argument('--owner-api-directory', required=True, type=Path)
    args = parser.parse_args()
    require(__debug__ and re.fullmatch('[0-9a-f]{40}', args.expected_head), 'Exact commit and assertions required.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Checkout differs from exact event head.')
    evidence = args.evidence.resolve()
    evidence.mkdir(mode=0o700, exist_ok=False)
    before = sources()
    selected_audio = args.audio_directory.resolve(strict=True)
    audio_before = audio_inputs(selected_audio)
    owner_directory = args.owner_api_directory.absolute()
    owner_before = owner_inputs(owner_directory)
    own_relative = Path(__file__).resolve().relative_to(ROOT.parent).as_posix()
    require(git('show', args.expected_head + ':' + own_relative) == Path(__file__).read_text(encoding='utf-8').strip(),
            'Orchestrator differs from its committed source.')
    identity = dict(source_commit=args.expected_head, source_tree=git('rev-parse', 'HEAD^{tree}'),
        inputs=before, audio_runtime=audio_before, owner_api=owner_before,
        python=sys.version, platform=sys.platform)
    write(evidence / 'identity-before.json', identity)
    ci = load('voice_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('voice_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    source_log = evidence / 'source-contracts.txt'
    source_groups = []
    if os.name == 'nt':
        # The Windows package suite exceeded 600 s while the other 20 wholes passed.
        # Isolate that complete suite, then keep all remaining suites in order.
        # 660 + 3300 = 3960 s including group cleanup allowances remains inside
        # the original 4500 s source envelope; every whole still has 600 s.
        package = 'tests/audio/test_voice_package_management.py'
        remaining = tuple(suite for suite in SUITES if suite != package)
        groups = (((package,), 1, 660), (remaining, 4, 3300))
        flattened = tuple(suite for selected, _, _ in groups for suite in selected)
        require(flattened == tuple(SUITES) and len(set(flattened)) == len(SUITES),
                'Windows source groups must cover every original whole exactly once.')
        with source_log.open('xb'):
            pass
        source_parent = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix='carr-voice-source-', dir=source_parent) as directory:
            for number, (selected, jobs, timeout) in enumerate(groups, 1):
                output = Path(directory) / f'source-{number}.txt'
                header = (f'\nVOICE_SOURCE_GROUP {number}: jobs={jobs}, '
                          f'wholes={len(selected)}, parent_seconds={timeout}\n').encode('ascii')
                print(header.decode('ascii'), flush=True)
                try:
                    code = child([sys.executable, '-B', str(ROOT / 'scripts/ci_check.py'),
                                  '--jobs', str(jobs), *selected],
                                 output, cwd=ROOT, ci=ci, timeout=timeout)
                finally:
                    if output.is_file():
                        require(output.stat().st_size <= 8 * 1024**2,
                                'Required source-group log exceeds its finite bound.')
                        raw = output.read_bytes()
                        with source_log.open('ab') as combined:
                            require(combined.tell() + len(header) + len(raw) + 1 <= 8 * 1024**2,
                                    'Combined source log exceeds its unchanged finite bound.')
                            combined.write(header + raw + b'\n')
                group_lines = raw.decode('utf-8').splitlines()
                group_expected = [
                    f'PASS: {suite}: junit_passed={SUITES[suite]}, failed=0, skipped=0, pytest_exit=0'
                    for suite in selected]
                valid = (code == 0 and all(group_lines.count(line) == 1 for line in group_expected)
                         and group_lines[-1:] == [
                             f'Required suites complete: {len(selected)}; passed={len(selected)}; failed=0'])
                record = dict(status='PASS' if valid else 'FAIL', suites=list(selected),
                              jobs=jobs, parent_seconds=timeout, suite_seconds=600,
                              exit_code=code, log_sha256=sha256(raw).hexdigest())
                source_groups.append(record)
                print('VOICE_SOURCE_GROUP_RESULT ' + json.dumps(record, sort_keys=True), flush=True)
        source_complete = (len(source_groups) == 2
                           and all(group['status'] == 'PASS' for group in source_groups))
    else:
        # Preserve the original Linux 21-whole command and 4500 s envelope.
        code = child([sys.executable, '-B', str(ROOT / 'scripts/ci_check.py'),
                      '--jobs', '3', *SUITES], source_log, cwd=ROOT, ci=ci, timeout=4500)
        group_lines = source_log.read_text(encoding='utf-8').splitlines()
        source_complete = (code == 0 and group_lines[-1:] == [
            f'Required suites complete: {len(SUITES)}; passed={len(SUITES)}; failed=0'])
    lines = source_log.read_text(encoding='utf-8').splitlines()
    expected = [f'PASS: {suite}: junit_passed={count}, failed=0, skipped=0, pytest_exit=0'
                for suite, count in SUITES.items()]
    require(source_complete and all(lines.count(line) == 1 for line in expected),
            'Every source whole suite must pass without skips or expected failures.')
    write(evidence / 'source-summary.json', dict(status='PASS', suites=SUITES,
          passed=sum(SUITES.values()), failed=0, skipped=0, expected_failures=False,
          log_sha256=sha256(source_log.read_bytes()).hexdigest(),
          **({'execution_groups': source_groups} if source_groups else {})))
    work_parent = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='carr-voice-management-', dir=work_parent) as tmp:
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
        owner = owner_contract(work=work, wheel=wheel, contract=contract, evidence=evidence,
            directory=owner_directory, owner_before=owner_before, inputs=before, ci=ci)
        native = native_contract(work=work, wheel=wheel, contract=contract,
            evidence=evidence, audio=selected_audio, ci=ci)
    require(owner['status'] == 'PASS' and set(owner['phases']) == {'source', 'installed'},
            'Both current-manager real-owner phases must pass all 25 exact checks.')
    require(owner_inputs(owner_directory) == owner_before, 'Acquired owner source changed during all contracts.')
    require(audio_inputs(selected_audio) == audio_before, 'Selected codec changed during actual contracts.')
    require(sources() == before, 'Pinned sources changed during actual contracts.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Source commit changed.')
    report = dict(status='PASS', **identity, source_passed=sum(SUITES.values()),
        installed_passed=sum(SUITES.values()), installed=installed, inputs_unchanged=True,
        application_wheel_sha256=proof['wheel_sha256'], all_children_reaped=True,
        expected_failures=False, native=native, actual_owner=owner,
        qualification='Exact whole voice-management/provider source and installed contracts, plus current-manager real-owner API in both modes; actual native management is required on Windows. Private inference, acoustics, recording, NVDA and release remain separate.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'all_children_reaped')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
