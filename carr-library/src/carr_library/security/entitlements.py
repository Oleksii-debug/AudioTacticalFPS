"""Versioned, signed public entitlement documents and crash-safe acceptance.

This is a neutral client envelope, not an issuer or a billing system. Ed25519
verification is provided by PyCA; no private signing key is shipped. Publisher
keys and accepted policy versions are installation inputs, never read from the
document being authenticated. Existing private operation stays perpetual.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Mapping

from ..delivery_contract import USER_DATA_ACTIONS
from ._files import TrustError, atomic_write, directory, json_value, locked, read_bytes, sync_directory
from .anchors import Anchor, AnchorStore

MAX_ENVELOPE = 65536
CONTEXT = b'CARR-ENTITLEMENT-v1\x00'
# Keep the canonical exact action names. The short names remain compatible
# with the independently reusable client; neither set uses prefix matching.
PROTECTED = USER_DATA_ACTIONS | frozenset({'read', 'export', 'backup', 'recovery', 'settings', 'prior-output'})
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


class ReviewRequired(TrustError):
    pass


class ReplayError(TrustError):
    pass


def _identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise TrustError('Invalid entitlement identity.')
    return value


def _integer(value, *, minimum=0, maximum=4102444800):
    if type(value) is not int or not minimum <= value <= maximum:
        raise TrustError('Invalid entitlement integer.')
    return value


def _b64(value, size=None):
    if not isinstance(value, str):
        raise TrustError('Invalid signed envelope encoding.')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise TrustError('Invalid signed envelope encoding.') from None
    if (size is not None and len(raw) != size) or base64.b64encode(raw).decode('ascii') != value:
        raise TrustError('Invalid signed envelope encoding.')
    return raw


@dataclass(frozen=True)
class PublicKey:
    raw: bytes
    generation: int = 1

    def __post_init__(self):
        if not isinstance(self.raw, bytes) or len(self.raw) != 32:
            raise TrustError('Ed25519 public keys must contain 32 bytes.')
        _integer(self.generation, minimum=1, maximum=1_000_000)

    @property
    def key_id(self):
        return hashlib.sha256(self.raw).hexdigest()


@dataclass(frozen=True)
class TrustPolicy:
    product: str
    subject: str
    device: str | None
    keys: tuple[PublicKey, ...]
    policy_versions: frozenset[str]
    features: frozenset[str]
    minimum_key_generation: int = 1

    def __post_init__(self):
        _identity(self.product)
        _identity(self.subject)
        if self.device is not None:
            _identity(self.device)
        if not 1 <= len(self.keys) <= 16 or len({key.key_id for key in self.keys}) != len(self.keys):
            raise TrustError('Supply a bounded, independently trusted public keyring.')
        if not self.policy_versions or len(self.policy_versions) > 16 or len(self.features) > 128:
            raise TrustError('Invalid owner policy configuration.')
        for value in (*self.policy_versions, *self.features):
            _identity(value)
        _integer(self.minimum_key_generation, minimum=1, maximum=1_000_000)


@dataclass(frozen=True)
class Entitlement:
    product: str
    entitlement_id: str
    subject: str
    device: str | None
    sequence: int
    profile: str
    issued_at: int
    not_before: int
    expires_at: int | None
    offline_until: int | None
    refresh_interval: int | None
    policy_version: str
    terms_sha256: str
    features: frozenset[str]
    revoked: bool
    request_nonce: str | None
    key_id: str
    key_generation: int

    @property
    def needs_refresh(self):
        return self.refresh_interval is not None and not self.revoked


@dataclass(frozen=True)
class Status:
    mode: str
    message: str
    features: frozenset[str] = PROTECTED
    sequence: int = 0
    expires_at: int | None = None
    next_refresh: int | None = None

    def permits(self, feature):
        return feature in PROTECTED or feature in self.features or self.mode == 'private-perpetual'


def private_status():
    return Status('private-perpetual',
        'Private perpetual installation. Local operation does not require an account or periodic connection. '
        'Reading, export, backups, recovery and existing outputs remain available.')


@dataclass(frozen=True)
class OperationLease:
    """An authorization captured at operation start, not polled mid-save/render."""
    feature: str
    sequence: int


def verify_envelope(raw: bytes, policy: TrustPolicy, *, now: int,
                    allow_expired=False, expected_nonce=None, historical=False) -> Entitlement:
    value = json_value(raw, maximum=MAX_ENVELOPE)
    if (type(value) is not dict or set(value) != {'version', 'kind', 'key_id', 'payload', 'signature'}
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'carr-entitlement'):
        raise TrustError('Unsupported signed entitlement envelope.')
    key = next((item for item in policy.keys if item.key_id == value['key_id']), None)
    if key is None:
        raise TrustError('The entitlement was signed by an unknown publisher key.')
    if not historical and key.generation < policy.minimum_key_generation:
        raise TrustError('This signing key has been retired for new entitlements.')
    payload, signature = _b64(value['payload']), _b64(value['signature'], 64)
    if len(payload) > 32768:
        raise TrustError('Entitlement payload exceeds its size budget.')
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(key.raw).verify(signature, CONTEXT + payload)
    except InvalidSignature:
        raise TrustError('Entitlement signature verification failed.') from None
    item = json_value(payload, maximum=32768)
    fields = {'schema', 'product', 'entitlement_id', 'subject', 'device', 'sequence', 'profile',
              'issued_at', 'not_before', 'expires_at', 'offline_until', 'refresh_interval',
              'policy_version', 'terms_sha256', 'features', 'revoked', 'request_nonce'}
    if type(item) is not dict or set(item) != fields or type(item['schema']) is not int or item['schema'] != 1:
        raise TrustError('Unsupported entitlement schema.')
    for name in ('product', 'entitlement_id', 'subject', 'policy_version'):
        _identity(item[name])
    if item['device'] is not None:
        _identity(item['device'])
    if (item['product'], item['subject'], item['device']) != (policy.product, policy.subject, policy.device):
        raise TrustError('Entitlement product, subject or device binding does not match.')
    if item['policy_version'] not in policy.policy_versions:
        raise ReviewRequired('The owner policy version is unsupported; explicit review is required.')
    if not isinstance(item['terms_sha256'], str) or not DIGEST.fullmatch(item['terms_sha256']):
        raise TrustError('Invalid terms identity.')
    _integer(item['sequence'], minimum=1, maximum=2**53 - 1)
    _integer(now)
    for name in ('issued_at', 'not_before'):
        _integer(item[name], minimum=1)
    if item['issued_at'] > now + 300 or item['not_before'] > now:
        raise TrustError('The entitlement is not valid yet; check the system clock.')
    if item['profile'] not in ('perpetual', 'renewable', 'evaluation') or type(item['revoked']) is not bool:
        raise TrustError('Invalid entitlement profile.')
    if item['profile'] == 'perpetual':
        if any(item[name] is not None for name in ('expires_at', 'offline_until', 'refresh_interval')):
            raise TrustError('A perpetual grant cannot require periodic renewal.')
    else:
        _integer(item['expires_at'], minimum=1)
        _integer(item['offline_until'], minimum=1)
        if not item['issued_at'] <= item['expires_at'] or not item['not_before'] < item['expires_at'] <= item['offline_until']:
            raise TrustError('Invalid entitlement validity interval.')
        if not allow_expired and item['expires_at'] <= now:
            raise TrustError('An expired renewal cannot replace the current grant.')
        if item['refresh_interval'] is not None:
            _integer(item['refresh_interval'], minimum=60, maximum=30 * 86400)
    features = item['features']
    if (type(features) is not list or len(features) > 128
            or any(not isinstance(feature, str) for feature in features)
            or len(set(features)) != len(features) or not set(features) <= policy.features):
        raise TrustError('Entitlement contains unsupported or duplicate features.')
    nonce = item['request_nonce']
    if nonce is not None and (not isinstance(nonce, str) or not re.fullmatch('[0-9a-f]{32}', nonce)):
        raise TrustError('Invalid renewal nonce.')
    if expected_nonce is not None and nonce != expected_nonce:
        raise ReplayError('Renewal response does not match the outstanding request.')
    return Entitlement(**{name: item[name] for name in fields - {'schema', 'features'}},
                       features=frozenset(features), key_id=key.key_id, key_generation=key.generation)


class EntitlementStore:
    """Retain the last verified token; reject replay using an OS-protected anchor.

    Commit order is pending file -> protected anchor -> current file. Recovery
    only publishes a pending file whose digest equals the protected anchor.
    Untrusted responses never replace the current token. Losing both files
    requires owner recovery; deleting the cache never becomes a fresh license.
    """
    def __init__(self, root, policy: TrustPolicy, anchor: AnchorStore, *, clock=time.time):
        self.root = directory(root, create=True)
        self.policy, self.anchor, self.clock = policy, anchor, clock
        self.current_path = self.root / 'entitlement.json'
        self.pending_path = self.root / 'entitlement.pending.json'

    def _now(self):
        return _integer(int(self.clock()))

    def _read_current(self, anchor, now):
        for path in (self.current_path, self.pending_path):
            if os.path.lexists(path):
                raw = read_bytes(path, MAX_ENVELOPE)
                if hashlib.sha256(raw).hexdigest() == anchor.digest:
                    grant = verify_envelope(raw, self.policy, now=now, allow_expired=True, historical=True)
                    if grant.sequence != anchor.sequence:
                        raise ReplayError('Protected entitlement sequence does not match the signed grant.')
                    if path == self.pending_path:
                        atomic_write(self.current_path, raw)
                        self.pending_path.unlink()
                        sync_directory(self.root)
                    return grant, raw
        if anchor.sequence:
            raise ReplayError('The saved grant is missing or older than protected client state. Owner recovery is required.')
        if os.path.lexists(self.current_path):
            raise ReplayError('An unanchored saved grant cannot authorize this installation.')
        return None, None

    @staticmethod
    def _clock(anchor, now):
        if now + 300 < anchor.max_utc:
            raise TrustError('The system clock moved backwards. Correct it or use owner recovery; data access remains available.')

    def current(self):
        with locked(self.root):
            anchor, now = self.anchor.load(), self._now()
            self._clock(anchor, now)
            return self._read_current(anchor, now)[0]

    def preview(self, raw):
        grant = verify_envelope(raw, self.policy, now=self._now())
        return MappingProxyType({
            'sha256': hashlib.sha256(raw).hexdigest(), 'product': grant.product,
            'profile': grant.profile, 'sequence': grant.sequence, 'features': tuple(sorted(grant.features)),
            'expires_at': grant.expires_at, 'offline_until': grant.offline_until,
            'terms_sha256': grant.terms_sha256, 'policy_version': grant.policy_version,
            'key_id': grant.key_id, 'revoked': grant.revoked,
        })

    def accept(self, raw, *, reviewed_digest=None, accepted_terms=None, expected_nonce=None,
               guard=lambda: None):
        guard()
        automatic = expected_nonce is not None
        digest = hashlib.sha256(raw).hexdigest()
        if not automatic and reviewed_digest != digest:
            raise ReviewRequired('Review this exact signed file before importing it.')
        with locked(self.root):
            previous, now = self.anchor.load(), self._now()
            self._clock(previous, now)
            old, _ = self._read_current(previous, now)
            grant = verify_envelope(raw, self.policy, now=now, expected_nonce=expected_nonce)
            if grant.sequence < previous.sequence or (grant.sequence == previous.sequence and digest != previous.digest):
                raise ReplayError('An older or conflicting entitlement cannot replace protected state.')
            if automatic and grant.sequence <= previous.sequence:
                raise ReplayError('An automatic renewal must advance the signed sequence.')
            if old and grant.entitlement_id != old.entitlement_id:
                raise ReviewRequired('Changing entitlement identity requires separate owner recovery.')
            if automatic:
                if old is None:
                    raise ReviewRequired('An initial entitlement requires explicit import and terms review.')
                if (grant.terms_sha256, grant.policy_version, grant.profile) != (
                        old.terms_sha256, old.policy_version, old.profile):
                    raise ReviewRequired('Renewal changed the terms or profile. The last verified grant is retained for review.')
                if not grant.revoked and not old.features <= grant.features:
                    raise ReviewRequired('Renewal removes features. Explicit review is required.')
                if grant.key_generation < old.key_generation:
                    raise ReplayError('Renewal cannot return to an older signing-key generation.')
            elif (old is None or grant.terms_sha256 != old.terms_sha256) and accepted_terms != grant.terms_sha256:
                raise ReviewRequired('Explicitly accept the reviewed terms identity before importing.')
            guard()
            if digest == previous.digest:
                return grant
            replacement = Anchor(grant.sequence, digest, max(now, previous.max_utc))
            atomic_write(self.pending_path, raw)
            guard()
            self.anchor.compare_and_set(previous, replacement, guard=guard)
            # Once protected state advances, finish its recoverable publication
            # even if shutdown arrives. Never mislabel a committed grant.
            atomic_write(self.current_path, raw)
            self.pending_path.unlink()
            sync_directory(self.root)
            return grant

    def status(self):
        try:
            with locked(self.root):
                anchor, now = self.anchor.load(), self._now()
                self._clock(anchor, now)
                grant, _ = self._read_current(anchor, now)
                if now >= anchor.max_utc + 60:
                    self.anchor.compare_and_set(anchor, Anchor(anchor.sequence, anchor.digest, now))
                if grant is None:
                    return Status('needs-import', 'No managed grant has been approved. Reading, export, backup and recovery remain available.')
                allowed = PROTECTED | grant.features
                due = grant.issued_at + grant.refresh_interval if grant.needs_refresh else None
                if grant.revoked:
                    return Status('revoked', 'A signed revocation stops new managed operations. Existing work and data access remain available.',
                                  PROTECTED, grant.sequence, grant.expires_at, due)
                if grant.expires_at is None or now < grant.expires_at:
                    return Status('valid', 'The signed entitlement is valid.', allowed, grant.sequence, grant.expires_at, due)
                if now < grant.offline_until:
                    return Status('offline-grace', 'Refresh is unavailable or overdue. The signed offline grace remains valid.',
                                  allowed, grant.sequence, grant.expires_at, due)
                return Status('expired', 'The managed entitlement needs renewal. Reading, export, backup, recovery and prior outputs remain available.',
                              PROTECTED, grant.sequence, grant.expires_at, due)
        except (TrustError, OSError):
            return Status('attention', 'Signed client state requires review or owner recovery. Reading, export, backup and recovery remain available.')

    def begin(self, feature):
        status = self.status()
        if not status.permits(feature):
            raise ReviewRequired('This new managed operation requires a valid entitlement. Existing work and data remain available.')
        return OperationLease(feature, status.sequence)
