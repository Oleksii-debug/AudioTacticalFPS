"""wx-independent review controller for explicitly configured owner clients."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import time
from types import MappingProxyType

from ..delivery_contract import EntitlementBoundary, IntegrationContext
from ._files import TrustError, read_bytes
from .boundary import ClientEntitlementProvider, ClientUpdateProvider
from .entitlements import MAX_ENVELOPE, EntitlementStore, private_status


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
                 context=None, automatic_refresh=False, handoff=None, provisioning=None):
        if (entitlement is not None or updates is not None) and type(context) is not IntegrationContext:
            raise TrustError('Configured clients require an explicit installation context.')
        if type(automatic_refresh) is not bool or (automatic_refresh and refresh is None):
            raise TrustError('Automatic renewal requires an explicitly configured renewal provider.')
        if refresh is not None and (entitlement is None or refresh.store is not entitlement):
            raise TrustError('Renewal must use this exact entitlement store.')
        if updates is not None and (context.product_id, context.channel) != (updates.policy.product, updates.policy.channel):
            raise TrustError('The update client belongs to another product or channel.')
        if handoff is not None and provisioning is not None:
            raise TrustError('Choose a prepared host or an explicit preparation recipe.')
        if handoff is not None:
            from .update_handoff import ManagedUpdateHandoffService
            if type(handoff) is not ManagedUpdateHandoffService or updates is None:
                raise TrustError('Apply requires the reviewed configured maintenance service.')
            if handoff.configuration.policy != updates.policy:
                raise TrustError('Apply and download must use the same independently trusted publisher policy.')
        if provisioning is not None:
            from .update_runtime import UpdateHostProvisioning
            if type(provisioning) is not UpdateHostProvisioning or updates is None or provisioning.policy != updates.policy:
                raise TrustError('Preparation requires the same explicitly configured publisher policy.')
        self.handoff, self.provisioning = handoff, provisioning
        self.update_library = None
        self.update_directories = None
        self.background_update_checks = False
        self._next_update_check = time.monotonic() + 3600
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

    def status_text(self):
        status = self.entitlement.status() if self.entitlement is not None else private_status()
        lines = [status.message]
        if self.entitlement is not None:
            lines.extend([f'Grant sequence: {status.sequence}', f'Valid until: {_date(status.expires_at)}'])
        if self.refresh is not None:
            lines.append(self.refresh.message)
        if self._notice is not None:
            lines.append(self._notice)
        lines.append('Signed offline update checks are available.' if self.updates is not None
                     else 'No signed update source has been configured for this installation.')
        if self.provisioning is not None:
            lines.append('Apply needs the separately prepared maintenance runtime. Choose Prepare update host to create it from the exact approved installed bundle; the current program and research data remain in place.')
        elif self.handoff is not None:
            lines.append('Apply is available after package verification and your explicit approval to close the idle application. The external maintenance host retains backup, health and recovery.')
        elif self.updates is not None:
            lines.append('Apply is unavailable: this installation has no independently configured external maintenance host.')
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

    def _update_current(self):
        if self.update_library is not None:
            from .update_handoff import _same_directories
            _same_directories(self.update_directories)
            if str(self.update_library.root) != self.update_directories[-1][0]:
                raise TrustError('The update review belongs to another current library.')
        if self.handoff is not None:
            self.handoff.configuration.check()
        if self.provisioning is not None:
            self.provisioning.check()

    @property
    def update_endpoint(self):
        return getattr(self.updates, 'endpoint', None)

    def _update_configuration(self):
        return self.handoff.configuration if self.handoff is not None else self.provisioning

    def load_update_check_preference(self):
        configuration = self._update_configuration()
        if configuration is None:
            return
        from ._files import json_value
        path = configuration.work / 'update-check-preferences.json'
        try:
            if not path.exists():
                return
            value = json_value(read_bytes(path, 4096), maximum=4096)
            if (type(value) is not dict or set(value) != {'schema', 'configuration_sha256', 'enabled'}
                    or type(value['schema']) is not int or value['schema'] != 1
                    or value['configuration_sha256'] != configuration.sha256
                    or type(value['enabled']) is not bool):
                raise TrustError('The saved update-check consent does not match this explicit configuration.')
            self._update_current()
            self.background_update_checks = value['enabled'] and self.update_endpoint is not None
        except (ValueError, OSError):
            self.background_update_checks = False
            self._notice = 'Saved background-update consent could not be verified. Checks remain manual.'

    def set_background_update_checks(self, enabled, *, accepted=False, cancelled=lambda: False):
        if type(enabled) is not bool or enabled and accepted is not True:
            raise TrustError('Background update checks require explicit consent.')
        configuration = self._update_configuration()
        if configuration is None or self.update_endpoint is None:
            raise TrustError('Background checks need an explicitly configured update site.')
        self._update_current()
        if cancelled():
            raise InterruptedError('Update-check preference cancelled.')
        from ._files import atomic_write, json_bytes, locked
        with locked(configuration.work, '.update-preferences.lock'):
            self._update_current()
            raw = json_bytes({'schema': 1, 'configuration_sha256': configuration.sha256, 'enabled': enabled})
            atomic_write(configuration.work / 'update-check-preferences.json', raw)
            self.background_update_checks = enabled
            self._next_update_check = time.monotonic() + 3600
        return enabled

    def background_update_due(self, *, now=None):
        return (self.background_update_checks and self.update_endpoint is not None
                and (time.monotonic() if now is None else now) >= self._next_update_check)

    def reserve_background_update_check(self):
        if not self.background_update_due():
            return False
        self._update_current()
        self._next_update_check = time.monotonic() + 3600
        return True

    def record_background_update(self, release):
        self._update_current()
        self._notice = ('A signed update is available: ' + release.version + ', build ' + str(release.build)
                        + '. Open Help, Check for updates / Update to review and choose it.')
        return self._notice

    def prepare_update_host(self, *, cancelled=lambda: False):
        self._update_current()
        if self.provisioning is None:
            raise TrustError('No new owner-approved maintenance preparation is pending.')
        from .update_runtime import prepare_update_host
        from .update_handoff import OwnerUpdateConfiguration, ManagedUpdateHandoffService
        result = prepare_update_host(self.provisioning, cancelled=cancelled)
        configuration = OwnerUpdateConfiguration(result['configuration'], result['configuration_sha256'])
        self.handoff = ManagedUpdateHandoffService(configuration)
        self.provisioning = None
        self.updates = configuration.client()
        self._notice = ('Maintenance runtime prepared. Configuration: ' + result['configuration']
                        + '\nConfiguration SHA256: ' + result['configuration_sha256'])
        return result

    def prepare_apply(self, repository, package, *, restart=False, cancelled=lambda: False):
        self._update_current()
        if self.handoff is None:
            raise TrustError('Prepare the explicitly configured external update host before Apply.')
        return self.handoff.prepare(repository, package, restart=restart, cancelled=cancelled)

    def check_repository(self, repository, *, cancelled=lambda: False):
        self._update_current()
        if self.updates is None:
            raise TrustError('No independently trusted update repository is configured.')
        return ClientUpdateProvider(self.context, self.updates, repository, self.target).check_release(
            self.context, cancelled=cancelled)

    def download_review(self, repository, release, *, cancelled=lambda: False, progress=None):
        self._update_current()
        if self.updates is None:
            raise TrustError('No independently trusted update repository is configured.')
        options = dict(reviewed_sha256=release.sha256, cancelled=cancelled)
        from .update_handoff import ConfiguredRepositoryClient
        if isinstance(self.updates, ConfiguredRepositoryClient):
            options['progress'] = progress
        return self.updates.download(repository, self.target, **options)


def release_text(release):
    return '\n'.join((
        f'Product: {release.product}', f'Channel: {release.channel}',
        f'Version: {release.version}; build {release.build}',
        f'Source commit: {release.source_commit}', f'Bytes: {release.length}',
        f'Package SHA256: {release.sha256}', '', release.notes,
        '', 'This action prepares the verified package. It does not interrupt open work or replace the running application.',
        'Cancel current operation stops or pauses the download. Download or resume reuses only a private prefix and verifies the whole signed target.',
        'This is an application update. Model/language packs and data migrations require their separate explicitly reviewed operations.'))


def configured_update_controller(path, expected_sha256, library):
    """Load only an explicit, independently hashed owner policy for this library."""
    from ._files import json_value
    from .update_handoff import (ConfiguredRepositoryClient, ManagedUpdateHandoffService,
        OwnerUpdateConfiguration, _digest, _directory_rows)
    raw = read_bytes(path, 65536)
    if sha256(raw).hexdigest() != _digest(expected_sha256):
        raise TrustError('The explicit owner update configuration changed.')
    value = json_value(raw, maximum=65536)
    kind = value.get('kind') if type(value) is dict else None
    if kind == 'carr-managed-update-provision':
        from .update_runtime import UpdateHostProvisioning
        configuration = UpdateHostProvisioning(path, expected_sha256)
        if configuration.library != library.root:
            raise TrustError('This owner update configuration belongs to another selected library.')
        selected = configuration.value['repository']
        client = ConfiguredRepositoryClient(configuration.work / 'download-cache', configuration.policy,
            endpoint=selected['endpoint'], timeout=selected['timeout'], ca_pem=selected['tls_ca_pem'])
        options = {'provisioning': configuration}
        target = selected['target']
    elif kind == 'carr-managed-update-host':
        configuration = OwnerUpdateConfiguration(path, expected_sha256)
        if configuration.library != library.root:
            raise TrustError('This owner update configuration belongs to another selected library.')
        client = configuration.client()
        options = {'handoff': ManagedUpdateHandoffService(configuration)}
        target = configuration.target
    else:
        raise TrustError('No supported explicit owner update configuration was selected.')
    if configuration.library != library.root:
        raise TrustError('This owner update configuration belongs to another selected library.')
    controller = TrustController(updates=client, target=target,
        context=IntegrationContext(configuration.policy.product,
            str(configuration.policy.current_build), configuration.policy.channel), **options)
    controller.update_library = library
    controller.update_directories = _directory_rows(library.root)
    controller.load_update_check_preference()
    return controller
