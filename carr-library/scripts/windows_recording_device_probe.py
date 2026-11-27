"""Explicit Windows DirectShow device acceptance with independently measured PCM.

--inventory-only discovers devices without opening capture. A capture requires
one exact --device-id or --device-name selected by the caller. The CI workflow
must choose only its reviewed virtual input. This probe never chooses a default
microphone, installs a driver, or treats generated loopback PCM as listening.
"""
import argparse
from array import array
from concurrent.futures import ThreadPoolExecutor
import faulthandler
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import traceback
import wave


def _write_tone(path, *, rate=48000, frequency=997, seconds=2):
    samples = array('h', (round(8000 * math.sin(2 * math.pi * frequency * frame / rate))
                         for frame in range(round(seconds * rate))))
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(samples.tobytes())


def measure_tone(path, *, frequency=997):
    """Independent PCM analysis; a nonempty WAV alone is never capture proof."""
    with wave.open(str(path), 'rb') as stream:
        if stream.getsampwidth() != 2 or stream.getnchannels() != 1:
            raise ValueError('The loopback fixture requires mono PCM16.')
        rate, frames = stream.getframerate(), stream.getnframes()
        if not 8000 <= rate <= 96000 or frames > 10 * rate:
            raise ValueError('The native probe exceeds its PCM analysis allowance.')
        raw = stream.readframes(frames)
        if len(raw) != frames * 2:
            raise ValueError('The measured device result contains truncated PCM.')
    samples = array('h', raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    width = rate // 2
    candidates = [samples[start:start + width] for start in range(0, len(samples) - width + 1, rate // 4)]
    if not candidates:
        raise ValueError('The device result is too short to measure.')
    selected = max(candidates, key=lambda row: sum(value * value for value in row))
    mean = sum(selected) / len(selected)
    centered = [value - mean for value in selected]
    rms = math.sqrt(sum(value * value for value in centered) / len(centered))
    def energy(target):
        coefficient, previous, older = 2 * math.cos(2 * math.pi * target / rate), 0.0, 0.0
        for sample in centered:
            current = sample + coefficient * previous - older
            older, previous = previous, current
        return max(0, previous * previous + older * older - coefficient * previous * older) / len(centered)**2
    wanted = energy(frequency)
    comparison = max(energy(frequency - 160), energy(frequency + 160), 1.0)
    return {'frames': frames, 'sample_rate': rate, 'rms_pcm16': rms,
            'tone_hz': frequency, 'tone_energy': wanted, 'comparison_energy': comparison,
            'matches_generated_tone': bool(rms > 30 and wanted > comparison * 16 and wanted > rms * rms * .1)}


def run(args, report):
    if os.name != 'nt':
        raise RuntimeError('This probe requires actual Windows DirectShow and MCI.')
    from carr_library.native_runtime import activate_bundled_runtime
    from carr_library.audio.recording import RecordingService, RecordingOptions
    from carr_library.speech.player import WavePlayer
    native = activate_bundled_runtime()
    try:
        with TemporaryDirectory(prefix='carr-virtual-recording-') as temporary:
            root = Path(temporary)
            library = root / 'library'
            library.mkdir()
            service = RecordingService(library, ffmpeg=args.ffmpeg)
            devices = service.devices()
            report['devices'] = [{'id': item.id, 'name': item.name} for item in devices]
            if args.inventory_only:
                report.update(status='PASS', scope='device inventory only; no capture opened')
                return
            selected = [item for item in devices if
                        (item.id == args.device_id if args.device_id is not None else item.name == args.device_name)]
            if len(selected) != 1:
                raise ValueError('Select one exact discovered virtual input identity; no default or fallback is allowed.')
            device = selected[0]
            report['selected_device'] = {'id': device.id, 'name': device.name}
            tone = root / 'independent-tone.wav'
            _write_tone(tone)
            report['generated_fixture_sha256'] = sha256(tone.read_bytes()).hexdigest()
            observed, stop, cancelled = threading.Event(), threading.Event(), threading.Event()
            measures = []
            def progress(seconds):
                measures.append(seconds)
                if seconds >= .1:
                    observed.set()
            player = WavePlayer()
            try:
                with ThreadPoolExecutor(max_workers=1) as worker:
                    future = worker.submit(service.capture, device.id,
                        title='Explicit virtual-input acceptance', options=RecordingOptions(
                            max_seconds=4, sample_rate=48000, channels=1, max_bytes=1024 * 1024),
                        progress=progress, stop_requested=stop.is_set, cancelled=cancelled.is_set)
                    try:
                        deadline = time.monotonic() + 15
                        while not observed.wait(.05):
                            if future.done():
                                future.result()
                                raise AssertionError('Device capture ended before measured progress.')
                            if time.monotonic() >= deadline:
                                raise TimeoutError('The selected native input did not produce measured progress.')
                        # Current WavePlayer, unchanged. The evaluation VM must
                        # route its default output to the explicitly chosen virtual input.
                        player.play(tone)
                        result = future.result(timeout=20)
                    except BaseException:
                        cancelled.set()
                        raise
                    finally:
                        player.stop()
                retained = service.get(result['id'])
                if retained != result or not measures:
                    raise AssertionError('The native capture did not retain its exact verified receipt.')
                report['measurement'] = measure_tone(result['path'])
                if not report['measurement']['matches_generated_tone']:
                    raise AssertionError('Actual native capture did not contain the independently generated 997 Hz PCM.')
                report.update(status='PASS', scope='explicit virtual input and current MCI output loopback',
                    actual_directshow_capture=True, actual_mci_output=True,
                    capture_receipt={key: result[key] for key in
                                     ('id', 'source', 'ffmpeg_sha256', 'audio', 'completion')})
            finally:
                cancelled.set()
                player.close()
    finally:
        if native is not None:
            native.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ffmpeg', required=True)
    parser.add_argument('--report', required=True, type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--inventory-only', action='store_true')
    selection.add_argument('--device-id')
    selection.add_argument('--device-name')
    args = parser.parse_args()
    report = {'status': 'FAILED', 'actual_directshow_capture': False, 'actual_mci_output': False,
              'physical_microphone': 'NOT_RUN', 'human_listening': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    faulthandler.dump_traceback_later(60, exit=True)
    try:
        run(args, report)
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    main()
