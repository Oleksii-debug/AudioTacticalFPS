"""Execute the exact bundled FFmpeg and ffprobe on synthetic local audio only."""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import wave


def run(command):
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if len(result.stdout) + len(result.stderr) > 2 * 1024 * 1024:
        raise RuntimeError('Audio runtime produced excessive diagnostic output.')
    if result.returncode:
        raise RuntimeError('Audio runtime failed: ' + result.stderr.decode('utf-8', 'replace')[-4000:])
    return result.stdout.decode('utf-8', 'strict')


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def probe(binary, path):
    return json.loads(run([binary, '-v', 'error', '-protocol_whitelist', 'file',
                           '-show_entries', 'stream=codec_name,channels,sample_rate:format=duration',
                           '-of', 'json', str(path)]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32' or struct.calcsize('P') != 8:
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Requires native Windows x64.', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library import audio_runtime
    from carr_library.audio_runtime import bundled_audio_tools, verify_audio_runtime
    from carr_library.audio.ffmpeg import select_audio_tool
    from carr_library.ui.audio_studio import StudioController
    bundle = args.bundle.absolute().resolve()
    if (Path(sys.prefix).resolve() != bundle / '.venv'
            or not Path(audio_runtime.__file__).resolve().is_relative_to(bundle / '.venv')):
        raise RuntimeError('Run this acceptance with the bundle installed interpreter and package.')
    tools = bundled_audio_tools(args.bundle.absolute())
    if tools is None:
        raise RuntimeError('The tested bundle does not include the reviewed audio runtime.')
    integrity = verify_audio_runtime(args.bundle.absolute() / 'audio-runtime')
    version = run([tools['ffmpeg'], '-hide_banner', '-version'])
    if integrity['version'] not in version.splitlines()[0]:
        raise RuntimeError('The executing FFmpeg identity differs from the pinned build.')
    filters = run([tools['ffmpeg'], '-hide_banner', '-filters'])
    encoders = run([tools['ffmpeg'], '-hide_banner', '-encoders'])
    required_filters = ['agate', 'afftdn', 'equalizer', 'acompressor', 'alimiter', 'loudnorm',
                        'aecho', 'amix', 'acrossfade', 'atempo', 'sidechaincompress']
    missing = [name for name in required_filters if not any(name in line.split()[:2] for line in filters.splitlines())]
    if missing:
        raise RuntimeError('Reviewed runtime lacks required audio filters: ' + ', '.join(missing))
    for name in ('pcm_s16le', 'flac', 'libmp3lame'):
        if not any(name in line.split()[:2] for line in encoders.splitlines()):
            raise RuntimeError('Reviewed runtime lacks required audio encoder: ' + name)
    checks = ['installed_package_identity', 'sealed_runtime_and_outer_manifest',
              'native_version', 'required_filters', 'required_encoders']
    with tempfile.TemporaryDirectory(prefix='CARR audio ž Україна ') as temp:
        root = Path(temp)
        controller = StudioController(private_root=root / 'private library')
        if (bundled_audio_tools() != tools
                or controller.effective_ffmpeg() != Path(tools['ffmpeg'])
                or select_audio_tool('ffmpeg') != Path(tools['ffmpeg'])):
            raise RuntimeError('The studio and CLI must discover the sealed bundled FFmpeg automatically.')
        checks.append('automatic_studio_and_cli_tool_selection')
        source, processed = root / 'original stereo.wav', root / 'processed stereo.wav'
        rate, frames = 24000, 72000
        samples = array.array('h')
        for index in range(frames):
            samples.extend((int(8192 * math.sin(2 * math.pi * 440 * index / rate)),
                            int(4096 * math.sin(2 * math.pi * 880 * index / rate))))
        if sys.byteorder != 'little':
            samples.byteswap()
        with wave.open(str(source), 'wb') as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(samples.tobytes())
        original_hash = file_hash(source)
        common = [tools['ffmpeg'], '-hide_banner', '-nostdin', '-loglevel', 'error', '-n',
                  '-protocol_whitelist', 'file', '-i', str(source)]
        run([*common, '-af', 'volume=0.5,highpass=f=60,lowpass=f=10000',
             '-ar', str(rate), '-ac', '2', '-c:a', 'pcm_s16le', str(processed)])
        with wave.open(str(processed), 'rb') as out:
            if (out.getnchannels(), out.getsampwidth(), out.getframerate(), out.getnframes()) != (2, 2, rate, frames):
                raise RuntimeError('Actual PCM output has incorrect format or duration.')
            result = array.array('h', out.readframes(frames))
        before_rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        after_rms = math.sqrt(sum(x * x for x in result) / len(result))
        if not .3 < after_rms / before_rms < .65:
            raise RuntimeError('Actual audio filter output has incorrect measured gain.')
        checks.extend(['unicode_paths_and_pcm_duration', 'actual_filter_response'])
        reports = {}
        for extension, codec in (('flac', 'flac'), ('mp3', 'libmp3lame')):
            destination = root / ('encoded.' + extension)
            run([*common, '-c:a', codec, str(destination)])
            report = probe(tools['ffprobe'], destination)
            if report['streams'][0]['channels'] != 2 or not 2.9 <= float(report['format']['duration']) <= 3.3:
                raise RuntimeError('Encoded audio has incorrect duration or channels.')
            reports[extension] = report
            checks.append('actual_' + extension + '_encode_probe')
        if file_hash(source) != original_hash:
            raise RuntimeError('Audio processing changed the source file.')
        checks.append('original_bytes_unchanged')
    print(json.dumps({'status': 'PASS', 'windows_execution': 'PASS', 'checks': len(checks),
                      'cases': checks, 'source_lock_sha256': integrity['source_lock_sha256'],
                      'runtime_manifest_sha256': integrity['manifest_sha256'],
                      'ffmpeg_version': version.splitlines()[0],
                      'source_sample_rate': rate, 'source_frames': frames,
                      'measured_rms_ratio': after_rms / before_rms, 'encoded_outputs': reports,
                      'network_use': 'none requested; external firewall enforcement required',
                      'playback_recording': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
