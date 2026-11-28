"""Configured update actions keep publisher authority separate from UI intent."""
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.security.controller import TrustController, configured_update_controller
from carr_library.security._files import TrustError, json_bytes

from .test_update_handoff import configuration
from .test_update_runtime import recipe, _portable_bootstrap


def _controller(configuration):
    return configured_update_controller(configuration.path, configuration.sha256,
        SimpleNamespace(root=configuration.library))


def _online(configuration):
    raw = json.loads(configuration.raw)
    raw['repository']['endpoint'] = 'https://synthetic-update.invalid/releases/'
    encoded = json_bytes(raw)
    configuration.path.write_bytes(encoded)
    return configured_update_controller(configuration.path, sha256(encoded).hexdigest(),
        SimpleNamespace(root=configuration.library))


def test_unconfigured_status_never_creates_update_authority_or_contacts_a_site(monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'create_connection',
        lambda *a, **k: pytest.fail('Default status must remain entirely local.'))
    controller = TrustController()
    assert controller.updates is None and controller.handoff is None
    assert controller.update_endpoint is None and not controller.background_update_due()
    assert 'No signed update source' in controller.status_text()


def test_explicit_current_library_obtains_only_actual_tuf_package_capability(configuration, repository):
    repository.publish(b'Exact synthetic signed target; never execute.')
    controller = _controller(configuration)
    release = controller.check_repository(repository.root)
    package = controller.download_review(repository.root, release)
    assert package.release == release and package.path.read_bytes() == b'Exact synthetic signed target; never execute.'
    assert controller.handoff is not None and controller.provisioning is None
    assert controller.update_library.root == configuration.library
    assert not controller.background_update_checks


def test_wrong_library_is_refused_before_creating_update_cache(configuration, tmp_path):
    other = tmp_path / 'other-library'
    other.mkdir()
    with pytest.raises(TrustError, match='another selected library'):
        configured_update_controller(configuration.path, configuration.sha256, SimpleNamespace(root=other))
    assert not (configuration.work / 'download-cache').exists()


@pytest.mark.parametrize('scope', ['library', 'runtime', 'work'])
def test_stale_same_byte_directory_scope_refuses_before_client_or_handoff(configuration, monkeypatch, scope):
    controller = _controller(configuration)
    target = getattr(configuration, scope)
    target.rename(target.with_name('retained-' + scope))
    target.mkdir()
    monkeypatch.setattr(controller.updates, 'check', lambda *a, **k: pytest.fail('Stale scope cannot check.'))
    monkeypatch.setattr(controller.handoff, 'prepare', lambda *a, **k: pytest.fail('Stale scope cannot Apply.'))
    with pytest.raises(TrustError):
        controller.check_repository('explicit-selection')
    with pytest.raises(TrustError):
        controller.prepare_apply('explicit-selection', object())


@pytest.mark.parametrize('enabled', [1, 'yes', None])
def test_background_update_setting_requires_typed_explicit_consent(configuration, enabled):
    controller = _online(configuration)
    with pytest.raises(TrustError, match='explicit consent'):
        controller.set_background_update_checks(enabled, accepted=True)
    assert not controller.background_update_checks
    assert not (configuration.work / 'update-check-preferences.json').exists()


def test_background_consent_is_bound_to_exact_owner_configuration_and_manual_by_default(configuration, monkeypatch):
    controller = _online(configuration)
    monkeypatch.setattr(controller.updates.online, 'check',
        lambda *a, **k: pytest.fail('Saving consent cannot contact a site.'))
    assert not controller.background_update_due(now=10**12)
    with pytest.raises(TrustError, match='explicit consent'):
        controller.set_background_update_checks(True)
    assert controller.set_background_update_checks(True, accepted=True)
    reopened = configured_update_controller(configuration.path, controller.handoff.configuration.sha256,
                                            controller.update_library)
    assert reopened.background_update_checks
    assert not reopened.background_update_due(now=0)
    reopened._next_update_check = 0
    assert reopened.reserve_background_update_check()
    assert not reopened.reserve_background_update_check()
    reopened.set_background_update_checks(False)
    assert not configured_update_controller(configuration.path,
        controller.handoff.configuration.sha256, controller.update_library).background_update_checks


def test_consent_for_another_configuration_never_enables_idle_network_checks(configuration):
    controller = _online(configuration)
    controller.set_background_update_checks(True, accepted=True)
    path = configuration.work / 'update-check-preferences.json'
    value = json.loads(path.read_bytes())
    value['configuration_sha256'] = '0' * 64
    path.write_bytes(json_bytes(value))
    reopened = configured_update_controller(configuration.path,
        controller.handoff.configuration.sha256, controller.update_library)
    assert not reopened.background_update_checks and 'Checks remain manual' in reopened.status_text()


def test_offline_only_configuration_cannot_enable_background_network(configuration):
    controller = _controller(configuration)
    with pytest.raises(TrustError, match='configured update site'):
        controller.set_background_update_checks(True, accepted=True)
    assert controller.update_endpoint is None and not controller.background_update_due(now=10**12)


def test_explicit_preparation_changes_only_this_controller_to_verified_apply_service(recipe, monkeypatch):
    calls = _portable_bootstrap(monkeypatch, recipe)
    controller = configured_update_controller(recipe.path, recipe.sha256, recipe.fixture_library)
    policy, target = controller.updates.policy, controller.target
    assert controller.provisioning is not None and controller.handoff is None
    result = controller.prepare_update_host()
    assert len(calls) == 2 and result['status'] == 'PREPARED'
    assert controller.provisioning is None and controller.handoff is not None
    assert controller.updates.policy == policy and controller.target == target
    assert controller.handoff.configuration.sha256 == result['configuration_sha256']
    assert controller.update_library is recipe.fixture_library
    assert (recipe.root / 'app/version.txt').read_bytes() == b'3.1.0'


def test_update_check_outage_preserves_unmanaged_reading_permission_and_no_apply(configuration, monkeypatch):
    controller = _controller(configuration)
    monkeypatch.setattr(controller.updates, 'check',
        lambda *a, **k: (_ for _ in ()).throw(OSError('Synthetic unavailable repository')))
    with pytest.raises(OSError):
        controller.check_repository('explicit repository')
    assert controller.entitlement is None and controller.refresh is None
    assert 'Updates and renewal never remove access' in controller.status_text()
    assert not (configuration.work / 'managed-activation.json').exists()
