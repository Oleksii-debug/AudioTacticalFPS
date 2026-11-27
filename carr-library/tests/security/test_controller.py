from hashlib import sha256
import json
import threading
import time

import pytest

from carr_library.security._files import TrustError
from carr_library.security.controller import TrustController
from carr_library.security.entitlements import EntitlementStore
from carr_library.security.refresh import RefreshWorker
from carr_library.security.__main__ import main
from carr_library.security.updates import LocalRepositoryClient
from .conftest import MemoryAnchor, REFERENCE, RenewalProvider, accept, envelope, grant, bundle


def test_private_status_has_no_import_or_update_configuration(capsys):
    controller = TrustController()
    assert controller.entitlement is controller.updates is controller.refresh is None
    assert 'Private perpetual' in controller.status_text()
    assert main(['status']) == 0
    assert 'periodic connection' in capsys.readouterr().out


def test_owner_terms_are_reviewed_as_actual_text_before_import(tmp_path, signer, policy, clock, integration_context):
    terms = 'Synthetic owner terms for a local test. No payment or production authority.'
    digest = sha256(terms.encode()).hexdigest()
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    path = tmp_path / 'renewal.json'
    path.write_bytes(envelope(signer, grant(clock, terms_sha256=digest)))
    controller = TrustController(entitlement=store, context=integration_context)
    with pytest.raises(TrustError, match='not available for review'):
        controller.preview_file(path)
    with pytest.raises(TrustError, match='pinned digest'):
        TrustController(entitlement=store, terms={digest: terms + 'changed'}, context=integration_context)
    controller = TrustController(entitlement=store, terms={digest: terms}, context=integration_context)
    review = controller.preview_file(path)
    assert terms in review.description
    assert 'Profile: renewable' in review.description
    with pytest.raises(TrustError, match='explicit review'):
        controller.import_review(review)
    controller.import_review(review, accepted=True)
    assert store.current().sequence == 1
    assert 'valid' in controller.status_text()
    # The dialog retains the exact reviewed bytes; changing the chosen file
    # after review cannot substitute a different grant at acceptance.
    path.write_bytes(b'changed after review')
    controller.import_review(review, accepted=True)
    assert store.current().sequence == 1


def test_cli_checks_and_downloads_an_actual_signed_local_repository(repository, tmp_path, capsys):
    repository.publish(bundle())
    bootstrap = tmp_path / 'independent-root.json'
    bootstrap.write_bytes(repository.bootstrap)
    args = ['--repository', str(repository.root), '--bootstrap', str(bootstrap),
        '--bootstrap-sha256', sha256(repository.bootstrap).hexdigest(),
        '--product', 'carr-test', '--channel', 'stable', '--current-build', '1',
        '--cache', str(tmp_path / 'cache'), '--target', repository.target_path]
    assert main(['check-update', *args]) == 0
    review = json.loads(capsys.readouterr().out)
    assert review['build'] == 2
    assert main(['download-update', *args, '--reviewed-sha256', review['sha256']]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['activated'] is False
    assert result['release']['sha256'] == review['sha256']


def test_cli_verifies_without_installing_or_claiming_authority(tmp_path, signer, clock, capsys):
    path = tmp_path / 'permission.json'
    path.write_bytes(envelope(signer, grant(clock)))
    args = ['verify-permission', str(path), '--public-key-hex', signer.public_key().public_bytes_raw().hex(),
        '--product', 'carr-test', '--subject', 'synthetic-subject', '--device', 'synthetic-device',
        '--policy-version', 'policy-1', '--feature', 'render']
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['verified'] and not result['imported']
    path.write_bytes(b'bad')
    assert main(args) == 2


def test_desktop_installs_help_hook():
    import ast
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py').read_text()
    tree = ast.parse(source)
    frame = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DesktopFrame')
    menus = next(node for node in frame.body if isinstance(node, ast.FunctionDef) and node.name == '_menus')
    calls = [node.func.id for node in ast.walk(menus) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert calls.count('install_trust_actions') == 1
    initialize = next(node for node in frame.body if isinstance(node, ast.FunctionDef) and node.name == '__init__')
    menu_call = next(node for node in ast.walk(initialize) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == '_menus')
    keyboard_call = next(node for node in ast.walk(initialize) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Name) and node.func.id == 'install_productivity_actions')
    assert menu_call.lineno < keyboard_call.lineno


def test_cancelled_explicit_import_does_not_advance_protected_state(
        tmp_path, signer, policy, clock, integration_context):
    terms = 'Reviewed synthetic terms.'
    digest = sha256(terms.encode()).hexdigest()
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    path = tmp_path / 'permission.json'
    path.write_bytes(envelope(signer, grant(clock, terms_sha256=digest)))
    controller = TrustController(entitlement=store, terms={digest: terms}, context=integration_context)
    review = controller.preview_file(path)
    with pytest.raises(InterruptedError, match='cancelled'):
        controller.import_review(review, accepted=True, cancelled=lambda: True)
    assert store.anchor.load().sequence == 0
    assert not store.current_path.exists()
    assert not store.pending_path.exists()


def test_schedule_observer_failure_keeps_successful_signed_import(
        tmp_path, signer, policy, clock, integration_context, monkeypatch):
    terms = 'Reviewed synthetic terms.'
    digest = sha256(terms.encode()).hexdigest()
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    path = tmp_path / 'permission.json'
    raw = envelope(signer, grant(clock, terms_sha256=digest))
    path.write_bytes(raw)
    worker = RefreshWorker(store, RenewalProvider(), REFERENCE, profile_id='trust-test', clock=clock)
    controller = TrustController(entitlement=store, refresh=worker,
                                 terms={digest: terms}, context=integration_context)
    def unavailable():
        raise OSError('synthetic PRIVATE credential diagnostic')
    monkeypatch.setattr(worker, 'acknowledge_review', unavailable)
    try:
        result = controller.import_review(controller.preview_file(path), accepted=True)
        assert result.sequence == store.current().sequence == 1
        assert store.current_path.read_bytes() == raw
        assert 'schedule needs attention' in controller.status_text()
        assert 'PRIVATE' not in controller.status_text()
    finally:
        worker.close()


@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_manual_job_rejects_late_transport_reply_without_advancing_grant(
        tmp_path, signer, policy, clock, integration_context, stop):
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    original = envelope(signer, grant(clock))
    accept(store, original)

    class Provider(RenewalProvider):
        calls = 0
        def renew(self, request, stopped):
            self.calls += 1
            entered.set()
            assert release.wait(2)
            return envelope(signer, grant(clock, sequence=2, request_nonce=request.nonce))

    provider = Provider()
    worker = RefreshWorker(store, provider, REFERENCE, profile_id='trust-test', clock=clock,
                           timeout_seconds=.06 if stop == 'deadline' else 20)
    controller = TrustController(entitlement=store, refresh=worker, context=integration_context)
    outcome = []
    def job():
        try:
            outcome.append(controller.check_renewal(cancelled=cancelled.is_set))
        except Exception as error:
            outcome.append(error)
    thread = threading.Thread(target=job)
    try:
        thread.start()
        assert entered.wait(1)
        if stop == 'cancel':
            cancelled.set()
        thread.join(.5)
        assert not thread.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], (TrustError, InterruptedError))
        assert worker.running
    finally:
        release.set()
        thread.join(2)
        if worker._thread is not None:
            worker._thread.join(2)
        worker.close()
    assert provider.calls == 1
    assert store.anchor.load().sequence == 1
    assert store.current_path.read_bytes() == original


def test_manual_budget_expired_in_local_vault_read_never_starts_transport(
        tmp_path, signer, policy, clock, integration_context, monkeypatch):
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    accept(store, envelope(signer, grant(clock)))
    reads = store.current
    def slow_read():
        time.sleep(.07)
        return reads()
    monkeypatch.setattr(store, 'current', slow_read)
    class Provider(RenewalProvider):
        calls = 0
        def renew(self, request, cancelled):
            self.calls += 1
            return envelope(signer, grant(clock, sequence=2, request_nonce=request.nonce))
    provider = Provider()
    worker = RefreshWorker(store, provider, REFERENCE, profile_id='trust-test', clock=clock, timeout_seconds=.05)
    controller = TrustController(entitlement=store, refresh=worker, context=integration_context)
    try:
        with pytest.raises(TrustError, match='time budget'):
            controller.check_renewal()
        assert not worker.running and provider.calls == 0
        assert store.anchor.load().sequence == 1
    finally:
        worker.close()
