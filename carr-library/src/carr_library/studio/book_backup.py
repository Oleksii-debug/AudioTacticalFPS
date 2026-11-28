"""Passive Book plan, source and batch binding for existing library backups.

This module reads only the verified copied state and an already verified SQLite
snapshot. It never opens an original file, discovers a voice, resumes a job or
grants restored plans new output authority.
"""
from collections import Counter
from hashlib import sha256
from pathlib import Path
import re
import sqlite3

from ..durable_state import encode, read_json, regular

_HEX = r'[0-9a-f]{64}'
_ID = r'[0-9a-f]{32}'
BOOK = re.compile(r'state/studio/book-plans/(' + _HEX + r')/(' + _ID + r')\.json\Z')
BATCH = re.compile(r'state/studio/book-batches/(' + _HEX + r')/(' + _ID + r')\.json\Z')
SOURCE = re.compile(r'state/studio/jobs/([a-z0-9_-]{1,80})/book-source\.json\Z')
JOB = re.compile(r'studio/jobs/([a-z0-9_-]{1,80})/job\.json\Z')
MAX_PLAN_BYTES = 16 * 1024 * 1024
MAX_BATCH_BYTES = 128 * 1024
MAX_REVIEW_BYTES = 64 * 1024 * 1024
MAX_REVIEW_CHARACTERS = 8_000_000


def namespace(name):
    name = name.casefold()
    return (name.startswith(('state/studio/book-plans/', 'state/studio/book-batches/'))
            or name.startswith('state/studio/jobs/') and name.endswith('/book-source.json'))


def limit(name):
    name = name.casefold()
    if BOOK.fullmatch(name) or SOURCE.fullmatch(name):
        return MAX_PLAN_BYTES
    if BATCH.fullmatch(name):
        return MAX_BATCH_BYTES
    return None


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Book backup verification cancelled.')


def validate_document(name, value):
    """Validate the declared data shape without consulting source paths."""
    from ..backup_producers import fields, digest, number
    from .book_audio import BookPlan
    from .book_batch import BookBatch
    name = name.casefold()
    if match := BOOK.fullmatch(name):
        fields(value, {'plan', 'fingerprint'})
        plan = BookPlan.from_dict(value['plan'])
        if (plan.id != match[2] or plan.fingerprint != value['fingerprint']
                or sha256(plan.source['source_anchor']['workspace'].encode('utf-8')).hexdigest() != match[1]):
            raise ValueError('Book plan differs from its saved scope or fingerprint.')
        return plan
    if match := BATCH.fullmatch(name):
        fields(value, {'batch', 'fingerprint'})
        batch = BookBatch.from_dict(value['batch'])
        if (batch.id != match[2] or batch.fingerprint != value['fingerprint']
                or sha256(batch.workspace.encode('utf-8')).hexdigest() != match[1]):
            raise ValueError('Book batch differs from its saved scope or fingerprint.')
        return batch
    if SOURCE.fullmatch(name):
        fields(value, {'schema', 'plan', 'plan_sha256', 'source_anchor', 'source_metadata',
                       'source_topic', 'warnings', 'source_sha256', 'part', 'notice'}, {'landmarks'})
        if (value['schema'] != 'carr-book-audio-source/1' or type(value['plan']) is not str
                or not re.fullmatch(_ID, value['plan'])):
            raise ValueError('Invalid Book source receipt identity.')
        for key in ('plan_sha256', 'source_sha256'):
            digest(value[key])
        fields(value['part'], {'index', 'start', 'end', 'text_sha256'})
        number(value['part']['index'], 1, 256, integer=True)
        number(value['part']['start'], 0, 2_000_000, integer=True)
        number(value['part']['end'], value['part']['start'] + 1, 2_000_000, integer=True)
        digest(value['part']['text_sha256'])
        if value['notice'] != 'A literal reading derivative; this anchor never authorizes a quotation.':
            raise ValueError('Book source receipt authority changed.')
        if 'landmarks' in value:
            fields(value['landmarks'], {'kind', 'review_sha256', 'boundaries'})
            if (value['landmarks']['kind'] not in ('headings', 'bookmarks')
                    or type(value['landmarks']['boundaries']) is not list
                    or len(value['landmarks']['boundaries']) > 2000):
                raise ValueError('Invalid Book landmark receipt.')
            digest(value['landmarks']['review_sha256'])
        # Complete source/metadata/warning/landmark fields must equal the typed
        # retained plan in validate_state, not merely a self-consistent hash.
        return value
    raise ValueError('Unsupported Book recovery path.')


def _inventory(entries):
    by_name, plans, batches, scoped = {}, {}, [], Counter()
    for entry in entries:
        name = entry['filename'].casefold()
        if name in by_name:
            raise ValueError('Duplicate portable Book recovery identity.')
        by_name[name] = entry
        full = 'state/' + name
        if not namespace(full):
            continue
        maximum = limit(full)
        if maximum is None or type(entry['size_bytes']) is not int or not 0 < entry['size_bytes'] <= maximum:
            raise ValueError('Invalid or oversized typed Book recovery file.')
        if match := BOOK.fullmatch(full):
            if match[2] in plans:
                raise ValueError('Book plan identities collide across saved scopes.')
            plans[match[2]] = entry
            scoped[('plan', match[1])] += 1
        elif match := BATCH.fullmatch(full):
            batches.append(entry)
            scoped[('batch', match[1])] += 1
    if any(count > 4096 for count in scoped.values()):
        raise ValueError('Book plan or batch recovery exceeds its scope inventory limit.')
    return by_name, plans, batches


def _read(root, entry, *, cancelled):
    _check(cancelled)
    path = Path(root) / entry['filename']
    if regular(path).st_size != entry['size_bytes']:
        raise ValueError('A copied Book recovery file changed its declared size.')
    value = read_json(path)
    _check(cancelled)
    return value


def _plan(root, entry, cancelled):
    return validate_document('state/' + entry['filename'], _read(root, entry, cancelled=cancelled))


def _source_text(db, anchor, cancelled):
    from ..reader.reading_state import ReadingLocation
    from ..speech.reading import _validated_source_anchor
    from ..backup_producers import fields
    fields(anchor, {'workspace', 'edition', 'revision', 'asset_sha256',
                    'positions', 'quotation_authority'})
    positions = anchor['positions']
    if type(positions) is not list or not 1 <= len(positions) <= 10000:
        raise ValueError('Invalid retained Book source positions.')
    texts, characters = [], 0
    for position in positions:
        _check(cancelled)
        fields(position, {'ordinal', 'kind', 'index', 'start', 'end',
                          'text_sha256', 'text_verified_at_capture'})
        location = ReadingLocation(anchor['workspace'], anchor['edition'], anchor['revision'],
            position['kind'], position['index'], position['text_sha256'], anchor['asset_sha256'])
        predicate = ('p.physical=?' if location.kind == 'physical_page'
                     else 'p.physical IS NULL AND p.ordinal=?')
        row = db.execute("""SELECT p.raw FROM revision r
            JOIN sealed s ON s.revision=r.id JOIN page p ON p.revision=r.id
            JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition AND e.asset=r.asset
            WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=?
              AND p.ordinal=? AND p.digest=? AND length(CAST(p.raw AS BLOB))<=8000000
              AND """ + predicate,
            (location.workspace, location.edition, location.revision, location.asset_sha256,
             position['ordinal'], location.text_sha256, location.index)).fetchone()
        if row is None or type(row[0]) is not str:
            raise ValueError('Book source no longer matches its exact sealed revision.')
        characters += len(row[0]) + (2 if texts else 0)
        if characters > 2_000_000:
            raise ValueError('Book source exceeds its bounded reading snapshot.')
        texts.append(row[0])
    value = '\n\n'.join(texts)
    _validated_source_anchor(anchor, value)
    return value


def validate_database_document(db, name, value, *, cancelled=lambda: False):
    typed = validate_document(name, value)
    name = name.casefold()
    if BOOK.fullmatch(name):
        if _source_text(db, typed.source['source_anchor'], cancelled) != typed.source['text']:
            raise ValueError('Book plan text differs from the retained database source.')
    elif BATCH.fullmatch(name):
        if db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (typed.workspace,)).fetchone() is None:
            raise ValueError('Book batch belongs to a missing saved workspace.')
    else:
        if sha256(_source_text(db, value['source_anchor'], cancelled).encode('utf-8')).hexdigest() != value['source_sha256']:
            raise ValueError('Book receipt text differs from its retained database source.')
    return typed


def validate_state(root, entries, *, cancelled=lambda: False):
    """Validate exact copied plan/job/receipt membership, with bounded loading."""
    from .book_batch import validate_plans
    from .project import Project
    from ..backup_producers import _studio_job
    by_name, plans, batches = _inventory(entries)
    validated, expected_jobs = set(), {}
    for identity, entry in plans.items():
        plan = _plan(root, entry, cancelled)
        validated.add(entry['filename'].casefold())
        for index in range(1, len(plan.parts) + 1):
            _check(cancelled)
            job_id = plan.job_id(index)
            if job_id in expected_jobs:
                raise ValueError('Book plans have overlapping deterministic job identities.')
            job_entry = by_name.get('studio/jobs/' + job_id + '/job.json')
            source_entry = by_name.get('studio/jobs/' + job_id + '/book-source.json')
            if job_entry is None:
                if source_entry is not None:
                    raise ValueError('A Book source receipt has no retained production job.')
                continue  # Immutable plan may precede an interrupted submission.
            expected_jobs[job_id] = identity
            job = _read(root, job_entry, cancelled=cancelled)
            _studio_job(job, job_id)
            expected_project = plan.project(index)
            if (Project.from_dict(job['project']).to_dict() != expected_project.to_dict()
                    or job['fingerprint'] != expected_project.fingerprint
                    or job['format'] != plan.format or job['stems'] is not False):
                raise ValueError('A retained Book job differs from its immutable reviewed plan.')
            expected_source = plan.provenance(index)
            source_hash = sha256(encode(expected_source)).hexdigest()
            if source_entry is not None:
                source = _read(root, source_entry, cancelled=cancelled)
                validate_document('state/' + source_entry['filename'], source)
                if source != expected_source:
                    raise ValueError('Book source receipt differs from its immutable reviewed plan.')
                validated.add(source_entry['filename'].casefold())
            if job['status'] == 'complete' and (source_entry is None
                    or job.get('result', {}).get('book_source_sha256') != source_hash):
                raise ValueError('Completed Book output lost its exact retained source receipt.')
    for entry in batches:
        batch = _plan(root, entry, cancelled)
        selected = [plans.get(item.plan_id) for item in batch.items]
        if (any(row is None for row in selected)
                or sum(row['size_bytes'] for row in selected) > MAX_REVIEW_BYTES):
            raise ValueError('Book batch has missing plans or exceeds its retained byte budget.')
        materialized, characters = [], 0
        for row in selected:
            plan = _plan(root, row, cancelled)
            characters += len(plan.source['text'])
            if characters > MAX_REVIEW_CHARACTERS:
                raise ValueError('Book batch exceeds its retained text budget.')
            materialized.append(plan)
        validate_plans(batch, tuple(materialized))
        validated.add(entry['filename'].casefold())
    for name, entry in by_name.items():
        _check(cancelled)
        if SOURCE.fullmatch('state/' + name) and name not in validated:
            raise ValueError('Book source receipt has no matching retained reviewed plan.')
        if match := JOB.fullmatch(name):
            if match[1] in expected_jobs:
                continue
            job = _read(root, entry, cancelled=cancelled)
            if type(job) is dict and type(job.get('project')) is dict:
                project = Project.from_dict(job['project'])
                if any(line.source_ref.startswith('carr-book-audio:')
                       for scene in project.scenes for line in scene.lines):
                    raise ValueError('Book production job has no matching retained reviewed plan.')
    return validated


def validate_database_bindings(db, state_root, entries, *, cancelled=lambda: False):
    """Bind passive Book records to the existing verified copied database."""
    if not any(namespace('state/' + entry['filename']) for entry in entries):
        return
    _inventory(entries)
    for entry in entries:
        _check(cancelled)
        name = 'state/' + entry['filename']
        if namespace(name):
            validate_database_document(db, name, _read(state_root, entry, cancelled=cancelled),
                                       cancelled=cancelled)


def validate_database_snapshot(snapshot_path, state_root, entries, *, cancelled=None):
    """No-op without Book paths; never opens any live or original database."""
    cancelled = cancelled or (lambda: False)
    if not any(namespace('state/' + entry['filename']) for entry in entries):
        return
    _check(cancelled)
    snapshot = Path(snapshot_path).absolute()
    regular(snapshot)
    connection = sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        validate_database_bindings(connection, state_root, entries, cancelled=cancelled)
    finally:
        connection.close()
