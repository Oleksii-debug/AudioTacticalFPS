"""Current retained cleanup ownership stays live through synchronous callers."""
from threading import Event

import pytest

from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner, wait_for_cleanup


def test_settling_requires_the_current_typed_pending_owner():
    with pytest.raises(TypeError, match='owned pending-cleanup'):
        wait_for_cleanup(RuntimeError('not an owned cleanup receipt'))


def test_scopes_wait_for_actual_owner_quiescence_and_callbacks_once(monkeypatch):
    attempts, callbacks, waits = [], [], []
    def attempt():
        attempts.append(len(attempts))
        return len(attempts) == 2
    owner = CleanupOwner(attempt)
    owner.add_quiescent_callback(lambda: callbacks.append(owner.quiescent))
    monkeypatch.setattr(owner.quiescent_event, 'wait', lambda seconds: waits.append(seconds))
    wait_for_cleanup(AudioCleanupPending('retained', cleanup_owner=owner))
    assert attempts == [0, 1] and callbacks == [True] and waits == [.25]
    assert owner.quiescent and owner.close() is True
    assert callbacks == [True]


@pytest.mark.parametrize('interruption', [KeyboardInterrupt(), SystemExit(17)],
                         ids=['keyboard-interrupt', 'system-exit'])
def test_interrupt_is_delivered_only_after_retained_owner_settles(interruption):
    attempts, callbacks = [], []
    def attempt():
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise interruption
        return True
    owner = CleanupOwner(attempt)
    owner.add_quiescent_callback(lambda: callbacks.append(owner.quiescent))
    with pytest.raises(type(interruption)) as caught:
        wait_for_cleanup(AudioCleanupPending('retained', cleanup_owner=owner))
    assert caught.value is interruption
    assert owner.quiescent and attempts == [0, 1] and callbacks == [True]
