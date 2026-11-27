"""Admission and ownership of the shared canonical production voice registry."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from carr_library import runtime_settings
from carr_library.studio.render import ProductionService


class Registry:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def test_discovery_is_shared_and_uses_state_root(tmp_path, monkeypatch):
    found = Registry()
    calls = []
    service = ProductionService(tmp_path / 'state' / 'studio')

    def discover(root, *, cancelled):
        assert cancelled() is False
        calls.append(root)
        return found

    monkeypatch.setattr(runtime_settings, 'voice_registry', discover)
    assert service.discover_voices() is found
    assert service.voices is found
    assert calls == [tmp_path / 'state']
    service.close()
    service.close()
    assert found.closed == 1
    with pytest.raises(InterruptedError):
        service.discover_voices()


def test_borrowed_registry_is_never_closed(tmp_path):
    found = Registry()
    service = ProductionService(tmp_path, voices=found)
    assert service.discover_voices() is found
    service.close()
    assert found.closed == 0


def test_close_during_discovery_discards_and_closes_result(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    found = Registry()
    service = ProductionService(tmp_path)

    def discover(root, *, cancelled):
        entered.set()
        assert release.wait(5)
        assert cancelled()
        return found

    monkeypatch.setattr(runtime_settings, 'voice_registry', discover)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(service.discover_voices)
        try:
            assert entered.wait(5)
            service.close()
        finally:
            release.set()
        with pytest.raises(InterruptedError):
            work.result(timeout=5)
    assert service._voices is None
    assert found.closed == 1


def test_waiting_discovery_cancels_without_starting_another_provider(tmp_path, monkeypatch):
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    found = Registry()
    calls = []
    service = ProductionService(tmp_path)

    def discover(root, *, cancelled):
        calls.append(root)
        entered.set()
        assert release.wait(5)
        return found

    monkeypatch.setattr(runtime_settings, 'voice_registry', discover)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.discover_voices)
        try:
            assert entered.wait(5)
            second = pool.submit(service.discover_voices, cancelled=stop.is_set)
            stop.set()
            with pytest.raises(InterruptedError):
                second.result(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=5) is found
    assert len(calls) == 1
    service.close()


def test_stateless_canonical_registry_needs_no_optional_close(tmp_path, monkeypatch):
    from carr_library.speech import VoiceRegistry
    found = VoiceRegistry(())
    monkeypatch.setattr(runtime_settings, 'voice_registry', lambda root, **kwargs: found)
    service = ProductionService(tmp_path)
    assert service.discover_voices() is found
    service.close()
    assert service._voices is None
