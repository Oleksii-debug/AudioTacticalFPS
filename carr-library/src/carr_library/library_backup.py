"""Complete local research-library backups using the canonical SQLite snapshot.

Backups include all workspaces, saved research and registered original files,
reader/editor/preferences state, assistant jobs and managed Audio Studio projects.
Version 5 also retains validated import journals, reviews and the exact source
bytes needed by queued or unfinished imports. Unreferenced retained bytes stay out.
The independently retained manifest digest binds the database and every asset.
Version 2 also identifies the exact schema, not just its numeric user_version.
Version 3 preserves the reader foundation's filename/root sidecar schema.
Version 4 adds managed audio/assistant state using path/sidecars and streamed
file checksums. Version 5 adds the typed pending-import component. The alternate
historical v3/v4 state-folder schemas are dispatched explicitly, never guessed. Live audio jobs must release their project locks. Assistant
publications share the application writer and state guards; a backup may borrow
only its own live same-process/thread execution guard.
Legacy version 1 backups and newer backups are accepted only for an exact
supported V3, V4 or V5 database schema. Older snapshots are restored unchanged;
opening the restored library performs the normal checked upgrade to V5.
Restoration always creates a new data directory and never merges or overwrites.
"""
from contextlib import closing, contextmanager, ExitStack, nullcontext
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import stat
from tempfile import TemporaryDirectory
import unicodedata

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from .ingest.bundle import _plain, _unique_object
from .ingest.catalog_asset_copy import copy_catalog_assets, verify_catalog_assets
from .ingest.retained_assets import MAX_BYTES, SUFFIXES, RetainedAssets
from .ingest.restore import RestoreError
from .ingest.writer_guard import exclusive_writer, _regular_if_present
from .data_paths import guard_data_directory
from .backup_files import local_path, publish_tree
from .backup_historical import (
    DIRECTORY as HISTORICAL_DIRECTORY, is_state_path as _historical_path,
    state_paths as _historical_files, validate_state_tree as _validate_historical,
    retain_state as _retain_historical,
)
from .operations.policy_backup import (
    MAX_BYTES as MAX_RESOURCE_POLICY,
    is_state_path as _resource_policy_path, state_paths as _resource_policy_files,
    validate_state_tree as _validate_resource_policy, snapshot_guard as _resource_policy_guard,
)
from .storage.revisions import Storage, CURRENT_SCHEMA_VERSION
from .storage.schema_identity import expected_schema_fingerprint, schema_fingerprint
from .reader.reading_state import (
    FILENAME as READING_STATE, MAX_BYTES as MAX_READING_STATE, ReadingState,
    ReadingLocation, _decode as _decode_reading_state, _file_identity,
)
from .reader.narration import (
    DIRECTORY as NARRATION_DIRECTORY, MAX_PROJECT_BYTES, MAX_PROJECTS,
    MAX_SNAPSHOT_BYTES as MAX_NARRATION_BYTES,
    snapshot_narration_files, validate_narration_payload,
)
from .keybindings import FILENAME as KEYBINDINGS, MAX_BYTES as MAX_KEYBINDINGS, decode_preferences
from .keyboard_preferences import MAX_BYTES as MAX_KEYMAP_BYTES, validate_keymap
from .evidence_drafts import (
    FILENAME as EVIDENCE_DRAFTS, MAX_BYTES as MAX_EVIDENCE_DRAFTS, validate_evidence_drafts,
)
from .extract.ocr_languages import FILENAME as OCR_PRESETS, MAX_PRESET_BYTES, validate_presets_bytes
from .progress import (
    FILENAME as PROGRESS_PREFERENCES, MAX_BYTES as MAX_PROGRESS_PREFERENCES,
    decode_preferences as decode_progress_preferences,
)
from .assistant.snapshots import (
    MAX_JOBS as MAX_ASSISTANT_JOBS, MAX_SNAPSHOT_BYTES as MAX_ASSISTANT_BYTES,
    is_state_path as _assistant_path, state_limit as _assistant_limit,
    state_paths as _assistant_files, validate_state_tree as _validate_assistants,
)

DATABASE = 'library.sqlite'
ORIGINALS = 'originals'
MANIFEST = 'manifest.json'
INCOMPLETE = '.restore-incomplete'
MAX_MANIFEST = 8 * 1024 * 1024
MAX_DATABASE = 512 * 1024 * 1024
MAX_ASSETS = 10000
MAX_ASSET_BYTES = 8 * 1024**3
SIDECARS = 'sidecars'
PENDING = 'pending'
MAX_SIDECAR_FILES = 100000
MAX_SIDECAR_BYTES = 128 * 1024**3
MAX_SIDECAR_FILE_BYTES = 32 * 1024**3
MAX_SIDECAR_JSON_BYTES = 64 * 1024**2
MAX_AUDIO_PROJECTS = 1000
_PROJECT_ID = re.compile(r'[0-9a-f]{32}')
_SIMPLE_SIDECARS = {
    KEYBINDINGS: (MAX_KEYBINDINGS, decode_preferences),
    'keyboard-shortcuts.json': (MAX_KEYMAP_BYTES, validate_keymap),
    EVIDENCE_DRAFTS: (MAX_EVIDENCE_DRAFTS, validate_evidence_drafts),
    PROGRESS_PREFERENCES: (MAX_PROGRESS_PREFERENCES, decode_progress_preferences),
    OCR_PRESETS: (MAX_PRESET_BYTES, validate_presets_bytes),
}
MAX_READER_BYTES = MAX_READING_STATE + MAX_NARRATION_BYTES + sum(row[0] for row in _SIMPLE_SIDECARS.values())


def _reader_limit(name):
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


def _reader_path(name):
    return (name == READING_STATE or name in _SIMPLE_SIDECARS
            or re.fullmatch(re.escape(NARRATION_DIRECTORY) + r'/[0-9a-f]{32}(?:\.draft)?\.json', name))

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

def _snapshot_readers(root):
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
            raise RestoreError('reading-state location disagrees with the backup database, '
                               'or narration copy does not match its exact saved source')

def _validate_readers(db, sidecars):
    for name, raw in sidecars.items():
        _reader_limit(name)
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


def _sidecar_budget(max_files, max_bytes, cancelled):
    if (type(max_files) is not int or not 1 <= max_files <= MAX_SIDECAR_FILES
            or type(max_bytes) is not int or not 1 <= max_bytes <= 1024**5
            or not callable(cancelled)):
        raise RestoreError('invalid sidecar backup budget')
    if cancelled():
        raise InterruptedError('Library backup or restore cancelled.')


def _portable_relative(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value:
        raise RestoreError('invalid audio-project artifact path')
    path = PurePosixPath(value)
    if (path.is_absolute() or len(path.parts) > 16
            or any(part in ('', '.', '..') for part in value.split('/'))
            or any(part.rstrip(' .') != part or PureWindowsPath(part).is_reserved()
                   or any(c in '<>:"|?*' or ord(c) < 32 or 127 <= ord(c) < 160 for c in part)
                   for part in path.parts)):
        raise RestoreError('invalid audio-project artifact path')
    return path.parts


def _translation_adapter():
    # A separate typed producer joins this backup without importing another
    # assistant or gateway. Its absence is harmless only when no such state is
    # present; a declared translation namespace must never be silently omitted.
    try:
        from .assistant import translation_state
    except ImportError as exc:
        raise RestoreError('The translation state backup adapter is unavailable.') from exc
    return translation_state


def _translation_path(value):
    if not isinstance(value, str) or not value.startswith('state/translation/'):
        return False
    return _translation_adapter().is_state_path(value)


def _translation_files(root, *, cancelled=lambda: False):
    if not os.path.lexists(root / 'state' / 'translation'):
        return ()
    return _translation_adapter().state_paths(root, cancelled=cancelled)


def _validate_translations(db, root, *, cancelled=lambda: False):
    if os.path.lexists(root / 'state' / 'translation'):
        _translation_adapter().validate_state_tree(db, root, cancelled=cancelled)


def _transcription_adapter():
    try:
        from . import transcription_backup
    except ImportError as exc:
        raise RestoreError('The supplied-transcription backup adapter is unavailable.') from exc
    return transcription_backup


def _transcription_path(value):
    if not isinstance(value, str) or not value.startswith('transcription-jobs/'):
        return False
    return _transcription_adapter().is_state_path(value)


def _transcription_files(root, *, cancelled=lambda: False):
    if not os.path.lexists(root / 'transcription-jobs'):
        return ()
    return _transcription_adapter().state_paths(root, cancelled=cancelled)


def _validate_transcriptions(db, root, *, cancelled=lambda: False):
    if os.path.lexists(root / 'transcription-jobs'):
        _transcription_adapter().validate_state_tree(db, root, cancelled=cancelled)


def _sidecar_allowed(value):
    parts = _portable_relative(value)
    if (_reader_path(value) or _assistant_path(value) or _historical_path(value)
            or _translation_path(value) or _transcription_path(value) or _resource_policy_path(value)):
        return True
    if (len(parts) < 3 or parts[0] != 'audio-projects'
            or not _PROJECT_ID.fullmatch(parts[1])):
        return False
    tail = '/'.join(parts[2:])
    if tail == 'restore-manifest.json':
        return True
    from .audio.backup import _allowed
    return bool(_allowed(tail))


def _claim_path(relative, names, *, allow_existing=False):
    """Reject aliases of either a file or one of its parent directories."""
    parts = _portable_relative(relative)
    for count in range(1, len(parts) + 1):
        name = '/'.join(parts[:count])
        key = unicodedata.normalize('NFC', name).casefold()
        item = (name, count == len(parts))
        previous = names.get(key)
        if previous is not None and (previous != item or (item[1] and not allow_existing)):
            raise RestoreError('duplicate or conflicting reading/audio paths on Windows')
        names[key] = item


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _production_link_path(relative):
    parts = PurePosixPath(relative).parts
    if len(parts) < 5 or parts[:1] != ('audio-projects',) or parts[2] != 'audio' or parts[3] not in ('cache', 'renders'):
        return False
    from .audio.backup import _allowed
    return bool(_allowed('/'.join(parts[2:])))


def _check_internal_links(root, inventory):
    """Canonical production links cache chunks into completed renders.

    Permit those links only when every name for that inode belongs to this
    bounded snapshot and the same project. Copies in the archive are independent.
    """
    transcription_pairs = (_transcription_adapter().internal_link_pairs(root)
        if any(identity[3] > 1 and _transcription_path(name)
               for name, identity in inventory.items()) else set())
    groups = {}
    for name, identity in inventory.items():
        if identity[3] > 1:
            groups.setdefault(identity[:2], []).append((name, identity))
    for group in groups.values():
        names = [name for name, _ in group]
        identities = {identity for _, identity in group}
        if (frozenset(names) in transcription_pairs and len(identities) == 1
                and len(group) == group[0][1][3] == 2):
            continue
        if (len(identities) != 1 or len(group) != group[0][1][3]
                or any(not _production_link_path(name) for name in names)
                or len({PurePosixPath(name).parts[1] for name in names}) != 1):
            raise RestoreError('audio hard links extend outside the verified project inventory')


def _sidecar_info(root, relative):
    if not _sidecar_allowed(relative):
        raise RestoreError('unsupported audio-project artifact')
    parts = _portable_relative(relative)
    parent = root
    _plain(parent, directory=True)
    for part in parts[:-1]:
        parent = parent / part
        _plain(parent, directory=True)
    info = _plain(parent / parts[-1])
    transcription_link = (_transcription_path(relative)
        and _transcription_adapter().link_path(relative))
    if info.st_nlink < 1 or (info.st_nlink != 1 and not _production_link_path(relative) and not transcription_link):
        raise RestoreError('sidecar files cannot be hard links')
    limit = (_reader_limit(relative) if _reader_path(relative) else
             _assistant_limit(relative) if _assistant_path(relative) else
             _translation_adapter().state_limit(relative) if _translation_path(relative) else
             _transcription_adapter().state_limit(relative) if _transcription_path(relative) else
             MAX_RESOURCE_POLICY if _resource_policy_path(relative) else
             MAX_SIDECAR_JSON_BYTES if relative.lower().endswith('.json') else MAX_SIDECAR_FILE_BYTES)
    minimum = 0 if (_historical_path(relative) or relative.lower().endswith(('.srt', '.vtt', '.ffmeta'))
        or _transcription_path(relative) and _transcription_adapter().allows_empty(relative)) else 1
    if not minimum <= info.st_size <= limit:
        raise RestoreError('sidecar file exceeds its size limit')
    return info


def _children(path, limit):
    _plain(path, directory=True)
    children = []
    for child in path.iterdir():
        if len(children) >= limit:
            raise RestoreError('audio-project directory exceeds entry limit')
        children.append(child)
    return tuple(sorted(children))


def _projects(root):
    folder = root / 'audio-projects'
    if not os.path.lexists(folder):
        return ()
    result = []
    for child in _children(folder, MAX_AUDIO_PROJECTS):
        if _PROJECT_ID.fullmatch(child.name):
            _plain(child, directory=True)
            result.append(child)
    return tuple(result)


def _audio_document(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_SIDECAR_JSON_BYTES + 1)
    if len(raw) > MAX_SIDECAR_JSON_BYTES:
        raise RestoreError('audio-project metadata exceeds its size limit')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RestoreError('invalid audio-project metadata') from exc
    if type(value) is not dict:
        raise RestoreError('invalid audio-project metadata')
    return value


def _assistant_documents(root, max_files=MAX_SIDECAR_FILES):
    """Reuse the canonical strict namespace and caller's tighter file budget."""
    documents = _assistant_files(root)
    if len(documents) > max_files:
        raise RestoreError('assistant checkpoints exceed the backup file budget')
    return documents


@contextmanager
def _locked_sidecars(root, max_files=MAX_SIDECAR_FILES, *, cancelled=lambda: False):
    from filelock import FileLock, Timeout
    from .audio.jobs import project_lock
    from .assistant.state import snapshot_job_guard, state_write_guard
    projects = _projects(root)
    assistant = _assistant_documents(root, max_files)
    translation = _translation_files(root)
    transcription = ()
    resource_policy = _resource_policy_files(root, cancelled=cancelled)
    with ExitStack() as stack:
        directory = root / 'state' / 'assistant'
        if os.path.lexists(directory):
            _plain(directory, directory=True)
            try:
                # The outer application writer is already held. Publication and
                # snapshot use that same order; never reacquire it reentrantly.
                stack.enter_context(state_write_guard(directory))
                for identity in sorted({path.name[:32] for path in assistant}):
                    stack.enter_context(snapshot_job_guard(directory, identity))
            except RuntimeError as exc:
                raise RestoreError('An assistant job or control is being updated. Retry the backup after it stops.') from exc
            if _assistant_documents(root, max_files) != assistant:
                raise RestoreError('Assistant checkpoints changed before backup. Retry when they are idle.')
        if os.path.lexists(root / 'state' / 'translation'):
            try:
                stack.enter_context(_translation_adapter().snapshot_guard(root))
            except RuntimeError as exc:
                raise RestoreError('A translation job is active; retry the backup when it is idle.') from exc
            if _translation_files(root) != translation:
                raise RestoreError('Translation checkpoints changed before backup; retry when idle.')
        if os.path.lexists(root / 'transcription-jobs'):
            try:
                stack.enter_context(_transcription_adapter().snapshot_guard(root))
            except RuntimeError as exc:
                raise RestoreError('A supplied transcription worker is active; retry the backup when it is idle.') from exc
            transcription = _transcription_files(root)
        stack.enter_context(_resource_policy_guard(root, cancelled=cancelled))
        if _resource_policy_files(root, cancelled=cancelled) != resource_policy:
            raise RestoreError('Resource allowances changed before backup; retry after the update.')
        keymap_lock = root / 'keyboard-shortcuts.json.lock'
        _regular_if_present(keymap_lock, empty=True)
        try:
            stack.enter_context(FileLock(str(keymap_lock), timeout=0))
        except Timeout as exc:
            raise RestoreError('Keyboard shortcuts are being saved. Retry the backup after it stops.') from exc
        for project in projects:
            lock_path = project / '.draft.lock'
            _regular_if_present(lock_path, empty=True)
            try:
                stack.enter_context(FileLock(str(lock_path), timeout=0))
            except Timeout as exc:
                raise RestoreError('An audio project is being edited. Retry the backup after it stops.') from exc
            if os.path.lexists(project / 'studio' / 'project.json'):
                try:
                    # A saved draft may have no render files yet. Establish its
                    # canonical lock before a renderer can publish those files.
                    stack.enter_context(project_lock(project / 'audio'))
                except RuntimeError as exc:
                    raise RestoreError('An audio project is rendering. Retry the backup after it stops.') from exc
        def verify():
            if (_projects(root) != projects or _assistant_documents(root, max_files) != assistant
                    or _translation_files(root) != translation
                    or _transcription_files(root) != transcription
                    or _resource_policy_files(root, cancelled=cancelled) != resource_policy):
                raise RestoreError('Saved projects or assistant checkpoints changed during backup. Retry when they are idle.')
        yield verify


def _studio_files(project, max_files):
    """Reuse the production backup member allowlist with a bounded traversal."""
    from .audio.backup import _allowed, input_assets
    saved = project / 'studio' / 'project.json'
    if not os.path.lexists(saved):
        return
    yield saved
    recovery = saved.with_name(saved.name + '.recovery.json')
    if os.path.lexists(recovery):
        yield recovery
    audio = project / 'audio'
    yield from input_assets(saved, audio, file_budget=max_files).values()
    if not os.path.lexists(audio):
        return
    for child in _children(audio, max_files):
        if _allowed('audio/' + child.name):
            yield child
        elif child.name == 'cache':
            for item in _children(child, max_files):
                if _allowed('audio/' + child.name + '/' + item.name):
                    yield item
        elif child.name == 'renders':
            for render in _children(child, max_files):
                if not _allowed('audio/renders/' + render.name + '/master.wav'):
                    continue
                for item in _children(render, max_files):
                    if _allowed('audio/renders/' + render.name + '/' + item.name):
                        yield item


def _validate_studio_source(db, payload):
    from .audio.backup import _studio
    from .reader.narration_text import NarrationDraft
    _studio(payload)
    reference = payload['source_reference']
    if reference is not None and reference['kind'] == 'saved-reading-copy':
        where = reference['source_location']
        if where is not None:
            try:
                location = ReadingLocation(**where)
            except (TypeError, ValueError) as exc:
                raise RestoreError('invalid saved reading-copy audio source location') from exc
            _validate_reading_locations(db, (location,))
        return
    if reference is None or reference['kind'] != 'library-source-reading-copy':
        return
    original = NarrationDraft.from_dict(payload['draft']).source_text
    pages = reference.get('pages')
    if type(pages) is not list or not 1 <= len(pages) <= 10000:
        raise RestoreError('invalid audio source-page inventory')
    offset, locations = 0, []
    for index, page in enumerate(pages):
        if (type(page) is not dict or set(page) != {'location', 'source_start', 'source_end', 'verified'}
                or type(page['location']) is not dict or type(page['source_start']) is not int
                or type(page['source_end']) is not int or type(page['verified']) is not bool
                or page['source_start'] != offset or not offset <= page['source_end'] <= len(original)):
            raise RestoreError('invalid audio source-page binding')
        try:
            location = ReadingLocation(**page['location'])
        except (TypeError, ValueError) as exc:
            raise RestoreError('invalid audio source location') from exc
        if (location.workspace != reference.get('workspace') or location.edition != reference.get('edition')
                or location.revision != reference.get('revision')
                or sha256(original[offset:page['source_end']].encode('utf-8')).hexdigest() != location.text_sha256):
            raise RestoreError('audio reading copy does not match its exact saved source')
        locations.append(location)
        offset = page['source_end']
        if index + 1 < len(pages):
            if original[offset:offset + 1] != '\f':
                raise RestoreError('invalid audio source-page separator')
            offset += 1
    if offset != len(original):
        raise RestoreError('audio source-page inventory is incomplete')
    _validate_reading_locations(db, locations)


def _validate_studios(db, root, *, strict_assets=False):
    from .audio.backup import input_assets
    for project in _projects(root):
        saved = project / 'studio' / 'project.json'
        if not os.path.lexists(saved):
            if strict_assets and os.path.lexists(project / 'audio' / 'assets'):
                raise RestoreError('archived sound inputs require their saved Studio project')
            continue
        payload = _audio_document(saved)
        _validate_studio_source(db, payload)
        if payload['project_id'] != project.name:
            raise RestoreError('saved audio project identity differs from its managed folder')
        recovery = saved.with_name(saved.name + '.recovery.json')
        if os.path.lexists(recovery):
            value = _audio_document(recovery)
            if (set(value) != {'kind', 'version', 'project_id', 'base_sha256', 'project'}
                    or value['kind'] != 'audio-studio-recovery' or type(value['version']) is not int
                    or value['version'] != 1 or value['project_id'] != payload['project_id']
                    or not isinstance(value['base_sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', value['base_sha256'])):
                raise RestoreError('invalid audio recovery draft')
            _validate_studio_source(db, value['project'])
            if value['project']['project_id'] != payload['project_id']:
                raise RestoreError('audio recovery draft belongs to another project')
        input_assets(saved, project / 'audio', strict=strict_assets)


def _sidecar_inventory(root, max_files, max_bytes, cancelled):
    files, names, total = {}, {}, 0
    def add(path):
        nonlocal total
        _sidecar_budget(max_files, max_bytes, cancelled)
        relative = path.relative_to(root).as_posix()
        if relative in files:
            return
        _claim_path(relative, names)
        info = _sidecar_info(root, relative)
        total += info.st_size
        if len(files) >= max_files or total > max_bytes:
            raise RestoreError('reading/audio sidecars exceed the backup file or byte budget')
        files[relative] = _identity(info)
    for name in _snapshot_readers(root):
        add(root / name)
    for path in _assistant_files(root, cancelled=cancelled):
        add(path)
    for path in _historical_files(root, cancelled=cancelled):
        add(path)
    for path in _translation_files(root, cancelled=cancelled):
        add(path)
    for path in _transcription_files(root, cancelled=cancelled):
        add(path)
    for path in _resource_policy_files(root, cancelled=cancelled):
        add(path)
    for project in _projects(root):
        for path in _studio_files(project, max_files):
            add(path)
        provenance = project / 'restore-manifest.json'
        if os.path.lexists(provenance):
            add(provenance)
    _check_internal_links(root, files)
    return dict(sorted(files.items()))


def _copy_sidecar(root, relative, target, identity, cancelled):
    before = _sidecar_info(root, relative)
    if _identity(before) != identity:
        raise RestoreError('Reading/audio state changed during backup or restore. Retry with the project idle.')
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    digest, count = sha256(), 0
    with os.fdopen(os.open(root / relative, flags), 'rb') as incoming, (target.open('xb') if target is not None else nullcontext()) as output:
        if _identity(os.fstat(incoming.fileno())) != identity:
            raise RestoreError('sidecar changed before copying')
        if relative.lower().endswith('.wav') and not _transcription_path(relative):
            header = incoming.read(12)
            if len(header) != 12 or header[:4] not in (b'RIFF', b'RF64') or header[8:] != b'WAVE':
                raise RestoreError('project audio artifact is not a WAV or RF64 file')
            incoming.seek(0)
        while block := incoming.read(min(1024 * 1024, before.st_size + 1 - count)):
            if cancelled():
                raise InterruptedError('Library backup or restore cancelled.')
            count += len(block)
            if count > before.st_size:
                raise RestoreError('sidecar grew while copying')
            if output is not None:
                output.write(block)
            digest.update(block)
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
        if _identity(os.fstat(incoming.fileno())) != identity:
            raise RestoreError('sidecar changed while copying')
    if count != before.st_size or _identity(_sidecar_info(root, relative)) != identity:
        raise RestoreError('sidecar changed while copying')
    return dict(path=relative, size_bytes=count, sha256=digest.hexdigest())


def _managed_output_directories(entries):
    # Empty output roots contain no file to archive. Recreate only the reserved
    # audio sibling of a verified managed studio document, never a JSON path.
    return tuple(sorted({str(PurePosixPath(entry['path']).parents[1] / 'audio')
                         for entry in entries if entry['path'].endswith('/studio/project.json')}))


def _publish_sidecars(stage, destination, entries, output_directories=()):
    directories = set()
    for relative in output_directories:
        directory = destination / relative
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directories.add(directory)
    for entry in entries:
        target = destination / entry['path']
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.link(stage / entry['path'], target)
        parent = target.parent
        while parent != destination:
            directories.add(parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        _sync_directory(directory)


def _verify_sidecar_tree(root, entries, max_files, ignored=()):
    by_name = {entry['path']: entry for entry in entries}
    expected = set(by_name)
    directories = {parent.as_posix() for name in expected
                   for parent in PurePosixPath(name).parents if parent != PurePosixPath('.')}
    seen = set()
    identities = {}
    pending = [root]
    _plain(root, directory=True)
    while pending:
        for child in _children(pending.pop(), max_files + len(directories)):
            relative = child.relative_to(root).as_posix()
            if relative in ignored:
                continue
            if relative in expected:
                info = _sidecar_info(root, relative)
                if info.st_size != by_name[relative]['size_bytes']:
                    raise RestoreError('sidecar size mismatch')
                identities[relative] = _identity(info)
                seen.add(relative)
            elif relative in directories:
                _plain(child, directory=True)
                pending.append(child)
            else:
                raise RestoreError('unexpected file or directory in sidecar backup')
    if seen != expected:
        raise RestoreError('missing file in sidecar backup')
    _check_internal_links(root, identities)


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


def create_backup(app, destination, *, max_sidecar_files=MAX_SIDECAR_FILES,
                  max_sidecar_bytes=MAX_SIDECAR_BYTES, cancelled=lambda: False):
    """Back up every library to a fresh folder and return its manifest SHA256.

    The host's explicit Backup action authorizes all-workspace inclusion. The
    private source/destination parents must be stable local directories. A
    publication interrupted before manifest.json is incomplete and not restorable.
    Without import journals, reader sidecars use v3 and managed state uses v4.
    A complete import component selects v5, preserving all current sidecars.
    Active audio jobs and other assistant executions are refused. The assistant
    can snapshot its own durable checkpoint using its actual native job guard.
    File and total-byte budgets are caller-controlled; audio is copied in chunks.
    """
    _sidecar_budget(max_sidecar_files, max_sidecar_bytes, cancelled)
    destination = _destination(destination, app.root)
    with exclusive_writer(app.root), _locked_sidecars(app.root, max_sidecar_files, cancelled=cancelled) as check_projects:
        _plain(app.database)
        if os.path.lexists(app.root / INCOMPLETE):
            raise RestoreError('the source library restore is incomplete')
        with TemporaryDirectory(prefix='.carr-library-backup-', dir=destination.parent) as tmp:
            stage = Path(tmp)
            inventory = _sidecar_inventory(app.root, max_sidecar_files, max_sidecar_bytes, cancelled)
            snapshot = stage / DATABASE
            with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
                storage.backup(snapshot)
            size = _plain(snapshot).st_size
            if not 0 < size <= MAX_DATABASE:
                raise RestoreError('database exceeds backup size limit')
            with closing(Storage(snapshot, schema_version=CURRENT_SCHEMA_VERSION)) as snapshot_storage:
                entries = copy_catalog_assets(snapshot_storage.db, app.assets, stage / ORIGINALS,
                    include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
                fingerprint = schema_fingerprint(snapshot_storage.db)
                _validate_readers(snapshot_storage.db, _snapshot_readers(app.root))
                _validate_studios(snapshot_storage.db, app.root)
                _validate_assistants(snapshot_storage.db, app.root, cancelled=cancelled)
                _validate_historical(app.root, cancelled=cancelled)
                _validate_translations(snapshot_storage.db, app.root, cancelled=cancelled)
                _validate_resource_policy(snapshot_storage.db, app.root, cancelled=cancelled)
                _validate_transcriptions(snapshot_storage.db, app.root, cancelled=cancelled)
            sidecars = [_copy_sidecar(app.root, name, stage / SIDECARS / name, identity, cancelled)
                        for name, identity in inventory.items()]
            # Refuse a newly introduced project before interpreting its files.
            # The final check also covers inventory changes during the rescan.
            check_projects()
            if inventory != _sidecar_inventory(app.root, max_sidecar_files, max_sidecar_bytes, cancelled):
                raise RestoreError('Reading/audio state changed during backup. Retry with the projects idle.')
            check_projects()
            pending = None
            if any(os.path.lexists(app.root / name) for name in
                   ('imports.sqlite', 'import-requests', 'import-batches', HISTORICAL_DIRECTORY)):
                from .pending_backup import snapshot_pending
                pending = snapshot_pending(app, stage / PENDING, cancelled=cancelled)
            # Metadata is not a content identity. In particular, Windows file
            # creation time can survive an equal-size edit with restored mtime.
            # Reuse the bounded streaming reader after all other components
            # have been captured, before publishing any completed backup.
            for entry in sidecars:
                if cancelled():
                    raise InterruptedError('Library backup cancelled during final source verification.')
                current = _copy_sidecar(app.root, entry['path'], None,
                                        inventory[entry['path']], cancelled)
                if current != entry:
                    raise RestoreError('Reading/audio state content changed during backup. Retry when it is idle.')
            check_projects()
            if inventory != _sidecar_inventory(app.root, max_sidecar_files, max_sidecar_bytes, cancelled):
                raise RestoreError('Reading/audio state changed during final backup verification.')
            check_projects()
            managed = pending is not None or any(not _reader_path(entry['path']) for entry in sidecars)
            manifest = dict(version=5 if pending is not None else 4 if managed else 3 if sidecars else 2, kind='carr-library', schema_version=CURRENT_SCHEMA_VERSION,
                schema_fingerprint=fingerprint,
                database=dict(filename=DATABASE, size_bytes=size, sha256=hash_file(snapshot)),
                assets=_asset_manifest(entries))
            if sidecars or pending is not None:
                manifest['sidecars'] = sidecars if managed else [
                    dict(filename=entry['path'], size_bytes=entry['size_bytes'], sha256=entry['sha256'])
                    for entry in sidecars]
            if pending is not None:
                manifest['pending'] = pending
            raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
            if len(raw) > MAX_MANIFEST:
                raise RestoreError('manifest size limit')
            _sync_file(stage / MANIFEST, raw)
            if cancelled():
                raise InterruptedError('Library backup cancelled before publication.')
            # mkdir provides exclusive no-replace ownership of the destination.
            # The final manifest is the completion marker; no partial bundle is
            # silently overwritten or presented as a successful backup.
            destination.mkdir(mode=0o700)
            (destination / ORIGINALS).mkdir(mode=0o700)
            os.link(snapshot, destination / DATABASE)
            for entry in entries:
                os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
            _sync_directory(destination / ORIGINALS)
            if sidecars or pending is not None:
                sidecar_destination = destination / SIDECARS if managed else destination
                if managed:
                    sidecar_destination.mkdir(mode=0o700)
                _publish_sidecars(stage / SIDECARS, sidecar_destination, sidecars)
                _sync_directory(sidecar_destination)
            if pending is not None:
                publish_tree(stage / PENDING, destination / PENDING, cancelled=cancelled)
            if cancelled():
                raise InterruptedError('Library backup cancelled before manifest publication.')
            os.link(stage / MANIFEST, destination / MANIFEST)
            _sync_directory(destination)
            _sync_directory(destination.parent)
    return sha256(raw).hexdigest()


def _legacy_state_layout(manifest):
    """The two historical v3/v4 families have disjoint exact field shapes."""
    return manifest['version'] in (3, 4) and (
        'state' in manifest or 'generated_audio_included' in manifest)


def _sidecar_layout(manifest):
    """Reading v3 keeps filename/root; typed v4/v5 uses path/sidecars."""
    return ('path', SIDECARS) if manifest['version'] >= 4 else ('filename', '')


def _declared_sidecars(manifest):
    """Keep the historical one-reading-file v3 shape as an exact read schema."""
    if _legacy_state_layout(manifest):
        return []
    if 'reading_state' not in manifest:
        return manifest.get('sidecars', [])
    reading = manifest['reading_state']
    if reading is None:
        return []
    if (type(reading) is not dict or set(reading) != {'filename', 'size_bytes', 'sha256'}
            or reading['filename'] != READING_STATE or type(reading['size_bytes']) is not int
            or not 0 < reading['size_bytes'] <= MAX_READING_STATE
            or not isinstance(reading['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', reading['sha256'])):
        raise RestoreError('invalid reading-state manifest')
    return [reading]


def _manifest_sidecars(manifest):
    field, _ = _sidecar_layout(manifest)
    return [dict(path=entry[field], size_bytes=entry['size_bytes'], sha256=entry['sha256'])
            for entry in _declared_sidecars(manifest)]


def _backup_sidecar_allowed(value):
    try:
        return _sidecar_allowed(value)
    except RestoreError:
        return False


def _read_manifest(directory, expected_manifest_sha256, *, max_sidecar_files=MAX_SIDECAR_FILES,
                   max_sidecar_bytes=MAX_SIDECAR_BYTES):
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
            or manifest['version'] not in (1, 2, 3, 4, 5)):
        raise RestoreError('unsupported library manifest')
    fields = {'version','kind','schema_version','database','assets'}
    if manifest['version'] >= 2:
        fields.add('schema_fingerprint')
    legacy_state = _legacy_state_layout(manifest)
    if legacy_state:
        fields.update(('state', 'generated_audio_included'))
        if manifest['version'] == 4:
            fields.add('pending')
    elif manifest['version'] == 3:
        fields.add('reading_state' if 'reading_state' in manifest else 'sidecars')
    elif manifest['version'] >= 4:
        fields.add('sidecars')
        if manifest['version'] == 5:
            fields.add('pending')
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
    if 'pending' in manifest:
        from .pending_backup import validate_pending
        validate_pending(manifest['pending'])
    if legacy_state:
        from .backup_legacy_state import validate_state
        if type(manifest['generated_audio_included']) is not bool:
            raise RestoreError('invalid legacy application state options')
        # Retention adds the original manifest as one authenticated sidecar.
        validate_state(manifest['state'], max_files=max_sidecar_files - 1,
                       max_bytes=max_sidecar_bytes - size)
        expected_names = {DATABASE, ORIGINALS, MANIFEST, 'state'}
        if 'pending' in manifest:
            expected_names.add(PENDING)
        if set(p.name for p in directory.iterdir()) != expected_names:
            raise RestoreError('incomplete legacy application state backup')
        _plain(directory / 'state', directory=True)
        return manifest
    field, subdirectory = _sidecar_layout(manifest)
    if manifest['version'] >= 3:
        entries = _declared_sidecars(manifest)
        minimum = 1 if manifest['version'] == 4 else 0
        if type(entries) is not list or not minimum <= len(entries) <= max_sidecar_files:
            raise RestoreError('invalid sidecar manifest count')
        names, total, reader_total, narration_total, reader_count = {}, 0, 0, 0, 0
        assistant_total, assistant_jobs, assistant_controls = 0, 0, 0
        translation_total, translation_jobs, translation_caches = 0, 0, 0
        transcription_total, transcription_files, transcription_jobs = 0, 0, set()
        for entry in entries:
            if (type(entry) is not dict or set(entry) != {field, 'size_bytes', 'sha256'}
                    or not _backup_sidecar_allowed(entry[field])
                    or (not subdirectory and not _reader_path(entry[field]))
                    or type(entry['size_bytes']) is not int or not 0 <= entry['size_bytes'] <= MAX_SIDECAR_FILE_BYTES
                    or (entry['size_bytes'] == 0 and not _historical_path(entry[field])
                        and not entry[field].lower().endswith(('.srt', '.vtt', '.ffmeta'))
                        and not (_transcription_path(entry[field])
                                 and _transcription_adapter().allows_empty(entry[field])))
                    or not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
                raise RestoreError('invalid backup sidecar manifest entry')
            _claim_path(entry[field], names)
            total += entry['size_bytes']
            if total > max_sidecar_bytes:
                raise RestoreError('sidecar manifest byte limit')
            if _resource_policy_path(entry[field]) and entry['size_bytes'] > MAX_RESOURCE_POLICY:
                raise RestoreError('resource policy sidecar byte limit')
            if _reader_path(entry[field]):
                reader_total += entry['size_bytes']
                reader_count += 1
                if entry['size_bytes'] > _reader_limit(entry[field]):
                    raise RestoreError('reader sidecar byte limit')
                if entry[field].startswith(NARRATION_DIRECTORY + '/'):
                    narration_total += entry['size_bytes']
            if (reader_total > MAX_READER_BYTES or narration_total > MAX_NARRATION_BYTES
                    or reader_count > 1 + len(_SIMPLE_SIDECARS) + MAX_PROJECTS * 2):
                raise RestoreError('reader sidecar count or byte limit')
            if _assistant_path(entry[field]):
                assistant_total += entry['size_bytes']
                assistant_controls += int(entry[field].endswith('.control.json'))
                assistant_jobs += int(not entry[field].endswith('.control.json'))
                if (entry['size_bytes'] > _assistant_limit(entry[field])
                        or assistant_total > MAX_ASSISTANT_BYTES
                        or max(assistant_jobs, assistant_controls) > MAX_ASSISTANT_JOBS):
                    raise RestoreError('assistant sidecar count or byte limit')
            if _translation_path(entry[field]):
                adapter = _translation_adapter()
                translation_total += entry['size_bytes']
                is_cache = PurePosixPath(entry[field]).name.startswith('languages-')
                translation_caches += int(is_cache)
                translation_jobs += int(not is_cache)
                if (entry['size_bytes'] > adapter.state_limit(entry[field])
                        or translation_total > adapter.MAX_SNAPSHOT_BYTES
                        or translation_jobs > adapter.MAX_JOBS or translation_caches > adapter.MAX_CACHES):
                    raise RestoreError('translation sidecar count or byte limit')
            if _transcription_path(entry[field]):
                adapter = _transcription_adapter()
                transcription_total += entry['size_bytes']
                transcription_files += 1
                transcription_jobs.add(PurePosixPath(entry[field]).parts[1])
                if (entry['size_bytes'] > adapter.state_limit(entry[field])
                        or transcription_total > adapter.MAX_SNAPSHOT_BYTES
                        or transcription_files > adapter.MAX_FILES
                        or len(transcription_jobs) > adapter.MAX_JOBS):
                    raise RestoreError('transcription sidecar count or byte limit')
    normalized = _manifest_sidecars(manifest)
    expected_names = {DATABASE, ORIGINALS, MANIFEST}
    if 'pending' in manifest:
        expected_names.add(PENDING)
    expected_names.update({SIDECARS} if subdirectory else {entry['path'].split('/')[0] for entry in normalized})
    if set(p.name for p in directory.iterdir()) != expected_names:
        raise RestoreError('incomplete backup or unexpected reading-state/sidecar files')
    _verify_sidecar_tree(directory / subdirectory, normalized, max_sidecar_files,
                         ignored=() if subdirectory else {DATABASE, ORIGINALS, MANIFEST})
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


def _verify_contents(source, stage, manifest, *, copy_files=False,
                     expected_manifest_sha256=None, cancelled=lambda: False):
    """One byte/schema/source verifier for restoration and read-only inspection."""
    _plain(source / ORIGINALS, directory=True)
    sidecars = _manifest_sidecars(manifest)
    _, subdirectory = _sidecar_layout(manifest)
    sidecar_source = source / subdirectory
    identities = {}
    for entry in sidecars:
        identity = _identity(_sidecar_info(sidecar_source, entry['path']))
        identities[entry['path']] = identity
        target = stage / SIDECARS / entry['path'] if copy_files else None
        actual = _copy_sidecar(sidecar_source, entry['path'], target, identity, cancelled)
        if actual != entry:
            raise RestoreError('sidecar size or digest mismatch')
    snapshot = stage / DATABASE
    _copy_database(source / DATABASE, snapshot, manifest)
    with closing(Storage(snapshot, schema_version=manifest['schema_version'])) as storage:
        storage.check()
        if sidecars:
            checked_root = stage / SIDECARS if copy_files else sidecar_source
            _validate_readers(storage.db, _snapshot_readers(checked_root))
            _validate_studios(storage.db, checked_root, strict_assets=True)
            _validate_assistants(storage.db, checked_root, cancelled=cancelled)
            _validate_historical(checked_root, cancelled=cancelled)
            _validate_translations(storage.db, checked_root, cancelled=cancelled)
            _validate_resource_policy(storage.db, checked_root, cancelled=cancelled)
            _validate_transcriptions(storage.db, checked_root, cancelled=cancelled)
        original_store = RetainedAssets(source / ORIGINALS)
        entries = verify_catalog_assets(storage.db, original_store,
            include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
        if _asset_manifest(entries) != manifest['assets']:
            raise RestoreError('asset manifest disagrees with the database')
        if copy_files:
            copy_catalog_assets(storage.db, original_store, stage / ORIGINALS,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
    if any(_identity(_sidecar_info(sidecar_source, name)) != identity
           for name, identity in identities.items()):
        raise RestoreError('sidecar changed during verification')
    if _legacy_state_layout(manifest):
        if copy_files:
            sidecars = _retain_historical(source, stage / SIDECARS, manifest,
                                         expected_manifest_sha256, cancelled=cancelled)
            _validate_historical(stage / SIDECARS, cancelled=cancelled)
        else:
            from .backup_legacy_state import verify_state
            verify_state(source / 'state', None, manifest['state'], cancelled=cancelled)
    if 'pending' in manifest and not copy_files:
        from .pending_backup import verify_pending
        verify_pending(source / PENDING, stage, manifest['pending'], cancelled=cancelled)
    if cancelled():
        raise InterruptedError('Library backup or restore cancelled.')
    return entries, sidecars, _managed_output_directories(sidecars)


def verify_backup(source, expected_manifest_sha256, *, max_sidecar_files=MAX_SIDECAR_FILES,
                  max_sidecar_bytes=MAX_SIDECAR_BYTES, cancelled=lambda: False):
    """Validate exact v1-v5 snapshots read-only without publishing a library.

    SQLite validation uses a private database copy. Audio and other sidecars
    are streamed without allocating another full production copy.
    """
    _sidecar_budget(max_sidecar_files, max_sidecar_bytes, cancelled)
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    manifest = _read_manifest(source, expected_manifest_sha256,
        max_sidecar_files=max_sidecar_files, max_sidecar_bytes=max_sidecar_bytes)
    with TemporaryDirectory(prefix='carr-backup-verify-') as tmp:
        _verify_contents(source, Path(tmp), manifest,
                         expected_manifest_sha256=expected_manifest_sha256, cancelled=cancelled)
    return manifest


def restore_backup(source, destination, expected_manifest_sha256, *,
                   max_sidecar_files=MAX_SIDECAR_FILES, max_sidecar_bytes=MAX_SIDECAR_BYTES,
                   cancelled=lambda: False):
    """Restore a trusted backup into a new data root; return that root's Path.

    Keep the expected digest independently of the backup. The source is never
    opened writable. A failed/terminated publication retains .restore-incomplete;
    the application must reject such a root instead of opening partial data.
    """
    _sidecar_budget(max_sidecar_files, max_sidecar_bytes, cancelled)
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    destination = _destination(destination, source)
    manifest = _read_manifest(source, expected_manifest_sha256,
        max_sidecar_files=max_sidecar_files, max_sidecar_bytes=max_sidecar_bytes)
    with TemporaryDirectory(prefix='.carr-library-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        entries, sidecars, output_directories = _verify_contents(source, stage, manifest,
            copy_files=True, expected_manifest_sha256=expected_manifest_sha256, cancelled=cancelled)
        pending = {'assets': [], 'paths': []}
        if 'pending' in manifest:
            from .pending_backup import restore_pending
            pending = restore_pending(source / PENDING, stage, destination, manifest['pending'],
                                      cancelled=cancelled)
        snapshot = stage / DATABASE
        _sidecar_budget(max_sidecar_files, max_sidecar_bytes, cancelled)
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        (destination / ORIGINALS).mkdir(mode=0o700)
        os.link(snapshot, destination / DATABASE)
        for entry in entries:
            os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
        _sync_directory(destination / ORIGINALS)
        for entry in pending['assets']:
            if cancelled():
                raise InterruptedError('Library restore cancelled before completion.')
            os.link(stage / ORIGINALS / entry['filename'], destination / ORIGINALS / entry['filename'])
        _publish_sidecars(stage / SIDECARS, destination, sidecars, output_directories)
        for name in pending['paths']:
            if cancelled():
                raise InterruptedError('Library restore cancelled before completion.')
            if (stage / name).is_dir():
                publish_tree(stage / name, destination / name, cancelled=cancelled)
            else:
                os.link(stage / name, destination / name)
        _sync_directory(destination / ORIGINALS)
        _sync_directory(destination)
    # Temporary hard links are now gone; retained originals have one link.
    (destination / INCOMPLETE).unlink()
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return destination
