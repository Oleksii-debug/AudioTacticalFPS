"""Complete local research-library backups using the canonical SQLite snapshot.

Backups include all workspaces, saved research, registered original files and
the optional reading-state sidecar containing bookmarks and resume positions,
plus saved narration copies and their recoverable drafts.
Keyboard preferences and named OCR-language selections are included when present.
Version 4 also includes typed production/recovery components and the exact
retained originals referenced by pending import receipts and checkpoints.
The independently retained manifest digest binds the database and every asset.
Version 2 also identifies the exact schema, not just its numeric user_version.
Version 3 extends that manifest with explicitly named, checksummed sidecars.
Legacy version 1/2 backups and version 3/4 backups are accepted only for an exact
supported V3 or V4 database schema. V3 snapshots are restored unchanged; opening the
restored library performs the normal checked upgrade to V4.
Restoration always creates a new data directory and never merges or overwrites.
"""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from tempfile import TemporaryDirectory

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from .ingest.bundle import _plain, _unique_object
from .ingest.catalog_asset_copy import copy_catalog_assets, verify_catalog_assets
from .ingest.retained_assets import MAX_BYTES, SUFFIXES, RetainedAssets
from .ingest.restore import RestoreError
from .ingest.writer_guard import _regular_if_present, exclusive_writer
from .data_paths import guard_data_directory
from .storage.revisions import Storage
from .storage.schema_identity import expected_schema_fingerprint, schema_fingerprint
from . import recovery_backup
from .reader.reading_state import (
    FILENAME as READING_STATE, MAX_BYTES as MAX_READING_STATE, ReadingState,
    _decode as _decode_reading_state, _file_identity,
)
from .reader.narration import (
    DIRECTORY as NARRATION_DIRECTORY, MAX_PROJECT_BYTES, MAX_PROJECTS,
    MAX_SNAPSHOT_BYTES as MAX_NARRATION_BYTES,
    snapshot_narration_files, validate_narration_payload,
)
from .keybindings import (
    FILENAME as KEYBINDINGS, MAX_BYTES as MAX_KEYBINDINGS, decode_preferences,
)
from .extract.ocr_languages import (
    FILENAME as OCR_PRESETS, MAX_PRESET_BYTES, validate_presets_bytes,
)
from .keyboard_preferences import MAX_BYTES as MAX_SHORTCUTS, validate_keymap
from .evidence_drafts import FILENAME as EVIDENCE_DRAFTS, MAX_BYTES as MAX_DRAFT_BYTES, validate_evidence_drafts

DATABASE = 'library.sqlite'
ORIGINALS = 'originals'
MANIFEST = 'manifest.json'
INCOMPLETE = '.restore-incomplete'
MAX_MANIFEST = 64 * 1024 * 1024
MAX_DATABASE = 512 * 1024 * 1024
MAX_ASSETS = 10000
MAX_ASSET_BYTES = 8 * 1024**3
MAX_SIDECARS = 5 + MAX_PROJECTS * 2
MAX_SIDECAR_BYTES = MAX_READING_STATE + MAX_NARRATION_BYTES + MAX_KEYBINDINGS + MAX_PRESET_BYTES + MAX_SHORTCUTS + MAX_DRAFT_BYTES
_SIMPLE_SIDECARS = {
    KEYBINDINGS: (MAX_KEYBINDINGS, decode_preferences),
    OCR_PRESETS: (MAX_PRESET_BYTES, validate_presets_bytes),
    'keyboard-shortcuts.json': (MAX_SHORTCUTS, validate_keymap),
    EVIDENCE_DRAFTS: (MAX_DRAFT_BYTES, validate_evidence_drafts),
}


def _destination(value, source):
    path = guard_data_directory(value, purpose='backup or restored library')
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


def _sidecar_manifest(sidecars):
    return [dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest())
            for name, raw in sorted(sidecars.items())]


def _sidecar_limit(name):
    # This is an allowlist, not a general folder backup. In particular, private
    # settings, credentials, caches and incomplete import journals cannot enter
    # a backup merely by appearing beside the research database.
    if name == READING_STATE:
        return MAX_READING_STATE
    if name in _SIMPLE_SIDECARS:
        return _SIMPLE_SIDECARS[name][0]
    if re.fullmatch(re.escape(NARRATION_DIRECTORY) + r'/[0-9a-f]{32}(?:\.draft)?\.json', name):
        return MAX_PROJECT_BYTES
    raise RestoreError('unsupported backup sidecar')


def _optional_sidecar(path, maximum):
    _regular_if_present(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not 0 < before.st_size <= maximum:
        raise RestoreError('backup sidecar is empty or exceeds its size limit')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or _file_identity(opened) != _file_identity(before)):
            raise RestoreError('backup sidecar changed while opening')
        raw = stream.read(maximum + 1)
    if len(raw) != before.st_size or _file_identity(path.lstat()) != _file_identity(before):
        raise RestoreError('backup sidecar changed while reading')
    return raw


def _snapshot_sidecars(root):
    raw = ReadingState(root).snapshot()
    sidecars = {} if raw is None else {READING_STATE: raw}
    sidecars.update(snapshot_narration_files(root))
    for name, (maximum, validate) in _SIMPLE_SIDECARS.items():
        raw = _optional_sidecar(Path(root) / name, maximum)
        if raw is not None:
            validate(raw)
            sidecars[name] = raw
    return sidecars


def _validate_reading_locations(db, locations):
    # Historical revisions and archived libraries remain valid backup contents.
    # Do not consult the active revision or retarget an older reading position.
    # Storage.check authenticates the page digests and the separately verified
    # original inventory covers each immutable edition/asset binding.
    for location in locations:
        row = db.execute('''SELECT 1 FROM revision r
            JOIN sealed s ON s.revision=r.id
            JOIN page p ON p.revision=r.id
            JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition
                AND e.asset=r.asset
            WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=?
                AND p.digest=?
                AND ((?='physical_page' AND p.physical=?) OR
                     (?='logical_segment' AND p.physical IS NULL AND p.ordinal=?))''',
            (location.workspace, location.edition, location.revision,
             location.asset_sha256, location.text_sha256, location.kind,
             location.index, location.kind, location.index)).fetchone()
        if row is None:
            raise RestoreError('reading state or narration copy does not match its exact saved source')


def _validate_sidecars(db, sidecars):
    for name, raw in sidecars.items():
        _sidecar_limit(name)
        try:
            if name == READING_STATE:
                positions, bookmarks, _ = _decode_reading_state(raw.decode('utf-8'))
                locations = {row[0] for row in positions.values()} | {
                    bookmark.location for bookmark in bookmarks.values()}
            elif name in _SIMPLE_SIDECARS:
                _SIMPLE_SIDECARS[name][1](raw)
                locations = set()
            else:
                project = validate_narration_payload(name, raw)
                locations = {project.source_location} if project.source_location is not None else set()
        except (UnicodeError, ValueError) as exc:
            raise RestoreError('invalid reading state, narration copy or preferences in backup') from exc
        _validate_reading_locations(db, locations)


def _stage_sidecars(stage, sidecars):
    for name, raw in sorted(sidecars.items()):
        (stage / name).parent.mkdir(mode=0o700, exist_ok=True)
        _sync_file(stage / name, raw)


def _publish_sidecars(stage, destination, sidecars):
    for name in sorted(sidecars):
        (destination / name).parent.mkdir(mode=0o700, exist_ok=True)
        os.link(stage / name, destination / name)
    for parent in {(destination / name).parent for name in sidecars}:
        _sync_directory(parent)


def _check_backup_namespace(directory, names, *, recovery=False):
    roots = {name.split('/')[0] for name in names}
    if recovery:
        roots.add(recovery_backup.DIRECTORY)
    if set(p.name for p in directory.iterdir()) != {DATABASE, ORIGINALS, MANIFEST} | roots:
        raise RestoreError('incomplete backup or unexpected files')
    narration = {name.split('/')[1] for name in names if '/' in name}
    if narration:
        folder = directory / NARRATION_DIRECTORY
        _plain(folder, directory=True)
        if set(p.name for p in folder.iterdir()) != narration:
            raise RestoreError('incomplete narration backup or unexpected files')


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


def create_backup(app, destination, *, authorized_workspaces=None):
    """Back up every library to a fresh folder and return its manifest SHA256.

    The host's explicit Backup action authorizes all-workspace inclusion. The
    private source/destination parents must be stable local directories. A
    publication interrupted before manifest.json is incomplete and not restorable.
    """
    authorized_workspaces = None if authorized_workspaces is None else frozenset(authorized_workspaces)
    destination = _destination(destination, app.root)
    with exclusive_writer(app.root):
        _plain(app.database)
        if os.path.lexists(app.root / INCOMPLETE):
            raise RestoreError('the source library restore is incomplete')
        with TemporaryDirectory(prefix='.carr-library-backup-', dir=destination.parent) as tmp:
            stage = Path(tmp)
            snapshot = stage / DATABASE
            with closing(Storage(app.database, schema_version=4)) as storage:
                storage.backup(snapshot)
            size = _plain(snapshot).st_size
            if not 0 < size <= MAX_DATABASE:
                raise RestoreError('database exceeds backup size limit')
            with closing(Storage(snapshot, schema_version=4)) as snapshot_storage:
                included = {row[0] for row in snapshot_storage.db.execute('SELECT id FROM workspace')}
                if authorized_workspaces is not None:
                    if not included <= authorized_workspaces:
                        raise RestoreError('Backup snapshot contains a library outside the approved scope.')
                entries = copy_catalog_assets(snapshot_storage.db, app.assets, stage / ORIGINALS,
                    include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
                fingerprint = schema_fingerprint(snapshot_storage.db)
                sidecars = _snapshot_sidecars(app.root)
                _validate_sidecars(snapshot_storage.db, sidecars)
                if authorized_workspaces is not None:
                    # A typed agent's library selection does not authorize
                    # unrelated drafts, manual scripts or global preferences.
                    sidecars = {name: raw for name, raw in sidecars.items()
                        if name == READING_STATE or (name.startswith(NARRATION_DIRECTORY + '/')
                            and validate_narration_payload(name, raw).source_location is not None)}
            _stage_sidecars(stage, sidecars)
            recovery = recovery_backup.snapshot(app, stage, included, {entry.digest for entry in entries},
                include_independent=authorized_workspaces is None)
            manifest = dict(version=4 if recovery else 3 if sidecars else 2, kind='carr-library', schema_version=4,
                schema_fingerprint=fingerprint,
                database=dict(filename=DATABASE, size_bytes=size, sha256=hash_file(snapshot)),
                assets=_asset_manifest(entries))
            if sidecars or recovery:
                manifest['sidecars'] = _sidecar_manifest(sidecars)
            if recovery:
                manifest['recovery'] = recovery
            raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
            if len(raw) > MAX_MANIFEST:
                raise RestoreError('manifest size limit')
            _sync_file(stage / MANIFEST, raw)
            # mkdir provides exclusive no-replace ownership of the destination.
            # The final manifest is the completion marker; no partial bundle is
            # silently overwritten or presented as a successful backup.
            destination.mkdir(mode=0o700)
            (destination / ORIGINALS).mkdir(mode=0o700)
            os.link(snapshot, destination / DATABASE)
            for entry in entries:
                os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
            _publish_sidecars(stage, destination, sidecars)
            if recovery:
                recovery_backup.publish(stage, destination, recovery, backup=True)
            _sync_directory(destination / ORIGINALS)
            os.link(stage / MANIFEST, destination / MANIFEST)
            _sync_directory(destination)
            _sync_directory(destination.parent)
    return sha256(raw).hexdigest()


def _read_manifest(directory, expected_manifest_sha256):
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(
            r'[0-9a-f]{64}', expected_manifest_sha256):
        raise RestoreError('independently saved manifest SHA256 required')
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
            or manifest['version'] not in (1, 2, 3, 4)):
        raise RestoreError('unsupported library manifest')
    fields = {'version','kind','schema_version','database','assets'}
    if manifest['version'] >= 2:
        fields.add('schema_fingerprint')
    if manifest['version'] >= 3:
        fields.add('sidecars')
    if manifest['version'] == 4:
        fields.add('recovery')
    if (set(manifest) != fields
            or manifest['kind'] != 'carr-library'
            or type(manifest['schema_version']) is not int or manifest['schema_version'] not in (3, 4)
            or type(manifest['assets']) is not list or len(manifest['assets']) > MAX_ASSETS):
        raise RestoreError('unsupported library manifest')
    if manifest['version'] >= 2:
        fingerprint = manifest['schema_fingerprint']
        if (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)
                or fingerprint != expected_schema_fingerprint(manifest['schema_version'])):
            raise RestoreError('unsupported backup schema fingerprint')
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
    sidecars = manifest.get('sidecars', [])
    if type(sidecars) is not list or len(sidecars) > MAX_SIDECARS:
        raise RestoreError('invalid backup sidecar manifest')
    names, total, narration_total = set(), 0, 0
    for entry in sidecars:
        if (type(entry) is not dict or set(entry) != {'filename', 'size_bytes', 'sha256'}
                or not isinstance(entry['filename'], str)
                or type(entry['size_bytes']) is not int
                or not 0 < entry['size_bytes'] <= _sidecar_limit(entry['filename'])
                or not isinstance(entry['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])
                or entry['filename'] in names):
            raise RestoreError('invalid backup sidecar manifest')
        names.add(entry['filename'])
        total += entry['size_bytes']
        if entry['filename'].startswith(NARRATION_DIRECTORY + '/'):
            narration_total += entry['size_bytes']
        if narration_total > MAX_NARRATION_BYTES:
            raise RestoreError('narration backup byte limit')
        if total > MAX_SIDECAR_BYTES:
            raise RestoreError('backup sidecar byte limit')
    if manifest['version'] == 4:
        recovery = recovery_backup.validate_manifest(manifest['recovery'])
        pending = {Path(row['path']).name for row in recovery['files']
                   if row['path'].startswith('pending-originals/')}
        if pending & filenames:
            raise RestoreError('Pending originals duplicate registered library assets.')
    _check_backup_namespace(directory, names, recovery=manifest['version'] == 4)
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


def restore_backup(source, destination, expected_manifest_sha256):
    """Restore a trusted backup into a new data root; return that root's Path.

    Keep the expected digest independently of the backup. The source is never
    opened writable. A failed/terminated publication retains .restore-incomplete;
    the application must reject such a root instead of opening partial data.
    """
    source = Path(source).absolute()
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
            sidecars = _snapshot_sidecars(source)
            if _sidecar_manifest(sidecars) != manifest.get('sidecars', []):
                raise RestoreError('backup sidecar size or digest mismatch')
            _validate_sidecars(storage.db, sidecars)
            _stage_sidecars(stage, sidecars)
            original_store = RetainedAssets(source / ORIGINALS)
            entries = verify_catalog_assets(storage.db, original_store,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
            if _asset_manifest(entries) != manifest['assets']:
                raise RestoreError('asset manifest disagrees with the database')
            copy_catalog_assets(storage.db, original_store, stage / ORIGINALS,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
            if manifest['version'] == 4:
                included = {row[0] for row in storage.db.execute('SELECT id FROM workspace')}
                recovery_backup.prepare_restore(source / recovery_backup.DIRECTORY,
                    stage, manifest['recovery'], destination, included, {entry.digest for entry in entries})
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        (destination / ORIGINALS).mkdir(mode=0o700)
        os.link(snapshot, destination / DATABASE)
        for entry in entries:
            os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
        _publish_sidecars(stage, destination, sidecars)
        if manifest['version'] == 4:
            recovery_backup.publish(stage, destination, manifest['recovery'], backup=False)
        _sync_directory(destination / ORIGINALS)
        _sync_directory(destination)
    # Temporary hard links are now gone; retained originals have one link.
    (destination / INCOMPLETE).unlink()
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return destination
