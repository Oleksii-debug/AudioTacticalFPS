"""Complete local research-library backups using the canonical SQLite snapshot.

Backups include all workspaces, saved research, registered original files and
the optional reading-state sidecar containing bookmarks and resume positions,
plus saved narration copies and their recoverable drafts.
Keyboard preferences and named OCR-language selections are included when present.
Provider profiles are sanitized; the atomic cost ledger preserves reservations.
Pending import requests, batches, reviews and journals retain their exact source
bytes and identities, including completed entries used by historical replay.
The independently retained manifest digest binds the database and every asset.
Version 2 also identifies the exact schema, not just its numeric user_version.
Version 3 extends that manifest with explicitly named, checksummed sidecars.
Version 4 includes progress notification preferences, disconnected provider
settings and a checked SQLite ledger, with no restored remote authorization or
credential references. Typed assistant checkpoints retain their history; every
restored job requires a new review of its current permissions before execution.
Version 5 adds a typed pending-import component to the exact version-4 shape.
Backups without ASR/resource-policy state use version 5, including an empty pending manifest.
Version 6 adds a nonempty typed ASR/resource-policy component under the same aggregate ceilings.
Legacy version 1/2/3/4/5 backups and version 6 backups are accepted only for an exact
supported V3, V4 or V5 database schema. Older snapshots are restored unchanged;
opening the restored library performs the normal checked upgrade to the current schema.
Restoration always creates a new data directory and never merges or overwrites.
"""
from contextlib import closing, nullcontext
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from tempfile import TemporaryDirectory

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from .ingest.bundle import _plain, _unique_object
from .ingest.catalog_asset_copy import catalog_inventory, copy_catalog_assets, verify_catalog_assets
from .ingest.retained_assets import MAX_BYTES, SUFFIXES, RetainedAssets
from .ingest.restore import RestoreError
from .ingest.writer_guard import _regular_if_present, exclusive_writer
from .data_paths import guard_data_directory
from .storage.revisions import Storage, CURRENT_SCHEMA_VERSION
from .storage.schema_identity import expected_schema_fingerprint, schema_fingerprint
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
from .providers import (
    PROFILES_FILENAME, LEDGER_FILENAME, MAX_REQUEST as MAX_PROFILE_BYTES,
    MAX_LEDGER_BYTES, snapshot_provider_files, validate_backup_profiles,
    validate_ledger_snapshot,
)
from .progress import (
    FILENAME as PROGRESS_SETTINGS, MAX_BYTES as MAX_PROGRESS_SETTINGS,
    validate_progress_settings_payload,
)

from .assistant.backup import (
    MAX_FILES as MAX_ASSISTANT_FILES, MAX_BYTES as MAX_ASSISTANT_BYTES,
    is_sidecar as is_assistant_sidecar, sidecar_limit as assistant_sidecar_limit,
    snapshot_files as snapshot_assistant_files, validate_pairs as validate_assistant_pairs,
    add_restore_reviews,
)
from .assistant.backup_state import validate_state_bytes as validate_assistant_state
from .assistant.translation_state import (
    is_state_path as is_translation_sidecar, state_limit as translation_sidecar_limit,
    snapshot_guard as translation_snapshot_guard, snapshot_files as snapshot_translation_files,
    validate_state_document as validate_translation_state,
)
from .pending_backup import (
    MAX_FILES as MAX_PENDING_FILES, MAX_TOTAL as MAX_PENDING_BYTES,
    snapshot_pending, validate_pending, restore_pending,
)

DATABASE = 'library.sqlite'
ORIGINALS = 'originals'
PENDING = 'pending'
TRANSCRIPTION = 'transcription'
from .transcription.backup import (
    snapshot as snapshot_transcription, restore as restore_transcription,
    validate_manifest as validate_transcription_manifest,
    MAX_FILES as MAX_TRANSCRIPTION_FILES, MAX_BYTES as MAX_TRANSCRIPTION_BYTES,
)
MANIFEST = 'manifest.json'
INCOMPLETE = '.restore-incomplete'
MAX_MANIFEST = 8 * 1024 * 1024
MAX_DATABASE = 512 * 1024 * 1024
MAX_ASSETS = 10000
MAX_ASSET_BYTES = 8 * 1024**3
MAX_SIDECARS = 6 + MAX_PROJECTS * 2 + MAX_ASSISTANT_FILES
MAX_SIDECAR_BYTES = (MAX_READING_STATE + MAX_NARRATION_BYTES + MAX_KEYBINDINGS
                     + MAX_PRESET_BYTES + MAX_PROFILE_BYTES + MAX_LEDGER_BYTES
                     + MAX_PROGRESS_SETTINGS + MAX_ASSISTANT_BYTES)
MAX_BACKUP_BYTES = MAX_DATABASE + MAX_ASSET_BYTES + MAX_SIDECAR_BYTES + MAX_MANIFEST
MAX_BACKUP_FILES = 2 + MAX_ASSETS + MAX_SIDECARS
_SIMPLE_SIDECARS = {
    KEYBINDINGS: (MAX_KEYBINDINGS, decode_preferences),
    OCR_PRESETS: (MAX_PRESET_BYTES, validate_presets_bytes),
}
_PROVIDER_SIDECARS = {
    PROFILES_FILENAME: (MAX_PROFILE_BYTES, validate_backup_profiles),
    LEDGER_FILENAME: (MAX_LEDGER_BYTES, validate_ledger_snapshot),
}
_V4_SIMPLE_SIDECARS = {
    PROGRESS_SETTINGS: (MAX_PROGRESS_SETTINGS, validate_progress_settings_payload),
}
_V4_SIDECARS = _PROVIDER_SIDECARS | _V4_SIMPLE_SIDECARS


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


def _sidecar_limit(name, *, version=4):
    # This is an allowlist, not a general folder backup. In particular, private
    # settings, credentials, caches and untyped neighboring files cannot enter
    # a backup merely by appearing beside the research database.
    if name == READING_STATE:
        return MAX_READING_STATE
    if name in _SIMPLE_SIDECARS:
        return _SIMPLE_SIDECARS[name][0]
    if version >= 4 and name in _V4_SIDECARS:
        return _V4_SIDECARS[name][0]
    if version >= 4 and is_assistant_sidecar(name):
        return assistant_sidecar_limit(name)
    if version >= 4 and is_translation_sidecar(name):
        return translation_sidecar_limit(name)
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


def _snapshot_sidecars(root, *, live=False):
    # Keep translation receipts frozen through the shared ledger snapshot; an
    # archive is checked read-only and must never acquire/create live lock files.
    guard = translation_snapshot_guard(root) if live else nullcontext()
    with guard:
        raw = ReadingState(root).snapshot()
        sidecars = {} if raw is None else {READING_STATE: raw}
        sidecars.update(snapshot_narration_files(root))
        sidecars.update(snapshot_assistant_files(root, live=live))
        sidecars.update(snapshot_translation_files(root, live=live))
        for name, (maximum, validate) in (_SIMPLE_SIDECARS | _V4_SIMPLE_SIDECARS).items():
            raw = _optional_sidecar(Path(root) / name, maximum)
            if raw is not None:
                validate(raw)
                sidecars[name] = raw
        if live:
            # Profiles must be exported without vault references, and a live SQLite
            # ledger must use its backup API rather than copying a possibly stale
            # main file while newer committed operations reside in WAL pages.
            sidecars.update(snapshot_provider_files(root))
        else:
            # A published backup already contains a sanitized closed snapshot.
            # Verify its exact manifest bytes before trusting its contents; never
            # sanitize or repair an archive while restoring it.
            for name, (maximum, _) in _PROVIDER_SIDECARS.items():
                raw = _optional_sidecar(Path(root) / name, maximum)
                if raw is not None:
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
    validate_assistant_pairs(sidecars)
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
            elif name in _V4_SIDECARS:
                _V4_SIDECARS[name][1](raw)
                locations = set()
            elif is_translation_sidecar(name):
                validate_translation_state(name.rsplit('/', 1)[-1], raw)
                locations = set()
            elif is_assistant_sidecar(name):
                for workspace in validate_assistant_state(name, raw):
                    if db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (workspace,)).fetchone() is None:
                        raise ValueError('Assistant checkpoint belongs to a missing library.')
                locations = set()
            else:
                project = validate_narration_payload(name, raw)
                locations = {project.source_location} if project.source_location is not None else set()
        except (UnicodeError, ValueError) as exc:
            raise RestoreError('invalid reading state, narration copy, preferences or provider state in backup') from exc
        _validate_reading_locations(db, locations)


def _stage_sidecars(stage, sidecars, *, cancelled=lambda: False):
    for name, raw in sorted(sidecars.items()):
        _check_cancelled(cancelled)
        (stage / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _sync_file(stage / name, raw)


def _publish_sidecars(stage, destination, sidecars, *, cancelled=lambda: False):
    for name in sorted(sidecars):
        _check_cancelled(cancelled)
        (destination / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.link(stage / name, destination / name)
    parents = {parent for name in sidecars for parent in (destination / name).parents
               if parent == destination or parent.is_relative_to(destination)}
    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        _sync_directory(parent)


def _check_backup_namespace(directory, names, *, pending=False, transcription=False):
    names = set(names)
    directories = {str(parent) for name in names for parent in PurePosixPath(name).parents
                   if str(parent) != '.'}
    roots = {name.split('/')[0] for name in names}
    expected_roots = {DATABASE, ORIGINALS, MANIFEST} | roots
    if pending:
        expected_roots.add(PENDING)
        _plain(directory / PENDING, directory=True)
    if transcription:
        expected_roots.add(TRANSCRIPTION)
        _plain(directory / TRANSCRIPTION, directory=True)
    if set(p.name for p in directory.iterdir()) != expected_roots:
        raise RestoreError('incomplete backup or unexpected files')
    for name in sorted(directories):
        folder = directory / name
        _plain(folder, directory=True)
        actual = {p.relative_to(directory).as_posix() for p in folder.iterdir()}
        expected = {p for p in names | directories if str(PurePosixPath(p).parent) == name}
        if actual != expected:
            raise RestoreError('incomplete sidecar backup or unexpected files')
    for name in names:
        _regular_if_present(directory / name)


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


def _check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Library backup cancelled.')


def _backup_limits(max_bytes, max_files):
    values = []
    for value, maximum, label in (
            (max_bytes, MAX_BACKUP_BYTES, 'byte'),
            (max_files, MAX_BACKUP_FILES, 'file')):
        if value is None:
            value = maximum
        if type(value) is not int or not 0 < value <= maximum:
            raise RestoreError('invalid backup ' + label + ' limit')
        values.append(value)
    return tuple(values)


def _check_backup_budget(size, files, maximum_bytes, maximum_files):
    if size > maximum_bytes:
        raise RestoreError('backup exceeds its total byte limit')
    if files > maximum_files:
        raise RestoreError('backup exceeds its total file limit')


def create_backup(app, destination, *, max_bytes=None, max_files=None,
                  cancelled=lambda: False):
    """Back up every library to a fresh folder and return its manifest SHA256.

    The optional limits cover all delivered regular files, including the final
    manifest. They may lower the supported aggregate ceilings, never raise them.
    Cancellation is cooperative between checked staging/publication operations;
    a SQLite backup or catalog-original copy operation finishes before the next
    check. Once the final manifest is published, completion wins a late cancel.

    The host's explicit Backup action authorizes all-workspace inclusion. The
    private source/destination parents must be stable local directories. A
    publication interrupted before manifest.json is incomplete and not restorable.
    """
    maximum_bytes, maximum_files = _backup_limits(max_bytes, max_files)
    if not callable(cancelled):
        raise RestoreError('invalid backup cancellation callback')
    _check_cancelled(cancelled)
    # Even an empty library delivers its database and the completion manifest.
    _check_backup_budget(0, 2, maximum_bytes, maximum_files)
    destination = _destination(destination, app.root)
    with exclusive_writer(app.root):
        _check_cancelled(cancelled)
        _plain(app.database)
        if os.path.lexists(app.root / INCOMPLETE):
            raise RestoreError('the source library restore is incomplete')
        with TemporaryDirectory(prefix='.carr-library-backup-', dir=destination.parent) as tmp:
            stage = Path(tmp)
            snapshot = stage / DATABASE
            with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
                storage.backup(snapshot)
            _check_cancelled(cancelled)
            size = _plain(snapshot).st_size
            if not 0 < size <= MAX_DATABASE:
                raise RestoreError('database exceeds backup size limit')
            _check_backup_budget(size, 2, maximum_bytes, maximum_files)
            with closing(Storage(snapshot, schema_version=CURRENT_SCHEMA_VERSION)) as snapshot_storage:
                planned = catalog_inventory(snapshot_storage.db,
                    include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
                _check_backup_budget(size + sum(entry.size for entry in planned),
                                     2 + len(planned), maximum_bytes, maximum_files)
                _check_cancelled(cancelled)
                entries = copy_catalog_assets(snapshot_storage.db, app.assets, stage / ORIGINALS,
                    include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
                _check_cancelled(cancelled)
                fingerprint = schema_fingerprint(snapshot_storage.db)
                sidecars = _snapshot_sidecars(app.root, live=True)
                _validate_sidecars(snapshot_storage.db, sidecars)
            _check_cancelled(cancelled)
            sidecar_bytes = sum(len(raw) for raw in sidecars.values())
            if len(sidecars) > MAX_SIDECARS or sidecar_bytes > MAX_SIDECAR_BYTES:
                raise RestoreError('backup sidecar inventory limit')
            total_bytes = size + sum(entry.size for entry in entries) + sidecar_bytes
            total_files = 2 + len(entries) + len(sidecars)
            _check_backup_budget(total_bytes, total_files, maximum_bytes, maximum_files)
            _stage_sidecars(stage, sidecars, cancelled=cancelled)
            pending = snapshot_pending(app, stage / PENDING, cancelled=cancelled,
                max_bytes=min(MAX_PENDING_BYTES, maximum_bytes - total_bytes),
                max_files=min(MAX_PENDING_FILES, maximum_files - total_files))
            total_bytes += sum(entry['size_bytes'] for entry in pending['files'])
            total_files += len(pending['files'])
            _check_backup_budget(total_bytes, total_files, maximum_bytes, maximum_files)
            try:
                transcription = snapshot_transcription(app.root, stage / TRANSCRIPTION,
                    cancelled=cancelled,
                    max_bytes=min(MAX_TRANSCRIPTION_BYTES, maximum_bytes - total_bytes),
                    max_files=min(MAX_TRANSCRIPTION_FILES, maximum_files - total_files))
            except (ValueError, TypeError, KeyError) as exc:
                raise RestoreError('invalid or inconsistent transcription state: ' + str(exc)) from exc
            total_bytes += sum(entry['size_bytes'] for entry in transcription['files'])
            total_files += len(transcription['files'])
            _check_backup_budget(total_bytes, total_files, maximum_bytes, maximum_files)
            manifest = dict(version=6 if transcription['files'] else 5,
                kind='carr-library', schema_version=CURRENT_SCHEMA_VERSION,
                schema_fingerprint=fingerprint,
                database=dict(filename=DATABASE, size_bytes=size, sha256=hash_file(snapshot)),
                assets=_asset_manifest(entries), sidecars=_sidecar_manifest(sidecars),
                provider_restore_mode='LOCAL_ONLY', pending=pending)
            if transcription['files']:
                manifest['transcription'] = transcription
            raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
            if len(raw) > MAX_MANIFEST:
                raise RestoreError('manifest size limit')
            _check_backup_budget(total_bytes + len(raw), total_files,
                                 maximum_bytes, maximum_files)
            _check_cancelled(cancelled)
            _sync_file(stage / MANIFEST, raw)
            # mkdir provides exclusive no-replace ownership of the destination.
            # The final manifest is the completion marker; no partial bundle is
            # silently overwritten or presented as a successful backup.
            _check_cancelled(cancelled)
            destination.mkdir(mode=0o700)
            (destination / ORIGINALS).mkdir(mode=0o700)
            _check_cancelled(cancelled)
            os.link(snapshot, destination / DATABASE)
            for entry in entries:
                _check_cancelled(cancelled)
                os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
            _publish_sidecars(stage, destination, sidecars, cancelled=cancelled)
            _check_cancelled(cancelled)
            # The checked private component moves on this same filesystem.
            # No temporary hard links remain in its journal or source inventory.
            (stage / PENDING).rename(destination / PENDING)
            if transcription['files']:
                _check_cancelled(cancelled)
                (stage / TRANSCRIPTION).rename(destination / TRANSCRIPTION)
            _sync_directory(destination / ORIGINALS)
            _check_cancelled(cancelled)
            os.link(stage / MANIFEST, destination / MANIFEST)
            _sync_directory(destination)
            _sync_directory(destination.parent)
    return sha256(raw).hexdigest()

def _read_manifest(directory, expected_manifest_sha256):
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(
            r'[0-9a-f]{64}', expected_manifest_sha256):
        raise RestoreError('independently saved manifest SHA256 required')
    try:
        size = _plain(directory / MANIFEST).st_size
    except FileNotFoundError as exc:
        raise RestoreError('incomplete backup: the final manifest was not published') from exc
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
    fields = {'version','kind','schema_version','database','assets'}
    if manifest['version'] >= 2:
        fields.add('schema_fingerprint')
    if manifest['version'] >= 3:
        fields.add('sidecars')
    if manifest['version'] >= 4:
        fields.add('provider_restore_mode')
    if manifest['version'] >= 5:
        fields.add('pending')
    if manifest['version'] == 6:
        fields.add('transcription')
    if (set(manifest) != fields
            or manifest['kind'] != 'carr-library'
            or type(manifest['schema_version']) is not int or manifest['schema_version'] not in (3, 4, 5)
            or type(manifest['assets']) is not list or len(manifest['assets']) > MAX_ASSETS):
        raise RestoreError('unsupported library manifest')
    if manifest['version'] >= 4 and manifest['provider_restore_mode'] != 'LOCAL_ONLY':
        raise RestoreError('restored provider state requires LOCAL ONLY mode')
    if manifest['version'] >= 5:
        try:
            validate_pending(manifest['pending'])
        except (ValueError, TypeError, KeyError) as exc:
            raise RestoreError('invalid pending import backup manifest') from exc
    if manifest['version'] == 6:
        try:
            validate_transcription_manifest(manifest['transcription'])
            if not manifest['transcription']['files']:
                raise ValueError('version 6 requires typed transcription or resource-policy state')
        except (ValueError, TypeError, KeyError) as exc:
            raise RestoreError('invalid transcription backup manifest') from exc
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
                or not 0 < entry['size_bytes'] <= _sidecar_limit(entry['filename'], version=manifest['version'])
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
    if manifest['version'] == 4 and not (names.intersection(_V4_SIDECARS)
                                        or any(is_assistant_sidecar(name) or is_translation_sidecar(name) for name in names)):
        raise RestoreError('version 4 backup manifest has no version 4 state')
    # Pending state shares the existing aggregate resource contract on read as
    # well as write. A newly signed outer manifest cannot increase that ceiling.
    inventory = declared_backup_files(manifest)
    _check_backup_budget(size + sum(entry['size_bytes'] for entry in inventory.values()),
                         1 + len(inventory), MAX_BACKUP_BYTES, MAX_BACKUP_FILES)
    _check_backup_namespace(directory, names, pending=manifest['version'] >= 5,
                            transcription=manifest['version'] == 6)
    return manifest


def declared_backup_files(manifest):
    """List delivered payload files from an already validated library manifest.

    Call only after _read_manifest has accepted the exact supported schema.
    manifest.json itself is excluded: its trusted digest comes from the producer,
    and its byte count must be taken from the checked serialized manifest.
    Pending assets and source bindings index pending.files; they are not copies.
    """
    files = {DATABASE: {key: manifest['database'][key]
                        for key in ('size_bytes', 'sha256')}}
    for prefix, entries in (
            (ORIGINALS + '/', manifest['assets']),
            ('', manifest.get('sidecars', [])),
            (PENDING + '/', manifest.get('pending', {}).get('files', [])),
            (TRANSCRIPTION + '/', manifest.get('transcription', {}).get('files', []))):
        for entry in entries:
            files[prefix + entry['filename']] = {
                key: entry[key] for key in ('size_bytes', 'sha256')}
    return files


def _copy_database(source, target, library_manifest, *, cancelled=lambda: False):
    manifest = library_manifest['database']
    if _plain(source).st_size != manifest['size_bytes']:
        raise RestoreError('database size mismatch')
    count = 0
    digest = sha256()
    with source.open('rb') as incoming, target.open('xb') as output:
        while block := incoming.read(min(1024 * 1024, manifest['size_bytes'] + 1 - count)):
            _check_cancelled(cancelled)
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
        raise RestoreError('invalid restore cancellation callback')
    _check_cancelled(cancelled)
    source = Path(source).absolute()
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    destination = _destination(destination, source)
    manifest = _read_manifest(source, expected_manifest_sha256)
    _plain(source / ORIGINALS, directory=True)
    with TemporaryDirectory(prefix='.carr-library-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        snapshot = stage / DATABASE
        _copy_database(source / DATABASE, snapshot, manifest, cancelled=cancelled)
        with closing(Storage(snapshot, schema_version=manifest['schema_version'])) as storage:
            storage.check()
            sidecars = _snapshot_sidecars(source)
            if _sidecar_manifest(sidecars) != manifest.get('sidecars', []):
                raise RestoreError('backup sidecar size or digest mismatch')
            _validate_sidecars(storage.db, sidecars)
            sidecars = add_restore_reviews(sidecars)
            _validate_sidecars(storage.db, sidecars)
            _stage_sidecars(stage, sidecars, cancelled=cancelled)
            original_store = RetainedAssets(source / ORIGINALS)
            entries = verify_catalog_assets(storage.db, original_store,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
            if _asset_manifest(entries) != manifest['assets']:
                raise RestoreError('asset manifest disagrees with the database')
            copy_catalog_assets(storage.db, original_store, stage / ORIGINALS,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
        pending = (restore_pending(source / PENDING, stage, destination, manifest['pending'],
                                   cancelled=cancelled) if manifest['version'] >= 5
                   else {'assets': [], 'paths': []})
        try:
            transcription_paths = (restore_transcription(source / TRANSCRIPTION, stage,
                manifest['transcription'], cancelled=cancelled) if manifest['version'] == 6 else [])
        except (ValueError, TypeError, KeyError) as exc:
            raise RestoreError('invalid or incomplete transcription backup: ' + str(exc)) from exc
        _check_cancelled(cancelled)
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        (destination / ORIGINALS).mkdir(mode=0o700)
        os.link(snapshot, destination / DATABASE)
        for entry in entries:
            _check_cancelled(cancelled)
            os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
        for entry in pending['assets']:
            _check_cancelled(cancelled)
            os.link(stage / ORIGINALS / entry['filename'], destination / ORIGINALS / entry['filename'])
        _publish_sidecars(stage, destination, sidecars, cancelled=cancelled)
        for name in pending['paths']:
            _check_cancelled(cancelled)
            (stage / name).rename(destination / name)
        for name in transcription_paths:
            _check_cancelled(cancelled)
            (destination / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            (stage / name).rename(destination / name)
            _sync_directory((destination / name).parent)
        _sync_directory(destination / ORIGINALS)
        _sync_directory(destination)
    # Temporary hard links are now gone; retained originals have one link.
    (destination / INCOMPLETE).unlink()
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return destination
