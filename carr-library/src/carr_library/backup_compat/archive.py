"""Authenticate the older backup dialect and preserve it without activation.

Only the common catalog, exact originals and compatible reading positions are
published into the active Resonance root. Original legacy component bytes remain
under ``legacy-backups/<manifest SHA256>``. They do not become providers, agents,
Studio stores, or runnable queues by sharing a filename with a current service.
"""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from ..backup_extensions import (LEGACY, SIDECARS, DORDIAI, READING, LIVE,
    MAX_JSON, _ancestors, _check, _encoded, _json, _ordered_inputs, _required,
    _stamp, _tree, copy_files, read_bytes, write_bytes)
from ..durable_state import regular
from ..ingest.catalog_asset_copy import verify_catalog_assets
from ..ingest.retained_assets import RetainedAssets
from ..ingest.restore import RestoreError
from ..speech.dordiai_settings import restored_settings_bytes
from ..storage.revisions import Storage
from . import library, recovery, studio

_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _files(manifest, digest, raw):
    entries = [dict(filename=library.DATABASE, **{key: manifest['database'][key]
        for key in ('size_bytes', 'sha256')}),
        dict(filename=library.MANIFEST, size_bytes=len(raw), sha256=digest)]
    for item in manifest['assets']:
        entries.append(dict(filename='originals/' + item['filename'],
                            size_bytes=item['size_bytes'], sha256=item['sha256']))
    entries.extend(manifest['sidecars'])
    for item in manifest.get('recovery', {}).get('files', []):
        entries.append(dict(filename='recovery/' + item['path'],
                            size_bytes=item['size_bytes'], sha256=item['sha256']))
    return sorted(entries, key=lambda item: item['filename'])


def _namespace(source, entries, cancelled):
    files, directories = _tree(source, cancelled=cancelled)
    names = {item['filename'] for item in entries}
    # A complete core backup always has an originals directory, even when empty.
    if set(files) != names or directories != _ancestors(names) | {'originals'}:
        raise RestoreError('Legacy backup contains missing, unlisted or ambiguous files.')
    return {name: _stamp(info) for name, info in files.items()}


def _studio_inputs(directory, manifest, digest, component, cancelled):
    value = studio._manifest(directory, digest, studio._canonical_schema(), cancelled)
    records = studio._records(directory / studio.DATABASE, studio._canonical_schema(), cancelled)
    expected, productions = studio._expected(*records, value['original_root'], value['include_audio'])
    actual = {item['path']: item for item in value['files'] if item['kind'] != 'history'}
    if set(actual) != set(expected) or any(any(actual[name].get(key) != field for key, field in item.items())
                                          for name, item in expected.items()):
        raise RestoreError('Legacy Studio files disagree with their committed project records.')
    # The enclosing recovery inventory has already verified every byte. These
    # checks authenticate the inner commitments too, including the database.
    for item in [value['database'], *value['files']]:
        path = directory / item['path']
        row = recovery._record(path, item['path'])
        if row['size_bytes'] != item['size_bytes'] or row['sha256'] != item['sha256']:
            raise RestoreError('Legacy Studio inner manifest digest differs from its recovery bytes.')
    studio._validate_assets(directory, productions, records[1], value['include_audio'], value['original_root'])
    required = []
    for revision, document in records[0].items():
        project = studio.Project.from_dict(json.loads(document))
        producer = component + '/revisions/' + revision + '.json'
        for role in project.roles:
            required.append(_required(producer, 'local-voice', role.voice_id))
        for scene in project.scenes:
            for clip in scene.clips:
                required.append(_required(producer, 'audio-file', clip.path))
    return required


def _notice(manifest, digest, required):
    components = [dict(component='catalog-and-originals', status='restored',
                       reason='Verified common catalog and exact registered original bytes.')]
    for item in manifest['sidecars']:
        name = item['filename']
        if name in (READING, LIVE):
            status, reason = 'restored', 'Exact source-bound positions; playback requires an explicit action.'
        elif name == DORDIAI:
            status, reason = 'restored-disabled', 'Local engine declarations retained; explicit reconnection is required.'
        else:
            status, reason = 'inert', 'Original bytes retained; a validated canonical import is required.'
        components.append(dict(component=name, status=status, reason=reason))
    value = manifest.get('recovery', {})
    names = {item['path'] for item in value.get('files', [])}
    for label, present in (
            ('pending-imports', any(name.startswith(('import-', 'pending-originals/')) or name == 'imports.sqlite' for name in names)),
            ('provider-settings', any(name.startswith('provider-settings/') for name in names)),
            ('agent-jobs', any(name.startswith('agent-jobs/') for name in names))):
        if present:
            components.append(dict(component=label, status='inert',
                reason='Original records retained; no foreign queue, provider, or job was activated.'))
    for item in value.get('projects', []):
        components.append(dict(component='audio-projects/' + item['id'], status='inert',
            reason='Original project revisions, receipts and included audio retained; canonical import is required.'))
    return dict(kind='carr-legacy-recovery-notice', version=1, manifest_sha256=digest,
        format='legacy-sidecars-v3' if manifest['version'] == 3 else 'legacy-recovery-v4',
        archive_status='inert', components=sorted(components, key=lambda item: item['component']),
        required_inputs=_ordered_inputs(required))


def _validate_stage(source, manifest, digest, cancelled):
    from ..library_backup import _copy_database, _asset_manifest, MAX_ASSETS, MAX_ASSET_BYTES
    # Storage.check uses normal SQLite integrity checks on a disposable copy.
    # The preserved original database is never opened writable or upgraded.
    with TemporaryDirectory(prefix='.legacy-catalog-check-', dir=source.parent) as tmp:
        database = Path(tmp) / 'library.sqlite'
        _copy_database(source / 'library.sqlite', database, manifest)
        with closing(Storage(database, schema_version=manifest['schema_version'])) as storage:
            storage.check()
            entries = verify_catalog_assets(storage.db, RetainedAssets(source / 'originals'),
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
            if _asset_manifest(entries) != manifest['assets']:
                raise RestoreError('Legacy asset manifest differs from its catalog.')
            sidecars = {item['filename']: read_bytes(source / item['filename'],
                library._sidecar_limit(item['filename'])) for item in manifest['sidecars']}
            library._validate_sidecars(storage.db, sidecars)
            allowed = {row[0] for row in storage.db.execute('SELECT id FROM workspace')}
    required = []
    prefix = LEGACY + '/' + digest + '/'
    if DORDIAI in sidecars:
        from .dordiai import validate_settings_bytes
        settings = validate_settings_bytes(sidecars[DORDIAI])
        for key in ('runtime_directory', 'python_executable'):
            required.append(_required(prefix + DORDIAI, 'dordiai-runtime', settings[key]))
    if LIVE in sidecars:
        from .live_positions import validate_live_state
        required.extend(_required(prefix + LIVE, 'local-voice', item.voice_id)
                        for item in validate_live_state(sidecars[LIVE]))
    if 'recovery' in manifest:
        value = manifest['recovery']
        recovery._verify(source / 'recovery', value, allowed, {item['sha256'] for item in manifest['assets']})
        for item in value['projects']:
            component = 'recovery/projects/' + item['id']
            required.extend(_studio_inputs(source / component, manifest, item['manifest_sha256'],
                                          prefix + component, cancelled))
        for item in value['files']:
            name = item['path']
            if name.startswith('import-requests/') and '/reviews/' not in name:
                request = recovery._json(source / 'recovery' / name)
                # Pinned originals are already present in the authenticated
                # archive. An unretained external selection is only declared.
                if not request.get('asset_sha256'):
                    required.append(_required(prefix + 'recovery/' + name, 'source-file', request['source']))
    return _notice(manifest, digest, required)


def stage_archive(source, target, digest, *, cancelled=lambda: False):
    """Copy and validate exact bytes into a private unpublished directory."""
    _check(cancelled)
    source, target = Path(source), Path(target)
    regular(source, directory=True)
    manifest = library._read_manifest(source, digest)
    if manifest['version'] not in (3, 4):
        raise RestoreError('Only the distinct legacy V3/V4 dialect belongs in an inert archive.')
    raw = read_bytes(source / library.MANIFEST, library.MAX_MANIFEST)
    if sha256(raw).hexdigest() != digest:
        raise RestoreError('Legacy manifest changed after authentication.')
    entries = _files(manifest, digest, raw)
    before = _namespace(source, entries, cancelled)
    copy_files(source, target, entries, cancelled=cancelled)
    (target / 'originals').mkdir(exist_ok=True, mode=0o700)
    if _namespace(source, entries, cancelled) != before:
        raise RestoreError('Legacy archive changed during copy.')
    notice = _validate_stage(target, manifest, digest, cancelled)
    return manifest, entries, notice


def verify_archive(source, digest, *, cancelled=lambda: False):
    with TemporaryDirectory(prefix='.legacy-backup-verify-') as tmp:
        return stage_archive(source, Path(tmp) / 'archive', digest, cancelled=cancelled)[2]


def restore_archive(source, destination, digest):
    from ..library_backup import (_destination, _copy_database, _sync_directory,
                                  _sync_file, INCOMPLETE)
    from ..state_backup import publish_state
    source = Path(source)
    destination = _destination(destination, source)
    with TemporaryDirectory(prefix='.legacy-library-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        archive = stage / LEGACY / digest
        manifest, _, notice = stage_archive(source, archive, digest)
        write_bytes(stage / LEGACY / (digest + '.json'), _encoded(notice))
        _copy_database(archive / 'library.sqlite', stage / 'library.sqlite', manifest)
        original_entries = [dict(filename=item['filename'], size_bytes=item['size_bytes'], sha256=item['sha256'])
                            for item in manifest['assets']]
        copy_files(archive / 'originals', stage / 'originals', original_entries)
        active = []
        for name in (READING, LIVE, DORDIAI):
            if not (archive / name).exists():
                continue
            raw = read_bytes(archive / name, SIDECARS[name])
            write_bytes(stage / name, restored_settings_bytes(raw) if name == DORDIAI else raw)
            active.append(name)
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        os.link(stage / 'library.sqlite', destination / 'library.sqlite')
        publish_state(stage / 'originals', destination / 'originals')
        publish_state(stage / LEGACY, destination / LEGACY)
        for name in active:
            os.link(stage / name, destination / name)
        _sync_directory(destination)
    (destination / INCOMPLETE).unlink()
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return destination


def _archive_keys(source):
    regular(source, directory=True)
    names = {path.name for path in source.iterdir()}
    keys = sorted(name for name in names if _SHA.fullmatch(name))
    if len(keys) > 1000 or names != set(keys) | {key + '.json' for key in keys}:
        raise RestoreError('Unexpected or incomplete retained legacy archive inventory.')
    return keys


def snapshot_archives(source, target, *, cancelled=lambda: False):
    source, target = Path(source), Path(target)
    keys = _archive_keys(source)
    records, required = [], []
    for key in keys:
        _check(cancelled)
        notice_raw = read_bytes(source / (key + '.json'), MAX_JSON)
        _, _, notice = stage_archive(source / key, target / key, key, cancelled=cancelled)
        if _json(notice_raw) != notice:
            raise RestoreError('Legacy recovery notice differs from its authenticated archive.')
        write_bytes(target / (key + '.json'), notice_raw)
        records.append(dict(manifest_sha256=key, format=notice['format'], status='inert'))
        required.extend(notice['required_inputs'])
    if _archive_keys(source) != keys:
        raise RestoreError('Legacy archive inventory changed during backup.')
    return records, _ordered_inputs(required)


def verify_archives(source, *, cancelled=lambda: False):
    with TemporaryDirectory(prefix='.legacy-archives-check-') as tmp:
        return snapshot_archives(source, Path(tmp) / LEGACY, cancelled=cancelled)


def recovery_notices(root):
    """Readable, validated status for the host after a successful restore."""
    directory = Path(root) / LEGACY
    if not os.path.lexists(directory):
        return ()
    result = []
    for key in _archive_keys(directory):
        notice = _json(read_bytes(directory / (key + '.json'), MAX_JSON))
        if (type(notice) is not dict or notice.get('kind') != 'carr-legacy-recovery-notice'
                or notice.get('manifest_sha256') != key or notice.get('archive_status') != 'inert'):
            raise RestoreError('Invalid legacy recovery notice.')
        result.append(notice)
    return tuple(result)
