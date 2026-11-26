"""Stable identity for the exact SQLite layout accepted by revision storage.

The numeric user_version alone cannot distinguish unreleased layouts that used
the same version. Hash the existing, ordered sqlite_master signature together
with that version. This intentionally preserves Storage's exact SQL comparison:
it is neither a schema normalizer nor permission to migrate an unknown layout.
Database contents, root-page numbers and filesystem paths are excluded.
"""
from hashlib import sha256
import json

from .revisions import _expected_signature, _schema_signature


def _fingerprint(version, signature):
    payload = dict(format='carr-sqlite-schema-v1', user_version=version,
                   sqlite_master=signature)
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return sha256(raw).hexdigest()


def schema_fingerprint(db):
    """Identify a stable database snapshot without writing to it."""
    version = db.execute('PRAGMA user_version').fetchone()[0]
    return _fingerprint(version, _schema_signature(db))


def expected_schema_fingerprint(version):
    """Identify the current canonical schema for an explicit storage version."""
    return _fingerprint(version, _expected_signature(version))
