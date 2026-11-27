from dataclasses import replace
from hashlib import sha256
import json

import pytest

from carr_library.providers import WindowsCredentialVault
from carr_library.delivery_contract import (
    USER_DATA_ACTIONS, DecisionReason, DecisionState, EntitlementBoundary,
    EntitlementRequest, Readiness, provider_status,
)
from carr_library.security._files import TrustError
from carr_library.security.anchors import Anchor, AnchorUnavailable, WindowsVaultAnchor
from carr_library.security.boundary import ClientEntitlementProvider, ClientUpdateProvider
from carr_library.security.controller import TrustController
from carr_library.security.entitlements import EntitlementStore
from carr_library.security.updates import LocalRepositoryClient, VerifiedPackage
from .conftest import MemoryAnchor, REFERENCE, accept, bundle, envelope, grant


class CanonicalVault:
    """Synthetic implementation of the current opaque-reference/binding API."""
    def __init__(self):
        self.values, self.calls, self.bindings = {}, [], {}

    def get(self, reference, binding):
        WindowsCredentialVault._target(reference, binding)
        self.calls.append(('get', reference, binding))
        if self.bindings.get(reference) != binding:
            raise KeyError('No credential at this exact bound vault target.')
        return self.values[reference]

    def put(self, reference, binding, value):
        WindowsCredentialVault._target(reference, binding)
        assert '\n' not in value and len(value.encode()) <= 2400
        self.calls.append(('put', reference, binding))
        self.values[reference], self.bindings[reference] = value, binding


def anchor_fixture():
    vault = CanonicalVault()
    anchor = WindowsVaultAnchor(REFERENCE, '1' * 64, product='carr-test', profile_id='trust-test', vault=vault)
    vault.put(REFERENCE, anchor.vault_binding, anchor.encode(Anchor()))
    return vault, anchor


def test_anchor_uses_canonical_profile_reference_and_preserves_monotonic_state():
    vault, anchor = anchor_fixture()
    advanced = Anchor(3, 'a' * 64, 100)
    anchor.compare_and_set(Anchor(), advanced)
    assert anchor.load() == advanced
    with pytest.raises(AnchorUnavailable, match='changed'):
        anchor.compare_and_set(Anchor(), Anchor(2, 'b' * 64, 100))
    with pytest.raises(TrustError, match='backwards'):
        anchor.compare_and_set(advanced, Anchor(2, 'b' * 64, 100))
    assert anchor.load() == advanced
    assert all(call[1:] == (REFERENCE, anchor.vault_binding) for call in vault.calls)


@pytest.mark.parametrize(('field', 'value'), [
    ('product', 'other-product'), ('profile_id', 'other-profile'), ('binding', '2' * 64),
    ('kind', 'provider-key'), ('version', True), ('anchor', {'sequence': 1, 'digest': None, 'max_utc': 1}),
])
def test_anchor_rejects_rebound_or_malformed_protected_value_without_rewriting(field, value):
    vault, anchor = anchor_fixture()
    document = json.loads(vault.values[REFERENCE])
    document[field] = value
    raw = vault.values[REFERENCE] = json.dumps(document)
    with pytest.raises(AnchorUnavailable):
        anchor.load()
    assert vault.values[REFERENCE] == raw


def test_wrong_profile_reference_and_missing_anchor_never_provision_or_read_another_identity():
    vault, valid = anchor_fixture()
    original = dict(vault.values)
    calls = len(vault.calls)
    with pytest.raises(TrustError, match='another profile'):
        WindowsVaultAnchor('carr/provider/other/' + '0' * 32, '1' * 64,
                           product='carr-test', profile_id='trust-test', vault=vault)
    assert len(vault.calls) == calls
    # The current vault's reference is opaque. Its independently bound target,
    # not a path prefix, prevents a different profile from reading this value.
    other = WindowsVaultAnchor(REFERENCE, '1' * 64, product='carr-test',
                               profile_id='other', vault=vault)
    assert other.vault_binding != valid.vault_binding
    with pytest.raises(AnchorUnavailable):
        other.compare_and_set(Anchor(), Anchor(1, 'a' * 64, 1))
    assert vault.values == original and vault.calls[-1] == ('get', REFERENCE, other.vault_binding)
    missing = WindowsVaultAnchor('url-api-' + '1' * 32, '1' * 64,
                                 product='carr-test', profile_id='trust-test', vault=vault)
    with pytest.raises(AnchorUnavailable):
        missing.load()
    assert vault.values == original


@pytest.mark.parametrize('action', sorted(USER_DATA_ACTIONS))
def test_expired_signed_grant_preserves_every_canonical_user_data_action(
        tmp_path, signer, policy, clock, integration_context, action):
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    accept(store, envelope(signer, grant(clock)))
    clock.now += 61 * 86400
    provider = ClientEntitlementProvider(integration_context, store)
    boundary = EntitlementBoundary(provider)
    request = EntitlementRequest(integration_context, 'request-1', action)
    assert store.status().mode == 'expired'
    assert store.status().permits(action)
    decision = boundary.decide(request)
    assert decision.request is request
    assert decision.state == DecisionState.ALLOWED
    assert decision.reason == DecisionReason.DATA_ACCESS_PRESERVED
    managed = boundary.decide(EntitlementRequest(integration_context, 'request-2', 'render'))
    assert managed.state == DecisionState.DENIED


@pytest.mark.parametrize('field', ['product_id', 'build_id', 'channel'])
def test_entitlement_provider_rejects_other_installation_context(
        tmp_path, signer, policy, clock, integration_context, field):
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    accept(store, envelope(signer, grant(clock)))
    boundary = EntitlementBoundary(ClientEntitlementProvider(integration_context, store))
    request = EntitlementRequest(replace(integration_context, **{field: 'other'}), 'request-1', 'render')
    decision = boundary.decide(request)
    assert decision.request is request
    assert decision.state == DecisionState.UNAVAILABLE
    assert decision.reason == DecisionReason.PROVIDER_UNAVAILABLE
    valid = boundary.decide(EntitlementRequest(integration_context, 'request-2', 'render'))
    assert valid.state == DecisionState.ALLOWED


def test_controller_requires_explicit_context_and_never_treats_declaration_as_qualification(
        tmp_path, policy, clock, integration_context):
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    with pytest.raises(TrustError, match='explicit installation context'):
        TrustController(entitlement=store)
    with pytest.raises(TrustError, match='another product'):
        TrustController(entitlement=store, context=replace(integration_context, product_id='other'))
    controller = TrustController(entitlement=store, context=integration_context)
    assert provider_status(controller.entitlement_provider, family='entitlement').state == Readiness.DECLARED
    assert not controller.automatic_refresh
    decision = controller.entitlement_boundary.decide(EntitlementRequest(integration_context, 'request-1', 'render'))
    assert decision.state == DecisionState.UNAVAILABLE


def test_real_tuf_offer_uses_canonical_protocol_but_is_not_install_authority(
        repository, tmp_path, integration_context):
    raw = bundle()
    repository.publish(raw)
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    provider = ClientUpdateProvider(integration_context, client, repository.root, repository.target_path)
    offer = provider.check(integration_context, cancelled=lambda: False)
    assert offer.context is integration_context
    assert offer.package_sha256 == sha256(raw).hexdigest()
    assert offer.package_bytes == len(raw) and offer.target_build_id == '2'
    with pytest.raises(TrustError, match='does not match'):
        provider.check(replace(integration_context, build_id='other'), cancelled=lambda: False)
    with pytest.raises(TrustError, match='another product or channel'):
        ClientUpdateProvider(replace(integration_context, channel='other'), client, repository.root, repository.target_path)
    with pytest.raises(TrustError, match='publisher approval'):
        VerifiedPackage(provider.check_release(integration_context), tmp_path / 'arbitrary.zip')
