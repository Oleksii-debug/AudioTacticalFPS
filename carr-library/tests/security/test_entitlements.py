from dataclasses import replace
from hashlib import sha256
import json
import os
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from carr_library.security._files import TrustError
from carr_library.security.anchors import Anchor, AnchorUnavailable, WindowsVaultAnchor
from carr_library.security.entitlements import (EntitlementStore, PublicKey, ReplayError,
    ReviewRequired, private_status, verify_envelope)
from .conftest import MemoryAnchor, accept, envelope, grant


def make_store(tmp_path, policy, clock, anchor=None):
    return EntitlementStore(tmp_path / 'client', policy, anchor or MemoryAnchor(), clock=clock)


def test_private_default_and_protected_operations_need_no_key_or_connection():
    status = private_status()
    assert status.mode == 'private-perpetual'
    for action in ('render', 'read', 'export', 'backup', 'recovery', 'settings', 'prior-output'):
        assert status.permits(action)


def test_signed_import_requires_exact_review_and_terms_then_survives_restart(tmp_path, signer, policy, clock):
    anchor = MemoryAnchor()
    store = make_store(tmp_path, policy, clock, anchor)
    raw = envelope(signer, grant(clock))
    with pytest.raises(ReviewRequired):
        store.accept(raw)
    with pytest.raises(ReviewRequired):
        store.accept(raw, reviewed_digest=sha256(raw).hexdigest())
    accept(store, raw)
    assert store.current().sequence == 1
    assert store.begin('render').sequence == 1
    assert make_store(tmp_path, policy, clock, anchor).status().mode == 'valid'
    assert store.current_path.read_bytes() == raw


@pytest.mark.parametrize('change', [
    {'product': 'other'}, {'device': 'other'}, {'subject': 'other'},
    {'policy_version': 'unreviewed'}, {'schema': 2}, {'unknown': True},
    {'expires_at': 1}, {'sequence': True}, {'features': ['render', 'render']},
    {'features': ['unknown']}, {'revoked': 1}, {'terms_sha256': 'bad'},
    {'profile': 'perpetual'}, {'refresh_interval': 1}, {'offline_until': 1},
])
def test_bad_signed_response_never_replaces_last_verified_grant(tmp_path, signer, policy, clock, change):
    store = make_store(tmp_path, policy, clock)
    raw = envelope(signer, grant(clock))
    accept(store, raw)
    updates = dict(sequence=2, request_nonce='b' * 32)
    updates.update(change)
    bad = envelope(signer, grant(clock, **updates))
    with pytest.raises(TrustError):
        store.accept(bad, expected_nonce='b' * 32)
    assert store.current_path.read_bytes() == raw
    assert store.status().mode == 'valid'


@pytest.mark.parametrize('kind', ['signature', 'unknown-key', 'duplicate-json', 'oversize', 'bad-base64'])
def test_structural_and_crypto_rejection(tmp_path, signer, policy, clock, kind):
    raw = envelope(signer, grant(clock))
    if kind == 'signature':
        item = json.loads(raw)
        item['signature'] = 'A' * 86 + '=='
        raw = json.dumps(item).encode()
    elif kind == 'unknown-key':
        raw = envelope(Ed25519PrivateKey.generate(), grant(clock))
    elif kind == 'duplicate-json':
        raw = envelope(signer, b'{"schema":1,"schema":1}')
    elif kind == 'oversize':
        raw = b' ' * 65537
    else:
        item = json.loads(raw)
        item['payload'] = '%%%='
        raw = json.dumps(item).encode()
    with pytest.raises(TrustError):
        verify_envelope(raw, policy, now=clock.now)


def test_replay_nonce_and_terms_change_require_review(tmp_path, signer, policy, clock):
    store = make_store(tmp_path, policy, clock)
    first = envelope(signer, grant(clock))
    accept(store, first)
    newer = envelope(signer, grant(clock, sequence=2, request_nonce='b' * 32))
    store.accept(newer, expected_nonce='b' * 32)
    with pytest.raises(ReplayError):
        store.accept(first, reviewed_digest=sha256(first).hexdigest())
    with pytest.raises(ReplayError):
        store.accept(newer, expected_nonce='b' * 32)
    with pytest.raises(ReplayError):
        store.accept(envelope(signer, grant(clock, sequence=3, request_nonce='c' * 32)), expected_nonce='b' * 32)
    changed = envelope(signer, grant(clock, sequence=3, request_nonce='b' * 32, terms_sha256='c' * 64))
    with pytest.raises(ReviewRequired):
        store.accept(changed, expected_nonce='b' * 32)
    assert store.current_path.read_bytes() == newer
    store.accept(changed, reviewed_digest=sha256(changed).hexdigest(), accepted_terms='c' * 64)
    assert store.current().terms_sha256 == 'c' * 64


def test_key_rotation_uses_independently_pinned_overlap_and_rejects_backslide(tmp_path, signer, policy, clock):
    store = make_store(tmp_path, policy, clock)
    accept(store, envelope(signer, grant(clock)))
    second = Ed25519PrivateKey.generate()
    rotated = replace(policy, keys=policy.keys + (PublicKey(second.public_key().public_bytes_raw(), 2),))
    store.policy = rotated
    new = envelope(second, grant(clock, sequence=2, request_nonce='d' * 32))
    store.accept(new, expected_nonce='d' * 32)
    with pytest.raises(ReplayError):
        store.accept(envelope(signer, grant(clock, sequence=3, request_nonce='e' * 32)), expected_nonce='e' * 32)
    store.policy = replace(rotated, minimum_key_generation=2)
    assert store.current().key_generation == 2
    with pytest.raises(TrustError, match='retired'):
        verify_envelope(envelope(signer, grant(clock, sequence=4)), store.policy, now=clock.now)


def test_outage_expiry_clock_rollback_and_revocation_preserve_data_and_started_lease(tmp_path, signer, policy, clock):
    store = make_store(tmp_path, policy, clock)
    accept(store, envelope(signer, grant(clock)))
    started = store.begin('render')
    clock.now += 31 * 86400
    assert store.status().mode == 'offline-grace'
    assert store.begin('render').feature == 'render'
    clock.now += 30 * 86400
    assert store.status().mode == 'expired'
    assert started.sequence == 1
    with pytest.raises(ReviewRequired):
        store.begin('render')
    for action in ('read', 'export', 'backup', 'recovery', 'prior-output'):
        store.begin(action)
    clock.now -= 86400
    assert store.status().mode == 'attention'
    store.begin('read')
    clock.now += 86400
    revoke = envelope(signer, grant(clock, sequence=2, revoked=True, request_nonce='f' * 32))
    store.accept(revoke, expected_nonce='f' * 32)
    assert store.status().mode == 'revoked'
    assert started.feature == 'render'


@pytest.mark.parametrize('point', ['before-anchor', 'after-anchor'])
def test_crash_safe_last_valid_publication(tmp_path, signer, policy, clock, monkeypatch, point):
    from carr_library.security import entitlements
    anchor = MemoryAnchor()
    store = make_store(tmp_path, policy, clock, anchor)
    original = envelope(signer, grant(clock))
    accept(store, original)
    replacement = envelope(signer, grant(clock, sequence=2, request_nonce='a' * 32))
    if point == 'before-anchor':
        anchor.fail = True
    else:
        actual = entitlements.atomic_write
        def fail_current(path, raw):
            if path == store.current_path:
                raise OSError('simulated crash before current publication')
            return actual(path, raw)
        monkeypatch.setattr(entitlements, 'atomic_write', fail_current)
    with pytest.raises((TrustError, OSError)):
        store.accept(replacement, expected_nonce='a' * 32)
    assert store.current_path.read_bytes() == original
    anchor.fail = False
    monkeypatch.undo()
    restarted = make_store(tmp_path, policy, clock, anchor)
    assert restarted.current().sequence == (1 if point == 'before-anchor' else 2)
    assert restarted.current_path.read_bytes() == (original if point == 'before-anchor' else replacement)


def test_replacing_saved_state_with_old_signed_token_is_detected(tmp_path, signer, policy, clock):
    anchor = MemoryAnchor()
    store = make_store(tmp_path, policy, clock, anchor)
    old = envelope(signer, grant(clock))
    accept(store, old)
    store.accept(envelope(signer, grant(clock, sequence=2, request_nonce='a' * 32)), expected_nonce='a' * 32)
    store.current_path.write_bytes(old)
    assert store.status().mode == 'attention'
    assert store.status().permits('backup')
    assert not store.status().permits('render')


def test_private_state_rejects_links(tmp_path, signer, policy, clock):
    store = make_store(tmp_path, policy, clock)
    accept(store, envelope(signer, grant(clock)))
    os.link(store.current_path, tmp_path / 'linked-copy')
    assert store.status().mode == 'attention'


def test_vault_errors_are_fixed_and_never_leak_credentials():
    class BadVault:
        def get(self, *args):
            raise RuntimeError('TOP-SECRET-TOKEN endpoint=https://secret.invalid')
    anchor = WindowsVaultAnchor('url-api-' + '0' * 32, '1' * 64,
                               product='carr-test', profile_id='trust-test', vault=BadVault())
    with pytest.raises(AnchorUnavailable) as error:
        anchor.load()
    assert 'TOP-SECRET' not in str(error.value)
    assert 'secret.invalid' not in str(error.value)


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows Credential Manager check.')
def test_real_windows_vault_anchor_roundtrip():
    from carr_library.providers import WindowsCredentialVault
    vault = WindowsCredentialVault()
    reference = 'url-api-' + uuid.uuid4().hex
    binding = sha256(b'synthetic-trust-test').hexdigest()
    anchor = WindowsVaultAnchor(reference, binding, product='carr-test', profile_id='trust-test', vault=vault)
    try:
        vault.put(reference, anchor.vault_binding, anchor.encode(Anchor()))
        anchor.compare_and_set(Anchor(), Anchor(1, 'a' * 64, 1))
        assert anchor.load() == Anchor(1, 'a' * 64, 1)
    finally:
        vault.delete(reference, anchor.vault_binding)
