"""Revision-bound metadata snapshots for the local authorized application.

These records preserve explicitly supplied catalog attribution; they never
approve extracted text or printed pagination. Source-byte verification remains
the independent citation attestor's job. Hashes detect accidental corruption,
not adversarial rewriting of the entire private database.
"""
from dataclasses import asdict
import json

from ..citations.anchors import Identity, Metadata, digest

MAX_METADATA = 65536


def encode_metadata(metadata):
    if not isinstance(metadata, Metadata):
        raise ValueError('invalid metadata snapshot')
    for name, value in asdict(metadata).items():
        if value is None and name != 'title':
            continue
        if (not isinstance(value, str) or len(value) > 16000 or '\x00' in value
                or (name == 'title' and not value.strip())):
            raise ValueError('invalid metadata field')
    payload = json.dumps(asdict(metadata), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(payload.encode('utf-8')) > MAX_METADATA:
        raise ValueError('metadata snapshot too large')
    return payload


def decode_metadata(payload, checksum):
    if (not isinstance(payload, str) or len(payload) > MAX_METADATA
            or len(payload.encode('utf-8')) > MAX_METADATA or digest(payload) != checksum):
        raise ValueError('metadata snapshot drift')
    try:
        metadata = Metadata(**json.loads(payload))
        if encode_metadata(metadata) != payload:
            raise ValueError('noncanonical metadata snapshot')
        return metadata
    except (TypeError, KeyError, RecursionError) as exc:
        raise ValueError('invalid metadata snapshot') from exc


class MetadataSnapshots:
    def __init__(self, db, workspace):
        if not isinstance(workspace, str) or not workspace.strip():
            raise ValueError('authorized workspace required')
        self.db = db
        self.workspace = workspace

    def _ready(self, identity):
        if (not self.db.in_transaction
                or self.db.execute('PRAGMA foreign_keys').fetchone()[0] != 1
                or self.db.execute('PRAGMA recursive_triggers').fetchone()[0] != 1
                or self.db.execute('PRAGMA read_uncommitted').fetchone()[0]):
            raise ValueError('consistent transaction with integrity guards required')
        if not isinstance(identity, Identity) or identity.workspace != self.workspace:
            raise ValueError('cross-workspace metadata')
        return (self.workspace, identity.edition_id, identity.asset_sha256, identity.revision)

    def stage(self, identity, metadata):
        """Record caller-approved metadata after staging its sealed revision."""
        key = self._ready(identity)
        payload = encode_metadata(metadata)
        self.db.execute('INSERT INTO metadata_snapshot '
                        '(workspace,edition,asset,revision,payload,checksum) VALUES(?,?,?,?,?,?)',
                        (*key, payload, digest(payload)))

    def load(self, identity, *, include_revoked=False):
        key = self._ready(identity)
        if type(include_revoked) is not bool:
            raise ValueError('explicit revocation flag required')
        row = self.db.execute('SELECT payload,checksum,active FROM metadata_snapshot '
                              'WHERE workspace=? AND edition=? AND asset=? AND revision=?', key).fetchone()
        if row is None or (row[2] != 1 and not include_revoked):
            raise ValueError('missing or revoked metadata snapshot')
        return decode_metadata(row[0], row[1])

    def attest(self, identity, metadata):
        try:
            return self.load(identity) == metadata
        except (LookupError, ValueError):
            return False

    def revoke(self, identity):
        key = self._ready(identity)
        if self.db.execute('UPDATE metadata_snapshot SET active=0 '
                           'WHERE workspace=? AND edition=? AND asset=? AND revision=?', key).rowcount != 1:
            raise ValueError('unknown metadata snapshot')
