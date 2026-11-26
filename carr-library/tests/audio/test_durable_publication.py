"""Actual file publication, flush failure and checked-open regressions."""
import errno
import os
from pathlib import Path

import pytest

from carr_library.audio.jobs import publish_new
from carr_library.audio import jobs


def test_shared_cache_file_flushes_writably_and_reopens_exactly(tmp_path, writable_file_fsync):
    source = tmp_path / 'cache.wav'
    source.write_bytes(b'Exact synthetic audio bytes\x00\xff')
    first, second = tmp_path / 'first.wav', tmp_path / 'second.wav'
    publish_new(source, first)
    # Cached audio is deliberately hard-linked into several completed outputs.
    publish_new(source, second)
    source.unlink()
    assert first.read_bytes() == second.read_bytes() == b'Exact synthetic audio bytes\x00\xff'
    assert writable_file_fsync


def test_flush_failure_never_publishes_and_closes_descriptor(tmp_path, monkeypatch):
    source, target = tmp_path / 'source.wav', tmp_path / 'output.wav'
    source.write_bytes(b'Keep the source intact')
    descriptors = []

    def fail_flush(descriptor):
        descriptors.append(descriptor)
        raise OSError(errno.EIO, 'Synthetic disk flush failure')

    monkeypatch.setattr(os, 'fsync', fail_flush)
    with pytest.raises(OSError, match='flush failure'):
        publish_new(source, target)
    assert not target.exists()
    assert source.read_bytes() == b'Keep the source intact'
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError) as failure:
            os.fstat(descriptor)
        assert failure.value.errno == errno.EBADF


def test_replaced_source_is_rejected_before_flush_or_publication(tmp_path, monkeypatch):
    source, target = tmp_path / 'source.wav', tmp_path / 'output.wav'
    replacement = tmp_path / 'replacement.wav'
    source.write_bytes(b'Original checked source')
    replacement.write_bytes(b'Different file at the same path')
    real_regular_file = jobs.regular_file

    def replace_after_validation(path):
        checked = real_regular_file(path)
        if Path(path) == source and replacement.exists():
            os.replace(replacement, source)
        return checked

    monkeypatch.setattr(jobs, 'regular_file', replace_after_validation)
    with pytest.raises(ValueError, match='changed'):
        publish_new(source, target)
    assert not target.exists()
    assert source.read_bytes() == b'Different file at the same path'
