"""Passive, manifest-bound closure for saved book-audio plans.

Only the already copied state and, for complete backups, the private copied
SQLite snapshot are inspected. Old output paths, voices and runtimes are inert.
Standalone state snapshots validate linkage without acquiring database authority.
"""
from hashlib import sha256
from pathlib import Path
import re

from .durable_state import encode, local_path

_PLAN = re.compile(r'studio/book-plans/([0-9a-f]{64})/([0-9a-f]{32})\.json\Z')
_RECEIPT = re.compile(r'studio/jobs/([A-Za-z0-9_-]{1,80})/book-source\.json\Z')
_JOB = re.compile(r'studio/jobs/([A-Za-z0-9_-]{1,80})/(job|project|timeline|book-source)\.json\Z', re.IGNORECASE)
_PROJECT = re.compile(r'studio/projects/([A-Za-z0-9_-]{1,80})/(?:project\.json|revisions/([0-9]{8})-([0-9a-f]{64})\.json)\Z', re.IGNORECASE)
MAX_RECORD_BYTES = 32 * 1024 * 1024


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Book-audio backup validation cancelled.')


def limit(name):
    if name.startswith('state/') and (_PLAN.fullmatch(name[6:]) or _RECEIPT.fullmatch(name[6:])):
        from .studio.book_audio import MAX_PLAN_BYTES
        return MAX_PLAN_BYTES
    return None


def validate_record(name, value):
    """Validate a record's own type; collection validation supplies its links."""
    from .studio.book_audio import validate_saved_plan
    relative = name[6:] if name.startswith('state/') else name
    if match := _PLAN.fullmatch(relative):
        validate_saved_plan(value, namespace=match[1], identity=match[2])
    elif _RECEIPT.fullmatch(relative):
        if type(value) is not dict or value.get('schema') != 'carr-book-audio-source/1':
            raise ValueError('Invalid saved book source receipt.')
    else:
        raise ValueError('Unknown saved book-audio record.')


def collection_budgets(entries):
    from .studio.book_audio import MAX_PLAN_BYTES, MAX_SAVED_PLANS
    counts = {}
    for entry in entries:
        name = entry['filename']
        name = name[6:] if name.startswith('state/') else name
        if not name.casefold().startswith('studio/book-plans/'):
            continue
        match = _PLAN.fullmatch(name)
        if match is None or not 1 <= entry['size_bytes'] <= MAX_PLAN_BYTES:
            raise ValueError('Invalid bounded book plan inventory.')
        counts[match[1]] = counts.get(match[1], 0) + 1
        if counts[match[1]] > MAX_SAVED_PLANS:
            raise ValueError('Book plans exceed their per-workspace collection budget.')


def _source(db, plan, cancelled):
    """Bind historical source text to this copied DB, without live approval."""
    if db is None:
        return
    from .backup_sidecars import validate_locations
    from .reader.reading_state import ReadingLocation
    source = plan.source
    anchor = source['source_anchor']
    row = db.execute('SELECT author,title,volume,edition_label FROM source_metadata '
                     'WHERE workspace=? AND edition=? AND revision=?',
                     (anchor['workspace'], anchor['edition'], anchor['revision'])).fetchone()
    if (row is None or dict(zip(('author', 'title', 'volume', 'edition'), tuple(row))) != source['source_metadata']
            or source['title'] != (row[1] or 'Unknown title')):
        raise ValueError('Book source metadata differs from the copied library.')
    if source['source_topic'] is not None and db.execute(
            'SELECT 1 FROM research_topic WHERE workspace=? AND id=?',
            (anchor['workspace'], source['source_topic'])).fetchone() is None:
        raise ValueError('Book source topic differs from its copied library.')
    for position in anchor['positions']:
        _check(cancelled)
        location = ReadingLocation(anchor['workspace'], anchor['edition'], anchor['revision'],
            position['kind'], position['index'], position['text_sha256'], anchor['asset_sha256'])
        validate_locations(db, (location,))
        original = source['text'][position['start']:position['end']]
        # Bound text materialization by the already validated 2M-character plan.
        row = db.execute('SELECT physical,digest,raw FROM page WHERE revision=? AND ordinal=? '
                         "AND typeof(raw)='text' AND length(raw)=?",
                         (anchor['revision'], position['ordinal'], len(original))).fetchone()
        physical = position['index'] if position['kind'] == 'physical_page' else None
        if (row is None or tuple(row) != (physical, position['text_sha256'], original)
                or sha256(original.encode('utf-8')).hexdigest() != position['text_sha256']):
            raise ValueError('Book source position differs from the copied library.')
    # Capture-time extraction flags stay historical. Backup never creates
    # quotation approval or the current local Studio execution nonce.


def _marked(value, kind):
    if type(value) is not dict:
        return False
    if kind == 'book-source':
        return True
    if kind == 'timeline':
        return 'book_source_sha256' in value
    declaration = value.get('project') if kind == 'job' else value
    if type(declaration) is dict and type(declaration.get('scenes')) is list:
        for scene in declaration['scenes']:
            if type(scene) is not dict or type(scene.get('lines')) is not list:
                continue
            if any(type(line) is dict and isinstance(line.get('source_ref'), str)
                   and line['source_ref'].startswith('carr-book-audio:') for line in scene['lines']):
                return True
    if kind == 'job':
        if type(value.get('result')) is dict and 'book_source_sha256' in value['result']:
            return True
        recovery = value.get('recovery')
        if type(recovery) is dict and type(recovery.get('previous')) is list:
            return any(type(previous) is dict and type(previous.get('result')) is dict
                       and 'book_source_sha256' in previous['result'] for previous in recovery['previous'])
    return False


def validate_collection(db, copied_state_root, entries, *, cancelled=lambda: False):
    """Require exact plan/project/receipt links using only copied manifest bytes.

    Zero jobs (or only a project saved before interrupted submission) is valid.
    Every existing book-marked job or derivative still needs its immutable plan.
    """
    from .backup_sidecars import _json, read_regular
    from .backup_producers import _studio_job, _timeline
    from .studio.book_audio import MAX_PLAN_BYTES, validate_saved_plan
    root = Path(copied_state_root)
    entries = tuple(entries)
    collection_budgets(entries)
    inventory = {entry['filename']: entry for entry in entries}
    if len(inventory) != len(entries):
        raise ValueError('Duplicate copied book-state inventory path.')
    plans, targets = [], {}
    for name, entry in inventory.items():
        _check(cancelled)
        if match := _PLAN.fullmatch(name):
            plans.append((name, match[1], match[2]))
        elif match := _JOB.fullmatch(name):
            targets.setdefault(match[1], []).append((name, match[2].lower(), None))
        elif match := _PROJECT.fullmatch(name):
            targets.setdefault(match[1], []).append((name, 'stored-project', match))

    def raw_record(name, maximum=MAX_RECORD_BYTES):
        _check(cancelled)
        entry = inventory[name]
        if not 1 <= entry['size_bytes'] <= maximum:
            raise ValueError('Copied book state exceeds its record budget.')
        path = local_path(root / name, root=root, exists=True)
        raw = read_regular(path, maximum)
        _check(cancelled)
        if len(raw) != entry['size_bytes'] or sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Copied book state differs from its verified manifest.')
        return raw

    def read(name, maximum=MAX_RECORD_BYTES):
        return _json(raw_record(name, maximum))

    checked = set()
    for name, namespace, identity in sorted(plans):
        _check(cancelled)
        plan = validate_saved_plan(read(name, MAX_PLAN_BYTES), namespace=namespace, identity=identity)
        _source(db, plan, cancelled)
        for index in range(1, len(plan.parts) + 1):
            _check(cancelled)
            job_id = plan.job_id(index)
            found = targets.get(job_id, ())
            if not found:
                continue
            if job_id in checked:
                raise ValueError('Two saved book plans claim the same Studio job.')
            expected_project = plan.project(index)
            expected = expected_project.to_dict()
            provenance = plan.provenance(index)
            receipt_digest = sha256(encode(provenance)).hexdigest()
            prefix = 'studio/jobs/' + job_id + '/'
            job_name = prefix + 'job.json'
            job = None

            def result(value):
                _timeline(value)
                if (value.get('book_source_sha256') != receipt_digest
                        or value['project'] != expected_project.id
                        or value['revision'] != expected_project.revision
                        or value['source_sha256'] != sha256(expected_project.original_script.encode('utf-8')).hexdigest()):
                    raise ValueError('Book result differs from its exact saved source receipt.')

            for target, kind, revision in found:
                _check(cancelled)
                value = read(target, MAX_PLAN_BYTES if kind == 'book-source' else MAX_RECORD_BYTES)
                if kind == 'job':
                    if target != job_name:
                        raise ValueError('Book job path differs from its canonical identity.')
                    _studio_job(value, job_id)
                    if (value['project'] != expected or value['fingerprint'] != expected_project.fingerprint
                            or value['format'] != plan.format or value['stems'] is not False):
                        raise ValueError('Book job differs from its immutable saved plan.')
                    job = value
                    if 'result' in value:
                        result(value['result'])
                    for previous in value.get('recovery', {}).get('previous', []):
                        if previous['result'] is not None:
                            result(previous['result'])
                elif kind in ('project', 'stored-project'):
                    if value != expected:
                        raise ValueError('Book project differs from its immutable saved plan.')
                    if kind == 'project' and target != prefix + 'project.json':
                        raise ValueError('Book project export path changed.')
                    if revision is not None:
                        current = 'studio/projects/' + job_id + '/project.json'
                        historical = ('studio/projects/' + job_id + '/revisions/' +
                                      f'{expected_project.revision:08d}-{expected_project.fingerprint}.json')
                        if target not in (current, historical):
                            raise ValueError('Book project revision path changed.')
                elif kind == 'timeline':
                    if target != prefix + 'timeline.json':
                        raise ValueError('Book timeline path changed.')
                    result(value)
                else:
                    if target != prefix + 'book-source.json' or value != provenance:
                        raise ValueError('Book source receipt differs from its immutable saved plan.')
            derivatives = {'project', 'timeline', 'book-source'}
            if job is None and any(kind in derivatives for _, kind, _ in found):
                raise ValueError('A generated book derivative has no saved Studio job.')
            if job is not None and job['status'] == 'complete':
                required = {prefix + leaf for leaf in ('project.json', 'timeline.json', 'book-source.json')}
                if 'result' not in job or not required <= inventory.keys():
                    raise ValueError('Completed book audio is missing its retained source receipts.')
            checked.add(job_id)

    for identity, found in targets.items():
        if identity in checked:
            continue
        for name, kind, _ in found:
            _check(cancelled)
            if kind == 'book-source':
                raise ValueError('A book source receipt has no matching immutable saved plan.')
            entry = inventory[name]
            if not 1 <= entry['size_bytes'] <= MAX_RECORD_BYTES:
                # Unrelated opaque historical state stays an archive.
                continue
            raw = raw_record(name)
            try:
                value = _json(raw)
            except (UnicodeError, ValueError, RecursionError):
                # Preserve unrelated opaque historical state; actual regular
                # bytes and manifest identity were checked before decoding.
                continue
            if _marked(value, kind):
                raise ValueError('A book-marked Studio record has no matching immutable saved plan.')
    _check(cancelled)
