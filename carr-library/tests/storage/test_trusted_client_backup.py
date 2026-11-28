"""Known trusted-client files are recovery bytes, never restored authority."""
import base64
from hashlib import sha256
import json
import os
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.credentials import WindowsCredentialVault
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.security import entitlements
from carr_library.security._files import TrustError
from carr_library.security.anchors import Anchor, WindowsVaultAnchor
from carr_library.security.entitlements import EntitlementStore, PublicKey, TrustPolicy


NAMES = ('entitlement.json', 'entitlement.pending.json')
PREFIX = 'state/trusted-client/'
KEY = PublicKey(bytes(range(32)))
POLICY = TrustPolicy('SyntheticProduct', 'synthetic-owner', None, (KEY,),
                     frozenset({'synthetic-policy'}), frozenset({'synthetic-managed'}))


class MemoryAnchor:
    def __init__(self, raw):
        self.value = Anchor(1, sha256(raw).hexdigest(), 100)
        self.changes = []

    def load(self):
        return self.value

    def compare_and_set(self, *args, **kwargs):
        self.changes.append((args, kwargs))
        pytest.fail('Unverified recovery bytes cannot advance a protected anchor.')


def payload(kind):
    if kind == 'empty':
        return b''
    if kind == 'damaged':
        return b'synthetic-owner-state-not-a-grant'
    if kind == 'maximum':
        return b'x' * entitlements.MAX_ENVELOPE
    return encode({'version': 1, 'kind': 'carr-entitlement', 'key_id': KEY.key_id,
                   'payload': base64.b64encode(b'{}').decode('ascii'),
                   'signature': base64.b64encode(b'\x00' * 64).decode('ascii')})


def no_authority(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Backup must not verify, activate, decrypt or advance trusted-client state.')
    monkeypatch.setattr(entitlements, 'verify_envelope', forbidden)
    monkeypatch.setattr(EntitlementStore, '__init__', forbidden)
    monkeypatch.setattr(WindowsVaultAnchor, 'load', forbidden)
    monkeypatch.setattr(WindowsVaultAnchor, 'compare_and_set', forbidden)
    monkeypatch.setattr(WindowsCredentialVault, 'get', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('kind', ('empty', 'damaged', 'forged', 'maximum'))
def test_exact_known_unverified_bytes_roundtrip_without_acquiring_a_grant(
        tmp_path, monkeypatch, name, kind):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Trusted-client recovery data')
    source = app.root / (PREFIX + name)
    source.parent.mkdir(parents=True)
    raw = payload(kind)
    source.write_bytes(raw)
    with monkeypatch.context() as passive:
        no_authority(passive)
        backup = tmp_path / 'backup'
        checksum = create_backup(app, backup)
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        assert manifest['version'] == 4
        entry, = [row for row in manifest['sidecars'] if row['filename'] == PREFIX + name]
        assert entry == dict(filename=PREFIX + name, size_bytes=len(raw),
                             sha256=sha256(raw).hexdigest())
        restored = restore_backup(backup, tmp_path / 'restored', checksum)
    assert (restored / PREFIX / name).read_bytes() == source.read_bytes() == raw
    assert (backup / PREFIX / name).read_bytes() == raw
    anchor = MemoryAnchor(raw)
    store = EntitlementStore(restored / PREFIX, POLICY, anchor, clock=lambda: 100)
    with pytest.raises(TrustError):
        store.current()
    status = store.status()
    assert status.mode == 'attention'
    assert status.permits('backup') and not status.permits('synthetic-managed')
    assert anchor.changes == []
    assert (restored / PREFIX / name).read_bytes() == raw


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('damage', ('oversize', 'hardlink', 'unknown-sibling'))
def test_trusted_recovery_keeps_exact_size_link_and_namespace_guards(tmp_path, name, damage):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Bounded recovery')
    source = app.root / (PREFIX + name)
    source.parent.mkdir(parents=True)
    raw = payload('damaged')
    if damage == 'oversize':
        raw = b'x' * (entitlements.MAX_ENVELOPE + 1)
    source.write_bytes(raw)
    if damage == 'hardlink':
        os.link(source, tmp_path / 'foreign-alias')
        assert source.stat().st_nlink == 2
    elif damage == 'unknown-sibling':
        (source.parent / (name + '.bak')).write_bytes(raw)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, target)
    assert not target.exists() and source.read_bytes() == raw
    if damage == 'hardlink':
        assert (tmp_path / 'foreign-alias').read_bytes() == raw


def test_unverified_recovery_bytes_remain_bound_to_the_checked_archive_digest(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Recovery hash')
    source = app.root / (PREFIX + NAMES[0])
    source.parent.mkdir(parents=True)
    source.write_bytes(b'first malformed owner state')
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    original = (backup / PREFIX / NAMES[0]).read_bytes()
    (backup / PREFIX / NAMES[0]).write_bytes(b'other malformed owner state')
    with pytest.raises(ValueError):
        restore_backup(backup, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()
    assert source.read_bytes() == original
