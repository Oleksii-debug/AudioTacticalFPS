"""Real host state transitions with explicitly synthetic private providers."""
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
import threading

import pytest

from carr_library.delivery_contract import (
    Capability, CONTRACT_VERSION, DecisionReason, DecisionState,
    EntitlementDecision, IntegrationContext, ProviderCapabilities, UpdateOffer,
    USER_DATA_ACTIONS,
)
from carr_library.delivery_service import (
    DeliveryError, DeliveryService, InstallApproval, LicenseSnapshot,
    LicenseState, PreparedUpdate, RefreshPolicy, UpdateBackup, UpdateResult,
)

CONTEXT = IntegrationContext('synthetic-product', 'build-1', 'test')
OTHER = IntegrationContext('synthetic-other', 'build-1', 'test')


class Provider:
    def __init__(self, now):
        self.now = now
        self.value = LicenseSnapshot(CONTEXT, LicenseState.VALID, 1, now[0], now[0] + 60 * 86400)
        self.calls = []
        self.error = None
        self.offered = UpdateOffer(CONTEXT, 'synthetic-offer', 'build-2', 7, 'a' * 64)
        self.ready = None

    def describe(self):
        return ProviderCapabilities(CONTRACT_VERSION, frozenset(Capability))

    def decide(self, request):
        return EntitlementDecision(request, DecisionState.DENIED, DecisionReason.PROVIDER_DENIED)

    def snapshot(self, context, *, cancelled):
        assert context == CONTEXT
        return self.value

    def refresh(self, context, *, cancelled):
        self.calls.append('refresh')
        if self.error:
            raise self.error
        self.value = replace(self.value, generation=self.value.generation + 1, last_refresh=self.now[0])
        return self.value

    def import_license(self, context, source, *, cancelled):
        self.calls.append(('import', source))
        if source.read_bytes() != b'explicit synthetic signed license':
            raise ValueError('private endpoint and secret detail must never appear')
        self.value = replace(self.value, generation=self.value.generation + 1)
        return self.value

    def export_license(self, context, *, cancelled):
        return b'explicit synthetic signed license'

    def check(self, context, *, cancelled):
        self.calls.append('check')
        return self.offered

    def stage(self, context, offer, *, cancelled, progress):
        self.calls.append('stage')
        self.ready = PreparedUpdate(offer, 'b' * 32)
        return self.ready

    def import_update(self, context, source, *, cancelled, progress):
        self.calls.append('import-update')
        assert source.read_bytes() == b'fixture'
        self.ready = PreparedUpdate(self.offered, 'b' * 32)
        return self.ready

    def install(self, context, preparation, *, backup, cancelled, progress):
        assert type(backup) is UpdateBackup
        self.calls.append('install')
        return UpdateResult(context, preparation.stage_id, preparation.offer.target_build_id, 'installed')

    def recover(self, context, *, cancelled):
        self.calls.append('recover')
        return self.ready


def setup(interval=7 * 86400, authorized=True):
    now = [1000000.0]
    provider = Provider(now)
    policy = RefreshPolicy(interval, 60, 3600, 0.2, authorized)
    service = DeliveryService(CONTEXT, entitlement=provider, updater=provider,
        refresh_policy=policy, clock=lambda: now[0], jitter=lambda: 0.5)
    return service, provider, now


def run_now(operation):
    future = Future()
    try:
        future.set_result(operation())
    except Exception as exc:
        future.set_exception(exc)
    return future


@pytest.mark.parametrize('days', [7, 14])
def test_background_cadence_is_independent_of_signed_validity(days):
    service, provider, now = setup(days * 86400)
    original_expiry = provider.value.valid_until
    assert not service.refresh_due()
    now[0] += days * 86400 - 1
    assert service.submit_refresh(run_now, idle=lambda: True) is None
    now[0] += 1
    result = service.submit_refresh(run_now, idle=lambda: True).result()
    assert result.generation == 2 and result.valid_until == original_expiry
    assert provider.calls == ['refresh']
    assert not service.refresh_due()


def test_outage_retains_verified_state_and_retries_with_bounded_backoff():
    service, provider, now = setup()
    old = provider.value
    provider.error = OSError('private transport endpoint')
    now[0] += 7 * 86400
    for delay in (60, 120, 240, 480, 960, 1920, 3600, 3600):
        future = service.submit_refresh(run_now, idle=lambda: True)
        with pytest.raises(DeliveryError, match='saved work'):
            future.result()
        assert provider.value is old
        assert service._next_refresh == now[0] + delay
        now[0] += delay
    provider.error = None
    assert service.submit_refresh(run_now, idle=lambda: True).result().generation == 2
    assert service._failures == 0


@pytest.mark.parametrize('state', [LicenseState.PERPETUAL, LicenseState.ABSENT, LicenseState.REVIEW])
def test_no_implicit_enrollment_or_perpetual_refresh(state):
    service, provider, now = setup()
    provider.value = replace(provider.value, state=state, valid_until=None)
    now[0] += 365 * 86400
    assert service.submit_refresh(run_now, idle=lambda: True).result() is None
    assert provider.calls == []


def test_network_authority_and_idle_are_explicit():
    service, provider, now = setup(authorized=False)
    now[0] += 365 * 86400
    assert service.submit_refresh(run_now, idle=lambda: True) is None
    with pytest.raises(DeliveryError, match='Approve'):
        service.refresh()
    with pytest.raises(DeliveryError, match='Approve'):
        service.check_update()
    service, provider, now = setup()
    now[0] += 7 * 86400
    assert service.submit_refresh(run_now, idle=lambda: False) is None
    assert provider.calls == []


def test_only_one_background_future_and_close_cancels_provider():
    service, provider, now = setup()
    now[0] += 7 * 86400
    queued, future = [], Future()
    def submit(operation):
        queued.append(operation)
        return future
    assert service.submit_refresh(submit, idle=lambda: True) is future
    assert service.submit_refresh(submit, idle=lambda: True) is None
    service.close()
    with pytest.raises(InterruptedError):
        queued[0]()
    future.set_exception(InterruptedError())
    assert provider.calls == []


def test_clock_rollback_defers_refresh_without_mutating_license():
    service, provider, now = setup()
    assert not service.refresh_due()
    old = provider.value
    now[0] -= 60
    assert not service.refresh_due()
    assert provider.value is old
    assert service._next_refresh == now[0] + 3600


@pytest.mark.parametrize('action', sorted(USER_DATA_ACTIONS))
def test_user_data_actions_survive_denied_private_policy(action):
    service, provider, _ = setup()
    assert service.require(action).reason is DecisionReason.DATA_ACCESS_PRESERVED
    with pytest.raises(DeliveryError, match='not authorized'):
        service.require('optional.premium')


def test_wrong_context_stale_generation_and_private_errors_are_rejected(tmp_path):
    service, provider, _ = setup()
    provider.value = replace(provider.value, context=OTHER)
    with pytest.raises(DeliveryError, match='installation'):
        service.snapshot()
    service, provider, _ = setup()
    provider.refresh = lambda *a, **k: replace(provider.value, generation=0)
    with pytest.raises(DeliveryError, match='stale'):
        service.refresh(network_approved=True)
    bad = tmp_path / 'bad'
    bad.write_bytes(b'not a signed license')
    with pytest.raises(DeliveryError) as error:
        service.import_license(bad)
    assert 'private endpoint' not in str(error.value)


def test_import_export_and_late_cancel_preserve_committed_result(tmp_path):
    service, provider, _ = setup()
    source = tmp_path / 'signed'
    source.write_bytes(service.export_license())
    event = threading.Event()
    old = provider.import_license
    def commit(*args, **kwargs):
        result = old(*args, **kwargs)
        event.set()
        return result
    provider.import_license = commit
    result = service.import_license(source, cancelled=event.is_set)
    assert result.generation == 2 and provider.value == result


def test_download_requires_exact_preview_and_explicit_network_approval():
    service, provider, _ = setup()
    offer = service.check_update(network_approved=True)
    with pytest.raises(DeliveryError, match='Choose'):
        service.stage_update(replace(offer), network_approved=True)
    with pytest.raises(DeliveryError, match='Approve'):
        service.stage_update(offer)
    prepared = service.stage_update(offer, network_approved=True)
    assert prepared.offer == offer and provider.calls == ['check', 'stage']


def test_install_requires_fresh_approval_idle_and_completed_backup(tmp_path):
    service, provider, _ = setup()
    offer = service.check_update(network_approved=True)
    ready = service.stage_update(offer, network_approved=True)
    approval = service.approve_install(ready)
    saved = UpdateBackup(tmp_path, 'c' * 64)
    backups = []
    def backup():
        backups.append('backup')
        assert 'install' not in provider.calls
        return saved
    with pytest.raises(DeliveryError, match='active jobs'):
        service.install_update(approval, idle=lambda: False, backup=backup)
    with pytest.raises(DeliveryError, match='fresh'):
        service.install_update(InstallApproval(ready, approval.nonce), idle=lambda: True, backup=backup)
    result = service.install_update(approval, idle=lambda: True, backup=backup)
    assert result.state == 'installed' and backups == ['backup']
    with pytest.raises(DeliveryError, match='fresh'):
        service.install_update(approval, idle=lambda: True, backup=backup)
    assert provider.calls.count('install') == 1


def test_failed_backup_and_scope_change_do_not_activate(tmp_path):
    service, provider, _ = setup()
    ready = service.stage_update(service.check_update(network_approved=True), network_approved=True)
    approval = service.approve_install(ready)
    with pytest.raises(DeliveryError, match='backup'):
        service.install_update(approval, idle=lambda: True, backup=lambda: None)
    assert 'install' not in provider.calls
    approval = service.approve_install(ready)
    idle = iter([True, False])
    with pytest.raises(InterruptedError, match='backup remains'):
        service.install_update(approval, idle=lambda: next(idle),
            backup=lambda: UpdateBackup(tmp_path, 'c' * 64))
    assert 'install' not in provider.calls


def test_recovery_revokes_previous_install_approval(tmp_path):
    service, provider, _ = setup()
    ready = service.stage_update(service.check_update(network_approved=True), network_approved=True)
    approval = service.approve_install(ready)
    recovered = service.recover_update()
    with pytest.raises(DeliveryError, match='fresh'):
        service.install_update(approval, idle=lambda: True,
            backup=lambda: UpdateBackup(tmp_path, 'c' * 64))
    assert recovered == ready
    assert 'install' not in provider.calls


def test_mismatched_preparation_and_receipt_fail_closed(tmp_path):
    service, provider, _ = setup()
    offer = service.check_update(network_approved=True)
    provider.stage = lambda *a, **k: PreparedUpdate(replace(offer, target_build_id='other'), 'b' * 32)
    with pytest.raises(DeliveryError, match='differs'):
        service.stage_update(offer, network_approved=True)
    service, provider, _ = setup()
    ready = service.stage_update(service.check_update(network_approved=True), network_approved=True)
    approval = service.approve_install(ready)
    provider.install = lambda *a, **k: UpdateResult(CONTEXT, 'd' * 32, 'build-2', 'installed')
    with pytest.raises(DeliveryError, match='result could not'):
        service.install_update(approval, idle=lambda: True,
            backup=lambda: UpdateBackup(tmp_path, 'c' * 64))


def test_observer_failure_does_not_reverse_successful_refresh():
    service, provider, now = setup()
    now[0] += 7 * 86400
    def broken(value):
        raise RuntimeError('detached UI')
    result = service.submit_refresh(run_now, idle=lambda: True, completed=broken).result()
    assert result == provider.value and result.generation == 2
    assert not service._background and not service.refresh_due()


def test_first_status_probe_uses_existing_worker_and_preserves_original_due_time():
    from concurrent.futures import ThreadPoolExecutor
    service, provider, now = setup()
    now[0] += 2 * 86400
    original = provider.snapshot
    entered, release = threading.Event(), threading.Event()
    caller = threading.get_ident()
    observed = []
    def slow(*args, **kwargs):
        observed.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    provider.snapshot = slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = service.submit_refresh(pool.submit, idle=lambda: True)
        try:
            assert entered.wait(5)
            assert not future.done() and observed == [observed[0]] and observed[0] != caller
        finally:
            release.set()
        assert future.result(5) is None
    assert provider.calls == []
    assert service._next_refresh == provider.value.last_refresh + 7 * 86400


def test_new_preparation_during_backup_revokes_previous_approval(tmp_path):
    service, provider, _ = setup()
    prepared = service.stage_update(service.check_update(network_approved=True), network_approved=True)
    approval = service.approve_install(prepared)
    def backup():
        service._accept_preparation(PreparedUpdate(prepared.offer, 'e' * 32))
        return UpdateBackup(tmp_path, 'c' * 64)
    with pytest.raises(DeliveryError, match='changed during backup'):
        service.install_update(approval, idle=lambda: True, backup=backup)
    assert 'install' not in provider.calls
