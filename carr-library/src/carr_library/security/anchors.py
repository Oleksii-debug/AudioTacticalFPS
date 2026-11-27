"""Protected monotonic state supplied by the installation owner.

A JSON file beside the license is not a rollback-resistant anchor. Managed
clients require a protected store. The existing Windows vault has no plaintext
fallback; missing state requires owner recovery rather than reprovisioning.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Protocol

from ..assistant.credentials import WindowsCredentialVault, validate_profile_id, validate_reference
from ._files import TrustError, json_bytes, json_value


class AnchorUnavailable(TrustError):
    pass


@dataclass(frozen=True)
class Anchor:
    sequence: int = 0
    digest: str | None = None
    max_utc: int = 0

    def __post_init__(self):
        if (type(self.sequence) is not int or not 0 <= self.sequence <= 2**53 - 1
                or type(self.max_utc) is not int or not 0 <= self.max_utc <= 4102444800
                or (self.digest is not None and
                    (not isinstance(self.digest, str) or not re.fullmatch('[0-9a-f]{64}', self.digest)))
                or (self.sequence > 0 and self.digest is None)):
            raise TrustError('Invalid protected state anchor.')

    def encode(self):
        return json_bytes({'version': 1, **asdict(self)})

    @classmethod
    def decode(cls, raw):
        value = json_value(raw, maximum=1024)
        if (type(value) is not dict or set(value) != {'version', 'sequence', 'digest', 'max_utc'}
                or type(value['version']) is not int or value['version'] != 1):
            raise TrustError('Invalid protected state anchor.')
        return cls(value['sequence'], value['digest'], value['max_utc'])


class AnchorStore(Protocol):
    def load(self) -> Anchor: ...
    def compare_and_set(self, expected: Anchor, replacement: Anchor, *, guard=lambda: None) -> None: ...


class WindowsVaultAnchor:
    """Use a product/profile-bound value in the canonical Windows credential vault.

    Owner setup explicitly provisions encode(Anchor()) at the supplied opaque
    reference. This class never creates or resets a missing credential. Callers
    serialize load/compare/advance under their cross-process client-store lock.
    One reference and binding belong to one stable client state root.
    """
    def __init__(self, reference: str, binding: str, *, product: str,
                 profile_id: str, vault=None):
        if (not isinstance(product, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', product)
                or not isinstance(binding, str) or not re.fullmatch(r'[0-9a-f]{64}', binding)):
            raise TrustError('Invalid protected anchor identity.')
        try:
            validate_profile_id(profile_id)
            validate_reference(reference, profile_id)
        except (ValueError, PermissionError):
            raise TrustError('The protected anchor reference belongs to another profile.') from None
        self.reference, self.binding = reference, binding
        self.product, self.profile_id = product, profile_id
        self.vault = vault if vault is not None else WindowsCredentialVault()

    def encode(self, anchor: Anchor) -> str:
        """Return a bound value for explicit owner provisioning; do not write it."""
        if type(anchor) is not Anchor:
            raise TrustError('A validated protected state anchor is required.')
        return json_bytes({'version': 1, 'kind': 'carr-protected-anchor',
                           'product': self.product, 'profile_id': self.profile_id,
                           'binding': self.binding, 'anchor': asdict(anchor)}).decode('ascii')

    def load(self):
        try:
            raw = self.vault.get(self.profile_id, self.reference).encode('ascii')
            value = json_value(raw, maximum=2048)
            if (type(value) is not dict
                    or set(value) != {'version', 'kind', 'product', 'profile_id', 'binding', 'anchor'}
                    or type(value['version']) is not int or value['version'] != 1
                    or value['kind'] != 'carr-protected-anchor'
                    or (value['product'], value['profile_id'], value['binding']) !=
                       (self.product, self.profile_id, self.binding)
                    or type(value['anchor']) is not dict
                    or set(value['anchor']) != {'sequence', 'digest', 'max_utc'}):
                raise TrustError('Protected client identity does not match.')
            return Anchor(**value['anchor'])
        except Exception:
            raise AnchorUnavailable('Protected client state is unavailable. Owner recovery is required; data access remains available.') from None

    def compare_and_set(self, expected, replacement, *, guard=lambda: None):
        if type(expected) is not Anchor or type(replacement) is not Anchor:
            raise TrustError('Validated protected state anchors are required.')
        if self.load() != expected:
            raise AnchorUnavailable('Protected client state changed. Retry after the other operation finishes.')
        if replacement.sequence < expected.sequence or replacement.max_utc < expected.max_utc:
            raise TrustError('Protected state cannot move backwards.')
        # The vault read above can block. Check cancellation/deadline again at
        # the final reversible boundary before the protected value advances.
        guard()
        try:
            self.vault.put(self.profile_id, self.encode(replacement), reference=self.reference)
        except Exception:
            raise AnchorUnavailable('Could not publish protected client state. The previous grant remains recoverable.') from None
