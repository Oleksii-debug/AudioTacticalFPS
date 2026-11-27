"""Commit-bound import attempt receipts in the existing catalog database.

Receipts are publication evidence, never text or quotation authorization. They
contain portable identities; local request/source paths remain independently
guarded by the worker and retained-asset services.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re

from ..catalog.membership import _identity, _metadata


PUBLICATION_SCHEMA = '''
CREATE TABLE publication_receipt(
 request_id TEXT NOT NULL CHECK(length(request_id)=32 AND request_id NOT GLOB '*[^0-9a-f]*'),
 attempt_id TEXT NOT NULL CHECK(length(attempt_id)=32 AND attempt_id NOT GLOB '*[^0-9a-f]*'),
 workspace TEXT NOT NULL, edition TEXT NOT NULL, revision TEXT NOT NULL UNIQUE,
 asset TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(request_id,attempt_id),
 FOREIGN KEY(workspace,edition,asset) REFERENCES catalog_edition(workspace,id,asset),
 FOREIGN KEY(workspace,edition,revision) REFERENCES revision(workspace,edition,id)
   DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(revision) REFERENCES sealed(revision) DEFERRABLE INITIALLY DEFERRED
);
CREATE TRIGGER publication_receipt_bi BEFORE INSERT ON publication_receipt
 WHEN EXISTS(SELECT 1 FROM sealed WHERE revision=new.revision)
 BEGIN SELECT RAISE(ABORT,'receipt requires a new publication'); END;
CREATE TRIGGER publication_receipt_bu BEFORE UPDATE ON publication_receipt
 BEGIN SELECT RAISE(ABORT,'immutable publication receipt'); END;
CREATE TRIGGER publication_receipt_bd BEFORE DELETE ON publication_receipt
 BEGIN SELECT RAISE(ABORT,'immutable publication receipt'); END;
'''

MAX_IDENTITY = 64_000
MAX_RECEIPT = MAX_IDENTITY + 4096
METADATA_FIELDS = {'title', 'authors', 'language', 'edition', 'volume',
                   'collections', 'work_id', 'edition_id'}
REQUEST_FIELDS = {'version', 'request_id', 'workspace', 'metadata', 'config',
                  'expected_manifest', 'pinned_asset', 'resume_job'}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _hex(value, length):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(length) + '}', value):
        raise ValueError('Invalid publication receipt identity.')
    return value


def _manifest(value):
    from ..ingest.retained_assets import MAX_BYTES, SUFFIXES
    if (not isinstance(value, dict) or set(value) != {'sha256', 'mime', 'size'}
            or value['mime'] not in SUFFIXES or type(value['size']) is not int
            or not 0 < value['size'] <= MAX_BYTES):
        raise ValueError('Invalid publication source manifest.')
    _hex(value['sha256'], 64)
    return dict(value)


def _registration(value):
    if not isinstance(value, dict) or set(value) != METADATA_FIELDS:
        raise ValueError('Invalid publication registration.')
    result = dict(value)
    for name in ('work_id', 'edition_id'):
        result[name] = _identity(value[name])
    for name in ('title', 'language', 'edition', 'volume'):
        result[name] = _metadata(value[name])
    for name in ('authors', 'collections'):
        if not isinstance(value[name], (list, tuple)) or len(value[name]) > 100:
            raise ValueError('Invalid publication registration sequence.')
        result[name] = [(_metadata(item) if name == 'authors' else _identity(item))
                        for item in value[name]]
    if any(not author or not author.strip() for author in result['authors']):
        raise ValueError('Invalid publication author.')
    return result


def _request(value):
    from ..extract.page_ocr import Config
    if (not isinstance(value, dict) or set(value) != REQUEST_FIELDS
            or type(value['version']) is not int or value['version'] != 1):
        raise ValueError('Invalid publication request schema.')
    result = dict(value)
    _hex(value['request_id'], 32)
    _identity(value['workspace'])
    result['metadata'] = _registration(value['metadata'])
    if not isinstance(value['config'], dict):
        raise ValueError('Invalid publication configuration.')
    result['config'] = asdict(Config(**value['config']))
    if result['config'] != value['config']:
        raise ValueError('Incomplete publication configuration.')
    if value['expected_manifest'] is not None:
        result['expected_manifest'] = _manifest(value['expected_manifest'])
    for name in ('pinned_asset', 'resume_job'):
        if value[name] is not None:
            _hex(value[name], 64)
    return result


def request_identity(record):
    """Freeze the exact portable launch identity before progress can change it."""
    value = dict(version=record['version'], request_id=record['request_id'],
                 workspace=record['workspace'], metadata=record['metadata'],
                 config=record['config'], expected_manifest=record.get('expected_manifest'),
                 pinned_asset=record.get('asset_sha256'), resume_job=record.get('job'))
    result = _canonical(_request(value))
    if len(result.encode('utf-8')) > MAX_IDENTITY:
        raise ValueError('Publication request identity exceeds its limit.')
    return result


@dataclass(frozen=True)
class PublicationAttempt:
    attempt_id: str
    identity: str

    def request(self):
        _hex(self.attempt_id, 32)
        if not isinstance(self.identity, str) or len(self.identity.encode('utf-8')) > MAX_IDENTITY:
            raise ValueError('Invalid publication attempt.')
        value = _request(json.loads(self.identity))
        if _canonical(value) != self.identity:
            raise ValueError('Noncanonical publication attempt.')
        return value


def stage_receipt(db, attempt, registration, workspace, revision, asset, settings, config):
    """Insert before stage_revision in its caller-owned publication transaction.

    Deferred source FKs require a sealed revision by commit. The INSERT trigger
    rejects already sealed revisions, including an exact replay by a new attempt.
    """
    if not isinstance(attempt, PublicationAttempt) or not db.in_transaction:
        raise ValueError('Publication attempt and caller transaction required.')
    request = attempt.request()
    actual = _registration(dict(work_id=registration.work, edition_id=registration.edition_id,
        title=registration.title, authors=registration.authors, language=registration.language,
        edition=registration.edition, volume=registration.volume, collections=registration.collections))
    manifest = _manifest(registration.manifest)
    if (request['workspace'] != workspace or request['metadata'] != actual
            or request['config'] != asdict(config) or manifest['sha256'] != asset
            or request['expected_manifest'] not in (None, manifest)
            or request['pinned_asset'] not in (None, asset)
            or request['resume_job'] not in (None, revision)):
        raise ValueError('Publication differs from its request attempt.')
    payload = _canonical(dict(version=1, attempt_id=attempt.attempt_id, request=request,
        source=manifest, extraction_sha256=sha256(settings.encode('utf-8')).hexdigest()))
    if len(payload.encode('utf-8')) > MAX_RECEIPT:
        raise ValueError('Publication receipt exceeds its limit.')
    db.execute('INSERT INTO publication_receipt VALUES(?,?,?,?,?,?,?,?)',
        (request['request_id'], attempt.attempt_id, workspace, registration.edition_id,
         revision, asset, payload, sha256(payload.encode('utf-8')).hexdigest()))


def validate_receipt(db, row):
    request_id, attempt_id, workspace, edition, revision, asset, payload, digest = tuple(row)
    if (not isinstance(payload, str) or len(payload.encode('utf-8')) > MAX_RECEIPT
            or sha256(payload.encode('utf-8')).hexdigest() != digest):
        raise ValueError('Publication receipt digest mismatch.')
    value = json.loads(payload)
    if (not isinstance(value, dict) or set(value) !=
            {'version', 'attempt_id', 'request', 'source', 'extraction_sha256'}
            or type(value['version']) is not int or value['version'] != 1
            or _canonical(value) != payload):
        raise ValueError('Invalid publication receipt payload.')
    request = _request(value['request'])
    _hex(attempt_id, 32)
    _hex(value['extraction_sha256'], 64)
    manifest = _manifest(value['source'])
    if (request_id != request['request_id'] or attempt_id != value['attempt_id']
            or workspace != request['workspace'] or edition != request['metadata']['edition_id']
            or asset != manifest['sha256'] or request['expected_manifest'] not in (None, manifest)
            or request['pinned_asset'] not in (None, asset)
            or request['resume_job'] not in (None, revision)):
        raise ValueError('Publication receipt binding mismatch.')
    bound = db.execute('SELECT r.config,e.work,c.mime,c.size,m.author,m.title,m.volume,m.edition_label '
        'FROM revision r JOIN sealed s ON s.revision=r.id '
        'JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition AND e.asset=r.asset '
        'JOIN catalog_asset c ON c.digest=r.asset JOIN source_metadata m '
        'ON m.workspace=r.workspace AND m.edition=r.edition AND m.revision=r.id '
        'WHERE r.id=? AND r.workspace=? AND r.edition=? AND r.asset=?',
        (revision, workspace, edition, asset)).fetchone()
    metadata = request['metadata']
    if (bound is None or bound[1] != metadata['work_id']
            or tuple(bound[2:4]) != (manifest['mime'], manifest['size'])
            or tuple(bound[4:]) != ('; '.join(metadata['authors']) or None,
                metadata['title'] or 'Unknown title', metadata['volume'], metadata['edition'])
            or not isinstance(bound[0], str)
            or sha256(bound[0].encode('utf-8')).hexdigest() != value['extraction_sha256']
            or sha256(_canonical([workspace, edition, asset, bound[0]]).encode('utf-8')).hexdigest() != revision):
        raise ValueError('Publication receipt source mismatch.')
    return value


def check_receipts(db):
    for row in db.execute('SELECT * FROM publication_receipt'):
        validate_receipt(db, row)
