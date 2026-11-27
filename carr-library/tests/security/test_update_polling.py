"""Actual signed local metadata; polling never downloads or activates packages."""
from dataclasses import FrozenInstanceError, replace
import importlib.util
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.security._files import TrustError
from carr_library.security.controller import TrustController
from carr_library.security.polling import IdleUpdatePollHost
from carr_library.security.updates import LocalRepositoryClient

from .conftest import bundle


class MonotonicClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, 'The background metadata check did not settle.'
        threading.Event().wait(.005)


def configured(repository, tmp_path, integration_context):
    repository.publish(bundle())
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    controller = TrustController(context=integration_context, updates=client, target=repository.target_path)
    return client, controller


def test_default_off_needs_explicit_client_repository_and_target(
        repository, tmp_path, integration_context):
    client, controller = configured(repository, tmp_path, integration_context)
    host = IdleUpdatePollHost(controller, repository.root)
    assert not host.start() and host.status.phase == 'disabled' and host.status.release is None
    with pytest.raises(AttributeError):
        host.repository = tmp_path / 'unreviewed repository'
    assert list(client.downloads.iterdir()) == [] and list(client.metadata.iterdir()) == []
    with pytest.raises(TrustError):
        TrustController(automatic_updates=True)
    with pytest.raises(TrustError):
        TrustController(update_repository=repository.root)
    with pytest.raises(TrustError):
        IdleUpdatePollHost(controller, repository.root, interval=0)
    with pytest.raises(TrustError):
        IdleUpdatePollHost(controller, repository.root, timeout=float('nan'))
    with pytest.raises(TrustError):
        IdleUpdatePollHost(controller, '//server/repository', enabled=True)
    host.close()


def test_recurring_signed_checks_use_same_client_without_payload_download(
        repository, tmp_path, integration_context, monkeypatch):
    client, controller = configured(repository, tmp_path, integration_context)
    clock, calls = MonotonicClock(), []
    original = client.check
    def checked(*args, **kwargs):
        calls.append(threading.get_ident())
        return original(*args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError('Automatic metadata polling cannot download a package.')
    monkeypatch.setattr(client, 'check', checked)
    monkeypatch.setattr(client, 'download', forbidden)
    host = IdleUpdatePollHost(controller, repository.root, enabled=True, interval=10, clock=clock)
    try:
        assert host.start() and not host.start()
        assert not calls
        host.set_idle(True)
        wait_for(lambda: host.status.phase == 'available')
        first = host.status
        assert first.release.build == 2 and calls == [host._thread.ident]
        assert calls[0] != threading.get_ident() and not list(client.downloads.iterdir())
        with pytest.raises(FrozenInstanceError):
            first.phase = 'forged'
        with pytest.raises(FrozenInstanceError):
            first.release.build = 99
        repository.advance()
        repository.publish(bundle('C'), build=3)
        clock.now += 10
        host.set_idle(True)
        wait_for(lambda: host.status.release.build == 3)
        assert len(calls) == 2 and first.release.build == 2 and not list(client.downloads.iterdir())
    finally:
        host.close()
    assert not host._thread.is_alive() and host.status.phase == 'stopped'


def test_invalid_metadata_retains_prior_verified_release_and_bounded_backoff(
        repository, tmp_path, integration_context, monkeypatch):
    client, controller = configured(repository, tmp_path, integration_context)
    clock, calls = MonotonicClock(), []
    original = client.check
    def checked(*args, **kwargs):
        calls.append(clock())
        return original(*args, **kwargs)
    monkeypatch.setattr(client, 'check', checked)
    host = IdleUpdatePollHost(controller, repository.root, enabled=True, interval=10, clock=clock)
    try:
        host.set_idle(True); host.start()
        wait_for(lambda: host.status.phase == 'available')
        verified = host.status.release
        (repository.root / 'metadata' / 'timestamp.json').write_text('untrusted SECRET owner-path')
        clock.now += 10; host.set_idle(True)
        wait_for(lambda: host.status.failures == 1)
        assert host.status.phase == 'attention' and host.status.release is verified
        assert 'SECRET' not in host.message and str(repository.root) not in host.message
        clock.now += 9; host.set_idle(True)
        threading.Event().wait(.03)
        assert len(calls) == 2
        clock.now += 1; host.set_idle(True)
        wait_for(lambda: host.status.failures == 2)
        assert host._next == clock.now + 20 and host.status.release is verified
        assert not list(client.downloads.iterdir())
    finally:
        host.close()


@pytest.mark.parametrize('stop', ['close', 'busy'])
def test_blocked_real_check_does_not_block_save_or_spawn_and_drops_late_result(
        repository, tmp_path, integration_context, monkeypatch, stop):
    client, controller = configured(repository, tmp_path, integration_context)
    entered, release = threading.Event(), threading.Event()
    calls, original = [], client.check
    def blocked(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return result
    monkeypatch.setattr(client, 'check', blocked)
    host = IdleUpdatePollHost(controller, repository.root, enabled=True)
    try:
        host.set_idle(True); host.start()
        assert entered.wait(2)
        worker = host._thread
        assert not host.start()
        app = LibraryApp(tmp_path / 'independent library')
        workspace = app.create_workspace('Save during update metadata check')
        saved = app.add_text(workspace, 'Unchanged owner document', 'Reading and saving remain independent.')
        assert saved['edition'] and app.search(workspace, 'independent')
        started = time.monotonic()
        if stop == 'close':
            host.close(.01)
        else:
            host.set_idle(False)
        assert time.monotonic() - started < .25
        release.set()
        if stop == 'close':
            worker.join(2)
            assert not worker.is_alive() and host.status.phase == 'stopped'
        else:
            wait_for(lambda: host.status.phase == 'waiting')
        assert host.status.release is None and len(calls) == 1 and not list(client.downloads.iterdir())
    finally:
        release.set()
        host.close()


@pytest.mark.parametrize('change', ['deadline', 'target', 'context', 'policy'])
def test_late_or_rebound_verified_result_cannot_replace_cached_state(
        repository, tmp_path, integration_context, monkeypatch, change):
    from carr_library.delivery_contract import IntegrationContext
    client, controller = configured(repository, tmp_path, integration_context)
    clock, original = MonotonicClock(), client.check
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == 'deadline':
            clock.now += 2
        elif change == 'target':
            controller.target = 'another-release.zip'
        elif change == 'context':
            controller.context = IntegrationContext('another-product', 'build-A', 'stable')
        else:
            client.policy = replace(client.policy, current_build=2)
        return result
    monkeypatch.setattr(client, 'check', changed)
    host = IdleUpdatePollHost(controller, repository.root, enabled=True, timeout=1, clock=clock)
    try:
        host.set_idle(True); host.start()
        wait_for(lambda: host.status.phase == 'attention')
        assert host.status.release is None and not list(client.downloads.iterdir())
    finally:
        host.close()


def test_native_owner_timer_admits_configured_polling_without_a_renewal_provider(
        repository, tmp_path, integration_context, monkeypatch):
    client, _ = configured(repository, tmp_path, integration_context)
    controller = TrustController(context=integration_context, updates=client, target=repository.target_path,
        update_repository=repository.root, automatic_updates=True)
    timers = []
    class Timer:
        def __init__(self, frame):
            self.running = False
            timers.append(self)
        def Start(self, value):
            self.running = True
        def Stop(self):
            self.running = False
    wx = SimpleNamespace(Dialog=object, Timer=Timer, ID_ANY=0,
        EVT_MENU='menu', EVT_TIMER='timer', EVT_WINDOW_DESTROY='destroy')
    monkeypatch.setitem(sys.modules, 'wx', wx)
    import carr_library
    module_path = Path(carr_library.__file__).resolve().parent / 'ui' / 'trust_status.py'
    spec = importlib.util.spec_from_file_location('carr_library.ui._polling_owner_fixture', module_path)
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)
    menu = SimpleNamespace(Append=lambda *args: object())
    bar = SimpleNamespace(GetMenuCount=lambda: 1, GetMenuLabelText=lambda index: 'Help',
                          GetMenu=lambda index: menu)
    bindings = {}
    frame = SimpleNamespace(trust_controller=controller, working=True, _closing=False,
        GetId=lambda: 443,
        GetMenuBar=lambda: bar, Bind=lambda event, callback, *args: bindings.__setitem__(event, callback))
    try:
        ui.install_trust_actions(frame)
        assert frame.trust_update_poll_host is controller.update_poll and timers[0].running
        assert controller.update_poll.status.phase == 'waiting' and not list(client.metadata.iterdir())
        frame.working = False
        bindings['timer']()
        wait_for(lambda: controller.update_poll.status.phase == 'available')
        assert 'Automatic checks do not download' in controller.status_text()
        skipped = []
        bindings['destroy'](SimpleNamespace(GetId=lambda: 444, GetEventObject=lambda: frame,
                                           Skip=lambda: skipped.append(True)))
        assert timers[0].running and controller.update_poll.status.phase == 'available'
        bindings['destroy'](SimpleNamespace(GetId=lambda: 443, GetEventObject=lambda: object(),
                                           Skip=lambda: skipped.append(True)))
        assert skipped and not timers[0].running and controller.update_poll.status.phase == 'stopped'
        assert not controller.update_poll._thread.is_alive() and not list(client.downloads.iterdir())
        def late_idle(*args):
            pytest.fail('Queued timer reached a stopped signed-client host.')
        monkeypatch.setattr(controller.update_poll, 'set_idle', late_idle)
        bindings['timer']()
    finally:
        controller.update_poll.close()
