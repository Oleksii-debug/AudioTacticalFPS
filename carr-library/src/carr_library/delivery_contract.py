"""Public, product-neutral boundary for a separately supplied private runtime.

This module performs no signature verification, networking, installation or
entitlement issuance. An injected provider is trusted application infrastructure,
not a plugin discovered from user data. Its declarations are not release evidence.
The current private application does not enable a new licensing restriction.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Callable, Protocol


CONTRACT_VERSION = 1
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
_SHA256 = re.compile(r'[0-9a-f]{64}')


def _identifier(value):
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError('A bounded, non-secret integration identifier is required.')


class Capability(str, Enum):
    ENTITLEMENT_DECISION = 'entitlement_decision'
    OFFLINE_LICENSE_IMPORT = 'offline_license_import'
    SIGNED_RENEWAL = 'signed_renewal'
    UPDATE_CHECK = 'update_check'
    OFFLINE_UPDATE_IMPORT = 'offline_update_import'
    UPDATE_INSTALL = 'update_install'
    UPDATE_RECOVERY = 'update_recovery'


ENTITLEMENT_CAPABILITIES = frozenset({Capability.ENTITLEMENT_DECISION,
    Capability.OFFLINE_LICENSE_IMPORT, Capability.SIGNED_RENEWAL})
UPDATE_CAPABILITIES = frozenset(Capability) - ENTITLEMENT_CAPABILITIES

# Reserved actions must stay available independently of any entitlement outcome.
# Call sites will map their existing typed operations explicitly; prefix matching
# cannot promote an arbitrary action into this set.
USER_DATA_ACTIONS = frozenset({
    'data.read', 'data.open', 'data.save', 'data.export', 'data.backup',
    'data.restore', 'data.recover', 'settings.read', 'settings.write',
    'job.finish_active',
})


@dataclass(frozen=True)
class IntegrationContext:
    product_id: str
    build_id: str
    channel: str

    def __post_init__(self):
        for value in (self.product_id, self.build_id, self.channel):
            _identifier(value)


@dataclass(frozen=True)
class ProviderCapabilities:
    contract_version: int
    capabilities: frozenset[Capability]

    def __post_init__(self):
        if type(self.contract_version) is not int or self.contract_version < 1:
            raise ValueError('Invalid integration contract version.')
        if (type(self.capabilities) is not frozenset
                or any(type(item) is not Capability for item in self.capabilities)):
            raise ValueError('Capabilities must be an immutable typed set.')


class Readiness(str, Enum):
    ABSENT = 'absent'
    DECLARED = 'declared_unqualified'
    INCOMPATIBLE = 'incompatible'
    UNAVAILABLE = 'unavailable'


@dataclass(frozen=True)
class IntegrationStatus:
    state: Readiness
    capabilities: frozenset[Capability] = frozenset()


class DecisionState(str, Enum):
    ALLOWED = 'allowed'
    DENIED = 'denied'
    UNAVAILABLE = 'unavailable'


class DecisionReason(str, Enum):
    DATA_ACCESS_PRESERVED = 'data_access_preserved'
    PROVIDER_ALLOWED = 'provider_allowed'
    PROVIDER_DENIED = 'provider_denied'
    PROVIDER_ABSENT = 'provider_absent'
    PROVIDER_INCOMPATIBLE = 'provider_incompatible'
    PROVIDER_UNAVAILABLE = 'provider_unavailable'
    RESPONSE_MISMATCH = 'response_mismatch'


@dataclass(frozen=True)
class EntitlementRequest:
    context: IntegrationContext
    request_id: str
    action: str

    def __post_init__(self):
        if type(self.context) is not IntegrationContext:
            raise ValueError('An explicit integration context is required.')
        _identifier(self.request_id)
        _identifier(self.action)


@dataclass(frozen=True)
class EntitlementDecision:
    request: EntitlementRequest
    state: DecisionState
    reason: DecisionReason

    def __post_init__(self):
        if (type(self.request) is not EntitlementRequest
                or type(self.state) is not DecisionState
                or type(self.reason) is not DecisionReason):
            raise ValueError('Invalid normalized entitlement decision.')


class EntitlementProvider(Protocol):
    """Private adapter owns trusted identities, signatures, policy and storage.

    describe is local and has no network or state-changing effects. Decisions
    do not initiate enrollment, renewal, billing or changed terms. The host runs
    potentially slow private operations in its existing cancellable job system.
    """
    def describe(self) -> ProviderCapabilities: ...
    def decide(self, request: EntitlementRequest) -> EntitlementDecision: ...


@dataclass(frozen=True)
class UpdateOffer:
    """Normalized display data; never sufficient authority to install bytes."""
    context: IntegrationContext
    offer_id: str
    target_build_id: str
    package_bytes: int
    package_sha256: str

    def __post_init__(self):
        if type(self.context) is not IntegrationContext:
            raise ValueError('An explicit integration context is required.')
        _identifier(self.offer_id)
        _identifier(self.target_build_id)
        if type(self.package_bytes) is not int or self.package_bytes <= 0:
            raise ValueError('Invalid update size.')
        if not isinstance(self.package_sha256, str) or not _SHA256.fullmatch(self.package_sha256):
            raise ValueError('Invalid update digest.')


class UpdateProvider(Protocol):
    """A separate private update authority; no entitlement or account coupling.

    A check is explicit. Trusted metadata verification, provenance, expiry,
    rollback floors and transport rules belong to the private provider. Installation
    additionally needs the existing job/recovery consent boundary; this protocol
    intentionally exposes no executable command or arbitrary download URL.
    """
    def describe(self) -> ProviderCapabilities: ...
    def check(self, context: IntegrationContext, *,
              cancelled: Callable[[], bool]) -> UpdateOffer | None: ...


def provider_status(provider, *, family: str) -> IntegrationStatus:
    """Report configuration only, never security qualification or test success."""
    if family not in ('entitlement', 'update'):
        raise ValueError('Choose the entitlement or update integration family.')
    if provider is None:
        return IntegrationStatus(Readiness.ABSENT)
    try:
        description = provider.describe()
    except Exception:
        # Private adapters can raise errors containing endpoints or credentials.
        return IntegrationStatus(Readiness.UNAVAILABLE)
    if type(description) is not ProviderCapabilities or description.contract_version != CONTRACT_VERSION:
        return IntegrationStatus(Readiness.INCOMPATIBLE)
    admitted = ENTITLEMENT_CAPABILITIES if family == 'entitlement' else UPDATE_CAPABILITIES
    capabilities = description.capabilities & admitted
    if not capabilities:
        return IntegrationStatus(Readiness.INCOMPATIBLE)
    return IntegrationStatus(Readiness.DECLARED, capabilities)


class EntitlementBoundary:
    """Optional future integration point, not an independent license verifier.

    Existing application entrypoints do not use this preparation-only boundary.
    Inject only an explicitly qualified private adapter through private assembly;
    there is no module-name loader, entry-point discovery or permissive fallback.
    """
    def __init__(self, provider: EntitlementProvider | None = None):
        self._provider = provider

    def decide(self, request: EntitlementRequest) -> EntitlementDecision:
        if type(request) is not EntitlementRequest:
            raise ValueError('A normalized entitlement request is required.')
        if request.action in USER_DATA_ACTIONS:
            return EntitlementDecision(request, DecisionState.ALLOWED, DecisionReason.DATA_ACCESS_PRESERVED)
        status = provider_status(self._provider, family='entitlement')
        if status.state != Readiness.DECLARED or Capability.ENTITLEMENT_DECISION not in status.capabilities:
            reason = {Readiness.ABSENT: DecisionReason.PROVIDER_ABSENT,
                      Readiness.UNAVAILABLE: DecisionReason.PROVIDER_UNAVAILABLE}.get(
                          status.state, DecisionReason.PROVIDER_INCOMPATIBLE)
            return EntitlementDecision(request, DecisionState.UNAVAILABLE, reason)
        try:
            result = self._provider.decide(request)
        except Exception:
            return EntitlementDecision(request, DecisionState.UNAVAILABLE, DecisionReason.PROVIDER_UNAVAILABLE)
        if (type(result) is not EntitlementDecision or result.request != request
                or result.reason not in (DecisionReason.PROVIDER_ALLOWED, DecisionReason.PROVIDER_DENIED,
                                         DecisionReason.PROVIDER_UNAVAILABLE)
                or {DecisionState.ALLOWED: DecisionReason.PROVIDER_ALLOWED,
                    DecisionState.DENIED: DecisionReason.PROVIDER_DENIED,
                    DecisionState.UNAVAILABLE: DecisionReason.PROVIDER_UNAVAILABLE}[result.state] != result.reason):
            return EntitlementDecision(request, DecisionState.UNAVAILABLE, DecisionReason.RESPONSE_MISMATCH)
        return result
