"""Synthetic adapter boundary tests only; no signature/security qualification.

The preserved boundary suite was executed after the environment recovered.
"""
from dataclasses import replace

import pytest

from carr_library.delivery_contract import (
    Capability, DecisionReason, DecisionState, EntitlementBoundary,
    EntitlementDecision, EntitlementRequest, IntegrationContext, ProviderCapabilities,
    Readiness, USER_DATA_ACTIONS, UpdateOffer, provider_status,
)


CONTEXT = IntegrationContext('example-product', 'example-build', 'private')
REQUEST = EntitlementRequest(CONTEXT, 'request-1', 'example.operation')


class SyntheticAdapter:
    def __init__(self, state=DecisionState.ALLOWED, *, version=1, response=None):
        self.state, self.version, self.response = state, version, response
        self.calls = []

    def describe(self):
        self.calls.append('describe')
        return ProviderCapabilities(self.version, frozenset({Capability.ENTITLEMENT_DECISION}))

    def decide(self, request):
        self.calls.append(request)
        if self.response is not None:
            return self.response
        reason = {DecisionState.ALLOWED: DecisionReason.PROVIDER_ALLOWED,
                  DecisionState.DENIED: DecisionReason.PROVIDER_DENIED,
                  DecisionState.UNAVAILABLE: DecisionReason.PROVIDER_UNAVAILABLE}[self.state]
        return EntitlementDecision(request, self.state, reason)


@pytest.mark.parametrize('action', sorted(USER_DATA_ACTIONS))
@pytest.mark.parametrize('provider', [None, SyntheticAdapter(DecisionState.DENIED, version=999)])
def test_user_data_access_never_contacts_or_depends_on_provider(action, provider):
    if provider:
        provider.calls.clear()
    request = replace(REQUEST, action=action)
    result = EntitlementBoundary(provider).decide(request)
    assert result == EntitlementDecision(request, DecisionState.ALLOWED, DecisionReason.DATA_ACCESS_PRESERVED)
    assert provider is None or provider.calls == []


def test_absent_adapter_is_explicit_and_does_not_authorize_new_feature():
    result = EntitlementBoundary().decide(REQUEST)
    assert result.state == DecisionState.UNAVAILABLE
    assert result.reason == DecisionReason.PROVIDER_ABSENT
    assert provider_status(None, family='update').state == Readiness.ABSENT


@pytest.mark.parametrize('state', list(DecisionState))
def test_correlated_normalized_synthetic_decision_is_forwarded(state):
    provider = SyntheticAdapter(state)
    assert EntitlementBoundary(provider).decide(REQUEST).state == state
    assert provider.calls == ['describe', REQUEST]
    assert provider_status(provider, family='entitlement').state == Readiness.DECLARED


def test_incompatible_version_does_not_call_authorizer():
    provider = SyntheticAdapter(version=2)
    result = EntitlementBoundary(provider).decide(REQUEST)
    assert result.reason == DecisionReason.PROVIDER_INCOMPATIBLE
    assert provider.calls == ['describe']


@pytest.mark.parametrize('field,value', [('product_id', 'other'), ('build_id', 'other'), ('channel', 'other')])
def test_cross_context_response_is_never_reused(field, value):
    other = replace(REQUEST, context=replace(CONTEXT, **{field: value}))
    response = EntitlementDecision(other, DecisionState.ALLOWED, DecisionReason.PROVIDER_ALLOWED)
    assert EntitlementBoundary(SyntheticAdapter(response=response)).decide(REQUEST).reason == DecisionReason.RESPONSE_MISMATCH


@pytest.mark.parametrize('other', [replace(REQUEST, request_id='request-2'),
                                  replace(REQUEST, action='another.operation')])
def test_cross_request_response_is_never_reused(other):
    response = EntitlementDecision(other, DecisionState.ALLOWED, DecisionReason.PROVIDER_ALLOWED)
    assert EntitlementBoundary(SyntheticAdapter(response=response)).decide(REQUEST).reason == DecisionReason.RESPONSE_MISMATCH


@pytest.mark.parametrize('response', [True, {'allowed': True}, 'allowed',
    EntitlementDecision(REQUEST, DecisionState.ALLOWED, DecisionReason.PROVIDER_DENIED),
    EntitlementDecision(REQUEST, DecisionState.ALLOWED, DecisionReason.DATA_ACCESS_PRESERVED)])
def test_malformed_or_inconsistent_decision_fails_at_boundary(response):
    assert EntitlementBoundary(SyntheticAdapter(response=response)).decide(REQUEST).reason == DecisionReason.RESPONSE_MISMATCH


@pytest.mark.parametrize('method', ['describe', 'decide'])
def test_adapter_exceptions_are_redacted(method):
    provider = SyntheticAdapter()
    def fail(*args):
        raise RuntimeError('private endpoint and credential must not leave adapter')
    setattr(provider, method, fail)
    result = EntitlementBoundary(provider).decide(REQUEST)
    assert result.reason == DecisionReason.PROVIDER_UNAVAILABLE
    assert 'credential' not in repr(result)


def test_update_capability_cannot_authorize_a_feature_and_families_remain_separate():
    class UpdateOnly:
        def describe(self):
            return ProviderCapabilities(1, frozenset({Capability.UPDATE_CHECK}))
        def decide(self, request):
            pytest.fail('update-only provider must never receive entitlement decisions')
    provider = UpdateOnly()
    assert provider_status(provider, family='update').state == Readiness.DECLARED
    assert EntitlementBoundary(provider).decide(REQUEST).reason == DecisionReason.PROVIDER_INCOMPATIBLE


def test_prefix_matching_cannot_create_an_ungated_operation():
    result = EntitlementBoundary().decide(replace(REQUEST, action='data.read_and_execute'))
    assert result.state == DecisionState.UNAVAILABLE


@pytest.mark.parametrize('changes', [dict(package_bytes=True), dict(package_bytes=0),
    dict(package_sha256='../payload'), dict(offer_id='https://invalid.example'),
    dict(target_build_id='bad\nidentifier')])
def test_display_offer_has_bounded_non_executable_fields(changes):
    values = dict(context=CONTEXT, offer_id='offer-1', target_build_id='build-2',
                  package_bytes=10, package_sha256='a' * 64)
    with pytest.raises(ValueError):
        UpdateOffer(**(values | changes))


def test_protocol_capability_records_are_immutable_and_strict():
    with pytest.raises(ValueError):
        ProviderCapabilities(True, frozenset())
    with pytest.raises(ValueError):
        ProviderCapabilities(1, {'update_check'})
    with pytest.raises(ValueError):
        ProviderCapabilities(1, frozenset({'update_check'}))
    with pytest.raises(ValueError):
        provider_status(None, family='unknown')
