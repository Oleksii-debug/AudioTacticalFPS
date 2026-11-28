"""Cross-platform regressions for native Windows publication failures."""
import os
from pathlib import Path
import stat

import pytest

from carr_library.audio import jobs
from carr_library.audio.jobs import publish_new


def test_publication_flushes_with_a_writable_handle(tmp_path, monkeypatch):
    source, output = tmp_path / "staged.wav", tmp_path / "final.wav"
    source.write_bytes(b"already generated PCM bytes")
    fsync = os.fsync
    flushed = []

    def windows_flush_semantics(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            # A zero-byte write checks writable access without changing data,
            # reproducing Windows FlushFileBuffers' access requirement on POSIX.
            os.write(descriptor, b"")
            flushed.append(descriptor)
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", windows_flush_semantics)
    publish_new(source, output)
    assert flushed
    assert output.read_bytes() == source.read_bytes() == b"already generated PCM bytes"


def test_publication_does_not_suppress_a_real_flush_failure(tmp_path, monkeypatch):
    source, output = tmp_path / "staged.wav", tmp_path / "final.wav"
    source.write_bytes(b"durability is required before success")

    def failed_flush(descriptor):
        raise OSError("storage flush failed")

    monkeypatch.setattr(os, "fsync", failed_flush)
    with pytest.raises(OSError, match="storage flush failed"):
        publish_new(source, output)
    assert not output.exists()
    assert source.read_bytes() == b"durability is required before success"


@pytest.mark.parametrize('stop_after_flush', [1, 2], ids=['copy-flush', 'final-stage-flush'])
def test_cancellation_during_actual_file_flush_does_not_publish(tmp_path, monkeypatch, stop_after_flush):
    source, output = tmp_path / 'source.wav', tmp_path / 'output.wav'
    content = b'Explicitly synthetic retained audio bytes'
    source.write_bytes(content)
    actual_fsync = os.fsync
    observed = []
    stopped = False

    def flush_then_stop(descriptor):
        nonlocal stopped
        actual_fsync(descriptor)
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            observed.append(info.st_size)
            if len(observed) == stop_after_flush:
                stopped = True

    monkeypatch.setattr(jobs.os, 'fsync', flush_then_stop)
    with pytest.raises(jobs.RenderCancelled, match='retained for resume'):
        publish_new(source, output, cancelled=lambda: stopped)
    assert observed == [len(content)] * stop_after_flush
    assert stopped and not output.exists()
    assert source.read_bytes() == content
    assert not list(tmp_path.glob('.publish-*'))


def test_cancellation_during_stage_directory_sync_preserves_unpublished_stage(tmp_path, monkeypatch):
    source, output = tmp_path / 'stage', tmp_path / 'output'
    source.mkdir()
    (source / 'complete.json').write_bytes(b'{"status":"complete"}')
    original_identity = (source.stat().st_dev, source.stat().st_ino)
    actual_sync = jobs._sync_directory
    observed = []
    stopped = False

    def sync_then_stop(directory):
        nonlocal stopped
        actual_sync(directory)
        if Path(directory) == source:
            observed.append(source.stat().st_ino)
            stopped = True

    monkeypatch.setattr(jobs, '_sync_directory', sync_then_stop)
    with pytest.raises(jobs.RenderCancelled, match='retained for resume'):
        jobs.publish_noreplace(source, output, cancelled=lambda: stopped)
    assert observed == [original_identity[1]]
    assert (source.stat().st_dev, source.stat().st_ino) == original_identity
    assert (source / 'complete.json').read_bytes() == b'{"status":"complete"}'
    assert not output.exists()


@pytest.mark.parametrize('kind', ['file-copy', 'directory'])
def test_cancellation_after_atomic_move_keeps_committed_publication(tmp_path, monkeypatch, kind):
    source, output = tmp_path / 'source', tmp_path / 'published'
    content = b'Committed independent bytes'
    if kind == 'directory':
        source.mkdir()
        (source / 'result').write_bytes(content)
    else:
        source.write_bytes(content)
    actual_sync = jobs._sync_directory
    observed = []
    stopped = False

    def sync_then_stop(directory):
        nonlocal stopped
        actual_sync(directory)
        if output.exists():
            observed.append(True)
            stopped = True

    monkeypatch.setattr(jobs, '_sync_directory', sync_then_stop)
    if kind == 'directory':
        jobs.publish_noreplace(source, output, cancelled=lambda: stopped)
        assert not source.exists()
        assert (output / 'result').read_bytes() == content
    else:
        publish_new(source, output, cancelled=lambda: stopped)
        assert source.read_bytes() == output.read_bytes() == content
        assert output.stat().st_nlink == 1
    assert stopped and observed == [True]
    assert not list(tmp_path.glob('.publish-*'))
