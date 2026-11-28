"""Reproducible 1/3/10-hour streaming proof using an explicitly repeated fixture.

This is an opt-in integration exercise, not full-length neural generation.
It generates one real local instrumental segment and reuses its unchanged bytes.
Final files are decoded for measurement and removed unless --keep-output is set.
"""
import argparse
import json
from pathlib import Path
import tempfile
import threading
import time

from carr_library.audio.music import MusicSpec, generate_music
from carr_library.audio.render import (RenderCancelled, concatenate_audio,
    file_sha256, mix_tracks, runtime_identity)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--durations', nargs='+', type=int, default=[3600, 10800, 36000])
    parser.add_argument('--keep-output', type=Path)
    args = parser.parse_args(argv)
    if any(duration <= 0 or duration % 10 for duration in args.durations):
        parser.error('Durations must be positive multiples of the ten-second fixture.')
    if args.report.exists():
        parser.error('Choose a new report path.')
    temporary = None
    if args.keep_output:
        args.keep_output.mkdir()
        root = args.keep_output
    else:
        temporary = tempfile.TemporaryDirectory(prefix='carr-audio-long-')
        root = Path(temporary.name)
    try:
        try:
            import psutil
            parent = psutil.Process()
        except ImportError:
            parent = None
        report = {'version': 1, 'fixture': 'Ten-second actual procedural instrumental, explicitly repeated',
                  'full_length_neural_generation': 'NOT_RUN',
                  'sample_rate': 8000, 'channels': 1, 'runtime': runtime_identity(), 'runs': []}
        segment = generate_music(MusicSpec(10, seed=20261126, mood='suspense'),
                                 root/'segment.wav', sample_rate=8000, channels=1)
        baseline = file_sha256(segment.path)
        start = time.monotonic()
        try:
            mix_tracks([], root/'cancelled.wav', duration_seconds=36000, sample_rate=8000, channels=1,
                       cancelled=lambda: time.monotonic() - start >= .2)
        except RenderCancelled:
            assert not (root/'cancelled.wav').exists()
            report['interrupted_stage'] = 'PASS: worker stopped, no partial published output, input chunk preserved'
        else:
            raise AssertionError('Expected cancellation of a real long assembly worker.')
        for duration in args.durations:
            stop, peaks = threading.Event(), []
            def monitor():
                while not stop.is_set():
                    if parent is not None:
                        total = parent.memory_info().rss
                        for child in parent.children(recursive=True):
                            try:
                                total += child.memory_info().rss
                            except psutil.Error:
                                pass
                        peaks.append(total)
                    stop.wait(.05)
            watcher = threading.Thread(target=monitor, daemon=True)
            watcher.start()
            begin = time.monotonic()
            try:
                result = concatenate_audio([segment.path], root/f'{duration}-seconds.flac',
                                           repeat=duration//10, max_output_bytes=1024**3)
                elapsed = time.monotonic() - begin
            finally:
                stop.set()
                watcher.join(timeout=2)
            assert result.frames == duration * 8000
            assert result.duration_seconds == duration
            assert result.channels == 1 and result.sample_rate == 8000
            assert result.metadata['repeat'] == duration // 10
            assert file_sha256(segment.path) == baseline
            peak_memory = max(peaks, default=None)
            if peak_memory is not None:
                assert peak_memory < 512 * 1024**2, 'Streaming memory exceeded the measured acceptance budget.'
            data = dict(duration_seconds=duration, frames=result.frames,
                        output_bytes=result.path.stat().st_size, elapsed_seconds=round(elapsed, 3),
                        peak_process_tree_rss_bytes=peak_memory, sha256=result.sha256,
                        peak_dbfs=result.peak_dbfs, repeat=result.metadata['repeat'],
                        input_sha256=baseline, status='PASS')
            report['runs'].append(data)
            print(json.dumps(data), flush=True)
            if not args.keep_output:
                result.path.unlink()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        return 0
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == '__main__':
    raise SystemExit(main())
