"""Passive reviewed translation-batch metadata over the existing child codec.

Only the known immutable batch manifest and its pause/run control are admitted.
No provider, credential store, source parser, HTTP request or task owner is
constructed. Existing translation children and usage files keep their validators.
"""
from pathlib import Path
import re

from .durable_state import atomic_json, MAX_JSON_BYTES
from .ingest.restore import RestoreError

PREFIX = 'state/translation/batches/'
_RECORD = re.compile(r'state/translation/batches/([0-9a-f]{32})(\.control)?\.json\Z')


def limit(name):
    if not name.startswith(PREFIX):
        return None
    match = _RECORD.fullmatch(name)
    if match is None:
        raise RestoreError('Unrecognized file in the typed translation batch inventory.')
    return 1024 if match[2] else MAX_JSON_BYTES


def validate_file(db, name, raw):
    from .backup_sidecars import _json
    from .assistant.translation_batch import validate_batch
    from .backup_producers import _translation_job
    match = _RECORD.fullmatch(name)
    if match is None:
        raise RestoreError('Unrecognized translation batch record.')
    maximum = limit(name)
    if not 0 < len(raw) <= maximum:
        raise RestoreError('Translation batch checkpoint exceeds its declared byte budget.')
    value = _json(raw)
    if match[2]:
        if type(value) is not dict or set(value) != {'command'} or value['command'] not in ('run', 'pause', 'cancel'):
            raise RestoreError('Invalid translation batch control.')
        return value
    manifest = validate_batch(value, match[1])
    for member in manifest['members']:
        job = member['job']
        # A missing, not-yet-prepared child still carries an exact reviewed
        # template. Validate it against the COPIED catalog, not a local source.
        _translation_job({**job, 'updated': job['created']}, job['id'], db)
        if job['kind'] == 'translation/2':
            anchor = job['source_binding']['anchor']
            row = db.execute('SELECT author,title,volume,edition_label FROM source_metadata '
                'WHERE workspace=? AND edition=? AND revision=?',
                (anchor['workspace'], anchor['edition'], anchor['revision'])).fetchone()
            expected = member['source_metadata']
            if row is None or dict(zip(('author', 'title', 'volume', 'edition'), row)) != expected:
                raise RestoreError('Reviewed translation metadata differs from the copied source catalog.')
    return manifest


def validate_collection(db, stage, entries, *, independent=True, cancelled=lambda: False):
    from .backup_sidecars import _check_cancelled, _json, read_regular
    from .assistant.translation_batch import MAX_BATCHES, validate_member
    from .backup_producers import _translation_job, limit as producer_limit
    names = {entry['filename'] for entry in entries}
    selected = sorted(name for name in names if name.startswith(PREFIX))
    if selected and not independent:
        raise RestoreError('Independent translation batches cannot enter a workspace-scoped backup.')
    manifests, controls = {}, set()
    for name in selected:
        _check_cancelled(cancelled)
        match = _RECORD.fullmatch(name)
        raw = read_regular(Path(stage) / name, limit(name))
        value = validate_file(db, name, raw)
        if match[2]:
            controls.add(match[1])
        else:
            manifests[match[1]] = value
            if len(manifests) > MAX_BATCHES:
                raise RestoreError('The translation batch inventory exceeds its 256-record limit.')
    if not controls <= set(manifests):
        raise RestoreError('Translation batch controls lack their immutable reviewed manifest.')
    claimed = set()
    for manifest in manifests.values():
        for index, member in enumerate(manifest['members']):
            _check_cancelled(cancelled)
            identity = member['job']['id']
            if identity in claimed:
                raise RestoreError('An execution child is claimed by more than one translation batch.')
            claimed.add(identity)
            name = 'state/translation/jobs/' + identity + '.json'
            if name not in names:
                # Preparation is deliberately resumable; no child receipt is
                # invented for a template that has never been saved.
                continue
            raw = read_regular(Path(stage) / name, producer_limit(name))
            job = _json(raw)
            _translation_job(job, identity, db)
            validate_member(manifest, index, job)
            # Local receipt consistency does not replace independent Gateway
            # usage evidence. The unchanged restored service verifies that
            # paired ledger before considering any child completed.


def project_restored(stage, entries, *, cancelled=lambda: False):
    from .backup_sidecars import _check_cancelled
    for entry in entries:
        _check_cancelled(cancelled)
        match = _RECORD.fullmatch(entry['filename'])
        if match is not None and match[2] is None:
            # Preserve every immutable manifest/child/approval byte. Restoring
            # a grouping cannot start its executor or renew remote permission.
            path = Path(stage) / entry['filename']
            atomic_json(path.with_name(match[1] + '.control.json'), {'command': 'pause'}, replace=True)
