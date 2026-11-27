"""Real encrypted/decrypted publications keep their outcome after the commit."""
from hashlib import sha256
import os
from pathlib import Path
import stat

import pytest

from carr_library.sync import crypto


PASSWORD = 'Synthetic saved-file boundary secret'


@pytest.fixture(scope='module')
def envelopes(tmp_path_factory):
    root = tmp_path_factory.mktemp('crypto-publication-original')
    source, target = root / 'plain', root / 'encrypted'
    plain = b'Exact authenticated publication\x00\xff' * 32769
    source.write_bytes(plain)
    crypto.encrypt_file(source, target, PASSWORD)
    return plain, target.read_bytes()


def operation(tmp_path, envelopes, decrypt):
    plain, encrypted = envelopes
    source, target = tmp_path / 'source', tmp_path / 'destination'
    source.write_bytes(encrypted if decrypt else plain)
    return source, target, crypto.decrypt_file if decrypt else crypto.encrypt_file


@pytest.mark.parametrize('decrypt', [False, True])
@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
@pytest.mark.parametrize('boundary', ['publication', 'cleanup', 'directory_sync'])
def test_committed_bytes_return_receipt_after_late_failures(tmp_path, monkeypatch, envelopes,
                                                         decrypt, error_type, boundary):
    source, target, invoke = operation(tmp_path, envelopes, decrypt)
    source_before = source.read_bytes()
    original_operation = getattr(os, 'rename' if os.name == 'nt' else 'link')
    original_unlink, original_fsync = os.unlink, os.fsync
    calls = []

    def publish_then_interrupt(*args, **kwargs):
        original_operation(*args, **kwargs)
        if target.exists():
            calls.append('publication')
            raise error_type('Synthetic interruption after actual atomic publication')

    def cleanup_then_interrupt(path, **kwargs):
        original_unlink(path, **kwargs)
        if target.exists() and '.carr-crypto-' in str(path):
            calls.append('cleanup')
            raise error_type('Synthetic interruption after actual private unlink')

    def sync_then_interrupt(fd):
        original_fsync(fd)
        if target.exists() and stat.S_ISDIR(os.fstat(fd).st_mode):
            calls.append('directory_sync')
            raise error_type('Synthetic directory durability interruption')

    if boundary == 'publication':
        monkeypatch.setattr(crypto.os, 'rename' if os.name == 'nt' else 'link', publish_then_interrupt)
    elif boundary == 'cleanup':
        if os.name == 'nt':
            # Windows publication moves the only alias, so exercise its owned
            # source-handle close instead of pretending a staging unlink exists.
            original_fdopen = os.fdopen

            class SourceHandle:
                def __init__(self, stream): self.stream = stream
                def __getattr__(self, name): return getattr(self.stream, name)
                def __enter__(self): return self
                def __exit__(self, *args):
                    result = self.stream.__exit__(*args)
                    if target.exists():
                        calls.append('cleanup')
                        raise error_type('Synthetic source handle close interruption')
                    return result

            def fdopen(fd, mode, *args, **kwargs):
                stream = original_fdopen(fd, mode, *args, **kwargs)
                return SourceHandle(stream) if mode == 'rb' and not target.exists() else stream
            monkeypatch.setattr(crypto.os, 'fdopen', fdopen)
        else:
            monkeypatch.setattr(crypto.os, 'unlink', cleanup_then_interrupt)
    else:
        # The concrete directory fsync belongs to the supported POSIX path;
        # Windows must honestly report that directory durability is unconfirmed.
        monkeypatch.setattr(crypto.os, 'fsync', sync_then_interrupt)
    try:
        receipt = invoke(source, target, PASSWORD)
    except KeyboardInterrupt:
        pytest.fail('A late interruption hid an already saved crypto receipt.')
    assert receipt.path == target and receipt.sha256 == sha256(target.read_bytes()).hexdigest()
    assert receipt.size == target.stat().st_size and receipt.encrypted is not decrypt
    assert source.read_bytes() == source_before
    if boundary != 'directory_sync' or os.name == 'posix':
        assert calls == [boundary]
        assert receipt.cleanup_warning
    if boundary == 'directory_sync':
        assert receipt.directory_synced is False
    assert not tuple(tmp_path.glob('.carr-crypto-*'))
    monkeypatch.undo()
    if decrypt:
        assert target.read_bytes() == envelopes[0]
    else:
        restored = tmp_path / 'checked-plaintext'
        crypto.decrypt_file(target, restored, PASSWORD)
        assert restored.read_bytes() == envelopes[0]


@pytest.mark.parametrize('decrypt', [False, True])
@pytest.mark.parametrize('change', ['foreign_equal_bytes', 'owned_corruption'])
def test_late_error_cannot_adopt_foreign_or_modified_publication(tmp_path, monkeypatch,
                                                              envelopes, decrypt, change):
    source, target, invoke = operation(tmp_path, envelopes, decrypt)
    original = getattr(os, 'rename' if os.name == 'nt' else 'link')
    relocate = os.rename
    observed = []

    def changed_publication(*args, **kwargs):
        original(*args, **kwargs)
        before = target.read_bytes()
        if change == 'foreign_equal_bytes':
            # Windows publication also uses os.rename. Keep the independent
            # replacement out of the patched publication callback itself.
            relocate(target, tmp_path / 'owned-original')
            target.write_bytes(before)
        else:
            with target.open('r+b') as stream:
                stream.write(bytes((before[0] ^ 1,)))
        observed.append(target.read_bytes())
        raise OSError('Synthetic interruption after independent output change')

    monkeypatch.setattr(crypto.os, 'rename' if os.name == 'nt' else 'link', changed_publication)
    with pytest.raises(crypto.CryptoError):
        invoke(source, target, PASSWORD)
    assert observed and target.read_bytes() == observed[0]


@pytest.mark.parametrize('decrypt', [False, True])
def test_commit_after_cancellation_check_cannot_be_reported_as_cancelled(tmp_path, monkeypatch,
                                                                     envelopes, decrypt):
    source, target, invoke = operation(tmp_path, envelopes, decrypt)
    original = getattr(os, 'rename' if os.name == 'nt' else 'link')
    stopped = []

    def publish_and_request_stop(*args, **kwargs):
        result = original(*args, **kwargs)
        stopped.append(True)
        return result

    monkeypatch.setattr(crypto.os, 'rename' if os.name == 'nt' else 'link', publish_and_request_stop)
    receipt = invoke(source, target, PASSWORD, cancelled=lambda: bool(stopped))
    assert stopped and receipt.sha256 == sha256(target.read_bytes()).hexdigest()
