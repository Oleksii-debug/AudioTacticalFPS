"""Cross-platform regressions for native Windows publication failures."""
import os
import stat

import pytest

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
