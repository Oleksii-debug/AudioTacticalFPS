"""Synthetic signed grants keep their exact protected commit after cleanup."""
from hashlib import sha256
from pathlib import Path

import pytest

from carr_library.security import entitlements
from carr_library.security.anchors import Anchor
from carr_library.security.controller import TrustController
from carr_library.security.entitlements import EntitlementStore, TrustError
from .conftest import MemoryAnchor, accept, envelope, grant


@pytest.mark.parametrize('operation', ['accept', 'recover'])
@pytest.mark.parametrize('boundary', ['current_write', 'pending_unlink', 'final_sync'])
@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
def test_exact_signed_commit_remains_readable_with_cleanup_notice(tmp_path, signer, policy, clock,
                                                               monkeypatch, integration_context,
                                                               operation, boundary, error_type):
    anchor = MemoryAnchor()
    store = EntitlementStore(tmp_path / 'client', policy, anchor, clock=clock)
    raw = envelope(signer, grant(clock))
    expected = Anchor(1, sha256(raw).hexdigest(), clock.now)
    if operation == 'recover':
        store.pending_path.write_bytes(raw)
        anchor.value = expected
    atomic, unlink, sync = entitlements.atomic_write, Path.unlink, entitlements.sync_directory
    triggered = []

    def write_then_fail(path, value):
        atomic(path, value)
        if Path(path) == store.current_path:
            triggered.append(True)
            raise error_type('Synthetic late current-file publication failure')

    def unlink_then_fail(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == store.pending_path:
            triggered.append(True)
            raise error_type('Synthetic pending cleanup failure')
        return result

    def sync_then_fail(path):
        sync(path)
        triggered.append(True)
        raise error_type('Synthetic final directory durability failure')

    if boundary == 'current_write':
        monkeypatch.setattr(entitlements, 'atomic_write', write_then_fail)
    elif boundary == 'pending_unlink':
        monkeypatch.setattr(Path, 'unlink', unlink_then_fail)
    else:
        monkeypatch.setattr(entitlements, 'sync_directory', sync_then_fail)
    try:
        result = accept(store, raw) if operation == 'accept' else store.current()
    except KeyboardInterrupt:
        pytest.fail('A committed signed grant was hidden by a late interruption.')
    assert triggered and result.sequence == 1
    assert anchor.load() == expected and store.current_path.read_bytes() == raw
    assert store.completion_warning and 'signed permission' in store.completion_warning
    controller = TrustController(entitlement=store, context=integration_context)
    assert store.completion_warning in controller.status_text()
    monkeypatch.undo()
    assert EntitlementStore(store.root, policy, anchor, clock=clock).current() == result


@pytest.mark.parametrize('damage', ['current', 'anchor'])
def test_late_failure_never_adopts_changed_current_or_protected_anchor(tmp_path, signer, policy,
                                                                    clock, monkeypatch, damage):
    anchor = MemoryAnchor()
    store = EntitlementStore(tmp_path / 'client', policy, anchor, clock=clock)
    raw = envelope(signer, grant(clock))
    original = Path.unlink
    def interfere(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == store.pending_path:
            if damage == 'current':
                store.current_path.write_bytes(b'Unrelated replacement')
            else:
                anchor.value = Anchor(2, 'a' * 64, clock.now)
            raise OSError('Synthetic competing state after commit')
        return result
    monkeypatch.setattr(Path, 'unlink', interfere)
    with pytest.raises((OSError, TrustError)):
        accept(store, raw)
    assert not getattr(store, 'completion_warning', None)
