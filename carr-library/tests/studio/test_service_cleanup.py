"""Owned warm subprocesses close after serial work; borrowed registries survive."""
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
from threading import Event, get_ident

import pytest

from carr_library.speech import Voice, VoiceRegistry
from carr_library.studio.render import ProductionService
from carr_library.ui.worker_cleanup import close_after_worker


class WarmProvider:
    name = 'Synthetic warm local provider'

    def __init__(self):
        self.process = None
        self.closed_on = None

    def voices(self):
        if self.process is None:
            self.process = subprocess.Popen([sys.executable, '-I', '-u', '-c',
                'import sys; print("ready", flush=True); sys.stdin.buffer.read()'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            assert self.process.stdout.readline() == b'ready\n'
        return (Voice('fixture:warm', 'Synthetic warm voice', 'fixture', 'en'),)

    def close(self):
        self.closed_on = get_ident()
        if self.process is not None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            finally:
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait(timeout=5)
                self.process.stdout.close()


def test_late_owned_registry_finishes_then_closes_real_warm_process_off_ui_thread(tmp_path, monkeypatch):
    started, release = Event(), Event()
    provider = WarmProvider()
    main = get_ident()
    def discover(root):
        started.set()
        assert release.wait(5), 'UI shutdown waited for unfinished discovery.'
        return VoiceRegistry((provider,))
    monkeypatch.setattr('carr_library.runtime_settings.voice_registry', discover)
    service = ProductionService(tmp_path / 'studio')
    executor = ThreadPoolExecutor(max_workers=1)
    work = executor.submit(lambda: service.voices.voices())
    try:
        assert started.wait(5)
        closing = close_after_worker(executor, service)
        assert not closing.done() and provider.process is None
        release.set()
        assert work.result(timeout=5)[0].id == 'fixture:warm'
        closing.result(timeout=5)
        assert provider.process.poll() == 0
        assert provider.closed_on != main
        with pytest.raises(RuntimeError, match='closed'):
            service.voices
        service.close()
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
        provider.close()


def test_borrowed_real_warm_registry_is_not_closed_by_production_service(tmp_path):
    provider = WarmProvider()
    registry = VoiceRegistry((provider,))
    try:
        service = ProductionService(tmp_path / 'studio', voices=registry)
        service.close()
        service.close()
        assert provider.process.poll() is None and provider.closed_on is None
        assert registry.voices()[0].id == 'fixture:warm'
    finally:
        registry.close()
    assert provider.process.poll() == 0


def test_closing_unused_service_never_discovers_a_voice_or_starts_a_process(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Closing an unused service must not discover voices.')
    monkeypatch.setattr('carr_library.runtime_settings.voice_registry', forbidden)
    service = ProductionService(tmp_path / 'studio')
    service.close()
    service.close()
    with pytest.raises(RuntimeError, match='closed'):
        service.create('Do not write a project after closing.')
    assert not service.projects.list()
