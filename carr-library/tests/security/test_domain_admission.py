"""Actual signed synthetic grants and the current domain/CLI/tool boundaries.

Keys, protocol workers and transport responses are deliberately synthetic.
These tests do not activate a publisher, payment service or production policy.
"""
from hashlib import sha256
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.delivery_contract import USER_DATA_ACTIONS
from carr_library.security._files import TrustError
from carr_library.security.admission import (
    FRESH_OPERATION_ACTIONS, admit_new_operation, admitted_operation, operation_context,
)
from carr_library.security.controller import TrustController
from carr_library.security.entitlements import EntitlementStore, ReviewRequired
from .conftest import MemoryAnchor, accept, envelope, grant


@pytest.fixture
def authority(tmp_path, signer, policy, clock, integration_context):
    store = EntitlementStore(tmp_path / 'permission-client', policy, MemoryAnchor(), clock=clock)
    accept(store, envelope(signer, grant(clock, features=['render', 'compose'])))
    controller = TrustController(entitlement=store, context=integration_context,
        premium_features={'audio.render': 'render', 'transcription.run': 'compose',
                          'assistant.plan': 'compose'})
    return SimpleNamespace(controller=controller, store=store, clock=clock,
                           signer=signer, policy=policy, context=integration_context)


def expire(authority):
    authority.clock.now += 61 * 86400
    assert not authority.store.status().permits('render')
    assert not authority.store.status().permits('compose')


def fixture_module(monkeypatch, relative):
    # These already-required fixture modules are also copied for the installed
    # gate. Reuse their exact current protocol instead of maintaining a second
    # synthetic worker or a weaker receipt format here.
    path = Path(__file__).resolve().parents[1] / relative
    name = '_carr_domain_admission_' + sha256(relative.encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('action', sorted(FRESH_OPERATION_ACTIONS))
def test_default_private_operation_never_contacts_a_provider(action, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('The default private installation consulted a provider.')
    controller = TrustController()
    monkeypatch.setattr(controller.entitlement_boundary, 'decide', forbidden)
    assert admit_new_operation(None, action).sequence == 0
    assert admit_new_operation(controller, action).sequence == 0
    assert 'WAITING_INPUT' in controller.status_text()
    assert controller.entitlement is controller.refresh is controller.update_poll is None


@pytest.mark.parametrize('action', sorted(USER_DATA_ACTIONS))
def test_protected_actions_remain_available_even_with_an_unavailable_authority(
        authority, action, monkeypatch):
    expire(authority)
    monkeypatch.setattr(authority.controller, 'begin_operation',
        lambda *_: pytest.fail('Protected user data consulted the premium gate.'))
    assert admit_new_operation(authority.controller, action).sequence == 0


@pytest.mark.parametrize('action', sorted(FRESH_OPERATION_ACTIONS))
def test_one_signed_store_admits_only_new_work_then_refuses_expiry(authority, action):
    lease = admit_new_operation(authority.controller, action)
    assert lease.sequence == 1
    expire(authority)
    with pytest.raises(ReviewRequired, match='Existing work'):
        admit_new_operation(authority.controller, action)


@pytest.mark.parametrize('mapping', [
    {'data.save': 'render'}, {'job.finish_active': 'render'},
    {'export': 'render'}, {'unknown.operation': 'render'},
    {'audio.render': 'unknown'}, {'audio.render': 'data.read'},
    {'audio.render': []},
], ids=['save', 'active-completion', 'export-alias', 'unwired-action',
        'untrusted-feature', 'protected-feature', 'non-string-feature'])
def test_owner_mapping_cannot_claim_unwired_or_protected_enforcement(authority, mapping):
    with pytest.raises(TrustError):
        TrustController(entitlement=authority.store, context=authority.context,
                        premium_features=mapping)


def test_configured_mapping_needs_the_actual_signed_store():
    with pytest.raises(TrustError, match='signed store'):
        TrustController(premium_features={'audio.render': 'render'})


def test_captured_operation_remains_authorized_after_expiry_but_next_work_does_not(authority):
    with admitted_operation(authority.controller, 'audio.render') as active:
        expire(authority)
        assert admit_new_operation(None, 'audio.render') is active
        with admitted_operation(authority.controller, 'audio.render') as nested:
            assert nested is active
        with pytest.raises(ReviewRequired):
            admit_new_operation(None, 'transcription.run')
    with pytest.raises(ReviewRequired):
        admit_new_operation(authority.controller, 'audio.render')
    # The private context was restored, not replaced with a global authority.
    assert admit_new_operation(None, 'audio.render').sequence == 0


def test_nested_tool_cannot_replace_the_selected_authority(authority):
    other = TrustController()
    with operation_context(authority.controller):
        with pytest.raises(ValueError, match='authority'):
            with operation_context(other):
                pytest.fail('A nested tool replaced its owner.')
    with operation_context(other):
        assert admit_new_operation(None, 'audio.render').sequence == 0


def test_revocation_refuses_next_operation_without_changing_the_retained_lease(authority):
    with admitted_operation(authority.controller, 'audio.render') as active:
        raw = envelope(authority.signer, grant(authority.clock, sequence=2,
            features=['render', 'compose'], revoked=True))
        accept(authority.store, raw)
        assert admit_new_operation(None, 'audio.render') is active
        assert active.sequence == 1
    with pytest.raises(ReviewRequired):
        admit_new_operation(authority.controller, 'audio.render')


def test_real_library_data_and_backup_remain_available_with_expired_permission(
        authority, tmp_path):
    expire(authority)
    app = LibraryApp(tmp_path / 'library', trust_controller=authority.controller)
    workspace = app.create_workspace('Owned data')
    app.add_text(workspace, 'Original source', 'An exact owned quotation remains available.')
    topic = app.create_topic(workspace, 'Notes')
    hit = app.search(workspace, 'owned quotation')[0]
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Human note.')
    assert 'owned quotation' in app.export_topic(workspace, topic)
    backup = tmp_path / 'data-backup'
    checksum = app.backup(backup)
    assert len(checksum) == 64 and (backup / 'manifest.json').is_file()
    assert app.trust_controller is authority.controller
    reopened = LibraryApp(app.root, trust_controller=authority.controller)
    assert 'Human note.' in reopened.export_topic(workspace, topic)


@pytest.mark.parametrize('command', ['transcribe', 'assistant'])
def test_primary_cli_forwards_the_exact_existing_controller(authority, monkeypatch, command):
    from carr_library import __main__ as main
    module_name = ('carr_library.transcription_cli' if command == 'transcribe'
                   else 'carr_library.assistant.cli')
    module = __import__(module_name, fromlist=['main'])
    seen = []
    def invoked(argv, *, trust_controller):
        seen.append((argv, trust_controller))
        return 27
    monkeypatch.setattr(module, 'main', invoked)
    assert main.main([command, '--help'], trust_controller=authority.controller) == 27
    assert seen == [(['--help'], authority.controller)]


def test_local_proposal_uses_same_permission_before_actual_gateway_call(
        authority, tmp_path, monkeypatch):
    fixture = fixture_module(monkeypatch, 'assistant/test_local_llm.py')
    value = fixture.setup.__wrapped__(tmp_path)
    value.app.trust_controller = authority.controller
    expire(authority)
    with pytest.raises(ReviewRequired):
        fixture.propose_local_plan(value.service, value.gateway, value.profile,
            'List my libraries', value.scope, approval=fixture.approve(value),
            operation_id='1' * 32)
    assert value.calls == [] and value.service.jobs() == ()
    # A local data operation through the same assistant still executes.
    identity = value.service.submit(value.service.preview('List libraries', value.scope))
    assert value.service.run(identity).status == 'complete'


def test_legacy_fresh_run_refused_before_worker_or_saved_job_mutation(
        authority, tmp_path, monkeypatch):
    fixture = fixture_module(monkeypatch, 'transcription/test_supplied_worker_bridge.py')
    app, _, service, source, _ = fixture.prepared.__wrapped__(tmp_path)
    app.trust_controller = authority.controller
    identity = service.submit([source], title='Synthetic signed admission')
    before = service.get(identity)
    expire(authority)
    monkeypatch.setattr('carr_library.transcription.run_process',
        lambda *a, **k: pytest.fail('A refused fresh run launched the protocol child.'))
    with pytest.raises(ReviewRequired):
        service.run(identity, timeout=30)
    assert service.get(identity) == before
    assert not (service._path(identity) / 'job_config.json').exists()


def test_actual_legacy_completed_protocol_replay_and_export_stay_available(
        authority, tmp_path, monkeypatch):
    fixture = fixture_module(monkeypatch, 'transcription/test_supplied_worker_bridge.py')
    app, _, service, source, _ = fixture.prepared.__wrapped__(tmp_path)
    app.trust_controller = authority.controller
    identity = service.submit([source], title='Synthetic protocol')
    complete = service.run(identity, timeout=30)
    assert complete['status'] == 'completed', complete.get('last_error')
    expire(authority)
    monkeypatch.setattr(authority.controller, 'begin_operation',
        lambda *_: pytest.fail('Verified completed transcription requested fresh permission.'))
    assert service.run(identity, timeout=30) == complete
    destination = service.export(identity, tmp_path / 'transcription-export')
    assert (destination / 'transcription-manifest.json').is_file()


def test_v43_fresh_run_refused_before_execution_or_original_control_mutation(
        authority, tmp_path, monkeypatch):
    fixture = fixture_module(monkeypatch, 'test_transcription_service.py')
    service, manifest, source, settings = fixture.prepared_environment.__wrapped__(tmp_path)()
    service.trust_controller = authority.controller
    identity = service.prepare([source], manifest, settings)
    before = {p.relative_to(service.root).as_posix(): p.read_bytes()
              for p in service.root.rglob('*') if p.is_file()}
    expire(authority)
    monkeypatch.setattr(fixture.service, '_start_process',
        lambda *a, **k: pytest.fail('A refused V43 job launched a child.'))
    with pytest.raises(ReviewRequired):
        service.run(identity, manifest, timeout=30)
    after = {p.relative_to(service.root).as_posix(): p.read_bytes()
             for p in service.root.rglob('*') if p.is_file()}
    assert after == before


def test_actual_v43_completed_receipt_and_reading_copy_stay_available(
        authority, tmp_path, monkeypatch):
    fixture = fixture_module(monkeypatch, 'test_transcription_service.py')
    service, manifest, source, settings = fixture.prepared_environment.__wrapped__(tmp_path)()
    service.trust_controller = authority.controller
    fixture.real_protocol_child.__wrapped__(monkeypatch)
    identity = service.prepare([source], manifest, settings)
    verified = service.run(identity, manifest, timeout=30)
    assert verified['output_count'] == 4
    expire(authority)
    monkeypatch.setattr(authority.controller, 'begin_operation',
        lambda *_: pytest.fail('A retained receipt requested new execution permission.'))
    completed = service.completed(identity)
    assert completed['completion_sha256'] == verified['completion_sha256']
    output = next(row['path'] for row in completed['outputs'] if row['format'] == 'txt')
    reading = service.reading_copy(identity, output)
    assert reading.current_text.startswith('Synthetic protocol text')
