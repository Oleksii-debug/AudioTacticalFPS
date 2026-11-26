"""Explicit disk-backed 1/3/10-hour assembly benchmark, with truthful fixtures.

Uses a genuinely generated short procedural score repeatedly. This verifies
streaming, recovery and RIFF/RF64, not hours of original neural composition.
No sparse files, fake headers, voice quality claims or public audio assets.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from time import monotonic

from carr_library.audio.ffmpeg import concatenate, process_voice, wav_info
from carr_library.audio.jobs import ChunkCache, RenderCancelled, sha256_file
from carr_library.audio.music import generate_music


def peak_rss():
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if sys.platform == 'darwin' else value * 1024
    except ImportError:
        return None


def run(root, ffmpeg, hours, sample_rate):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'original-short-score.wav'
    if not source.exists():
        generate_music(source, duration=10, sample_rate=sample_rate, mood='calm',
                       tempo=96, seed=42, ffmpeg=ffmpeg)
    stereo = root / 'stereo-score.wav'
    if not stereo.exists():
        process_voice(source, stereo, ffmpeg=ffmpeg, sample_rate=sample_rate)
    source_info = wav_info(stereo)
    assert source_info.frames == sample_rate * 10
    cache = ChunkCache(root / 'cache', disk_budget_bytes=1024**3)
    key = sha256(('benchmark-loop-v1:' + sha256_file(stereo)).encode()).hexdigest()
    ready = cache.get(key)
    if ready is None:
        block = root / 'ten-minute-block.wav'
        concatenate([stereo] * 60, block)
        ready = cache.seal(key, block, provenance={'fixture': 'repeated short procedural score', 'loops': 60})
        block.unlink()
    # Reopen the durable cache before continuing, as after a process restart.
    reopened = ChunkCache(root / 'cache', disk_budget_bytes=1024**3).get(key)
    assert reopened is not None and reopened[1]['sha256'] == ready[1]['sha256']
    chunk = reopened[0]
    checks = [0]

    def cancelled():
        checks[0] += 1
        return checks[0] > 3

    interrupted = root / 'interrupted.wav'
    try:
        concatenate([chunk] * 6, interrupted, cancelled=cancelled)
    except RenderCancelled:
        pass
    else:
        raise AssertionError('Cancellation was not observed.')
    assert not interrupted.exists(), 'Incomplete output was published.'
    records = []
    for count in hours:
        total = int(count * 3600 * sample_rate)
        expected_bytes = total * 4
        if shutil.disk_usage(root).free < expected_bytes + 256 * 1024**2:
            raise OSError('Insufficient space for the requested real streaming benchmark.')
        output = root / f'{count}-hours.wav'
        start = monotonic()
        concatenate([chunk] * (count * 6), output)
        elapsed = monotonic() - start
        info = wav_info(output)
        assert (info.frames, info.rate, info.channels) == (total, sample_rate, 2)
        with output.open('rb') as stream:
            header = stream.read(4).decode('ascii')
            stream.seek(info.data_offset)
            beginning = stream.read(128)
            stream.seek(info.data_offset + info.data_size - 128)
            ending = stream.read(128)
        with chunk.open('rb') as stream:
            chunk_info = wav_info(chunk)
            stream.seek(chunk_info.data_offset)
            assert beginning == stream.read(128)
            stream.seek(chunk_info.data_offset + chunk_info.data_size - 128)
            assert ending == stream.read(128)
        assert header == ('RF64' if expected_bytes + 36 > 0xffffffff else 'RIFF')
        records.append({'hours': count, 'duration_seconds': info.seconds, 'frames': info.frames,
            'file_bytes': output.stat().st_size, 'container': header, 'elapsed_seconds': elapsed,
            'python_peak_rss_bytes': peak_rss(), 'sample_rate': sample_rate,
            'sha256': sha256_file(output), 'complete_input_blocks': count * 6})
        output.unlink()
    return {'status': 'PASS', 'fixture': 'Repeated actual local procedural score; not full-length original neural music.',
        'source_sha256': sha256_file(source), 'checkpoint_reopen': 'PASS',
        'cancelled_output_not_published': 'PASS', 'checks': records,
        'neural_music_inference': 'NOT_RUN', 'listening_quality': 'NOT_RUN'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', nargs='+', type=int, default=[1, 3, 10])
    parser.add_argument('--sample-rate', type=int, choices=(8000, 48000), default=8000)
    parser.add_argument('--ffmpeg', type=Path, default=Path(shutil.which('ffmpeg') or 'ffmpeg'))
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if any(n < 1 or n > 100 for n in args.hours):
        parser.error('Benchmark hours must be integers from 1 to 100.')
    with TemporaryDirectory(prefix='carr-long-audio-', dir=args.report.parent) as temporary:
        result = run(temporary, args.ffmpeg, args.hours, args.sample_rate)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
