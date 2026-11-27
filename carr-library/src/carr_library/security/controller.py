"""wx-independent review controller for explicitly configured owner clients."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import time
from types import MappingProxyType
from uuid import uuid4

from ..delivery_contract import (EntitlementBoundary, IntegrationContext, EntitlementRequest,
                                 DecisionState, USER_DATA_ACTIONS)
from ._files import TrustError, read_bytes
from .boundary import ClientEntitlementProvider, ClientUpdateProvider
from .entitlements import (MAX_ENVELOPE, PROTECTED, EntitlementStore, OperationLease, ReviewRequired,
                           private_status)


def _date(value):
    return 'No expiry' if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat()


@dataclass(frozen=True)
class GrantReview:
    raw: bytes
    digest: str
    terms_digest: str
    description: str


class TrustController:
    def __init__(self, *, entitlement=None, refresh=None, updates=None, target=None, terms=None,
                 context=None, automatic_refresh=False, automatic_updates=False,
                 update_repository=None, update_interval=3600.0, update_timeout=20.0,
                 premium_features=None, standard_update=None):
        if (entitlement is not None or updates is not None) and type(context) is not IntegrationContext:
            raise TrustError('Configured clients require an explicit installation context.')
        if type(automatic_refresh) is not bool or (automatic_refresh and refresh is None):
            raise TrustError('Automatic renewal requires an explicitly configured renewal provider.')
        if refresh is not None and (entitlement is None or refresh.store is not entitlement):
            raise TrustError('Renewal must use this exact entitlement store.')
        if updates is not None and (context.product_id, context.channel) != (updates.policy.product, updates.policy.channel):
            raise TrustError('The update client belongs to another product or channel.')
        if type(automatic_updates) is not bool or (automatic_updates and update_repository is None):
            raise TrustError('Automatic update checks require an explicitly selected signed repository.')
        from .admission import FRESH_OPERATION_ACTIONS, _action
        if premium_features is not None and type(premium_features) is not dict:
            raise TrustError('Explicit owner feature mappings must be an ordinary mapping.')
        mapping = dict(premium_features or {})
        if len(mapping) > 256:
            raise TrustError('The configured feature map exceeds its allowance.')
        if mapping and not isinstance(entitlement, EntitlementStore):
            raise TrustError('Managed operation admission requires the explicitly configured signed store.')
        for action, feature in mapping.items():
            _action(action)
            if action in PROTECTED:
                raise TrustError('User data, settings and active completion cannot require an entitlement.')
            if action not in FRESH_OPERATION_ACTIONS:
                raise TrustError('This application has no admission hook for the configured operation.')
            if (type(feature) is not str or feature in PROTECTED
                    or feature not in entitlement.policy.features):
                raise TrustError('Map new work only to an independently configured premium feature.')
        if standard_update is not None:
            from .standard_installation import StandardUpdateTransaction
            if (type(standard_update) is not StandardUpdateTransaction or updates is None
                    or standard_update.policy is not updates.policy):
                raise TrustError('Standard activation must share this exact configured update policy and client.')
        self.standard_update = standard_update
        self.premium_features = MappingProxyType(mapping)
        self.context, self.automatic_refresh = context, automatic_refresh
        self.entitlement, self.refresh, self.updates, self.target = entitlement, refresh, updates, target
        self.entitlement_provider = ClientEntitlementProvider(context, entitlement) if entitlement is not None else None
        self.entitlement_boundary = EntitlementBoundary(self.entitlement_provider)
        self._notice = None
        approved_terms = dict(terms or {})
        for digest, text in approved_terms.items():
            if (not isinstance(text, str) or len(text.encode('utf-8')) > 128 * 1024
                    or sha256(text.encode('utf-8')).hexdigest() != digest):
                raise TrustError('Owner-supplied terms do not match their pinned digest.')
        self.terms = MappingProxyType(approved_terms)
        if (updates is None) != (target is None):
            raise TrustError('Supply both the trusted update client and its target.')
        self.update_poll = None
        if update_repository is not None:
            from .polling import IdleUpdatePollHost
            self.update_poll = IdleUpdatePollHost(self, update_repository, enabled=automatic_updates,
                                                  interval=update_interval, timeout=update_timeout)

    def begin_operation(self, action):
        """Admit only explicitly mapped new work through the same local verifier."""
        from .admission import _action
        action = _action(action)
        if action in USER_DATA_ACTIONS or action not in self.premium_features:
            return OperationLease(action, 0)
        feature = self.premium_features[action]
        request = EntitlementRequest(self.context, uuid4().hex, feature)
        decision = self.entitlement_boundary.decide(request)
        if decision.state is not DecisionState.ALLOWED:
            raise ReviewRequired('This new managed operation needs valid permission. '
                                 'Existing work, reading, saving and exports remain available.')
        # The final begin captures the existing store's immutable sequence and
        # closes a possible expiry/revocation change before work actually starts.
        return self.entitlement.begin(feature)

    def status_text(self):
        status = self.entitlement.status() if self.entitlement is not None else private_status()
        lines = [status.message]
        if self.entitlement is None and self.updates is None:
            lines.append('WAITING_INPUT: managed permissions and signed updates need independently supplied owner configuration.')
        if self.entitlement is not None:
            lines.extend([f'Grant sequence: {status.sequence}', f'Valid until: {_date(status.expires_at)}'])
        if self.refresh is not None:
            lines.append(self.refresh.message)
        if self._notice is not None:
            lines.append(self._notice)
        lines.append('Signed offline update checks are available.' if self.updates is not None
                     else 'No signed update source has been configured for this installation.')
        if self.update_poll is not None:
            lines.append(self.update_poll.message)
        if self.standard_update is not None:
            lines.append('Reviewed standard upgrades keep the previous version and active work. '
                         'Activation requires a complete backup and verified installed health checks.')
        elif self.updates is not None:
            lines.append('Standard activation is WAITING_INPUT: no installation transaction is configured.')
        lines.append('Updates and renewal never remove access to reading, export, backups, recovery or prior outputs.')
        return '\n\n'.join(lines)

    def preview_file(self, path):
        if self.entitlement is None:
            raise TrustError('This private installation has no managed entitlement authority.')
        raw = read_bytes(path, MAX_ENVELOPE)
        review = self.entitlement.preview(raw)
        terms = self.terms.get(review['terms_sha256'])
        if terms is None:
            raise TrustError('The signed file refers to terms that are not available for review. Ask the owner for the matching terms document.')
        description = '\n'.join((
            f"Product: {review['product']}", f"Profile: {review['profile']}",
            f"Grant sequence: {review['sequence']}",
            f"Features: {', '.join(review['features']) or 'None'}",
            f"Valid until: {_date(review['expires_at'])}",
            f"Offline grace until: {_date(review['offline_until'])}",
            f"Revoked: {'Yes' if review['revoked'] else 'No'}",
            f"Owner policy: {review['policy_version']}",
            f"Terms identity: {review['terms_sha256']}",
            '', 'Terms supplied by the owner:', terms,
            '', 'Reading, export, backups, recovery and prior outputs remain available.'))
        return GrantReview(raw, review['sha256'], review['terms_sha256'], description)

    def import_review(self, review, *, accepted=False, cancelled=lambda: False):
        if self.entitlement is None or type(review) is not GrantReview or accepted is not True:
            raise TrustError('An explicit review and acceptance are required.')
        if self.terms.get(review.terms_digest) is None:
            raise TrustError('The reviewed terms are no longer configured.')
        def guard():
            if cancelled():
                raise InterruptedError('Permission import cancelled before protected publication.')
        result = self.entitlement.accept(review.raw, reviewed_digest=review.digest,
                                         accepted_terms=review.terms_digest, guard=guard)
        if self.refresh is not None:
            try:
                self.refresh.acknowledge_review()
                self._notice = None
            except Exception:
                # The signed grant is already durably committed. A retry-
                # schedule observer must not turn that success into a failure.
                self._notice = 'The signed permission was imported. Its automatic renewal schedule needs attention.'
        return result

    def check_renewal(self, *, cancelled=lambda: False):
        """Run only in the host's existing cancellable job executor."""
        if self.refresh is None:
            raise TrustError('No signed-renewal provider is configured.')
        if cancelled():
            raise InterruptedError('Renewal check cancelled.')
        deadline = time.monotonic() + self.refresh.timeout_seconds
        self.refresh.tick(manual=True, cancelled=lambda: cancelled() or time.monotonic() >= deadline)
        while self.refresh.running:
            if cancelled():
                self.refresh.cancel_current()
                raise InterruptedError('Renewal wait cancelled. Any already accepted permission remains available.')
            if time.monotonic() >= deadline:
                self.refresh.cancel_current()
                raise TrustError('Renewal wait exceeded its time budget. Any already accepted permission remains available.')
            time.sleep(.02)
        if cancelled():
            raise InterruptedError('Renewal check cancelled.')
        if time.monotonic() >= deadline:
            raise TrustError('Renewal wait exceeded its time budget. Any already accepted permission remains available.')
        return self.status_text()

    def check_repository(self, repository, *, cancelled=lambda: False):
        if self.updates is None:
            raise TrustError('No independently trusted update repository is configured.')
        return ClientUpdateProvider(self.context, self.updates, repository, self.target).check_release(
            self.context, cancelled=cancelled)

    def download_review(self, repository, release, *, cancelled=lambda: False):
        if self.updates is None:
            raise TrustError('No independently trusted update repository is configured.')
        return self.updates.download(repository, self.target, reviewed_sha256=release.sha256, cancelled=cancelled)

    def install_review(self, package, *, accepted=False, cancelled=lambda: False):
        if self.standard_update is None or accepted is not True:
            raise TrustError('A configured standard installation and explicit activation review are required.')
        from .updates import require_verified_package
        require_verified_package(package, policy=self.updates.policy)
        if package.release.target != self.target:
            raise TrustError('The reviewed package is not this controller\'s configured update target.')
        return self.standard_update.apply(package, cancelled=cancelled)

    def recover_update(self):
        if self.standard_update is None:
            raise TrustError('Standard recovery is WAITING_INPUT: no installation transaction is configured.')
        return self.standard_update.recover()


def release_text(release):
    return '\n'.join((
        f'Product: {release.product}', f'Channel: {release.channel}',
        f'Version: {release.version}; build {release.build}',
        f'Source commit: {release.source_commit}', f'Bytes: {release.length}',
        f'Package SHA256: {release.sha256}', '', release.notes,
        '', 'This action prepares the verified package. It does not interrupt open work or replace the running application.'))
