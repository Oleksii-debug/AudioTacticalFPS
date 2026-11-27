"""Real selected WinMM output of a growing generated recording fixture.

This does not open any input device. It exercises the same bounded dry-monitor
buffers and private-file tap used by RecordingService. Exact endpoint identity
is required before playback; submission is never a human-listening claim.
"""
import argparse
from array import array
import faulthandler
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import time
import traceback
import wave


def run(args, report):
    if os.name != 'nt':
        raise RuntimeError('Actual Windows WinMM output is required.')
    from carr_library.native_runtime import activate_bundled_runtime
    native = activate_bundled_runtime()
    try:
        import carr_library.audio.recording_monitor as monitor
        import carr_library.speech.output_devices as devices

        def check(name, condition):
            if not condition:
                raise AssertionError(name)
            report['checks'].append(name)
            print('PASS: ' + name, flush=True)

        checkout = Path(__file__).resolve().parents[1]
        for module in (monitor, devices):
            actual = Path(module.__file__).resolve()
            expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
            check('exact module source: ' + module.__name__,
                  (not actual.is_relative_to(checkout) if args.expect_installed else actual == expected)
                  and sha256(actual.read_bytes()).digest() == sha256(expected.read_bytes()).digest())

        inventory = devices.output_devices()
        matches = [device for device in inventory if device.index == args.device_index]
        check('one explicitly indexed native output', len(matches) == 1)
        selected = matches[0]
        check('reviewed stable endpoint identity still matches before playback',
              selected.identity == args.expected_device_identity)
        report['output'] = {'index': selected.index, 'name': selected.name,
                            'identity': selected.identity}

        rate, block_frames, blocks = 48000, 2400, 12
        samples = array('h', (round(1600 * math.sin(2 * math.pi * 997 * frame / rate))
                             for frame in range(block_frames * blocks)))
        if sys.byteorder != 'little':
            samples.byteswap()
        expected_pcm = samples.tobytes()
        check('generated PCM has nonzero measured samples', any(samples))
        maximum_queued = 0

        with TemporaryDirectory(prefix='resonance-monitor-proof-') as temporary:
            path = Path(temporary) / 'generated-capture.wav'
            with monitor.monitored_capture(path, sample_rate=rate, channels=1,
                    maximum_frames=rate * 2, device=selected) as owner:
                check('actual native handle is bound to the chosen output',
                      bool(owner.monitor.handle.value)
                      and owner.monitor.device.interface == selected.interface)
                with path.open('wb') as stream, wave.open(stream, 'wb') as writer:
                    writer.setparams((1, 2, rate, 0, 'NONE', 'PCM'))
                    writer.setnframes(block_frames * blocks)
                    for index in range(blocks):
                        writer.writeframesraw(expected_pcm[index * block_frames * 2:
                                                           (index + 1) * block_frames * 2])
                        stream.flush()
                        owner.poll()
                        maximum_queued = max(maximum_queued, len(owner.monitor.pending))
                        time.sleep(.05)
                owner.poll()
                receipt = owner.receipt()
                check('native submission and dropped-frame counters cover the actual fixture',
                      receipt['submitted_frames'] > 0
                      and receipt['submitted_frames'] + receipt['dropped_frames'] == block_frames * blocks)
                check('native queued PCM remains within the four-buffer allowance',
                      1 <= maximum_queued <= 4
                      and all(token.header.length <= block_frames * 2
                              for token in owner.monitor.pending))
                deadline = time.monotonic() + 5
                while owner.monitor.pending:
                    owner.monitor.poll()
                    if owner.monitor.pending:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Actual WinMM buffers did not complete within five seconds.')
                        time.sleep(.01)
                check('actual completed buffers were unprepared before context close',
                      not owner.monitor.pending)
                with wave.open(str(path), 'rb') as reader:
                    actual_pcm = reader.readframes(reader.getnframes())
                check('monitoring leaves the generated recording PCM unchanged',
                      actual_pcm == expected_pcm)
                final_hash = sha256(path.read_bytes()).hexdigest()
            check('actual native monitor handle and growing-file reader are closed',
                  not owner.monitor.handle.value and not owner.monitor.pending
                  and owner.tap.reader is None and owner.tap.stream is None
                  and not monitor._UNRELEASED_MONITORS)
            check('completed capture bytes remain unchanged after monitor cleanup',
                  sha256(path.read_bytes()).hexdigest() == final_hash)
            path.unlink()
            check('private capture is deletable after actual resource cleanup', not path.exists())

        report.update(status='PASS', actual_winmm_output=True,
                      source='generated PCM through the existing private-file monitoring tap',
                      maximum_queued_buffers=maximum_queued, monitoring_receipt=receipt)
    finally:
        if native is not None:
            native.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device-index', type=int, required=True)
    parser.add_argument('--expected-device-identity', required=True)
    parser.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.device_index < 64:
        parser.error('--device-index must identify an explicitly reviewed WinMM output.')
    if re.fullmatch(r'[0-9a-f]{64}', args.expected_device_identity) is None:
        parser.error('--expected-device-identity must be the reviewed lower-case SHA-256 identity.')
    report = {'status': 'FAILED', 'checks': [], 'actual_winmm_output': False,
              'actual_input_capture': 'NOT_RUN', 'physical_microphone': 'NOT_RUN',
              'human_listening': 'NOT_RUN', 'nvda': 'NOT_RUN',
              'independent_deadline_seconds': 45}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    faulthandler.dump_traceback_later(45, exit=True)
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
