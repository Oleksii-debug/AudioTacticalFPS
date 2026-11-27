"""Shutdown acknowledgements survive the canonical progress-voice adapter."""

import gc
from pathlib import Path
import sys
import threading
import weakref

import pytest

from carr_library.audio import cleanup
from carr_library.speech import Voice, VoiceRegistry
from carr_library.speech.progress_speech import LocalProgressSpeech, _RegistryOwnership

from carr_library.speech.progress_speech import SnapshotProgressSpeech


class Provider:
    library_root = None
    label = "Explicitly selected local voice"

    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0
        self.quiescent = False

    def close(self):
        self.calls += 1
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        if result is True:
            self.quiescent = True
        return result


def test_pending_provider_close_remains_pending_until_its_actual_acknowledgement():
    provider = Provider([False, True])
    selected = SnapshotProgressSpeech(provider)

    assert selected.close() is False
    assert selected.provider is provider
    assert selected.quiescent is False
    assert provider.calls == 1

    # The existing owner can retry the same provider; the adapter does not
    # turn the first explicit pending result into an implicit success.
    assert selected.close() is True
    assert selected.quiescent is True
    assert provider.calls == 2


def test_provider_without_explicit_close_result_keeps_its_existing_contract():
    provider = Provider([None])
    assert SnapshotProgressSpeech(provider).close() is None
    assert provider.calls == 1


def test_provider_close_failure_keeps_the_same_owned_error_and_can_be_retried():
    error = OSError("The owned local voice worker has not stopped.")
    provider = Provider([error, True])
    selected = SnapshotProgressSpeech(provider)

    with pytest.raises(OSError) as raised:
        selected.close()
    assert raised.value is error
    assert selected.provider is provider
    assert selected.quiescent is False
    assert selected.close() is True
    assert provider.calls == 2


class OwnedRegistryProvider:
    """A real registry provider with a bounded, observable close handshake."""
    name = "Owned progress-voice fixture"
    executable = Path(sys.executable)

    def __init__(self):
        self.voice = Voice("fixture:owned-progress", "Explicit owned voice", "Fixture", "en",
                           capabilities=("volume",))
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def voices(self):
        return (self.voice,)

    def close(self):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("The synthetic registry close was not released.")
        return True


def _reject_cleanup_threads(monkeypatch):
    start = threading.Thread.start
    attempts = []

    def rejected(worker):
        if worker.name in ("audio-cleanup", "progress-voice-cleanup-retry"):
            attempts.append(worker.name)
            raise RuntimeError("Synthetic cleanup thread admission was rejected.")
        return start(worker)

    monkeypatch.setattr(threading.Thread, "start", rejected)
    return start, attempts


def test_actual_registry_is_retained_when_cleanup_thread_admission_fails_and_view_is_collected(monkeypatch):
    provider = OwnedRegistryProvider()
    registry = VoiceRegistry((provider,))
    ownership = _RegistryOwnership(registry)
    selected = LocalProgressSpeech(registry, provider.voice.id, _ownership=ownership)
    provider_ref, registry_ref = weakref.ref(provider), weakref.ref(registry)
    selected_ref = weakref.ref(selected)
    owner_ref = None
    retry = None
    original_start, attempts = _reject_cleanup_threads(monkeypatch)
    try:
        assert selected.close() is False
        assert selected.close() is False
        assert selected.close() is False
        assert ownership.references == 0
        assert not ownership.finished.is_set()
        assert provider.calls == 0 and len(attempts) >= 2
        owner_ref = weakref.ref(ownership._cleanup_owner)
        assert owner_ref() is not None and not owner_ref().quiescent

        del selected, ownership, registry, provider
        gc.collect()
        assert selected_ref() is None, "A destroyed native view need not be retained."
        assert registry_ref() is not None and provider_ref() is not None
        assert owner_ref() in cleanup._RETAINED
        assert provider_ref().calls == 0

        # The actual retained owner remains independently retryable after
        # thread admission recovers; no GUI/window object is needed.
        monkeypatch.setattr(threading.Thread, "start", original_start)
        retained_owner = owner_ref()
        retry = threading.Thread(target=retained_owner.close, name="test-owned-registry-retry", daemon=True)
        retry.start()
        assert provider_ref().entered.wait(3)
        assert not owner_ref().quiescent
        assert owner_ref() in cleanup._RETAINED
        provider_ref().release.set()
        retry.join(3)
        assert not retry.is_alive()
        assert retained_owner.quiescent
        assert retained_owner not in cleanup._RETAINED
        assert not registry_ref()._providers
    finally:
        monkeypatch.setattr(threading.Thread, "start", original_start)
        retained_provider = provider_ref()
        if retained_provider is not None:
            retained_provider.release.set()
        if retry is not None:
            retry.join(3)
        retained_owner = owner_ref() if owner_ref is not None else None
        if retained_owner is not None:
            retained_owner.close()


def test_copy_selection_releases_once_and_close_retries_the_same_registry_off_caller_thread(monkeypatch):
    provider = OwnedRegistryProvider()
    registry = VoiceRegistry((provider,))
    ownership = _RegistryOwnership(registry)
    first = LocalProgressSpeech(registry, provider.voice.id, _ownership=ownership)
    second = first.copy_selection()
    original_start, _attempts = _reject_cleanup_threads(monkeypatch)
    try:
        assert ownership.references == 2
        assert first.close() is True
        assert ownership.references == 1
        assert ownership._cleanup_owner is None
        assert registry.get(provider.voice.id) == provider.voice
        assert not second._closed.is_set() and provider.calls == 0

        assert second.close() is False
        assert second.close() is False
        assert ownership.references == 0 and provider.calls == 0
        owner = ownership._cleanup_owner
        assert owner is not None and not owner.quiescent

        monkeypatch.setattr(threading.Thread, "start", original_start)
        assert second.close() is False
        assert provider.entered.wait(3)
        assert second.close() is False
        assert ownership.references == 0
        assert ownership._cleanup_owner is owner and provider.calls == 1
        assert ownership._retry_thread.is_alive()
        assert not ownership.finished.is_set()

        provider.release.set()
        assert ownership.finished.wait(3)
        ownership._retry_thread.join(3)
        assert not ownership._retry_thread.is_alive()
        assert second.close() is True
        assert ownership.references == 0 and provider.calls == 1
        assert owner.quiescent and owner not in cleanup._RETAINED
    finally:
        monkeypatch.setattr(threading.Thread, "start", original_start)
        provider.release.set()
        if ownership._cleanup_owner is not None:
            ownership._cleanup_owner.close()
        if ownership._retry_thread is not None:
            ownership._retry_thread.join(3)
