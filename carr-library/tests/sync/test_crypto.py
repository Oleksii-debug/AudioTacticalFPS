"""Real PyCA crypto and real filesystem acceptance for portable envelopes."""
from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
from threading import Event

import pytest

from carr_library.sync import crypto


PASSWORD = 'Synthetic acceptance passphrase Ω 2026'


@pytest.fixture(scope='module')
def encrypted(tmp_path_factory):
    folder = tmp_path_factory.mktemp('real-envelope')
    plain = folder / 'prepared.bin'
    # Deliberately opaque: encryption must not inspect malformed archive members.
    payload = b'PK\x03\x04../untrusted-member\x00' + bytes(range(256)) * 12289
    plain.write_bytes(payload)
    target = folder / 'sealed.carrenc'
    receipt = crypto.encrypt_file(plain, target, PASSWORD)
    assert receipt.sha256 == sha256(target.read_bytes()).hexdigest()
    return payload, target.read_bytes()


def no_stage(folder):
    assert not tuple(folder.glob('.carr-crypto-*'))


@pytest.mark.parametrize('size', [0, 1, crypto.CHUNK_SIZE, crypto.CHUNK_SIZE + 13])
def test_real_roundtrip_and_bounded_progress(tmp_path, size, capsys):
    source, sealed, restored = (tmp_path / name for name in ('original', 'encrypted', 'restored'))
    data = (b'Synthetic source\x00\xff\n' * (size // 19 + 1))[:size]
    source.write_bytes(data)
    progress = []
    receipt = crypto.encrypt_file(source, sealed, PASSWORD, on_progress=lambda a, b: progress.append((a, b)))
    assert receipt.encrypted is True and receipt.plaintext_size == len(data)
    assert receipt.size == sealed.stat().st_size and receipt.path == sealed
    assert receipt.sha256 == sha256(sealed.read_bytes()).hexdigest()
    result = crypto.decrypt_file(sealed, restored, PASSWORD)
    assert result.encrypted is False and result.size == len(data)
    assert result.sha256 == sha256(data).hexdigest()
    assert restored.read_bytes() == source.read_bytes() == data
    assert progress[0] == (0, len(data)) and progress[-1] == (len(data), len(data))
    assert [a for a, _ in progress] == sorted(a for a, _ in progress)
    assert all(b == len(data) for _, b in progress)
    assert set(asdict(receipt)) == {'path', 'sha256', 'size', 'plaintext_size', 'encrypted', 'format_version'}
    assert PASSWORD not in repr(receipt)
    assert capsys.readouterr() == ('', '')
    no_stage(tmp_path)


def test_random_salt_nonce_and_independent_standard_primitives(tmp_path, encrypted):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    payload, previous = encrypted
    source, target = tmp_path / 'source', tmp_path / 'fresh'
    source.write_bytes(payload)
    crypto.encrypt_file(source, target, PASSWORD)
    current = target.read_bytes()
    wire = struct.Struct('>8sBBBBIIIQ16s4s')
    a, b = wire.unpack(previous[:wire.size]), wire.unpack(current[:wire.size])
    assert a[-2] != b[-2] and a[-1] != b[-1] and previous != current
    magic, version, cipher, kdf, log_n, r, p, chunk, size, salt, prefix = b
    assert (magic, version, cipher, kdf, log_n, r, p, chunk, size) == (
        b'CARRSYNC', 1, 1, 1, 17, 8, 1, 1048576, len(payload))
    master = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(PASSWORD.encode())
    def aes(group):
        key = HKDFExpand(algorithm=hashes.SHA256(), length=32,
                         info=b'CARRSYNC-v1-key\x00' + struct.pack('>Q', group)).derive(master)
        return AESGCM(key)
    header, tag = current[:wire.size], current[wire.size:wire.size + 16]
    assert aes(0).decrypt(prefix + struct.pack('>Q', 0), tag, b'CARRSYNC-v1-header\x00' + header) == b''
    offset, decoded = wire.size + 16, bytearray()
    count = (size + chunk - 1) // chunk
    for index in range(1, count + 1):
        length = min(chunk, size - len(decoded))
        aad = b'CARRSYNC-v1-chunk\x00' + header + tag + struct.pack('>QI', index, length)
        decoded += aes(1 + (index - 1) // 1024).decrypt(
            prefix + struct.pack('>Q', index), current[offset:offset + length + 16], aad)
        offset += length + 16
    assert bytes(decoded) == payload
    assert aes(0).decrypt(prefix + struct.pack('>Q', count + 1), current[offset:],
        b'CARRSYNC-v1-final\x00' + header + tag + struct.pack('>QQ', count, size)) == b''
    # Group keys are domain-separated, including beyond the first GiB boundary.
    from cryptography.exceptions import InvalidTag
    sample = aes(1).encrypt(prefix + struct.pack('>Q', 1025), b'synthetic', b'group binding')
    with pytest.raises(InvalidTag):
        aes(2).decrypt(prefix + struct.pack('>Q', 1025), sample, b'group binding')


@pytest.mark.parametrize('field,value', [
    (0, b'OTHERFMT'), (1, 2), (2, 2), (3, 2), (4, 31),
    (4, 1), (5, 2**31), (6, 2**31), (7, 2**31), (8, 2**63),
])
def test_untrusted_header_rejected_before_kdf(tmp_path, encrypted, monkeypatch, field, value):
    _, sealed = encrypted
    fields = list(crypto._HEADER.unpack(sealed[:crypto._HEADER.size]))
    fields[field] = value
    source, output = tmp_path / 'untrusted', tmp_path / 'output'
    source.write_bytes(crypto._HEADER.pack(*fields) + sealed[crypto._HEADER.size:])
    # Not a crypto substitute: no KDF is permitted for structurally invalid input.
    def forbidden(*args):
        pytest.fail('An untrusted cost/length reached key derivation.')
    monkeypatch.setattr(crypto, '_master', forbidden)
    with pytest.raises(crypto.CryptoError, match='invalid or the passphrase'):
        crypto.decrypt_file(source, output, PASSWORD)
    assert not output.exists()
    no_stage(tmp_path)


@pytest.mark.parametrize('where', ['salt', 'nonce', 'header_tag', 'data', 'data_tag', 'final'])
def test_real_authentication_rejects_every_envelope_region(tmp_path, encrypted, where):
    _, original = encrypted
    offsets = {'salt': 32, 'nonce': 48, 'header_tag': 52, 'data': 68,
               'data_tag': 68 + crypto.CHUNK_SIZE, 'final': len(original) - 1}
    changed = bytearray(original)
    changed[offsets[where]] ^= 1
    source, output = tmp_path / 'changed', tmp_path / 'output'
    source.write_bytes(changed)
    with pytest.raises(crypto.CryptoError, match='invalid or the passphrase'):
        crypto.decrypt_file(source, output, PASSWORD)
    assert not output.exists() and source.read_bytes() == changed
    no_stage(tmp_path)


@pytest.mark.parametrize('change', ['swap', 'repeat', 'drop', 'trailing', 'truncated_tag', 'header_only', 'empty'])
def test_order_length_final_marker_and_exact_eof(tmp_path, encrypted, change):
    _, raw = encrypted
    start, block = crypto._HEADER.size + 16, crypto.CHUNK_SIZE + 16
    variants = {
        'swap': raw[:start] + raw[start + block:start + 2 * block] + raw[start:start + block] + raw[start + 2 * block:],
        'repeat': raw[:start + block] + raw[start:start + block] + raw[start + 2 * block:],
        'drop': raw[:start] + raw[start + block:],
        'trailing': raw + b'\x00', 'truncated_tag': raw[:-1],
        'header_only': raw[:start], 'empty': b'',
    }
    source, output = tmp_path / 'changed', tmp_path / 'output'
    source.write_bytes(variants[change])
    with pytest.raises(crypto.CryptoError, match='invalid or the passphrase'):
        crypto.decrypt_file(source, output, PASSWORD)
    assert not output.exists()
    no_stage(tmp_path)


def test_password_failure_is_sanitized_and_exact_unicode_is_not_normalized(tmp_path, capsys):
    source, sealed, output = (tmp_path / n for n in ('source', 'sealed', 'output'))
    source.write_bytes(b'synthetic secret content')
    password = 'Exact Caf\u00e9 passphrase 2026'
    crypto.encrypt_file(source, sealed, password)
    with pytest.raises(crypto.CryptoError) as wrong:
        crypto.decrypt_file(sealed, output, password.replace('\u00e9', 'e\u0301'))
    assert str(wrong.value) == crypto._AUTH_ERROR
    assert password not in str(wrong.value) and not output.exists()
    crypto.decrypt_file(sealed, output, password)
    assert output.read_bytes() == source.read_bytes()
    assert capsys.readouterr() == ('', '')
    no_stage(tmp_path)


@pytest.mark.parametrize('password', [None, b'not-a-string', '', 'short', '\ud800' * 12, '\U0001f600' * 300])
def test_invalid_passphrase_never_creates_output(tmp_path, password):
    source, output = tmp_path / 'source', tmp_path / 'output'
    source.write_bytes(b'payload')
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt_file(source, output, password)
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize('decrypt', [False, True])
@pytest.mark.parametrize('when', ['before', 'chunk', 'last'])
def test_cancel_removes_staging_and_preserves_source(tmp_path, encrypted, decrypt, when):
    plain, sealed = encrypted
    data = sealed if decrypt else plain
    source, output = tmp_path / 'source', tmp_path / 'output'
    source.write_bytes(data)
    stop = Event()
    if when == 'before':
        stop.set()
    def progress(done, total):
        if (when == 'chunk' and done) or (when == 'last' and done == total):
            stop.set()
    with pytest.raises(InterruptedError, match='cancelled'):
        (crypto.decrypt_file if decrypt else crypto.encrypt_file)(source, output, PASSWORD,
            cancelled=stop.is_set, on_progress=progress)
    assert not output.exists() and source.read_bytes() == data
    no_stage(tmp_path)


@pytest.mark.parametrize('decrypt', [False, True])
def test_callback_failure_cleans_staging(tmp_path, encrypted, decrypt):
    plain, sealed = encrypted
    source, output = tmp_path / 'source', tmp_path / 'output'
    source.write_bytes(sealed if decrypt else plain)
    def progress(done, total):
        if done:
            raise RuntimeError('synthetic callback failure')
    with pytest.raises(RuntimeError, match='synthetic callback'):
        (crypto.decrypt_file if decrypt else crypto.encrypt_file)(source, output, PASSWORD, on_progress=progress)
    assert not output.exists()
    no_stage(tmp_path)


@pytest.mark.parametrize('race', [False, True])
def test_existing_destination_never_replaced_even_during_atomic_publish(tmp_path, monkeypatch, race):
    source, output = tmp_path / 'source', tmp_path / 'output'
    source.write_bytes(b'original prepared archive')
    foreign = b'existing unrelated user file'
    if race:
        operation = 'rename' if os.name == 'nt' else 'link'
        original_publish = getattr(os, operation)
        def create_then_publish(*args, **kwargs):
            output.write_bytes(foreign)
            return original_publish(*args, **kwargs)
        monkeypatch.setattr(crypto.os, operation, create_then_publish)
    else:
        output.write_bytes(foreign)
    with pytest.raises(crypto.CryptoError, match='preserved|never replaced'):
        crypto.encrypt_file(source, output, PASSWORD)
    assert output.read_bytes() == foreign and source.read_bytes() == b'original prepared archive'
    no_stage(tmp_path)


@pytest.mark.parametrize('change', ['grow', 'replace'])
def test_source_change_during_streaming_is_rejected(tmp_path, encrypted, change):
    plain, _ = encrypted
    source, output = tmp_path / 'source', tmp_path / 'output'
    source.write_bytes(plain)
    changed = []
    def progress(done, total):
        if done and not changed:
            changed.append(True)
            if change == 'grow':
                with source.open('ab') as stream:
                    stream.write(b'changed')
            else:
                source.rename(tmp_path / 'old-source')
                source.write_bytes(plain)
    # Windows can reject the replacement itself because the original handle is
    # open; that is also a safe refusal, followed by removal of our staging.
    with pytest.raises((crypto.CryptoError, PermissionError)):
        crypto.encrypt_file(source, output, PASSWORD, on_progress=progress)
    assert changed and not output.exists()
    no_stage(tmp_path)


def test_failed_exclusive_staging_does_not_delete_an_unowned_entry(tmp_path, monkeypatch):
    source, output = tmp_path / 'source', tmp_path / 'output'
    source.write_bytes(b'prepared archive')
    original_open = os.open
    collided = []
    foreign = b'unrelated file created concurrently'
    def open_after_collision(path, flags, mode=0o777, **kwargs):
        if flags & os.O_EXCL and '.carr-crypto-' in str(path) and not collided:
            fd = original_open(path, flags, mode, **kwargs)
            os.write(fd, foreign)
            os.close(fd)
            collided.append(tmp_path / Path(path).name)
        return original_open(path, flags, mode, **kwargs)
    monkeypatch.setattr(crypto.os, 'open', open_after_collision)
    with pytest.raises(FileExistsError):
        crypto.encrypt_file(source, output, PASSWORD)
    assert len(collided) == 1 and collided[0].read_bytes() == foreign
    assert not output.exists()


@pytest.mark.parametrize('kind', ['source_link', 'parent_link', 'hardlink', 'directory', 'destination_link', 'reserved', 'traversal'])
def test_safe_local_paths_and_regular_files(tmp_path, monkeypatch, kind):
    real = tmp_path / 'real'
    real.mkdir()
    source, output = real / 'source', tmp_path / 'output'
    source.write_bytes(b'prepared archive')
    before = source.read_bytes()
    if kind == 'source_link':
        link = tmp_path / 'link'
        link.symlink_to(source)
        source = link
    elif kind == 'parent_link':
        link = tmp_path / 'linked-parent'
        link.symlink_to(real, target_is_directory=True)
        source = link / 'source'
    elif kind == 'hardlink':
        os.link(source, tmp_path / 'twin')
    elif kind == 'directory':
        source = real
    elif kind == 'destination_link':
        output.symlink_to(source)
    elif kind == 'reserved':
        output = tmp_path / 'NUL.txt'
    else:
        output = real / '..' / 'escaped'
    # Windows resolves NUL.txt as an existing device without a directory entry.
    output_existed = output.exists()
    entries_before = set(tmp_path.rglob('*'))
    def no_key_derivation(*args):
        pytest.fail('An invalid snapshot path must be rejected before key derivation.')
    monkeypatch.setattr(crypto, '_master', no_key_derivation)
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt_file(source, output, PASSWORD)
    assert (real / 'source').read_bytes() == before
    assert output.exists() == output_existed
    assert set(tmp_path.rglob('*')) == entries_before
    if kind not in ('destination_link', 'reserved'):
        assert not output.exists()
    no_stage(tmp_path)


def test_private_stage_permissions_and_cancelled_parent_move_cleanup(tmp_path, encrypted):
    if os.name != 'posix':
        return  # Descriptor-anchored cleanup is a POSIX path, not a Windows claim.
    plain, _ = encrypted
    source = tmp_path / 'source'
    source.write_bytes(plain)
    parent, moved = tmp_path / 'target', tmp_path / 'moved'
    parent.mkdir()
    stop = Event()
    def progress(done, total):
        if done and not stop.is_set():
            staged = tuple(parent.glob('.carr-crypto-*'))
            assert len(staged) == 1 and stat.S_IMODE(staged[0].stat().st_mode) == 0o600
            parent.rename(moved)
            stop.set()
    with pytest.raises(InterruptedError):
        crypto.encrypt_file(source, parent / 'output', PASSWORD,
                            cancelled=stop.is_set, on_progress=progress)
    assert not (moved / 'output').exists() and list(moved.iterdir()) == []


def test_missing_optional_dependency_has_actionable_failure_without_plaintext(tmp_path):
    source, output = tmp_path / 'source', tmp_path / 'output'
    source.write_bytes(b'prepared archive')
    program = '''
import importlib.abc, pathlib, sys
sys.path.insert(0, sys.argv[1])
class MissingCrypto(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'cryptography' or fullname.startswith('cryptography.'):
            raise ModuleNotFoundError('optional dependency absent')
sys.meta_path.insert(0, MissingCrypto())
from carr_library.sync.crypto import CryptoUnavailable, encrypt_file
try:
    encrypt_file(sys.argv[2], sys.argv[3], 'synthetic secret passphrase')
except CryptoUnavailable as exc:
    assert 'cryptography==50.0.2' in str(exc) and 'sync extra' in str(exc)
    assert 'synthetic secret' not in str(exc)
else:
    raise AssertionError('encryption silently fell back')
assert not pathlib.Path(sys.argv[3]).exists()
'''
    root = Path(__file__).resolve().parents[2] / 'src'
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(root), str(source), str(output)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not result.stdout and not result.stderr and not output.exists()
    no_stage(tmp_path)
