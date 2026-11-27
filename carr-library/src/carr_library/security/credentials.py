"""Bind inherited trust references to the application's existing OS vault.

The portable client contract carries a profile-scoped opaque reference. Only
the local anchor adapter maps it to providers.WindowsCredentialVault's existing
reference/binding API. This module stores nothing and has no fallback backend.
"""
from hashlib import sha256
import re

from ._files import json_bytes


def validate_profile_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ValueError('Invalid trusted-client profile identity.')
    return value


def validate_reference(reference, profile_id):
    validate_profile_id(profile_id)
    if not isinstance(reference, str) or not re.fullmatch(
            r'carr/provider/' + re.escape(profile_id) + r'/[0-9a-f]{32}', reference):
        raise PermissionError('The credential reference belongs to another provider profile.')
    return reference


def anchor_vault_identity(reference, binding, *, product, profile_id):
    """Domain-separate protected state from every endpoint credential.

    The caller already validated product and installation binding. No endpoint
    secret or existing provider credential can become a protected anchor.
    """
    validate_reference(reference, profile_id)
    native_reference = 'url-api-' + reference.rsplit('/', 1)[1]
    native_binding = sha256(json_bytes({
        'kind': 'carr-protected-anchor-vault-v1',
        'product': product, 'profile_id': profile_id,
        'reference': reference, 'installation_binding': binding,
    })).hexdigest()
    return native_reference, native_binding
