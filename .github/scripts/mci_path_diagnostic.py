"""One-off same-PCM MCI filename diagnostic on the reviewed Windows endpoint.

Only public generated PCM is used. This does not qualify capture, hearing,
NVDA, a product repair or the application's unchanged acceptance gates.
"""
from __future__ import annotations

import argparse
import ctypes
import faulthandler
from hashlib import sha1, sha256
import importlib
import importlib.metadata
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import sys
import tempfile
import time
import wave

APPLICATION_COMMIT = '10b9a4e36b6415cdf9a21fbc36bcd9ef2b72420a'
EXPECTED_NAME = 'CABLE In 16 Ch (VB-Audio Virtua'
EXPECTED_BLOBS = {
    "speech/player.py": "b67e1134fe9a5b63a3c5822e1ecd5ca8691942ac",
    "speech/_wave_output.py": "6d827eddc503ccd68c6b1f2f3d3c85ed8651fedf",
    "speech/output_devices.py": "a2b604ac3be0fdb7591179be12e76dd4b49d9f9f",
    "speech/_io.py": "90d86bac79bd7b34b279a56afe2e6d49e776e188",
    "audio/recording_monitor.py": "d30d359e9b69400716ddeb81e2be2c06d2eec9f1",
    "audio/process.py": "afbccf32c08bf5b6d20cf8cdea420bedfd6b26e8",
    "audio/windows_process.py": "12b249c548c211333d783877a7ef0c08f2dd2ed1"
}


def _read(path, limit):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise RuntimeError('A diagnostic input exceeds its fixed byte allowance.')
    return raw


def _record(path, report):
    raw = (json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + '\n').encode()
    if len(raw) > 64 * 1024:
        raise RuntimeError('The diagnostic receipt exceeds its byte allowance.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.partial')
    with temporary.open('wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _phase(args, report, value):
    report['phase'] = value
    _record(args.report, report)
    print(value, flush=True)


def _installed_owner(package_root):
    owners = []
    for distribution in importlib.metadata.distributions():
        if distribution.metadata.get('Name', '').replace('_', '-').casefold() != 'universal-research-library':
            continue
        matches = [Path(distribution.locate_file(member)).resolve(strict=True)
                   for member in distribution.files or ()
                   if str(member).replace('\\', '/') == 'carr_library/__init__.py']
        if matches == [package_root / '__init__.py']:
            owners.append(distribution)
    if len(owners) != 1:
        raise RuntimeError('The actual imported package has no unique wheel RECORD owner.')
    return owners[0]


def _origin(args, report):
    checkout = Path(__file__).resolve(strict=True).parents[2] / 'carr-library'
    checkout = checkout.resolve(strict=True)
    if args.source is not None:
        if args.expect_installed or args.source.resolve(strict=True) != checkout:
            raise RuntimeError('Choose this exact source checkout or the installed wheel.')
        sys.path.insert(0, str(checkout / 'src'))
    elif not args.expect_installed:
        raise RuntimeError('An explicit source or installed origin is required.')
    import carr_library
    package_root = Path(carr_library.__file__).resolve(strict=True).parent
    distribution = None if args.source is not None else _installed_owner(package_root)
    helper_path = checkout / 'scripts/windows_clipboard_reader_smoke.py'
    helper_raw = _read(helper_path, 128 * 1024)
    spec = importlib.util.spec_from_file_location('_mci_source_evidence', helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    modules = []
    for relative, expected in EXPECTED_BLOBS.items():
        raw = _read(checkout / 'src/carr_library' / relative, 2 * 1024 * 1024)
        actual = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        if actual != expected:
            raise RuntimeError('The native diagnostic source differs from the frozen P3 application.')
        module = importlib.import_module('carr_library.' + relative[:-3].replace('/', '.'))
        modules.append((relative, module))
    report['module_evidence'] = helper._module_evidence(
        checkout, package_root, modules, source_mode=args.source is not None,
        distribution=distribution)
    report['expected_source_blobs'] = EXPECTED_BLOBS
    report['source_proof_helper_sha256'] = sha256(helper_raw).hexdigest()
    report['package_root'] = str(package_root)
    report['distribution'] = None if distribution is None else {
        'name': distribution.metadata['Name'], 'version': distribution.version}
    return dict(modules)


def _pcm():
    stream = io.BytesIO()
    frames = b''.join(struct.pack('<h', round(5000 * math.sin(2 * math.pi * 997 * i / 16000)))
                      for i in range(3 * 16000))
    with wave.open(stream, 'wb') as writer:
        writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        writer.writeframes(frames)
    return stream.getvalue()


def _units(path):
    return len(str(path).encode('utf-16-le')) // 2


def _fixtures(root, raw):
    checksum = sha256(raw).hexdigest()
    result = []
    for kind in ('short', 'long'):
        for suffix in ('.wav', '.audio'):
            if kind == 'short':
                path = root / ('s' + suffix)
            else:
                candidate = root / 'r' / 'state/studio/inputs' / (checksum + suffix)
                extra = 174 - _units(candidate)
                if not 0 <= extra <= 120:
                    raise RuntimeError('This runner cannot represent the exact 174-unit fixture safely.')
                path = root / ('r' + 'x' * extra) / 'state/studio/inputs' / (checksum + suffix)
            if ((kind == 'short' and _units(path) + 1 > 128)
                    or (kind == 'long' and _units(path) != 174)):
                raise RuntimeError('The requested short/long filename condition was not met.')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if _read(path, len(raw)) != raw:
                raise RuntimeError('The native fixtures do not contain exactly the same PCM bytes.')
            with wave.open(str(path), 'rb') as reader:
                if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(),
                        reader.getnframes(), reader.getcomptype()) != (1, 2, 16000, 48000, 'NONE'):
                    raise RuntimeError('The actual PCM fixture differs from the declared format.')
            result.append((kind, suffix, path))
    # Keep the original four observations, then isolate basename from full path.
    # The identical retained 64-hex leaf appears below and in both long cases.
    for kind, target_units in (('short-same-leaf', None), ('boundary-127', 127),
                               ('boundary-128', 128)):
        for suffix in ('.wav', '.audio'):
            leaf = checksum + suffix
            if target_units is None:
                path = root / leaf
                if _units(path) + 1 > 128:
                    raise RuntimeError('The same-leaf short filename does not fit the documented allowance.')
            else:
                base = root / 'r' / leaf
                extra = target_units - _units(base)
                if not 0 <= extra <= 120:
                    raise RuntimeError('This runner cannot represent the requested filename boundary.')
                path = root / ('r' + 'x' * extra) / leaf
                if _units(path) != target_units:
                    raise RuntimeError('The requested exact filename boundary was not reached.')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if _read(path, len(raw)) != raw:
                raise RuntimeError('The basename/boundary controls changed the actual PCM bytes.')
            with wave.open(str(path), 'rb') as reader:
                if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(),
                        reader.getnframes(), reader.getcomptype()) != (1, 2, 16000, 48000, 'NONE'):
                    raise RuntimeError('The basename/boundary controls changed the PCM format.')
            result.append((kind, suffix, path))
    return result


def _error(exc):
    result = {'type': type(exc).__name__, 'message': str(exc)[:1200]}
    match = re.search(r'\(MCI ([0-9]+)\)', str(exc))
    if match is not None:
        code = int(match.group(1))
        result['mci_code'] = code
        text = ctypes.create_unicode_buffer(512)
        function = ctypes.windll.winmm.mciGetErrorStringW
        function.argtypes = (ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint)
        function.restype = ctypes.c_int
        result['native_error_text'] = text.value if function(code, text, len(text)) else None
    return result


def _mci_case(player_module, path):
    player = player_module.WavePlayer()
    result = {'api': 'unchanged WavePlayer._mci_open / mciSendCommandW',
              'play_called': False, 'opened': False, 'closed': None}
    try:
        player._mci_open(path)
        result['opened'] = True
        result['mci_code'] = 0
    except Exception as exc:
        result['error'] = _error(exc)
    finally:
        if result['opened']:
            try:
                player._mci('close ' + player._alias)
                result['closed'] = True
            except Exception as exc:
                result['closed'] = False
                result['close_error'] = _error(exc)
    return result


def _wave_case(wave_module, device_module, selected, path, args, report):
    output = wave_module.WaveOutput()
    result = {'status': 'FAIL', 'selected_index': selected.index,
              'selected_identity': selected.identity, 'opened': False,
              'closed': False, 'position_seconds': 0.0}
    report['wave_output'] = result
    try:
        selected = device_module.verify_output_device(selected)
        info = path.stat()
        expected = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        output.open(path, device=selected, paused=True, expected=expected)
        result.update(opened=True, admitted_paused=output.paused,
                      actual_index=output.device.index, actual_identity=output.device.identity,
                      format={'rate': output.rate, 'channels': output.channels,
                              'width': output.width, 'frames': output.last_frame})
        if output.device.identity != selected.identity or not output.paused:
            raise RuntimeError('The long-path output was not admitted paused on the exact reviewed endpoint.')
        _phase(args, report, 'long-path-wave-output-resume')
        output.resume()
        deadline = time.monotonic() + 5
        while True:
            output.pump()
            position = output.position_seconds
            result['position_seconds'] = position
            if position >= 0.15:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('The actual long-path wave output did not advance its measured position.')
            time.sleep(0.01)
        output.check_output()
        output.pause()
        result['paused_after_advance'] = output.paused
        result['status'] = 'PASS'
    except Exception as exc:
        result['error'] = _error(exc)
    finally:
        try:
            output.close()
            result['closed'] = not output.handle.value and not output.pending and output.stream is None
            if not result['closed']:
                raise RuntimeError('The native wave output did not release its handle, memory and source.')
        except Exception as exc:
            result['status'] = 'FAIL'
            result['close_error'] = _error(exc)
    return result


def run(args, report):
    if (sys.platform != 'win32' or struct.calcsize('P') != 8
            or os.environ.get('GITHUB_REPOSITORY') != 'Oleksii-debug/AudioTacticalFPS'
            or os.environ.get('GITHUB_REF') != 'refs/heads/carr/ci-endpoint-evaluation-20261127'
            or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'):
        raise RuntimeError('This diagnostic is restricted to the existing reviewed hosted Windows evaluation.')
    _phase(args, report, 'source-and-wheel-record')
    modules = _origin(args, report)
    devices = modules['speech/output_devices.py']
    inventory = devices.output_devices()
    report['outputs'] = [{'index': item.index, 'name': item.name, 'identity': item.identity}
                         for item in inventory]
    matches = [item for item in inventory if item.name == EXPECTED_NAME]
    if len(matches) != 1:
        raise RuntimeError('The exact reviewed secondary render endpoint is missing or ambiguous.')
    selected = devices.verify_output_device(matches[0])
    report['selected_output'] = {'index': selected.index, 'name': selected.name,
                                 'identity': selected.identity}
    report['mci_open_parameters_bytes'] = ctypes.sizeof(modules['speech/player.py']._MciOpenParameters)
    raw = _pcm()
    report['pcm'] = {'sha256': sha256(raw).hexdigest(), 'bytes': len(raw),
                     'rate': 16000, 'channels': 1, 'bits': 16, 'frames': 48000}
    root = Path(tempfile.mkdtemp(prefix='carr-mci-', dir=os.environ['RUNNER_TEMP'])).resolve(strict=True)
    report['fixture_root'] = str(root)
    report['fixture_cleanup'] = 'PENDING'
    fixtures = _fixtures(root, raw)
    report['matrix'] = []
    for kind, suffix, path in fixtures:
        devices.verify_output_device(selected)
        case = {'length': kind, 'suffix': suffix, 'path': str(path),
                'filename_utf16_units': _units(path), 'same_pcm_sha256': sha256(_read(path, len(raw))).hexdigest()}
        report['matrix'].append(case)
        _phase(args, report, 'typed-MCI-' + kind + suffix)
        case.update(_mci_case(modules['speech/player.py'], path))
        _record(args.report, report)
        if case.get('closed') is False:
            raise RuntimeError('A typed MCI fixture could not be closed; its source remains retained.')
    long_path = next(path for kind, suffix, path in fixtures if (kind, suffix) == ('long', '.audio'))
    _phase(args, report, 'existing-wave-output-exact-long-audio')
    result = _wave_case(modules['speech/_wave_output.py'], devices, selected, long_path, args, report)
    _record(args.report, report)
    if result['closed']:
        for _, _, path in fixtures:
            if _read(path, len(raw)) != raw:
                raise RuntimeError('A synthetic source changed during native diagnosis.')
        shutil.rmtree(root)
        report['fixture_cleanup'] = 'COMPLETE'
    if result['status'] != 'PASS':
        raise RuntimeError('The actual long-path WaveOutput diagnostic failed; inspect its retained native result.')
    if any(not item['opened'] for item in report['matrix'] if item['length'] == 'short'):
        raise RuntimeError('The short-path typed MCI control failed; long-path causality is not established.')
    if any(not item['opened'] and 'mci_code' not in item.get('error', {}) for item in report['matrix']):
        raise RuntimeError('A matrix case failed outside the actual MCI error contract.')
    report['status'] = 'COMPLETE'
    report['application_acceptance'] = 'NOT_RUN'
    _phase(args, report, 'complete-diagnostic-observations')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = {'status': 'FAIL', 'phase': 'starting', 'commit': os.environ.get('GITHUB_SHA'),
              'application_commit': APPLICATION_COMMIT, 'python': sys.version,
              'platform': platform.platform(), 'installed': args.expect_installed,
              'microphone_capture': 'NOT_RUN', 'human_listening': 'NOT_RUN',
              'default_device_changed': False, 'device_policy_changed': False,
              'application_acceptance': 'NOT_RUN'}
    _record(args.report, report)
    faulthandler.enable()
    faulthandler.dump_traceback_later(75, exit=True)
    try:
        run(args, report)
        print(json.dumps(report, ensure_ascii=True, allow_nan=False), flush=True)
        return 0
    except BaseException as exc:
        report['status'] = 'FAIL'
        report['failure'] = _error(exc)
        _record(args.report, report)
        print(json.dumps(report, ensure_ascii=True, allow_nan=False), flush=True)
        return 1
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
