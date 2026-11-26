"""Keep speech output publication compatible with Windows file flushing."""
import errno
import os
from pathlib import Path
import stat

import pytest

from carr_library.speech import _io


def test_speech_flushes_writable_staging_before_publication(tmp_path, monkeypatch):
    target = tmp_path / 'speech.wav'
    content = b'Synthetic generated speech bytes; no recording.'
    actual_fsync, actual_link = os.fsync, os.link
    flushed = set()

    def windows_file_flush(descriptor):
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode) and os.name == 'posix':
            import fcntl
            mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
            if mode == os.O_RDONLY:
                raise OSError(errno.EBADF, 'Windows requires a writable file descriptor for fsync')
        result = actual_fsync(descriptor)
        if stat.S_ISREG(info.st_mode):
            flushed.add((info.st_dev, info.st_ino))
        return result

    def checked_link(source, destination, **kwargs):
        info = Path(source).stat()
        assert (info.st_dev, info.st_ino) in flushed, 'Speech was published before its successful file flush.'
        return actual_link(source, destination, **kwargs)

    monkeypatch.setattr(_io.os, 'fsync', windows_file_flush)
    monkeypatch.setattr(_io.os, 'link', checked_link)
    with _io.new_output(target) as staged:
        staged.write_bytes(content)
    assert target.read_bytes() == content
    assert not staged.exists()
    assert target.stat().st_nlink == 1
    with pytest.raises(FileExistsError):
        with _io.new_output(target):
            pytest.fail('An existing output must not be rendered over.')
    assert target.read_bytes() == content


def test_failed_speech_flush_discards_staging_without_publication(tmp_path, monkeypatch):
    target = tmp_path / 'speech.wav'

    def failed_flush(descriptor):
        raise OSError(errno.EIO, 'Synthetic device flush failure')

    monkeypatch.setattr(_io.os, 'fsync', failed_flush)
    with pytest.raises(OSError, match='device flush failure'):
        with _io.new_output(target) as staged:
            staged.write_bytes(b'Synthetic staged speech')
    assert not target.exists()
    assert not staged.exists()
