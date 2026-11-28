"""Adapt canonical 5cdd598's real flush/publication boundary to the shared publisher.

No new process or publication subsystem: all current audio adapters use jobs.
"""
import os
from pathlib import Path
import stat
from threading import Event

import pytest

from carr_library.audio import recording, engines, ffmpeg, jobs
from carr_library.audio.recording_process import AudioCancelled


def _publisher(kind):
    return {'move': jobs.publish_noreplace, 'copy': jobs.publish_new,
            'capture': recording.publish_file, 'speech': engines._publish}[kind]


def _expected(kind):
    return {'move': jobs.RenderCancelled, 'copy': jobs.RenderCancelled,
            'capture': AudioCancelled, 'speech': InterruptedError}[kind]


@pytest.mark.parametrize('kind', ['move', 'copy', 'capture', 'speech'])
def test_cancellation_during_final_flush_does_not_publish(tmp_path, monkeypatch, kind):
    staged, target = tmp_path / 'staged.wav', tmp_path / 'published.wav'
    content = b'Synthetic rendered bytes; no private recording.'
    staged.write_bytes(content)
    actual_fsync, stop, flushes = os.fsync, Event(), []
    final_flush = 2 if kind in ('copy', 'capture') else 1

    def cancel_at_final_flush(descriptor):
        actual_fsync(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            flushes.append(descriptor)
            if len(flushes) == final_flush:
                stop.set()

    monkeypatch.setattr(os, 'fsync', cancel_at_final_flush)
    with pytest.raises(_expected(kind)):
        _publisher(kind)(staged, target, cancelled=stop.is_set)
    assert len(flushes) == final_flush, 'Cancellation must happen inside the final publisher flush.'
    assert not target.exists()
    assert staged.read_bytes() == content
    assert not list(tmp_path.glob('.publish-*'))


@pytest.mark.parametrize('kind', ['move', 'copy', 'capture', 'speech'])
def test_late_cancellation_cannot_reverse_actual_move(tmp_path, monkeypatch, kind):
    staged, target = tmp_path / 'staged.wav', tmp_path / 'published.wav'
    content = b'Completed output has crossed its atomic publication boundary.'
    staged.write_bytes(content)
    stop, actual_sync, observed = Event(), jobs._sync_directory, []

    def cancel_after_move(path):
        actual_sync(path)
        if target.exists():
            assert target.read_bytes() == content
            observed.append(Path(path))
            stop.set()

    def before_commit_only():
        assert not target.exists(), 'Cancellation was queried after publication committed.'
        return stop.is_set()

    monkeypatch.setattr(jobs, '_sync_directory', cancel_after_move)
    _publisher(kind)(staged, target, cancelled=before_commit_only)
    assert observed and stop.is_set() and target.read_bytes() == content
    assert target.stat().st_nlink == 1
    assert staged.exists() is (kind in ('copy', 'capture'))


def test_directory_flush_cancellation_preserves_unpublished_complete_stage(tmp_path, monkeypatch):
    staged, target = tmp_path / 'stage', tmp_path / 'render'
    staged.mkdir()
    marker = staged / 'complete.json'
    marker.write_bytes(b'{"status":"complete"}')
    actual_sync, stop = jobs._sync_directory, Event()

    def cancel_after_stage_flush(path):
        actual_sync(path)
        if Path(path) == staged:
            stop.set()

    monkeypatch.setattr(jobs, '_sync_directory', cancel_after_stage_flush)
    with pytest.raises(jobs.RenderCancelled):
        jobs.publish_noreplace(staged, target, cancelled=stop.is_set)
    assert marker.read_bytes() == b'{"status":"complete"}' and not target.exists()


def test_real_pcm_silence_cannot_publish_after_final_flush_cancellation(tmp_path, monkeypatch):
    target = tmp_path / 'silence.wav'
    actual_fsync, stop, flushes = os.fsync, Event(), []

    def flushed(descriptor):
        actual_fsync(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            flushes.append(descriptor)
            if len(flushes) == 2:
                stop.set()

    monkeypatch.setattr(os, 'fsync', flushed)
    with pytest.raises(jobs.RenderCancelled):
        ffmpeg.silence(target, 64, sample_rate=8000, cancelled=stop.is_set)
    assert len(flushes) == 2 and not target.exists()
    assert not list(tmp_path.iterdir())


def test_flush_failure_still_preserves_stage_and_prevents_publication(tmp_path, monkeypatch):
    staged, target = tmp_path / 'stage.wav', tmp_path / 'output.wav'
    content = b'Unpublished synthetic bytes'
    staged.write_bytes(content)

    def failed_flush(descriptor):
        raise OSError('Injected flush failure')

    monkeypatch.setattr(os, 'fsync', failed_flush)
    with pytest.raises(OSError, match='flush failure'):
        jobs.publish_noreplace(staged, target)
    assert staged.read_bytes() == content and not target.exists()
