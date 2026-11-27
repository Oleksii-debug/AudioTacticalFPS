"""Synthetic adapter boundary tests only; no signature/security qualification.

Native private-runtime qualification and signature verification are separate gates.
"""
from dataclasses import replace

import pytest

from carr_library.delivery_contract import (
    Capability, DecisionReason, DecisionState, EntitlementBoundary,
    EntitlementDecision, EntitlementRequest, IntegrationContext, ProviderCapabilities,
    Readiness, USER_DATA_ACTIONS, UpdateOffer, provider_status, IntegrationStatus,
    RefreshCadence, RefreshCheckpoint, RefreshPlan, RefreshState, plan_refresh,
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



@pytest.mark.parametrize(('state', 'capabilities'), [
    ('declared_unqualified', frozenset({Capability.UPDATE_CHECK})),
    (Readiness.ABSENT, frozenset({Capability.UPDATE_CHECK})),
    (Readiness.DECLARED, frozenset()),
    (Readiness.DECLARED, {'update_check'}),
    (Readiness.DECLARED, frozenset({'update_check'})),
])
def test_integration_status_cannot_promote_malformed_or_absent_provider(state, capabilities):
    with pytest.raises(ValueError):
        IntegrationStatus(state, capabilities)


def test_update_display_size_is_bounded_without_authorizing_any_install():
    with pytest.raises(ValueError):
        UpdateOffer(CONTEXT, 'offer-1', 'build-2', 1 << 63, 'a' * 64)


@pytest.mark.parametrize('days', [7, 14])
def test_refresh_cadence_is_explicit_and_independent_of_offline_authorization(days):
    interval = days * 24 * 60 * 60
    cadence = RefreshCadence(interval, (60, 300), max_jitter_seconds=20)
    checkpoint = RefreshCheckpoint(last_attempt_utc=100, last_success_utc=100)
    before = plan_refresh(cadence, checkpoint, now_utc=100 + interval - 1)
    due = plan_refresh(cadence, checkpoint, now_utc=100 + interval)
    assert before == RefreshPlan(RefreshState.WAITING, 100 + interval)
    assert due == RefreshPlan(RefreshState.DUE, 100 + interval)
    # Scheduling neither extends a license nor restricts access to existing work.
    denied = EntitlementBoundary(SyntheticAdapter(DecisionState.DENIED))
    for action in USER_DATA_ACTIONS:
        assert denied.decide(replace(REQUEST, action=action)).reason == DecisionReason.DATA_ACCESS_PRESERVED


def test_disabled_perpetual_profile_has_no_periodic_contact_requirement():
    cadence = RefreshCadence(None)
    for now in (0, 7 * 86400, 14 * 86400, 365 * 86400):
        assert plan_refresh(cadence, RefreshCheckpoint(), now_utc=now) == RefreshPlan(RefreshState.DISABLED)
    # Historical failure, even with an inconsistent current clock, cannot
    # silently enable refresh in a profile that explicitly disables it.
    assert plan_refresh(cadence, RefreshCheckpoint(200, 100, 3), now_utc=50).state == RefreshState.DISABLED


def test_unattended_retry_burst_is_bounded_then_returns_to_normal_cadence():
    cadence = RefreshCadence(7 * 86400, (60, 300, 900))
    for failures, delay in ((1, 60), (2, 300), (3, 900), (4, 7 * 86400), (99, 7 * 86400)):
        checkpoint = RefreshCheckpoint(1000, 100, failures)
        assert plan_refresh(cadence, checkpoint, now_utc=1000 + delay - 1) == RefreshPlan(
            RefreshState.WAITING, 1000 + delay)
        assert plan_refresh(cadence, checkpoint, now_utc=1000 + delay).state == RefreshState.DUE


def test_long_outage_and_return_preserve_existing_data_and_reschedule_after_success():
    cadence = RefreshCadence(7 * 86400, (60,))
    old = RefreshCheckpoint(30 * 86400, 100, 4)
    provider = SyntheticAdapter()
    provider.decide = lambda request: (_ for _ in ()).throw(RuntimeError('private outage'))
    boundary = EntitlementBoundary(provider)
    assert boundary.decide(REQUEST).reason == DecisionReason.PROVIDER_UNAVAILABLE
    for action in USER_DATA_ACTIONS:
        assert boundary.decide(replace(REQUEST, action=action)).reason == DecisionReason.DATA_ACCESS_PRESERVED
    now = 40 * 86400
    assert plan_refresh(cadence, old, now_utc=now).state == RefreshState.DUE
    recovered = RefreshCheckpoint(now, now, 0)
    assert plan_refresh(cadence, recovered, now_utc=now) == RefreshPlan(RefreshState.WAITING, now + 7 * 86400)
    assert old == RefreshCheckpoint(30 * 86400, 100, 4)


def test_clock_rollback_requires_review_without_mutating_history_or_data_access():
    checkpoint = RefreshCheckpoint(200, 100, 1)
    assert plan_refresh(RefreshCadence(86400, (60,)), checkpoint, now_utc=199) == RefreshPlan(
        RefreshState.CLOCK_REVIEW)
    assert checkpoint == RefreshCheckpoint(200, 100, 1)
    request = replace(REQUEST, action='job.finish_active')
    assert EntitlementBoundary().decide(request).reason == DecisionReason.DATA_ACCESS_PRESERVED


def test_jitter_only_delays_within_supplied_bound():
    cadence = RefreshCadence(100, (10,), max_jitter_seconds=5)
    initial = plan_refresh(cadence, RefreshCheckpoint(), now_utc=50, jitter_seconds=5)
    assert initial == RefreshPlan(RefreshState.DUE, 50)
    assert plan_refresh(cadence, RefreshCheckpoint(), now_utc=55, jitter_seconds=5) == RefreshPlan(RefreshState.DUE, 55)
    previous = RefreshCheckpoint(100, 100)
    assert plan_refresh(cadence, previous, now_utc=200, jitter_seconds=5) == RefreshPlan(RefreshState.WAITING, 205)
    with pytest.raises(ValueError):
        plan_refresh(cadence, previous, now_utc=200, jitter_seconds=6)


@pytest.mark.parametrize('arguments', [
    (True,), (0,), (-1,), (1 << 63,), (60, [1, 2]), (60, (2, 1)),
    (60, (0,)), (60, (True,)), (60, (1,) * 17), (60, (), True),
    (None, (1,)), (None, (), 1),
])
def test_refresh_policy_rejects_unbounded_or_coercible_values(arguments):
    with pytest.raises(ValueError):
        RefreshCadence(*arguments)


@pytest.mark.parametrize('arguments', [
    (None, 1, 0), (None, None, 1), (100, 200, 1), (100, None, 0),
    (100, 50, 0), (True, True, 0), (100, 100, True), (100, 100, -1),
])
def test_refresh_history_rejects_inconsistent_or_coercible_values(arguments):
    with pytest.raises(ValueError):
        RefreshCheckpoint(*arguments)


@pytest.mark.parametrize('now', [True, -1, 1 << 63, float('inf')])
def test_refresh_clock_is_a_bounded_integer(now):
    with pytest.raises(ValueError):
        plan_refresh(RefreshCadence(60), RefreshCheckpoint(), now_utc=now)


def test_refresh_deadline_overflow_does_not_create_a_runnable_plan():
    with pytest.raises(ValueError):
        plan_refresh(RefreshCadence(60), RefreshCheckpoint((1 << 63) - 2, (1 << 63) - 2),
                     now_utc=(1 << 63) - 2)


def test_malformed_refresh_plan_is_not_a_runnable_status():
    with pytest.raises(ValueError):
        RefreshPlan('due', 10)
    with pytest.raises(ValueError):
        RefreshPlan(RefreshState.DUE)
    with pytest.raises(ValueError):
        RefreshPlan(RefreshState.DISABLED, 10)
