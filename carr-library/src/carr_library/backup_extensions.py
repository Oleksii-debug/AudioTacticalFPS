"""Typed V5 additions to the canonical state and pending-import backup.

The original V1--V4 formats and their producers remain authoritative. This
module binds exact source positions, typed reading sessions, and inert legacy
archives to that snapshot. Recorded engine and audio paths are declarations;
backup and restore never inspect or execute those external inputs.
"""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3

from .durable_state import regular
from .ingest.bundle import _unique_object
from .ingest.restore import RestoreError
from .reader.reading_state import FILENAME as READING, MAX_BYTES as MAX_READING, _decode
from .reader.live_state import FILENAME as LIVE, MAX_BYTES as MAX_LIVE, validate_live_state
from .speech.dordiai_settings import (FILENAME as DORDIAI, MAX_BYTES as MAX_DORDIAI,
    validate_settings_bytes, restored_settings_bytes)
from .state_backup import relative_name, validate_portable_paths

DIRECTORY = 'extensions'
LEGACY = 'legacy-backups'
MAX_FILES = 100_000
MAX_BYTES = 80 * 1024**3
MAX_JSON = 64 * 1024**2
SIDECARS = {READING: MAX_READING, LIVE: MAX_LIVE, DORDIAI: MAX_DORDIAI}
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_READER = re.compile(r'speech/[0-9a-f]{64}\.reader\.json\Z')
_PROJECT = re.compile(r'studio/projects/[a-zA-Z0-9_-]{1,80}/(?:project\.json|revisions/[0-9]{8,}-[0-9a-f]{64}\.json)\Z')


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Backup cancelled.')


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def _json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON number.')))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RestoreError('Invalid typed backup JSON.') from exc


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_bytes(path, maximum, *, optional=False):
    try:
        before = regular(path)
    except FileNotFoundError:
        if optional:
            return None
        raise
    if not 0 < before.st_size <= maximum:
        raise RestoreError('Typed backup file exceeds its byte budget.')
    with Path(path).open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise RestoreError('Typed backup file changed while opening.')
        raw = stream.read(maximum + 1)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise RestoreError('Typed backup file changed while reading.')
    if len(raw) != before.st_size or _stamp(regular(path)) != _stamp(before):
        raise RestoreError('Typed backup file changed during snapshot.')
    return raw


def write_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def workspaces(database):
    with closing(sqlite3.connect(Path(database).as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        return sorted(row[0] for row in db.execute('SELECT id FROM workspace'))


def _source_text(db, location):
    row = db.execute('''SELECT p.raw FROM revision r
        JOIN sealed s ON s.revision=r.id JOIN page p ON p.revision=r.id
        JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition AND e.asset=r.asset
        WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=? AND p.digest=?
        AND ((?='physical_page' AND p.physical=?) OR
             (?='logical_segment' AND p.physical IS NULL AND p.ordinal=?))''',
        (location.workspace, location.edition, location.revision, location.asset_sha256,
         location.text_sha256, location.kind, location.index, location.kind, location.index)).fetchone()
    if row is None or sha256(row[0].encode('utf-8')).hexdigest() != location.text_sha256:
        raise RestoreError('Reading recovery does not match its exact authenticated source.')
    return row[0]


def validate_sidecars(database, values, allowed):
    with closing(sqlite3.connect(Path(database).as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        for name, raw in values.items():
            if name == READING:
                positions, bookmarks, _ = _decode(raw.decode('utf-8'))
                locations = [row[0] for row in positions.values()] + [b.location for b in bookmarks.values()]
                for location in locations:
                    if location.workspace not in allowed:
                        raise RestoreError('Reading state is outside the authorized backup scope.')
                    _source_text(db, location)
            elif name == LIVE:
                for item in validate_live_state(raw):
                    if item.location.workspace not in allowed or len(_source_text(db, item.location)) != item.text_length:
                        raise RestoreError('Live-reading position does not match its exact source length or scope.')
            elif name == DORDIAI:
                validate_settings_bytes(raw)
            else:
                raise RestoreError('Unsupported typed backup sidecar.')


def _required(component, kind, reference):
    if (not isinstance(reference, str) or not reference or len(reference) > 32768
            or any(ord(c) < 32 for c in reference)):
        raise RestoreError('Invalid declared external backup input.')
    return dict(component=component, kind=kind, reference=reference, included=False)


def _ordered_inputs(values):
    unique = {_encoded(item): item for item in values}
    return [unique[key] for key in sorted(unique)]


def inspect_state(root, entries, database, allowed, *, independent, cancelled=lambda: False):
    """Authenticate typed producers without interpreting unrelated V3 state."""
    from .speech.session import decode_reader_resume
    from .studio.project import Project
    sessions, projects, required = [], [], []
    with closing(sqlite3.connect(Path(database).as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        for entry in entries:
            _check(cancelled)
            name = entry['filename']
            if _READER.fullmatch(name):
                raw = read_bytes(Path(root) / name, MAX_JSON)
                record = decode_reader_resume(_json(raw))
                if not independent and not record.source_locations:
                    raise RestoreError('An unbound reading draft cannot enter a workspace-scoped backup.')
                if any(location.workspace not in allowed for location in record.source_locations):
                    raise RestoreError('Reading draft is outside the authorized backup scope.')
                if record.source_locations:
                    text = '\n\n'.join(_source_text(db, location) for location in record.source_locations)
                    if text != record.document.source_text:
                        raise RestoreError('Reading draft source text differs from its authenticated pages.')
                sessions.append(name)
                if record.voice_id:
                    required.append(_required('state/' + name, 'local-voice', record.voice_id))
            elif _PROJECT.fullmatch(name):
                if not independent:
                    raise RestoreError('Independent audio projects cannot enter a workspace-scoped backup.')
                project = Project.from_dict(_json(read_bytes(Path(root) / name, MAX_JSON)))
                if project.id != name.split('/')[2]:
                    raise RestoreError('Audio project identity differs from its saved directory.')
                projects.append(name)
                for role in project.roles:
                    if role.voice_id:
                        required.append(_required('state/' + name, 'local-voice', role.voice_id))
                for scene in project.scenes:
                    for cue in scene.cues:
                        required.append(_required('state/' + name, 'audio-file', cue.path))
                    if scene.music is not None and scene.music.engine != 'procedural':
                        required.append(_required('state/' + name, 'music-engine', scene.music.engine))
            elif not independent:
                raise RestoreError('Only explicitly source-bound reading drafts are supported in a scoped state snapshot.')
    return sorted(sessions), sorted(projects), _ordered_inputs(required)


def scoped_state_selection(root, database, allowed):
    """Return exact bound draft bytes; slot hashes never grant workspace scope."""
    from .speech.session import decode_reader_resume
    root = Path(root)
    if not os.path.lexists(root):
        return {}
    regular(root, directory=True)
    directory = root / 'speech'
    if not os.path.lexists(directory):
        return {}
    regular(directory, directory=True)
    selected = {}
    for path in sorted(directory.iterdir()):
        name = path.relative_to(root).as_posix()
        if not _READER.fullmatch(name):
            continue
        raw = read_bytes(path, MAX_JSON)
        record = decode_reader_resume(_json(raw))
        if record.source_locations and all(location.workspace in allowed for location in record.source_locations):
            selected[name] = sha256(raw).hexdigest()
    entries = [dict(filename=name) for name in selected]
    inspect_state(root, entries, database, allowed, independent=False)
    return selected


def snapshot(root, target, database, state_entries, *, independent=True, cancelled=lambda: False):
    root, target = Path(root), Path(target)
    allowed = workspaces(database)
    sidecars = {}
    for name, maximum in SIDECARS.items():
        _check(cancelled)
        if name == DORDIAI and not independent:
            continue
        raw = read_bytes(root / name, maximum, optional=True)
        if raw is not None:
            sidecars[name] = raw
    validate_sidecars(database, sidecars, allowed)
    sessions, projects, required = inspect_state(target.parent / 'state', state_entries,
        database, allowed, independent=independent, cancelled=cancelled)
    archives = []
    if independent and os.path.lexists(root / LEGACY):
        from .backup_compat.archive import snapshot_archives
        archives, archive_inputs = snapshot_archives(root / LEGACY, target / LEGACY, cancelled=cancelled)
        required.extend(archive_inputs)
    if not sidecars and not archives and not sessions and not projects and independent:
        return None
    target.mkdir(exist_ok=True, mode=0o700)
    for name, raw in sidecars.items():
        write_bytes(target / name, raw)
    if DORDIAI in sidecars:
        settings = validate_settings_bytes(sidecars[DORDIAI])
        for field in ('runtime_directory', 'python_executable'):
            required.append(_required(DORDIAI, 'dordiai-runtime', settings[field]))
    for item in validate_live_state(sidecars[LIVE]) if LIVE in sidecars else ():
        required.append(_required(LIVE, 'local-voice', item.voice_id))
    files = inventory(target, cancelled=cancelled)
    manifest = dict(version=1, workspaces=allowed, independent_state=independent,
        files=files, reader_sessions=sessions, studio_projects=projects,
        legacy_archives=archives, required_inputs=_ordered_inputs(required))
    validate_manifest(manifest)
    return manifest


def _tree(root, *, cancelled=lambda: False):
    regular(root, directory=True)
    files, directories, folded = {}, set(), set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        _check(cancelled)
        if len(Path(directory).relative_to(root).parts) > 16:
            raise RestoreError('Typed backup directory nesting exceeds its budget.')
        for name in dirs:
            path = Path(directory) / name
            regular(path, directory=True)
            relative = relative_name(path.relative_to(root).as_posix()).as_posix()
            if relative.casefold() in folded:
                raise RestoreError('Duplicate typed backup path.')
            folded.add(relative.casefold())
            directories.add(relative)
        for name in names:
            path = Path(directory) / name
            info = regular(path)
            relative = relative_name(path.relative_to(root).as_posix()).as_posix()
            if relative.casefold() in folded:
                raise RestoreError('Duplicate typed backup path.')
            folded.add(relative.casefold())
            files[relative] = info
        if len(files) > MAX_FILES or len(folded) > MAX_FILES * 4:
            raise RestoreError('Typed backup exceeds its file budget.')
    return files, directories


def _ancestors(names):
    return {parent.as_posix() for name in names for parent in Path(name).parents if parent != Path('.')}


def _legacy_original_directories(names):
    # Empty catalogs still carry the exact required originals directory. It is
    # the sole empty-directory exception in the typed extension inventory.
    return {name.rsplit('/', 1)[0] + '/originals' for name in names
            if re.fullmatch(r'legacy-backups/[0-9a-f]{64}/manifest\.json', name)}


def inventory(root, *, cancelled=lambda: False):
    before, directories = _tree(root, cancelled=cancelled)
    if directories != _ancestors(before) | _legacy_original_directories(before):
        raise RestoreError('Typed backup contains an unlisted empty directory.')
    result, total = [], 0
    for name, info in sorted(before.items()):
        total += info.st_size
        if info.st_size < 1 or total > MAX_BYTES:
            raise RestoreError('Typed backup exceeds its byte budget.')
        digest, size = sha256(), 0
        with (Path(root) / name).open('rb') as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(info):
                raise RestoreError('Typed backup file changed.')
            while block := stream.read(1024 * 1024):
                _check(cancelled)
                size += len(block)
                if size > info.st_size:
                    raise RestoreError('Typed backup file grew.')
                digest.update(block)
        if size != info.st_size:
            raise RestoreError('Typed backup file changed.')
        result.append(dict(filename=name, size_bytes=size, sha256=digest.hexdigest()))
    after, later_directories = _tree(root, cancelled=cancelled)
    if ({name: _stamp(info) for name, info in before.items()} !=
            {name: _stamp(info) for name, info in after.items()} or directories != later_directories):
        raise RestoreError('Typed backup inventory changed.')
    return result


def validate_manifest(value):
    fields = {'version', 'workspaces', 'independent_state', 'files', 'reader_sessions',
              'studio_projects', 'legacy_archives', 'required_inputs'}
    if (type(value) is not dict or set(value) != fields or type(value['version']) is not int
            or value['version'] != 1 or type(value['independent_state']) is not bool
            or type(value['workspaces']) is not list or len(value['workspaces']) > 10000
            or any(not isinstance(item, str) or not item or len(item) > 512 for item in value['workspaces'])
            or value['workspaces'] != sorted(set(value['workspaces']))):
        raise RestoreError('Unsupported typed backup manifest or scope.')
    files = value['files']
    if type(files) is not list or len(files) > MAX_FILES:
        raise RestoreError('Invalid typed backup file inventory.')
    total, names = 0, set()
    for item in files:
        if (type(item) is not dict or set(item) != {'filename', 'size_bytes', 'sha256'}
                or type(item['size_bytes']) is not int or item['size_bytes'] < 1
                or not isinstance(item['sha256'], str) or not _HASH.fullmatch(item['sha256'])):
            raise RestoreError('Invalid typed backup file record.')
        name = relative_name(item['filename']).as_posix()
        if (name not in SIDECARS and not re.fullmatch(r'legacy-backups/[0-9a-f]{64}(?:\.json|/.+)', name)):
            raise RestoreError('Unsupported typed backup filename.')
        if name.casefold() in names:
            raise RestoreError('Duplicate typed backup filename.')
        names.add(name.casefold())
        total += item['size_bytes']
        if total > MAX_BYTES or (name in SIDECARS and item['size_bytes'] > SIDECARS[name]):
            raise RestoreError('Typed backup exceeds its byte budget.')
    if [item['filename'] for item in files] != sorted(item['filename'] for item in files):
        raise RestoreError('Typed backup files must be ordered.')
    validate_portable_paths(item['filename'] for item in files)
    for field, pattern in (('reader_sessions', _READER), ('studio_projects', _PROJECT)):
        values = value[field]
        if (type(values) is not list or len(values) > MAX_FILES
                or any(not isinstance(name, str) or not pattern.fullmatch(name) for name in values)
                or values != sorted(set(values))):
            raise RestoreError('Invalid typed state producer inventory.')
    archives = value['legacy_archives']
    if type(archives) is not list or len(archives) > 1000:
        raise RestoreError('Invalid legacy archive inventory.')
    keys = []
    for item in archives:
        if (type(item) is not dict or set(item) != {'manifest_sha256', 'format', 'status'}
                or not isinstance(item['manifest_sha256'], str) or not _HASH.fullmatch(item['manifest_sha256'])
                or item['format'] not in ('legacy-sidecars-v3', 'legacy-recovery-v4') or item['status'] != 'inert'):
            raise RestoreError('Invalid legacy archive declaration.')
        keys.append(item['manifest_sha256'])
    if keys != sorted(set(keys)) or {name.split('/')[1].removesuffix('.json') for name in names if name.startswith(LEGACY + '/')} != set(keys):
        raise RestoreError('Legacy archive inventory differs from its declarations.')
    inputs = value['required_inputs']
    if type(inputs) is not list or len(inputs) > MAX_FILES:
        raise RestoreError('Invalid required-input inventory.')
    for item in inputs:
        if (type(item) is not dict or set(item) != {'component', 'kind', 'reference', 'included'}
                or item['included'] is not False or item['kind'] not in
                ('local-voice', 'audio-file', 'music-engine', 'dordiai-runtime', 'source-file')
                or not isinstance(item['component'], str)):
            raise RestoreError('Invalid required-input declaration.')
        relative_name(item['component'])
        _required(item['component'], item['kind'], item['reference'])
    if inputs != _ordered_inputs(inputs):
        raise RestoreError('Required inputs must be unique and ordered.')
    if not value['independent_state'] and (archives or DORDIAI in names or value['studio_projects']):
        raise RestoreError('Independent private state is outside the workspace backup scope.')
    return value


def restore(source, stage, manifest, state_entries, *, cancelled=lambda: False):
    value = validate_manifest(manifest)
    if workspaces(Path(stage) / 'library.sqlite') != value['workspaces']:
        raise RestoreError('Typed backup workspace scope differs from the catalog.')
    if inventory(source, cancelled=cancelled) != value['files']:
        raise RestoreError('Typed backup digest or inventory mismatch.')
    temporary = Path(stage) / '.verified-extensions'
    # Canonical state copier enforces the same exact bytes. Its 32-GiB state
    # budget is intentionally not reused for larger inert archive components.
    copy_files(source, temporary, value['files'], cancelled=cancelled)
    for name in _legacy_original_directories(item['filename'] for item in value['files']):
        (temporary / name).mkdir(exist_ok=True, mode=0o700)
    sidecars = {name: read_bytes(temporary / name, maximum) for name, maximum in SIDECARS.items()
                if (temporary / name).exists()}
    validate_sidecars(Path(stage) / 'library.sqlite', sidecars, value['workspaces'])
    sessions, projects, required = inspect_state(Path(stage) / 'state', state_entries,
        Path(stage) / 'library.sqlite', value['workspaces'], independent=value['independent_state'], cancelled=cancelled)
    if sessions != value['reader_sessions'] or projects != value['studio_projects']:
        raise RestoreError('Typed state producer inventory differs from the snapshot.')
    paths = []
    if value['legacy_archives']:
        from .backup_compat.archive import verify_archives
        archives, legacy_inputs = verify_archives(temporary / LEGACY, cancelled=cancelled)
        if archives != value['legacy_archives']:
            raise RestoreError('Legacy archive declarations differ from their verified contents.')
        required.extend(legacy_inputs)
        os.rename(temporary / LEGACY, Path(stage) / LEGACY)
        paths.append(LEGACY)
    if DORDIAI in sidecars:
        settings = validate_settings_bytes(sidecars[DORDIAI])
        for field in ('runtime_directory', 'python_executable'):
            required.append(_required(DORDIAI, 'dordiai-runtime', settings[field]))
    for item in validate_live_state(sidecars[LIVE]) if LIVE in sidecars else ():
        required.append(_required(LIVE, 'local-voice', item.voice_id))
    if _ordered_inputs(required) != value['required_inputs']:
        raise RestoreError('Required external inputs differ from the typed saved state.')
    for name, raw in sidecars.items():
        write_bytes(Path(stage) / name, restored_settings_bytes(raw) if name == DORDIAI else raw)
        paths.append(name)
    return paths


def copy_files(source, target, entries, *, cancelled=lambda: False):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for item in entries:
        _check(cancelled)
        incoming = Path(source) / relative_name(item['filename'])
        before = regular(incoming)
        if before.st_size != item['size_bytes']:
            raise RestoreError('Backup file size changed.')
        outgoing = target / item['filename']
        outgoing.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest, count = sha256(), 0
        with incoming.open('rb') as read, outgoing.open('xb') as write:
            if _stamp(os.fstat(read.fileno())) != _stamp(before):
                raise RestoreError('Backup file changed while opening.')
            while block := read.read(1024 * 1024):
                _check(cancelled)
                count += len(block)
                if count > item['size_bytes']:
                    raise RestoreError('Backup file grew during copy.')
                digest.update(block)
                write.write(block)
            write.flush()
            os.fsync(write.fileno())
        if (count != item['size_bytes'] or digest.hexdigest() != item['sha256']
                or _stamp(regular(incoming)) != _stamp(before)):
            raise RestoreError('Backup file changed or its digest is invalid.')
