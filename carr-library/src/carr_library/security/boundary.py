"""Adapt verified local client state to the existing delivery contract.

Configuration is explicit trusted infrastructure. Declared capabilities are not
release qualification, and this adapter never enrolls an account or starts a
renewal while answering an entitlement decision.
"""
from __future__ import annotations

from ..delivery_contract import (
    CONTRACT_VERSION, Capability, DecisionReason, DecisionState,
    EntitlementDecision, EntitlementRequest, IntegrationContext,
    ProviderCapabilities, UpdateOffer,
)
from ._files import TrustError
from .entitlements import EntitlementStore


class ClientEntitlementProvider:
    def __init__(self, context: IntegrationContext, store: EntitlementStore):
        if type(context) is not IntegrationContext or not isinstance(store, EntitlementStore):
            raise TrustError('An explicit integration context and verified entitlement store are required.')
        if context.product_id != store.policy.product:
            raise TrustError('The entitlement client belongs to another product.')
        self.context, self.store = context, store

    def describe(self):
        return ProviderCapabilities(CONTRACT_VERSION, frozenset({
            Capability.ENTITLEMENT_DECISION, Capability.OFFLINE_LICENSE_IMPORT}))

    def decide(self, request):
        if type(request) is not EntitlementRequest:
            raise TrustError('A normalized entitlement request is required.')
        if request.context != self.context:
            return EntitlementDecision(request, DecisionState.UNAVAILABLE,
                                       DecisionReason.PROVIDER_UNAVAILABLE)
        status = self.store.status()
        if status.mode in ('attention', 'needs-import'):
            state, reason = DecisionState.UNAVAILABLE, DecisionReason.PROVIDER_UNAVAILABLE
        elif status.permits(request.action):
            state, reason = DecisionState.ALLOWED, DecisionReason.PROVIDER_ALLOWED
        else:
            state, reason = DecisionState.DENIED, DecisionReason.PROVIDER_DENIED
        return EntitlementDecision(request, state, reason)


class ClientUpdateProvider:
    """One explicit offline repository through the canonical update protocol.

UpdateOffer is display information. Only LocalRepositoryClient.download can
produce the VerifiedPackage consumed by the existing update transaction.
"""
    def __init__(self, context, client, repository, target):
        if type(context) is not IntegrationContext:
            raise TrustError('An explicit update integration context is required.')
        if (context.product_id, context.channel) != (client.policy.product, client.policy.channel):
            raise TrustError('The update client belongs to another product or channel.')
        self.context, self.client = context, client
        self.repository, self.target = repository, target

    def describe(self):
        return ProviderCapabilities(CONTRACT_VERSION, frozenset({
            Capability.UPDATE_CHECK, Capability.OFFLINE_UPDATE_IMPORT}))

    def check_release(self, context, *, cancelled=lambda: False):
        if type(context) is not IntegrationContext or context != self.context:
            raise TrustError('The update request does not match this installation.')
        return self.client.check(self.repository, self.target, cancelled=cancelled)

    def check(self, context, *, cancelled):
        release = self.check_release(context, cancelled=cancelled)
        return UpdateOffer(context, release.sha256, str(release.build),
                           release.length, release.sha256)
