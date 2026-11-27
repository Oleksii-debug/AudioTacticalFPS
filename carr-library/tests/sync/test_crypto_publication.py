"""Actual cipher/file regressions for exact snapshot publication."""
from hashlib import sha256
import os
from pathlib import Path
from threading import Event

import pytest

from carr_library.sync import crypto

PASSWORD = 'Synthetic publication regression 2026'


def source_for(tmp_path, decrypt):
    source = tmp_path / 'source'
    source.write_bytes(b'unchanged private synthetic source\n' * 257)
    if decrypt:
        sealed = tmp_path / 'input.carr'
        crypto.encrypt_file(source, sealed, PASSWORD)
        return sealed, crypto.decrypt_file
    return source, crypto.encrypt_file


@pytest.mark.parametrize('decrypt', [False, True])
@pytest.mark.parametrize('change', ['same_length', 'grow'])
def test_fsync_time_stage_mutation_is_never_accepted(tmp_path, monkeypatch, decrypt, change):
    source, transform = source_for(tmp_path, decrypt)
    original = source.read_bytes()
    output = tmp_path / 'output'
    fsync = crypto.os.fsync
    changed = []
    def mutate(fd):
        fsync(fd)
        if changed:
            return
        stage, = tmp_path.glob('.carr-crypto-*')
        before = stage.stat()
        os.lseek(fd, 0 if change == 'same_length' else before.st_size, os.SEEK_SET)
        os.write(fd, b'X')
        fsync(fd)
        # Equal size and restored public timestamps cannot authorize new bytes.
        os.utime(stage, ns=(before.st_atime_ns, before.st_mtime_ns))
        changed.append(stage)
    monkeypatch.setattr(crypto.os, 'fsync', mutate)
    with pytest.raises(crypto.CryptoError, match='changed|bytes'):
        transform(source, output, PASSWORD)
    assert changed and not output.exists()
    assert source.read_bytes() == original
    assert not tuple(tmp_path.glob('.carr-crypto-*'))


@pytest.mark.parametrize('decrypt', [False, True])
def test_after_commit_mutation_is_retained_but_not_accepted(tmp_path, monkeypatch, decrypt):
    source, transform = source_for(tmp_path, decrypt)
    output = tmp_path / 'output'
    method = 'rename' if os.name == 'nt' else 'link'
    publish = getattr(crypto.os, method)
    committed = []
    def change_after(*args, **kwargs):
        result = publish(*args, **kwargs)
        before = output.stat()
        with output.open('r+b') as stream:
            stream.write(b'X')
        os.utime(output, ns=(before.st_atime_ns, before.st_mtime_ns))
        committed.append(True)
        return result
    monkeypatch.setattr(crypto.os, method, change_after)
    with pytest.raises(crypto.CryptoError, match='published.*retained'):
        transform(source, output, PASSWORD)
    assert committed and output.is_file() and output.read_bytes().startswith(b'X')
    assert not tuple(tmp_path.glob('.carr-crypto-*'))


def test_late_cancel_preserves_verified_committed_receipt(tmp_path, monkeypatch):
    source, transform = source_for(tmp_path, False)
    output = tmp_path / 'output'
    stopped = Event()
    method = 'rename' if os.name == 'nt' else 'link'
    publish = getattr(crypto.os, method)
    def cancel_after(*args, **kwargs):
        result = publish(*args, **kwargs)
        stopped.set()
        return result
    monkeypatch.setattr(crypto.os, method, cancel_after)
    receipt = transform(source, output, PASSWORD, cancelled=stopped.is_set)
    assert stopped.is_set()
    assert receipt.sha256 == sha256(output.read_bytes()).hexdigest()
    assert receipt.size == output.stat().st_size
    assert not tuple(tmp_path.glob('.carr-crypto-*'))


def test_competing_destination_is_never_read_or_deleted(tmp_path, monkeypatch):
    source, transform = source_for(tmp_path, False)
    output = tmp_path / 'output'
    method = 'rename' if os.name == 'nt' else 'link'
    publish, opened = getattr(crypto.os, method), crypto.os.open
    rival = b'foreign output must remain'
    def compete(*args, **kwargs):
        output.write_bytes(rival)
        return publish(*args, **kwargs)
    def forbid_read(path, flags, *args, **kwargs):
        if Path(path).name == output.name:
            pytest.fail('A competing destination must not be opened for byte verification.')
        return opened(path, flags, *args, **kwargs)
    monkeypatch.setattr(crypto.os, method, compete)
    monkeypatch.setattr(crypto.os, 'open', forbid_read)
    with pytest.raises(crypto.CryptoError, match='appeared.*preserved'):
        transform(source, output, PASSWORD)
    assert output.read_bytes() == rival
    assert not tuple(tmp_path.glob('.carr-crypto-*'))


def test_cancel_after_byte_review_does_not_commit(tmp_path, monkeypatch):
    source, transform = source_for(tmp_path, False)
    output = tmp_path / 'output'
    stopped = Event()
    verify = crypto._Output._verify
    def cancel_after_review(self, name, **options):
        result = verify(self, name, **options)
        stopped.set()
        return result
    monkeypatch.setattr(crypto._Output, '_verify', cancel_after_review)
    with pytest.raises(InterruptedError):
        transform(source, output, PASSWORD, cancelled=stopped.is_set)
    assert stopped.is_set() and not output.exists()
    assert not tuple(tmp_path.glob('.carr-crypto-*'))


def test_replaced_stage_name_is_not_deleted_by_cleanup(tmp_path, monkeypatch):
    source, transform = source_for(tmp_path, False)
    output, retained = tmp_path / 'output', tmp_path / 'owned-stage-retained'
    original = crypto._Output.check
    replaced = []
    def replace_after_close(self):
        original(self)
        if self.stream is not None and self.stream.closed and not replaced:
            stage = self.path.parent / self.name
            os.rename(stage, retained)
            stage.write_bytes(b'foreign replacement')
            replaced.append(stage)
    monkeypatch.setattr(crypto._Output, 'check', replace_after_close)
    with pytest.raises(crypto.CryptoError, match='ownership.*retained'):
        transform(source, output, PASSWORD)
    assert not output.exists() and retained.is_file()
    assert len(replaced) == 1 and replaced[0].read_bytes() == b'foreign replacement'
