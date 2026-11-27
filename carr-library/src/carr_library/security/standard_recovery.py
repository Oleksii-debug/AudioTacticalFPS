"""Protected restart authority for one explicitly configured standard transaction.

Only the current Windows vault anchor's already configured reference is reused.
The large native activation snapshot stays in bounded ordinary state; this
separate domain stores only its exact transaction authority digest.
"""
from __future__ import annotations

from hashlib import sha256
import hmac

from ._files import TrustError, directory, json_bytes
from .anchors import Anchor, WindowsVaultAnchor

DOMAIN = 'carr-standard-recovery-authority/1'
MAX_CREDENTIAL = 2400


class StandardRecoveryAuthority:
    def __init__(self, anchor, policy, program, data, work):
        if not isinstance(anchor, WindowsVaultAnchor):
            raise TrustError('Protected standard recovery requires the configured Windows vault anchor.')
        if anchor.product != policy.product:
            raise TrustError('The standard recovery policy belongs to another protected product.')
        self.anchor = anchor
        self.scope = {
            'domain': DOMAIN, 'anchor_reference': anchor.reference, 'anchor_binding': anchor.vault_binding,
            'product': policy.product, 'channel': policy.channel,
            'bootstrap_sha256': policy.bootstrap_sha256,
            'policy': {name: getattr(policy, name) for name in (
                'current_build', 'data_schema', 'max_package_bytes', 'max_unpacked_bytes', 'max_files')},
            'program': str(directory(program)), 'data': str(directory(data)),
            'work': str(directory(work)),
        }

    def binding(self, record):
        # A fresh UUID separates transactions. Scope and anchor separation mean
        # no API credential, protected anchor or another installation is reused.
        return sha256(json_bytes({**self.scope, 'transaction': record['transaction']})).hexdigest()

    def value(self, record):
        prior = json_bytes(record['prior'])
        observed = None if record['observed'] is None else json_bytes(record['observed'])
        identity = {
            **self.scope, 'transaction': record['transaction'],
            'before': record['before'], 'release': record['release'], 'candidate': record['candidate'],
            'stage': record['stage'], 'archive': record['archive'],
            'backup': record['backup'], 'backup_sha256': record['backup_sha256'],
            'prior_sha256': sha256(prior).hexdigest(), 'prior_bytes': len(prior),
            'candidate_generation': record['pending']['candidate_generation'],
            'observed_sha256': None if observed is None else sha256(observed).hexdigest(),
            'observed_bytes': 0 if observed is None else len(observed),
        }
        value = json_bytes({'kind': DOMAIN, 'transaction': record['transaction'],
                            'authority_sha256': sha256(json_bytes(identity)).hexdigest()}).decode('ascii')
        if not 1 <= len(value.encode('ascii')) <= MAX_CREDENTIAL:
            raise TrustError('Protected standard recovery metadata exceeds the vault limit.')
        return value

    def _read(self, record):
        try:
            reader = self.anchor.vault.get_optional
            value = reader(self.anchor.reference, self.binding(record))
            if value is not None:
                if not isinstance(value, str) or not 1 <= len(value.encode('ascii')) <= MAX_CREDENTIAL:
                    raise ValueError('Invalid protected recovery value.')
            return value
        except Exception:
            raise TrustError('Protected standard recovery state is unavailable; program and data are preserved.') from None

    def seal(self, record, before, *, guard=lambda: None):
        if record['observed'] is not None or record['pending']['candidate_generation']:
            raise TrustError('Initial protected authority must precede native generation selection.')
        if type(before) is not Anchor or self.anchor.load() != before:
            raise TrustError('The protected update anchor changed before recovery authority was saved.')
        expected = self.value(record)
        current = self._read(record)
        if current is not None and current != expected:
            raise TrustError('A conflicting protected recovery credential must not be overwritten.')
        guard()
        if current is None:
            try:
                self.anchor.vault.put(self.anchor.reference, self.binding(record), expected)
            except Exception:
                raise TrustError('Protected standard recovery state could not be saved; no installer was started.') from None
        if self._read(record) != expected or self.anchor.load() != before:
            raise TrustError('Protected standard recovery readback or anchor identity changed.')
        guard()

    def observe(self, record, before, *, guard=lambda: None):
        """Bind the exact owned activation before its durable activated checkpoint."""
        if record['observed'] is None or not record['pending']['candidate_generation']:
            raise TrustError('An exact observed native generation is required.')
        initial = dict(record, observed=None,
                       pending=dict(record['pending'], candidate_generation=''))
        expected_before, expected_after = self.value(initial), self.value(record)
        current = self._read(record)
        if current not in (expected_before, expected_after) or self.anchor.load() != before:
            raise TrustError('The protected prelaunch authority changed before native observation.')
        guard()
        if current == expected_before:
            try:
                self.anchor.vault.put(self.anchor.reference, self.binding(record), expected_after)
            except Exception:
                raise TrustError('The observed activation could not be protected and remains uncommitted.') from None
        if self._read(record) != expected_after or self.anchor.load() != before:
            raise TrustError('The exact observed activation did not survive protected readback.')
        guard()

    def require(self, record):
        expected, current = self.value(record), self._read(record)
        if not isinstance(current, str) or not hmac.compare_digest(current, expected):
            raise TrustError('The prior native activation has no matching protected restart authority; '
                             'program and data are preserved.')

    def settle(self, record):
        """Delete only this exact credential after terminal state and pending cleanup."""
        expected, current = self.value(record), self._read(record)
        if current is None:
            return
        if not isinstance(current, str) or not hmac.compare_digest(current, expected):
            raise TrustError('Changed protected recovery authority is retained for owner review.')
        try:
            self.anchor.vault.delete(self.anchor.reference, self.binding(record))
        except Exception:
            raise TrustError('The completed update could not clear its own protected recovery authority.') from None
        if self._read(record) is not None:
            raise TrustError('The completed update still has protected recovery authority.')
