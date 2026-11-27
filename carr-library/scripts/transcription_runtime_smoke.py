"""Measure local ASR imports, CPU capability and synthetic PCM decoding.

Run with the application installed, or with PYTHONPATH=src from carr-library.
This script neither acquires a model nor performs neural transcription.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import struct
import sys
import tempfile
import time
import wave


@contextmanager
def capture_native_diagnostics(stream):
    """Capture native stdout/stderr diagnostics, including denied sockets."""
    sys.stdout.flush()
    sys.stderr.flush()
    previous = [(descriptor, os.dup(descriptor)) for descriptor in (1, 2)]
    try:
        for descriptor, _ in previous:
            os.dup2(stream.fileno(), descriptor)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        for descriptor, saved in previous:
            os.dup2(saved, descriptor)
            os.close(saved)


def check_runtime(directory):
    import faster_whisper
    from faster_whisper import WhisperModel
    import ctranslate2
    import av
    import onnxruntime
    import numpy
    from carr_library.audio.transcription import transcription_runtime_identity

    if metadata.version('faster-whisper') != '1.2.1' or not callable(WhisperModel):
        raise RuntimeError('This smoke check requires the selected faster-whisper 1.2.1 runtime.')
    # Imports above execute the actual native extension loading boundaries.
    if not faster_whisper.__file__:
        raise RuntimeError('The faster-whisper import has no installed module identity.')
    compute_types = sorted(ctranslate2.get_supported_compute_types('cpu'))
    if 'int8' not in compute_types:
        raise RuntimeError('The measured CTranslate2 CPU runtime does not support int8.')
    providers = onnxruntime.get_available_providers()
    if 'CPUExecutionProvider' not in providers:
        raise RuntimeError('The measured ONNX Runtime has no CPU execution provider.')

    rate, frames = 16000, 4000
    samples = [round(4096 * math.sin(2 * math.pi * 440 * index / rate)) for index in range(frames)]
    pcm = struct.pack('<' + str(frames) + 'h', *samples)
    source = directory / 'synthetic PCM Україна.wav'
    with wave.open(str(source), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    decoded = []
    with av.open(str(source), mode='r') as container:
        if len(container.streams.audio) != 1:
            raise RuntimeError('PyAV did not identify the synthetic mono PCM stream.')
        for frame in container.decode(audio=0):
            if frame.sample_rate != rate or len(frame.layout.channels) != 1 or frame.format.name != 's16':
                raise RuntimeError('PyAV changed the synthetic PCM format.')
            decoded.append(frame.to_ndarray().reshape(-1))
    if not decoded:
        raise RuntimeError('PyAV decoded no synthetic audio frames.')
    actual = numpy.concatenate(decoded)
    expected = numpy.frombuffer(pcm, dtype='<i2')
    if actual.size != frames or not numpy.array_equal(actual, expected):
        raise RuntimeError('PyAV did not recover the exact synthetic PCM samples.')
    identity = transcription_runtime_identity()
    return {
        'checks': ['faster_whisper_1_2_1_import', 'native_dependency_imports',
                   'ctranslate2_cpu_int8', 'onnxruntime_cpu_provider',
                   'pyav_exact_synthetic_pcm_decode', 'installed_runtime_file_fingerprint'],
        'cpu_compute_types': compute_types,
        'onnxruntime_providers': providers,
        'synthetic_audio': {'sample_rate': rate, 'channels': 1, 'sample_width_bytes': 2,
                            'decoded_frames': int(actual.size), 'exact_samples': True},
        'runtime': identity,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optional JSON evidence file')
    args = parser.parse_args(argv)
    for name in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY'):
        os.environ[name] = '1'
    attempted = []
    def audit(event, arguments):
        if event in {'socket.connect', 'socket.connect_ex', 'socket.bind', 'socket.getaddrinfo',
                     'socket.sendto', 'urllib.Request'}:
            attempted.append(event)
            raise PermissionError('Python network access is disabled during the ASR runtime smoke check.')
    sys.addaudithook(audit)
    started = time.monotonic()
    report = {
        'schema_version': 1, 'status': 'failed',
        'platform': platform.system(), 'machine': platform.machine(),
        'python': platform.python_version(),
        'model_acquisition': 'NOT_RUN', 'neural_inference': 'NOT_RUN',
        'quality_qualification': 'NOT_RUN',
        'network_observation': {
            'python_audit_events': attempted,
            'python_policy': 'Known socket and urllib network events are denied by a Python audit hook.',
            'native_boundary': 'Native C/C++ calls are outside that hook; this is not complete offline-inference qualification.',
        },
    }
    with tempfile.TemporaryFile(mode='w+b') as diagnostics:
        try:
            with tempfile.TemporaryDirectory(prefix='resonance-asr-runtime-') as temporary:
                with capture_native_diagnostics(diagnostics):
                    report.update(check_runtime(Path(temporary)))
            report['status'] = 'passed'
        except Exception as exc:
            report['error'] = f'{type(exc).__name__}: {exc}'
        diagnostics.seek(0)
        captured = diagnostics.read(65537)
        report['native_diagnostics'] = captured[:65536].decode('utf-8', 'replace')
        report['native_diagnostics_truncated'] = len(captured) > 65536
    report['seconds'] = round(time.monotonic() - started, 3)
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    if args.output is not None:
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
