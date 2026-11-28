"""Verify saved-audio failure cleanup from current source and a verified installed wheel.

The required qualification is cleanup-only: 13 source cases, 13 isolated installed
cases, and 15 actual native observations. A second default playback run is a bounded
diagnostic. Its own result is retained independently and cannot qualify playback
from a failed exit or from the cleanup-only result. No physical listening or NVDA
claim is made. All generated audio, wheels and installed runtime bytes stay private
to the runner; only closed diagnostic text, JSON and XML are retained.
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
    "tests/ui/test_native_saved_audio_cleanup.py": 13
}
COMPANIONS = ["scripts/windows_audio_player_smoke.py"]
MODULES = [
    "carr_library.speech._wave_output",
    "carr_library.speech.player",
    "carr_library.speech.playlist",
    "carr_library.speech.playback_devices",
    "carr_library.ui.audio_playback",
    "carr_library.ui.reader_playback",
    "carr_library.ui.desktop_app",
    "carr_library.studio.clips",
    "carr_library.ui.save_all",
    "carr_library.operations.broker",
    "carr_library.backup_playback_queue",
    "carr_library.application",
    "carr_library.native_runtime",
    "carr_library.ui.worker_lifetime",
    "carr_library.ui.worker_cleanup",
    "carr_library.audio.process",
    "carr_library.audio.cleanup",
    "carr_library.runtime_settings",
    "carr_library.operations._locks",
    "bootstrap"
]
PINS = {
    "src/carr_library/speech/_wave_output.py": "67c3ec2d0fa1e785615b49e659506f5887606348",
    "src/carr_library/speech/player.py": "6f758c6073e239f771a7b8627713aca8bff51d7f",
    "src/carr_library/speech/playlist.py": "544f845fa425ff643620f68fad777421a85004f4",
    "src/carr_library/speech/playback_devices.py": "3f675bccbfeea8f0b0977d814e123b435bb7f0ca",
    "src/carr_library/ui/audio_playback.py": "4850332970c92001564cc495c35e52bec4da1393",
    "src/carr_library/ui/reader_playback.py": "a96a5b2662cf41b5ae04f017264a59e02f4c6f6a",
    "src/carr_library/ui/desktop_app.py": "85c0991f744bd521ca247a0e15da93aa585ea30d",
    "src/carr_library/studio/clips.py": "f77512fc1f0f242a6202ffa247e5ce9049debefc",
    "src/carr_library/ui/save_all.py": "c109f48ccb561b25fc9f348dacc4688f734f2e6b",
    "src/carr_library/operations/broker.py": "107e1a600bb4bdf0f3edb7735ee5fcdb68d9fb36",
    "src/carr_library/backup_playback_queue.py": "1e1c3e8aec3674bdd8ac182216bfdbbb8acd5023",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "src/carr_library/native_runtime.py": "50ca93914325753f7d9cb91f1bc4f48fc9ae230f",
    "src/carr_library/ui/worker_lifetime.py": "ed62603159bc095724c3b240d4754f989141a2c9",
    "src/carr_library/ui/worker_cleanup.py": "2f071d9d2762ef15f5f8ee3da2fcf25de165b15a",
    "src/carr_library/audio/process.py": "480272d0224770f6b0ec9c8efc8e70d9aab42b0b",
    "src/carr_library/audio/cleanup.py": "77d69f8406db93dc0ff6110b9597ad805bd92dc5",
    "src/carr_library/runtime_settings.py": "00df790d393f9d54f87d0dcf891e6506916437a1",
    "src/carr_library/operations/_locks.py": "ae161b8483f84b2a4aa2f153b4b11b08f76b9693",
    "bootstrap/__init__.py": "6981b7faa1cca8d29f37fe85751c110c693dae55",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "packaging/windows-audio.lock.json": "2bc706ffc6208f5596a10abee61d3da33f20e6c6",
    "scripts/windows_audio_player_smoke.py": "cc68ddba234a396d0c8738a6ec49a57b7116e5f5",
    "tests/ui/test_native_saved_audio_cleanup.py": "712167756982f5b728e7118603f70399efe18ec0"
}
NATIVE_CONTRACT = {
    "origins": [
        "carr_library.speech._wave_output",
        "carr_library.speech.player",
        "carr_library.speech.playlist",
        "carr_library.speech.playback_devices",
        "carr_library.ui.audio_playback",
        "carr_library.ui.reader_playback",
        "carr_library.ui.desktop_app",
        "carr_library.studio.clips",
        "carr_library.ui.save_all",
        "carr_library.operations.broker",
        "carr_library.backup_playback_queue"
    ],
    "cleanup_checks": [
        "one stable native command without a conflicting default shortcut",
        "controlled worker failure closes only its owned passive playback modal",
        "actual host Close drains saved playback owners and the existing worker",
        "all installed queue sources stay exact after native execution",
        "exact installed player module: carr_library.speech._wave_output",
        "exact installed player module: carr_library.speech.player",
        "exact installed player module: carr_library.speech.playlist",
        "exact installed player module: carr_library.speech.playback_devices",
        "exact installed player module: carr_library.ui.audio_playback",
        "exact installed player module: carr_library.ui.reader_playback",
        "exact installed player module: carr_library.ui.desktop_app",
        "exact installed player module: carr_library.studio.clips",
        "exact installed player module: carr_library.ui.save_all",
        "exact installed player module: carr_library.operations.broker",
        "exact installed player module: carr_library.backup_playback_queue"
    ],
    "playback_checks": [
        "actual keyboard Space",
        "native file chooser Enter",
        "standalone opens without a source or native sound",
        "actual file picker order and original hashes",
        "review is passive",
        "queue selection is silent",
        "stopped live setting does not open a device",
        "same canonical player owns bounded native output",
        "actual pause is stable and disarms queue advancement",
        "independent handles refer to the same Windows output",
        "read independent owned output level",
        "read muted owned output level",
        "read unaffected independent output level",
        "live mute and balance preserve volume and other player",
        "live paused controls never resume audio",
        "unmute retains reviewed native level",
        "actual unmuted stereo balance matches the owned native handle",
        "actual queue transition keeps levels and exact next file",
        "Stop closes owned native output and cancels queue",
        "native reorder is passive",
        "silent original-frame cursor before save",
        "actual Ctrl+S down",
        "actual Ctrl+S character",
        "actual Ctrl+S up",
        "Ctrl+S durably saved order and exact original-frame cursor",
        "restart loads passive metadata without opening audio",
        "explicit review restores original cursor and settings without autoplay",
        "one stable native command without a conflicting default shortcut",
        "native modal close acknowledges player ownership",
        "original PCM remains exact after live controls",
        "closed source handle is released",
        "saved queue reopens after original files moved",
        "all installed queue sources stay exact after native execution",
        "controlled worker failure closes only its owned passive playback modal",
        "actual host Close drains saved playback owners and the existing worker",
        "exact installed player module: carr_library.speech._wave_output",
        "exact installed player module: carr_library.speech.player",
        "exact installed player module: carr_library.speech.playlist",
        "exact installed player module: carr_library.speech.playback_devices",
        "exact installed player module: carr_library.ui.audio_playback",
        "exact installed player module: carr_library.ui.reader_playback",
        "exact installed player module: carr_library.ui.desktop_app",
        "exact installed player module: carr_library.studio.clips",
        "exact installed player module: carr_library.ui.save_all",
        "exact installed player module: carr_library.operations.broker",
        "exact installed player module: carr_library.backup_playback_queue"
    ],
    "cleanup_fields": {
        "status": "PASS",
        "mode": "failure-cleanup-only",
        "installed_modules": "PASS",
        "failure_cleanup": "PASS",
        "actual_windows_waveout": "NOT_RUN",
        "actual_native_queue_controls": "NOT_RUN",
        "instance_volume_isolation": "NOT_RUN",
        "physical_device": "NOT_RUN",
        "audible_listening": "NOT_RUN",
        "nvda": "NOT_RUN"
    },
    "playback_fields": {
        "status": "PASS",
        "mode": "full-playback",
        "installed_modules": "PASS",
        "failure_cleanup": "PASS",
        "actual_windows_waveout": "PASS",
        "actual_native_queue_controls": "PASS",
        "instance_volume_isolation": "PASS",
        "physical_device": "NOT_RUN",
        "audible_listening": "NOT_RUN",
        "nvda": "NOT_RUN"
    },
    "diagnostic_prefix": "SAVED_PLAYBACK_HOST_CLOSE: ",
    "close_label": "actual host Close drains saved playback owners and the existing worker"
}


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


def native_report(raw):
    reports = []
    for match in re.finditer(r'(?m)^\{', raw):
        try:
            value, _ = json.JSONDecoder().raw_decode(raw[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and 'checks' in value:
            reports.append(value)
    require(len(reports) == 1, 'The actual native report is missing or ambiguous.')
    return reports[0]


def close_observations(raw):
    rows = []
    for line in raw.splitlines():
        if not line.startswith(NATIVE_CONTRACT['diagnostic_prefix']):
            continue
        require(len(line) <= 16384, 'A native Close observation exceeds its finite bound.')
        row = json.loads(line[len(NATIVE_CONTRACT['diagnostic_prefix']):])
        require(type(row) is dict, 'A native Close observation is invalid.')
        rows.append(row)
        require(len(rows) <= 2, 'The actual host Close observations are ambiguous.')
    requested = rows[0] if len(rows) == 2 and rows[0].get('event') == 'requested' else {}
    settled = rows[1] if len(rows) == 2 and rows[1].get('event') == 'settled' else {}
    before = requested.get('before')
    typed_before = (type(before) is dict
        and all(type(before.get(key)) is bool for key in
                ('working', 'busy', 'closing', 'main_loop', 'destroy_event_seen')))
    accepted = requested.get('accepted') is True
    running = typed_before and before['main_loop'] is True
    dead = settled.get('native_window_present') is False
    destroyed = settled.get('destroy_event_seen') is True
    windows_typed = (type(requested.get('native_window_present')) is bool
                     and type(settled.get('native_window_present')) is bool)
    label_count = raw.splitlines().count('PASS: ' + NATIVE_CONTRACT['close_label'])
    passed = bool(accepted and running and dead and destroyed and windows_typed and label_count == 1)
    return dict(status='PASS' if passed else 'NOT_PROVEN', observations=rows,
        requested_accepted=accepted, requested_during_main_loop=bool(running),
        settled_window_absent=dead, actual_destroy_event=destroyed,
        typed_hwnd_observations=windows_typed, actual_close_and_drain_label_count=label_count)


def validate_native(value, raw, first, *, cleanup_only):
    fields = NATIVE_CONTRACT['cleanup_fields' if cleanup_only else 'playback_fields']
    required = NATIVE_CONTRACT['cleanup_checks' if cleanup_only else 'playback_checks']
    require(all(value.get(name) == expected for name, expected in fields.items()),
            'The required native result fields do not match the actual requested mode.')
    checks = value.get('checks')
    require(type(checks) is list and len(checks) <= 256
            and all(type(label) is str and len(label) <= 512 for label in checks),
            'The native check inventory is invalid.')
    require(set(required) <= set(checks), 'A required native saved-audio check is absent.')
    if cleanup_only:
        require(len(checks) == len(set(checks)) == len(required) == 15,
                'The cleanup-only mode must contain exactly its 15 observations.')
    for label in required:
        require(checks.count(label) >= 1 and raw.splitlines().count('PASS: ' + label) >= 1,
                'A required native observation was not printed: ' + label)
        if cleanup_only:
            require(checks.count(label) == raw.splitlines().count('PASS: ' + label) == 1,
                    'A cleanup-only native observation is duplicated: ' + label)
    identity = value.get('source_identity')
    require(type(identity) is dict and set(identity) == set(NATIVE_CONTRACT['origins']),
            'Exactly 11 native module identities are required.')
    require(all(type(digest) is str and digest == first['origins'][name]['sha256']
                for name, digest in identity.items()),
            'The native process used different installed module bytes.')
    return close_observations(raw)


def native_phase(*, work, wheel, contract, evidence, ci, cleanup_only, inputs, audio, head):
    label = 'cleanup-only' if cleanup_only else 'default-diagnostic'
    wrapper = work / ('native-' + label + '-origin-child.py')
    wrapper.write_text(ORIGIN_CHILD, encoding='utf-8')
    prefix = 'native-' + label
    def snapshot(which):
        receipt = evidence / (prefix + '-origins-' + which + '.json')
        log = evidence / (prefix + '-origins-' + which + '.txt')
        require(child([sys.executable, '-I', '-B', str(wrapper), str(wheel),
            str(evidence / 'source-wheel-proof.json'), str(ROOT.parent),
            str(receipt), str(contract)], log, cwd=work, ci=ci, timeout=60) == 0,
            'The exact installed native origin snapshot failed.')
        value = json.loads(receipt.read_text(encoding='utf-8'))
        require(value.get('status') == 'PASS' and set(value.get('origins', {})) == set(MODULES),
                'The native origin snapshot does not cover all 20 current modules.')
        return value
    require(sources() == inputs and git('rev-parse', 'HEAD') == head,
            'Current sources changed before native execution.')
    audio_before = audio_inputs(audio)
    first = snapshot('before')
    code, value, failure, timeout = None, None, None, False
    raw = ''
    log = evidence / (prefix + '.txt')
    close = None
    try:
        command = [sys.executable, '-I', '-B', '-X', 'utf8',
                   str(ROOT / 'scripts/windows_audio_player_smoke.py'), '--installed']
        if cleanup_only:
            command.append('--failure-cleanup-only')
        try:
            code = child(command, log, cwd=work, ci=ci, timeout=120)
        except subprocess.TimeoutExpired:
            # child() first terminates and waits for the actual process tree.
            # No raw exit is invented when its wait was interrupted by this bound.
            timeout = True
        require(log.is_file() and log.stat().st_size <= 8 * 1024**2,
                'The native diagnostic log is absent or exceeds its finite bound.')
        raw = log.read_text(encoding='utf-8-sig', errors='replace')
        close = close_observations(raw)
        try:
            value = native_report(raw)
            write(evidence / (prefix + '-report.json'), value)
            observed_close = validate_native(value, raw, first, cleanup_only=cleanup_only)
            require(observed_close['status'] == 'PASS',
                    'Actual ordinary host Close, HWND destruction and worker drain are unproven.')
            require(code == 0 and not timeout,
                    'The native child did not complete successfully; its primary log is retained.')
        except Exception as error:
            failure = dict(type=type(error).__name__, error=str(error)[:2048])
    finally:
        last = snapshot('after')
        require(last == first, 'Installed native origins or wheel payload changed during this process.')
        require(sources() == inputs and git('rev-parse', 'HEAD') == head,
                'Current sources changed during this native process.')
        require(audio_inputs(audio) == audio_before,
                'Selected codec bytes changed during this native process.')
    passed = code == 0 and not timeout and failure is None and value is not None
    result = dict(status='PASS' if passed else ('FAIL' if cleanup_only else 'NOT_PASS'),
        qualification='cleanup-only' if cleanup_only else 'independent-default-playback-diagnostic',
        requested_mode='failure-cleanup-only' if cleanup_only else 'full-playback',
        required_checks=15 if cleanup_only else 46, required_native_origins=11,
        native_origin_checks_observed=sum(
            raw.splitlines().count('PASS: exact installed player module: ' + name) == 1
            for name in NATIVE_CONTRACT['origins']), installed_origins=20,
        raw_exit=code, timed_out=timeout, outer_timeout_seconds=120,
        native_watchdog_seconds=90, direct_child_wait_completed=code is not None,
        timeout_cleanup_attempted=timeout,
        native_owners_settled='PASS' if close and close['status'] == 'PASS' else 'NOT_PROVEN',
        installed_payload_unchanged=True, inputs_unchanged=True,
        host_close=close, failure=failure, log_sha256=sha256(log.read_bytes()).hexdigest(),
        full_playback='NOT_RUN' if cleanup_only else ('PASS' if passed else 'NOT_PASS'),
        actual_windows_waveout=('NOT_RUN' if cleanup_only else ('PASS' if passed else 'NOT_PASS')),
        actual_native_queue_controls=('NOT_RUN' if cleanup_only else ('PASS' if passed else 'NOT_PASS')),
        instance_volume_isolation=('NOT_RUN' if cleanup_only else ('PASS' if passed else 'NOT_PASS')),
        physical_device='NOT_RUN', audible_listening='NOT_RUN', nvda='NOT_RUN', release='NOT_RUN')
    write(evidence / (prefix + '-summary.json'), result)
    if cleanup_only:
        require(passed, 'Every required cleanup-only native observation must pass.')
    return result


def native_contract(*, work, wheel, contract, evidence, audio, ci, inputs, head):
    require(os.name == 'nt', 'This qualification requires an actual Windows desktop.')
    cleanup = native_phase(work=work, wheel=wheel, contract=contract, evidence=evidence,
        ci=ci, cleanup_only=True, inputs=inputs, audio=audio, head=head)
    diagnostic = native_phase(work=work, wheel=wheel, contract=contract, evidence=evidence,
        ci=ci, cleanup_only=False, inputs=inputs, audio=audio, head=head)
    result = dict(status='PASS', qualification='cleanup-only', cleanup_only=cleanup,
        default_playback_diagnostic=diagnostic, default_playback_required_for_main=True,
        physical_device='NOT_RUN', audible_listening='NOT_RUN', nvda='NOT_RUN', release='NOT_RUN')
    write(evidence / 'native-summary.json', result)
    return result


def main():
    # Isolated Python ignores PYTHONUTF8; display the retained UTF-8 child log.
    # This changes only this orchestrator's stream, not child/test encodings.
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--audio-directory', required=True, type=Path)
    args = parser.parse_args()
    require(os.name == 'nt' and __debug__ and re.fullmatch('[0-9a-f]{40}', args.expected_head),
            'Actual Windows, exact commit and assertions are required.')
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
    ci = load('saved_audio_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('saved_audio_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    source_log = evidence / 'source-contracts.txt'
    # One whole suite retains its original 600-second child limit; the
    # 660-second outer envelope also admits finite cleanup and reporting.
    code = child([sys.executable, '-B', str(ROOT / 'scripts/ci_check.py'),
                  '--jobs', '1', *SUITES], source_log, cwd=ROOT, ci=ci, timeout=660)
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
    with tempfile.TemporaryDirectory(prefix='carr-saved-audio-cleanup-', dir=work_parent) as tmp:
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
                'Every selected saved-audio/host/worker/resource origin and bootstrap are required.')
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
            evidence=evidence, audio=selected_audio, ci=ci, inputs=before, head=args.expected_head)
    require(audio_inputs(selected_audio) == audio_before, 'Selected codec changed during actual contracts.')
    require(sources() == before, 'Pinned sources changed during actual contracts.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Source commit changed.')
    report = dict(status='PASS', **identity, source_passed=sum(SUITES.values()),
        installed_passed=sum(SUITES.values()), installed=installed, inputs_unchanged=True,
        application_wheel_sha256=proof['wheel_sha256'], required_direct_child_waits_complete=True,
        required_native_cleanup=native['cleanup_only']['native_owners_settled'],
        default_native_cleanup=native['default_playback_diagnostic']['native_owners_settled'],
        expected_failures=False, native=native,
        qualification='Cleanup-only: exact whole13 source and isolated installed cases plus all15 actual native cleanup observations. The independent default playback diagnostic retains its own exit and qualification; no failed playback, physical listening, NVDA or release claim is transferred from cleanup success.')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
          'source_passed', 'installed_passed', 'inputs_unchanged', 'required_native_cleanup', 'default_native_cleanup')}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
