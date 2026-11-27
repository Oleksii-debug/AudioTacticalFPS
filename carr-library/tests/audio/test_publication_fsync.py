"""Verify the Windows writable-file flush boundary before audio publication."""
import errno
import os
from pathlib import Path
import stat

import pytest

from carr_library.audio import process


def test_publication_flushes_writable_file_before_link(tmp_path, monkeypatch):
    staged, target = tmp_path / 'staged.wav', tmp_path / 'published.wav'
    content = b'Synthetic rendered bytes; no voice recording.'
    staged.write_bytes(content)
    actual_fsync, actual_link = os.fsync, os.link
    flushed = set()

    def windows_file_flush(descriptor):
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode) and os.name == 'posix':
            import fcntl
            mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
            if mode == os.O_RDONLY:
                raise OSError(errno.EBADF, 'Windows requires a writable file descriptor for fsync')
        # On Windows the real fsync enforces the same writable-file requirement.
        result = actual_fsync(descriptor)
        if stat.S_ISREG(info.st_mode):
            flushed.add((info.st_dev, info.st_ino))
        return result

    def checked_link(source, destination, **kwargs):
        info = Path(source).stat()
        assert (info.st_dev, info.st_ino) in flushed, 'Publication preceded a successful file flush.'
        return actual_link(source, destination, **kwargs)

    monkeypatch.setattr(process.os, 'fsync', windows_file_flush)
    monkeypatch.setattr(process.os, 'link', checked_link)
    process.publish_file(staged, target)
    assert staged.read_bytes() == content == target.read_bytes()
    with pytest.raises(FileExistsError):
        process.publish_file(staged, target)
    assert target.read_bytes() == content


def test_flush_failure_preserves_staging_and_prevents_publication(tmp_path, monkeypatch):
    staged, target = tmp_path / 'staged.wav', tmp_path / 'published.wav'
    content = b'Synthetic staged output'
    staged.write_bytes(content)

    def failed_flush(descriptor):
        raise OSError(errno.EIO, 'Synthetic device flush failure')

    monkeypatch.setattr(process.os, 'fsync', failed_flush)
    with pytest.raises(OSError, match='device flush failure'):
        process.publish_file(staged, target)
    assert staged.read_bytes() == content
    assert not target.exists()


def test_cancellation_during_flush_does_not_publish(tmp_path, monkeypatch):
    staged, target = tmp_path / 'staged.wav', tmp_path / 'published.wav'
    staged.write_bytes(b'Synthetic rendered bytes')
    actual_fsync = os.fsync
    cancelled = False

    def cancel_during_flush(descriptor):
        nonlocal cancelled
        actual_fsync(descriptor)
        cancelled = True

    monkeypatch.setattr(process.os, 'fsync', cancel_during_flush)
    with pytest.raises(process.AudioCancelled, match='before publication'):
        process.publish_file(staged, target, cancelled=lambda: cancelled)
    assert staged.read_bytes() == b'Synthetic rendered bytes'
    assert not target.exists()


def test_cancellation_after_link_does_not_reclassify_committed_output(tmp_path, monkeypatch):
    staged, target = tmp_path / 'staged.wav', tmp_path / 'published.wav'
    staged.write_bytes(b'Synthetic rendered bytes')
    actual_link = os.link
    cancelled = False

    def cancel_after_link(source, destination, **kwargs):
        nonlocal cancelled
        actual_link(source, destination, **kwargs)
        cancelled = True

    monkeypatch.setattr(process.os, 'link', cancel_after_link)
    process.publish_file(staged, target, cancelled=lambda: cancelled)
    assert cancelled
    assert target.read_bytes() == staged.read_bytes()
