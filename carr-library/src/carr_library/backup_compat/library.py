"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from bootstrap.vendor.cairn_extractors.hasher import hash_file
from ..ingest.bundle import _plain, _unique_object
from ..ingest.retained_assets import MAX_BYTES
from .formats import SUFFIXES
from ..ingest.restore import RestoreError
from ..ingest.writer_guard import _regular_if_present
from ..storage.schema_identity import expected_schema_fingerprint
from . import recovery as recovery_backup
from ..reader.reading_state import FILENAME as READING_STATE, MAX_BYTES as MAX_READING_STATE, _decode as _decode_reading_state, _file_identity
from .live_positions import FILENAME as LIVE_READING_STATE, MAX_BYTES as MAX_LIVE_READING_STATE, validate_live_state
from .narration import DIRECTORY as NARRATION_DIRECTORY, MAX_PROJECT_BYTES, MAX_PROJECTS, MAX_SNAPSHOT_BYTES as MAX_NARRATION_BYTES, validate_narration_payload
from .keybindings import FILENAME as KEYBINDINGS, MAX_BYTES as MAX_KEYBINDINGS, decode_preferences
from .keymap import MAX_BYTES as MAX_SHORTCUTS, validate_keymap
from .ocr import FILENAME as OCR_PRESETS, MAX_PRESET_BYTES, validate_presets_bytes
from .evidence import FILENAME as EVIDENCE_DRAFTS, MAX_BYTES as MAX_DRAFT_BYTES, validate_evidence_drafts
from .progress import FILENAME as PROGRESS_PREFERENCES, MAX_BYTES as MAX_PROGRESS_PREFERENCES, validate_progress_preferences
from .dordiai import FILENAME as DORDIAI_SETTINGS, MAX_BYTES as MAX_DORDIAI_SETTINGS, validate_settings_bytes as validate_dordiai_settings


DATABASE = 'library.sqlite'


ORIGINALS = 'originals'


MANIFEST = 'manifest.json'


MAX_MANIFEST = 64 * 1024 * 1024


MAX_DATABASE = 512 * 1024 * 1024


MAX_ASSETS = 10000


MAX_ASSET_BYTES = 8 * 1024**3


MAX_SIDECARS = 8 + MAX_PROJECTS * 2


MAX_SIDECAR_BYTES = (MAX_READING_STATE + MAX_NARRATION_BYTES + MAX_KEYBINDINGS
                     + MAX_PRESET_BYTES + MAX_SHORTCUTS + MAX_DRAFT_BYTES + MAX_PROGRESS_PREFERENCES
                     + MAX_LIVE_READING_STATE + MAX_DORDIAI_SETTINGS)


_SIMPLE_SIDECARS = {
    KEYBINDINGS: (MAX_KEYBINDINGS, decode_preferences),
    OCR_PRESETS: (MAX_PRESET_BYTES, validate_presets_bytes),
    'keyboard-shortcuts.json': (MAX_SHORTCUTS, validate_keymap),
    EVIDENCE_DRAFTS: (MAX_DRAFT_BYTES, validate_evidence_drafts),
    PROGRESS_PREFERENCES: (MAX_PROGRESS_PREFERENCES, validate_progress_preferences),
    DORDIAI_SETTINGS: (MAX_DORDIAI_SETTINGS, validate_dordiai_settings),
}


def _sidecar_manifest(sidecars):
    return [dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest())
            for name, raw in sorted(sidecars.items())]


def _sidecar_limit(name):
    # This is an allowlist, not a general folder backup. In particular, private
    # settings, credentials, caches and incomplete import journals cannot enter
    # a backup merely by appearing beside the research database.
    if name == READING_STATE:
        return MAX_READING_STATE
    if name == LIVE_READING_STATE:
        return MAX_LIVE_READING_STATE
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


def _validate_reading_locations(db, locations, *, text_lengths=None):
    # Historical revisions and archived libraries remain valid backup contents.
    # Do not consult the active revision or retarget an older reading position.
    # Storage.check authenticates the page digests and the separately verified
    # original inventory covers each immutable edition/asset binding.
    for location in locations:
        row = db.execute('''SELECT p.raw FROM revision r
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
        if text_lengths is not None and text_lengths[location] != len(row[0]):
            raise RestoreError('live-reading text length does not match its exact saved source')


def _validate_sidecars(db, sidecars):
    for name, raw in sidecars.items():
        _sidecar_limit(name)
        text_lengths = None
        try:
            if name == READING_STATE:
                positions, bookmarks, _ = _decode_reading_state(raw.decode('utf-8'))
                locations = {row[0] for row in positions.values()} | {
                    bookmark.location for bookmark in bookmarks.values()}
            elif name == LIVE_READING_STATE:
                positions = validate_live_state(raw)
                locations = {position.location for position in positions}
                text_lengths = {position.location: position.text_length for position in positions}
            elif name in _SIMPLE_SIDECARS:
                _SIMPLE_SIDECARS[name][1](raw)
                locations = set()
            else:
                project = validate_narration_payload(name, raw)
                locations = {project.source_location} if project.source_location is not None else set()
        except (UnicodeError, ValueError) as exc:
            raise RestoreError('invalid reading state, narration copy or preferences in backup') from exc
        _validate_reading_locations(db, locations, text_lengths=text_lengths)


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
