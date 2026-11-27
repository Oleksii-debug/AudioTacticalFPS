"""Complete local research-library backups using the canonical SQLite snapshot.

Backups include all workspaces, saved research and registered original files.
Canonical version 3 includes private application state; version 4 also includes
resumable import journals and their pinned unpublished sources. Version 5 adds
the personal reading-state file with exact bookmarks and resume positions.
Version 6 additionally binds retained Studio inputs, microphone originals and
inert publication receipts without changing the database or reading formats.
The independently retained manifest digest binds the database and every asset.
Version 2 also identifies the exact schema, not just its numeric user_version.
Historical reading-only version 3 backups have a distinct exact manifest shape
and remain readable alongside canonical versions 1 through 6. Every version
requires an exact supported V3, V4 or V5 database schema. Older snapshots are
restored unchanged; opening the restored library performs the normal checked
upgrade to the current schema.
Restoration always creates a new data directory and never merges or overwrites.
"""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from .ingest.bundle import _plain, _unique_object
from .ingest.catalog_asset_copy import copy_catalog_assets, verify_catalog_assets
from .ingest.retained_assets import MAX_BYTES, SUFFIXES, RetainedAssets
from .ingest.restore import RestoreError
from .ingest.writer_guard import exclusive_writer
from .data_paths import guard_data_directory
from .durable_state import local_path
from .storage.revisions import Storage, CURRENT_SCHEMA_VERSION
from .storage.schema_identity import expected_schema_fingerprint, schema_fingerprint
from .reader import reading_state

DATABASE = 'library.sqlite'
ORIGINALS = 'originals'
MANIFEST = 'manifest.json'
INCOMPLETE = '.restore-incomplete'
MAX_MANIFEST = 8 * 1024 * 1024
MAX_DATABASE = 512 * 1024 * 1024
MAX_ASSETS = 10000
MAX_ASSET_BYTES = 8 * 1024**3
READING_STATE = reading_state.FILENAME


def _destination(value, source):
    path = local_path(guard_data_directory(value, purpose='backup or restored library'))
    _plain(path.parent, directory=True)
    if os.path.lexists(path):
        raise FileExistsError(path)
    path = path.parent.resolve() / path.name
    source = Path(source).resolve(strict=True)
    if path.is_relative_to(source) or source.is_relative_to(path):
        raise RestoreError('destination must be outside the source directory')
    return path


def _asset_manifest(entries):
    return [dict(filename=e.filename, sha256=e.digest, mime=e.mime, size_bytes=e.size)
            for e in entries]


def _sync_file(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    # Windows lacks a Python directory-fsync handle. Every file is separately
    # flushed; on POSIX also persist namespace changes before completion.
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _validate_reading_locations(db, positions, bookmarks):
    """Bind personal state to exact sealed pages, including historical revisions.

    A backup preserves archived libraries and inactive reading positions. It
    does not grant permission to open an archived library, approve a quotation,
    or move an old position to a newer revision. The reader rechecks access and
    current revision when those locations are opened after restoration.
    """
    locations = {row[0] for row in positions.values()} | {
        bookmark.location for bookmark in bookmarks.values()}
    for location in locations:
        # Select the appropriate existing composite index without scanning all
        # pages in a revision for every saved location. Both predicates are
        # fixed application text; identities and indexes stay bound parameters.
        predicate = ('p.physical=?' if location.kind == 'physical_page'
                     else 'p.physical IS NULL AND p.ordinal=?')
        row = db.execute('''SELECT p.digest FROM revision r
            JOIN sealed s ON s.revision=r.id
            JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition
                AND e.asset=r.asset
            JOIN page p ON p.revision=r.id
            WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=? AND ''' + predicate,
            (location.workspace, location.edition, location.revision,
             location.asset_sha256, location.index)).fetchone()
        if row is None or row[0] != location.text_sha256:
            raise RestoreError('reading state does not match the exact library source snapshot')


def _reading_snapshot(root, db, expected=None):
    """Validate bounded personal state using the reader's ordinary file rules."""
    raw, positions, bookmarks, _ = reading_state.ReadingState(root)._read_validated()
    if expected is not None:
        if (raw is None or len(raw) != expected['size_bytes']
                or sha256(raw).hexdigest() != expected['sha256']):
            raise RestoreError('reading-state digest or size mismatch')
    if raw is not None:
        _validate_reading_locations(db, positions, bookmarks)
    return raw



def _check_backup_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Backup or restoration cancelled.')


def _required_media_input(component, kind, reference):
    if (not isinstance(reference, str) or not reference or len(reference) > 32768
            or any(ord(c) < 32 for c in reference)):
        raise RestoreError('Invalid declared external Studio input.')
    return dict(component=component, kind=kind, reference=reference, included=False)


def _ordered_media_inputs(values):
    unique = {json.dumps(item, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')): item for item in values}
    return [unique[key] for key in sorted(unique)]


def _inspect_studio_media(root, entries, *, cancelled=lambda: False):
    """Inspect verified staged files, without opening recorded external paths."""
    from .durable_state import read_json
    from .studio.backup_inputs import inspect_snapshot_inputs, _record

    root = Path(root)
    by_name = {entry['filename']: entry for entry in entries}
    receipt = inspect_snapshot_inputs(root, entries, cancelled=cancelled)
    media = dict(schema=1, studio_input_receipt=receipt is not None,
                 studio_publications=[], studio_recordings=[],
                 studio_unverified_recordings=[], required_inputs=[])
    required = []
    if receipt is not None:
        for binding in receipt['projects']:
            _check_backup_cancelled(cancelled)
            found = _record(root, binding['filename'])
            if found is None:
                raise RestoreError('A verified Studio project binding disappeared.')
            project = found[2]
            component = 'state/' + binding['filename']
            for role in project.roles:
                if role.voice_id:
                    required.append(_required_media_input(component, 'local-voice', role.voice_id))
            for scene in project.scenes:
                if scene.music is not None and scene.music.engine != 'procedural':
                    required.append(_required_media_input(component, 'music-engine', scene.music.engine))
    for name in sorted(by_name):
        _check_backup_cancelled(cancelled)
        if name.casefold().startswith('studio/publications/'):
            if not re.fullmatch(r'studio/publications/[0-9a-f]{32}\.json', name):
                raise RestoreError('Invalid typed Studio publication filename.')
            from .studio.publishing import validate_publication_receipt
            record = validate_publication_receipt(read_json(root / name),
                publication_id=Path(name).stem)
            media['studio_publications'].append(name)
            required.append(_required_media_input('state/' + name, 'publication-output', record['output']))
        elif name.casefold().startswith('studio/recordings/'):
            if re.fullmatch(r'studio/recordings/[0-9a-f]{32}\.json', name):
                from .audio.recording import validate_recording_receipt
                record = validate_recording_receipt(read_json(root / name),
                    recording_id=Path(name).stem)
                audio = by_name.get(name.removesuffix('.json') + '.wav')
                if (audio is None or audio['size_bytes'] != record['audio']['size_bytes']
                        or audio['sha256'] != record['audio']['sha256']):
                    raise RestoreError('A recording receipt differs from its retained original.')
                from .speech.runtime import _wave_info
                expected_pcm = tuple(record['audio'][key] for key in
                    ('sample_rate', 'channels', 'frames', 'sample_width'))
                if _wave_info(root / audio['filename'], cancelled=cancelled,
                        max_output_bytes=record['limits']['max_bytes']) != expected_pcm:
                    raise RestoreError('A recording receipt differs from its retained PCM header.')
                media['studio_recordings'].append(name)
            elif re.fullmatch(r'studio/recordings/[0-9a-f]{32}\.wav', name):
                if name.removesuffix('.wav') + '.json' not in by_name:
                    media['studio_unverified_recordings'].append(name)
            else:
                raise RestoreError('Invalid typed Studio recording filename.')
    media['required_inputs'] = _ordered_media_inputs(required)
    return media


def _validate_studio_media(media, entries):
    """Validate the exact V6 envelope; staged content is checked separately."""
    from .state_backup import MAX_STATE_FILES

    fields = {'schema', 'studio_input_receipt', 'studio_publications',
              'studio_recordings', 'studio_unverified_recordings', 'required_inputs'}
    if (type(media) is not dict or set(media) != fields
            or type(media['schema']) is not int or media['schema'] != 1
            or type(media['studio_input_receipt']) is not bool):
        raise RestoreError('Unsupported Studio media manifest.')
    names = {entry['filename'] for entry in entries}
    for field, pattern in (
            ('studio_publications', r'studio/publications/[0-9a-f]{32}\.json'),
            ('studio_recordings', r'studio/recordings/[0-9a-f]{32}\.json'),
            ('studio_unverified_recordings', r'studio/recordings/[0-9a-f]{32}\.wav')):
        values = media[field]
        if (type(values) is not list or len(values) > MAX_STATE_FILES
                or any(not isinstance(name, str) or not re.fullmatch(pattern, name)
                       or name not in names for name in values)
                or values != sorted(set(values))):
            raise RestoreError('Invalid Studio media file manifest.')
    required = media['required_inputs']
    if type(required) is not list or len(required) > MAX_STATE_FILES:
        raise RestoreError('Invalid required Studio inputs.')
    for item in required:
        if (type(item) is not dict
                or set(item) != {'component', 'kind', 'reference', 'included'}
                or not isinstance(item['component'], str)
                or not item['component'].startswith('state/')
                or item['component'][len('state/'):] not in names
                or item['kind'] not in ('local-voice', 'music-engine', 'publication-output')
                or item['included'] is not False):
            raise RestoreError('Invalid required Studio input.')
        _required_media_input(item['component'], item['kind'], item['reference'])
    if required != _ordered_media_inputs(required):
        raise RestoreError('Duplicate or unordered required Studio inputs.')
    if not any(media[field] for field in ('studio_input_receipt', 'studio_publications',
                                          'studio_recordings', 'studio_unverified_recordings')):
        raise RestoreError('Empty Studio media manifest.')


def create_backup(app, destination, *, include_generated=False, cancelled=lambda: False):
    """Back up every library to a fresh folder and return its manifest SHA256.

    The host's explicit Backup action authorizes all-workspace inclusion. The
    private source/destination parents must be stable local directories. A
    publication interrupted before manifest.json is incomplete and not restorable.
    """
    if type(include_generated) is not bool or not callable(cancelled):
        raise ValueError('Invalid backup options.')
    if cancelled():
        raise InterruptedError('Backup cancelled.')
    destination = _destination(destination, app.root)
    with exclusive_writer(app.root):
        _plain(app.database)
        if os.path.lexists(app.root / INCOMPLETE):
            raise RestoreError('the source library restore is incomplete')
        with TemporaryDirectory(prefix='.carr-library-backup-', dir=destination.parent) as tmp:
            stage = Path(tmp)
            snapshot = stage / DATABASE
            with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
                storage.backup(snapshot)
            size = _plain(snapshot).st_size
            if not 0 < size <= MAX_DATABASE:
                raise RestoreError('database exceeds backup size limit')
            with closing(Storage(snapshot, schema_version=CURRENT_SCHEMA_VERSION)) as snapshot_storage:
                reading = _reading_snapshot(app.root, snapshot_storage.db)
                entries = copy_catalog_assets(snapshot_storage.db, app.assets, stage / ORIGINALS,
                    include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
                fingerprint = schema_fingerprint(snapshot_storage.db)
            reading_manifest = None
            if reading is not None:
                _sync_file(stage / READING_STATE, reading)
                reading_manifest = dict(filename=READING_STATE, size_bytes=len(reading),
                                        sha256=sha256(reading).hexdigest())
            manifest = dict(version=2, kind='carr-library', schema_version=CURRENT_SCHEMA_VERSION,
                schema_fingerprint=fingerprint,
                database=dict(filename=DATABASE, size_bytes=size, sha256=hash_file(snapshot)),
                assets=_asset_manifest(entries))
            studio_media = None
            if os.path.lexists(app.root / 'state'):
                from .state_backup import (snapshot_state, validate_state,
                                           MAX_STATE_BYTES, MAX_STATE_FILES)
                from .studio.backup_inputs import snapshot_inputs
                (stage / 'state').mkdir()
                state = snapshot_state(app.root / 'state', stage / 'state',
                                       include_generated=include_generated, cancelled=cancelled)
                # The private snapshot retains the exact project bytes. Only
                # explicitly selected Cue inputs are copied into its receipt.
                state = snapshot_inputs(stage / 'state', state,
                    max_bytes=MAX_STATE_BYTES, max_files=MAX_STATE_FILES, cancelled=cancelled)
                validate_state(state)
                observed = _inspect_studio_media(stage / 'state', state, cancelled=cancelled)
                if any(observed[field] for field in ('studio_input_receipt', 'studio_publications',
                                                      'studio_recordings', 'studio_unverified_recordings')):
                    _validate_studio_media(observed, state)
                    studio_media = observed
                manifest.update(version=3, state=state, generated_audio_included=include_generated)
            if any(os.path.lexists(app.root / name) for name in
                   ('imports.sqlite', 'import-requests', 'import-batches')):
                from .pending_backup import snapshot_pending
                manifest.update(version=4, pending=snapshot_pending(app, stage / 'pending', cancelled=cancelled))
                if 'state' not in manifest:
                    (stage / 'state').mkdir()
                    manifest.update(state=[], generated_audio_included=include_generated)
            if reading is not None:
                # A distinct version avoids confusing the two historical v3
                # layouts. Keep the canonical component producers unchanged.
                if 'state' not in manifest:
                    (stage / 'state').mkdir()
                    manifest.update(state=[], generated_audio_included=include_generated)
                manifest.update(version=5, reading_state=reading_manifest)
                manifest.setdefault('pending', None)
            if studio_media is not None:
                # V5 remains the canonical reading-state format. Media uses a
                # new explicit envelope and cannot reinterpret an older V5.
                manifest.update(version=6, studio_media=studio_media,
                                reading_state=reading_manifest)
                manifest.setdefault('pending', None)
            raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
            if len(raw) > MAX_MANIFEST:
                raise RestoreError('manifest size limit')
            _sync_file(stage / MANIFEST, raw)
            if cancelled():
                raise InterruptedError('Backup cancelled.')
            # mkdir provides exclusive no-replace ownership of the destination.
            # The final manifest is the completion marker; no partial bundle is
            # silently overwritten or presented as a successful backup.
            destination.mkdir(mode=0o700)
            (destination / ORIGINALS).mkdir(mode=0o700)
            os.link(snapshot, destination / DATABASE)
            for entry in entries:
                os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
            if reading is not None:
                os.link(stage / READING_STATE, destination / READING_STATE)
            if 'state' in manifest:
                from .state_backup import publish_state
                publish_state(stage / 'state', destination / 'state')
                if manifest.get('pending') is not None:
                    publish_state(stage / 'pending', destination / 'pending')
            _sync_directory(destination / ORIGINALS)
            if cancelled():
                raise InterruptedError('Backup cancelled before publication. The incomplete folder has no manifest.')
            os.link(stage / MANIFEST, destination / MANIFEST)
            _sync_directory(destination)
            _sync_directory(destination.parent)
    return sha256(raw).hexdigest()


def _read_manifest(directory, expected_manifest_sha256):
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(
            r'[0-9a-f]{64}', expected_manifest_sha256):
        raise RestoreError('independently saved manifest SHA256 required')
    names = set(p.name for p in directory.iterdir())
    required = {DATABASE, ORIGINALS, MANIFEST}
    if not required <= names or not names <= required | {READING_STATE, 'state', 'pending'}:
        raise RestoreError('incomplete backup or unexpected files')
    size = _plain(directory / MANIFEST).st_size
    if not 0 < size <= MAX_MANIFEST:
        raise RestoreError('manifest size limit')
    with (directory / MANIFEST).open('rb') as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) != size or sha256(raw).hexdigest() != expected_manifest_sha256:
        raise RestoreError('manifest digest mismatch')
    try:
        manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError('invalid manifest') from exc
    if (type(manifest) is not dict or type(manifest.get('version')) is not int
            or manifest['version'] not in (1, 2, 3, 4, 5, 6)):
        raise RestoreError('unsupported library manifest')
    version = manifest['version']
    fields = {'version','kind','schema_version','database','assets'}
    if version >= 2:
        fields.add('schema_fingerprint')
    if version == 3 and set(manifest) == fields | {'reading_state'}:
        # Earlier reading backups used v3 before canonical application-state
        # snapshots did. Only this complete historical shape selects that path.
        fields.add('reading_state')
    elif version >= 3:
        fields.update(('state', 'generated_audio_included'))
    if version >= 4:
        fields.add('pending')
    if version >= 5:
        fields.add('reading_state')
    if version == 6:
        fields.add('studio_media')
    if (set(manifest) != fields
            or manifest['kind'] != 'carr-library'
            or type(manifest['schema_version']) is not int or manifest['schema_version'] not in (3, 4, 5)
            or type(manifest['assets']) is not list or len(manifest['assets']) > MAX_ASSETS):
        raise RestoreError('unsupported library manifest')
    if manifest['version'] >= 2:
        fingerprint = manifest['schema_fingerprint']
        if (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)
                or fingerprint != expected_schema_fingerprint(manifest['schema_version'])):
            raise RestoreError('unsupported backup schema fingerprint')
    if 'state' in fields:
        from .state_backup import validate_state
        if type(manifest['generated_audio_included']) is not bool:
            raise RestoreError('invalid application state backup options')
        validate_state(manifest['state'])
        required.add('state')
    if 'pending' in fields:
        if manifest['pending'] is None:
            if version not in (5, 6):
                raise RestoreError('missing pending import backup')
        else:
            from .pending_backup import validate_pending
            validate_pending(manifest['pending'])
            required.add('pending')
    if version == 6:
        _validate_studio_media(manifest['studio_media'], manifest['state'])
    reading = manifest.get('reading_state')
    if version == 5 and reading is None:
        raise RestoreError('missing reading-state manifest')
    if reading is not None:
        if (type(reading) is not dict or set(reading) != {'filename','size_bytes','sha256'}
                or reading['filename'] != READING_STATE
                or type(reading['size_bytes']) is not int
                or not 0 < reading['size_bytes'] <= reading_state.MAX_BYTES
                or not isinstance(reading['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', reading['sha256'])):
            raise RestoreError('invalid reading-state manifest')
        required.add(READING_STATE)
    if names != required:
        raise RestoreError('incomplete backup or unexpected component files')
    database = manifest['database']
    if (type(database) is not dict or set(database) != {'filename','size_bytes','sha256'}
            or database['filename'] != DATABASE or type(database['size_bytes']) is not int
            or not 0 < database['size_bytes'] <= MAX_DATABASE
            or not isinstance(database['sha256'],str)
            or not re.fullmatch(r'[0-9a-f]{64}',database['sha256'])):
        raise RestoreError('invalid database manifest')
    filenames, digests, total = set(), set(), 0
    for asset in manifest['assets']:
        if (type(asset) is not dict or set(asset) != {'filename','sha256','mime','size_bytes'}
                or not isinstance(asset['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', asset['sha256'])
                or not isinstance(asset['mime'], str) or asset['mime'] not in SUFFIXES
                or asset['filename'] != asset['sha256'] + SUFFIXES[asset['mime']]
                or type(asset['size_bytes']) is not int or not 0 < asset['size_bytes'] <= MAX_BYTES):
            raise RestoreError('invalid asset manifest')
        if asset['filename'] in filenames or asset['sha256'] in digests:
            raise RestoreError('duplicate asset manifest')
        filenames.add(asset['filename'])
        digests.add(asset['sha256'])
        total += asset['size_bytes']
        if total > MAX_ASSET_BYTES:
            raise RestoreError('asset manifest byte limit')
    return manifest


def _copy_database(source, target, library_manifest):
    manifest = library_manifest['database']
    if _plain(source).st_size != manifest['size_bytes']:
        raise RestoreError('database size mismatch')
    count = 0
    digest = sha256()
    with source.open('rb') as incoming, target.open('xb') as output:
        while block := incoming.read(min(1024 * 1024, manifest['size_bytes'] + 1 - count)):
            count += len(block)
            if count > manifest['size_bytes']:
                raise RestoreError('database grew during restore')
            digest.update(block)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if count != manifest['size_bytes'] or digest.hexdigest() != manifest['sha256']:
        raise RestoreError('database digest mismatch')
    # Inspect the closed private copy before allowing writable Storage access.
    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        fingerprint = schema_fingerprint(db)
        if fingerprint != expected_schema_fingerprint(library_manifest['schema_version']):
            raise RestoreError('backup schema mismatch')
        if (library_manifest['version'] >= 2
                and fingerprint != library_manifest['schema_fingerprint']):
            raise RestoreError('backup schema fingerprint mismatch')


def restore_backup(source, destination, expected_manifest_sha256, *, cancelled=lambda: False):
    """Restore a trusted backup into a new data root; return that root's Path.

    Keep the expected digest independently of the backup. The source is never
    opened writable. A failed/terminated publication retains .restore-incomplete;
    the application must reject such a root instead of opening partial data.
    """
    if not callable(cancelled):
        raise ValueError('Invalid restore options.')
    _check_backup_cancelled(cancelled)
    # Validate the original lexical path before resolving it, otherwise an
    # ancestor symlink/junction would be hidden from the component verifiers.
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    destination = _destination(destination, source)
    manifest = _read_manifest(source, expected_manifest_sha256)
    _plain(source / ORIGINALS, directory=True)
    with TemporaryDirectory(prefix='.carr-library-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        snapshot = stage / DATABASE
        _copy_database(source / DATABASE, snapshot, manifest)
        with closing(Storage(snapshot, schema_version=manifest['schema_version'])) as storage:
            storage.check()
            reading = None
            if manifest.get('reading_state') is not None:
                reading = _reading_snapshot(source, storage.db, manifest['reading_state'])
                _sync_file(stage / READING_STATE, reading)
            original_store = RetainedAssets(source / ORIGINALS)
            entries = verify_catalog_assets(storage.db, original_store,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
            if _asset_manifest(entries) != manifest['assets']:
                raise RestoreError('asset manifest disagrees with the database')
            copy_catalog_assets(storage.db, original_store, stage / ORIGINALS,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
        pending = {'assets': [], 'paths': []}
        if 'state' in manifest:
            from .state_backup import restore_state
            restore_state(source / 'state', stage / 'state', manifest['state'])
            _check_backup_cancelled(cancelled)
            if manifest['version'] == 6:
                observed = _inspect_studio_media(stage / 'state', manifest['state'],
                                                cancelled=cancelled)
                if observed != manifest['studio_media']:
                    raise RestoreError('Studio media manifest differs from its exact staged state.')
            # Legacy paths describe the source machine. They remain declarations
            # until explicitly reviewed; even a completed job needs a new output.
            # V6 rewrites only the private projection, never the source archive.
            from .studio.backup_inputs import restore_inputs
            restore_inputs(stage / 'state', destination / 'state', manifest['state'],
                           cancelled=cancelled)
        if manifest.get('pending') is not None:
            from .pending_backup import restore_pending
            pending = restore_pending(source / 'pending', stage, destination, manifest['pending'],
                                      cancelled=cancelled)
        _check_backup_cancelled(cancelled)
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        (destination / ORIGINALS).mkdir(mode=0o700)
        os.link(snapshot, destination / DATABASE)
        for entry in entries:
            os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
        if reading is not None:
            os.link(stage / READING_STATE, destination / READING_STATE)
        for entry in pending['assets']:
            os.link(stage / ORIGINALS / entry['filename'], destination / ORIGINALS / entry['filename'])
        if 'state' in manifest:
            from .state_backup import publish_state
            publish_state(stage / 'state', destination / 'state')
            for name in pending['paths']:
                if (stage / name).is_dir():
                    publish_state(stage / name, destination / name)
                else:
                    os.link(stage / name, destination / name)
        _sync_directory(destination / ORIGINALS)
        _sync_directory(destination)
    _check_backup_cancelled(cancelled)
    # Temporary hard links are now gone; retained originals have one link.
    (destination / INCOMPLETE).unlink()
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return destination
