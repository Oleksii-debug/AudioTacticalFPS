"""Data-only identity contract shared by the trusted updater and fixed setup.

This file is copied unchanged into the standard setup helper inventory. It
does not authenticate a publisher or grant installation authority. Approval
must still come from the existing authenticated delivery provider. The proof
binds that approval to one captured installation while its actual exclusive
maintenance lease remains held.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


PROOF_SCHEMA = 'carr-managed-upgrade/1'
PROOF_FILE = 'upgrade-proof.json'
MAX_PROOF = 16 * 1024


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate managed update identity field.')
        result[key] = value
    return result


def digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid managed update checksum.')
    return value


def encode_proof(value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    decode_proof(raw)
    return raw


def decode_proof(raw, expected_sha256=None):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PROOF:
        raise ValueError('Managed update identity exceeds its byte budget.')
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != digest(expected_sha256):
        raise ValueError('The captured managed update identity changed.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite update identity.')))
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid managed update identity.') from error
    if (type(value) is not dict or set(value) != {'schema', 'transaction', 'root', 'old', 'new'}
            or value['schema'] != PROOF_SCHEMA
            or not isinstance(value['transaction'], str)
            or not re.fullmatch('[0-9a-f]{32}', value['transaction'])
            or not isinstance(value['root'], str) or not 0 < len(value['root']) <= 32768
            or any(ord(character) < 32 for character in value['root'])):
        raise ValueError('Unsupported managed update identity.')
    path = Path(value['root'])
    if not path.is_absolute() or '..' in path.parts or path == Path(path.anchor):
        raise ValueError('Managed update identity needs one absolute local installation.')
    if (type(value['old']) is not dict
            or set(value['old']) != {'bundle_sha256', 'setup_sha256', 'owner_sha256'}
            or type(value['new']) is not dict
            or set(value['new']) != {'bundle_sha256', 'setup_sha256', 'source_commit', 'version'}):
        raise ValueError('Invalid old or new managed installation identity.')
    for checksum in value['old'].values():
        digest(checksum)
    for field in ('bundle_sha256', 'setup_sha256'):
        digest(value['new'][field])
    if (not isinstance(value['new']['source_commit'], str)
            or not re.fullmatch('[0-9a-f]{40}', value['new']['source_commit'])
            or not isinstance(value['new']['version'], str)
            or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}(?:[a-z0-9.]*)?', value['new']['version'])):
        raise ValueError('Invalid managed update source or version.')
    if value['old']['bundle_sha256'] == value['new']['bundle_sha256']:
        raise ValueError('Use Repair for the same managed application bundle.')
    return value


def previous_path(proof):
    return Path(proof['root']).parent / ('.resonance-previous-' + proof['transaction'])


def failed_path(proof):
    return Path(proof['root']).parent / ('.resonance-failed-' + proof['transaction'])


def require_binding(proof, root, bundle_sha256, setup_sha256):
    if (Path(proof['root']) != Path(root).absolute()
            or proof['new']['bundle_sha256'] != digest(bundle_sha256)
            or proof['new']['setup_sha256'] != digest(setup_sha256)):
        raise ValueError('The managed update approval belongs to another installation or setup.')
