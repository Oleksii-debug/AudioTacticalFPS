"""Bounded, checksummed snapshots of private application state outside SQLite.

The caller owns the library writer lock. Studio/assistant state locks cover
accepted project, chunk and job writes; temporary render work is excluded.
Restoration validates an entire private stage before publishing any data root.
Only explicit local root bindings change. External source paths and citation
identities remain the user's original values.
"""
from contextlib import ExitStack, closing, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from types import SimpleNamespace

from filelock import FileLock, Timeout

from .ingest.bundle import _unique_object
from .ingest.restore import RestoreError
from .extract.document_formats import MIME_SUFFIXES


DIRECTORY = 'sidecars'
MAX_FILES = 100_000
MAX_BYTES = 64 * 1024**3
MAX_FILE_BYTES = 32 * 1024**3
MAX_JSON_BYTES = 32 * 1024**2
MAX_JOURNAL_BYTES = 512 * 1024**2
_ID = r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}'
_HASH = r'[0-9a-f]{64}'
_REQUEST = r'[0-9a-f]{32}'
_ORIGINAL_EXTENSIONS = '|'.join(sorted(re.escape(value[1:]) for value in set(MIME_SUFFIXES.values())))
_PATHS = (
    r'reading-state\.json', r'connections\.json', r'imports\.sqlite',
    r'keybindings\.json', r'ocr-language-presets\.json',
    r'evidence-drafts\.json', r'keyboard-shortcuts\.json', r'progress-preferences\.json',
    r'desktop-preferences\.json', r'portable-sync-state\.json',
    rf'import-batches/{_REQUEST}\.json',
    rf'import-requests/(?:reviews/)?{_REQUEST}\.json',
    rf'pending-originals/{_HASH}\.(?:{_ORIGINAL_EXTENSIONS})',
    r'studio/music-settings\.json',
    r'studio/voice-settings\.json',
    rf'studio/projects/{_ID}/(?:project|draft)\.json',
    rf'studio/projects/{_ID}/revisions/[0-9]{{8,}}\.json',
    rf'studio/projects/{_ID}/cache/{_HASH}\.(?:json|wav)',
    rf'studio/projects/{_ID}/assets/{_HASH}\.wav',
    rf'studio/projects/{_ID}/jobs/{_ID}\.json',
    rf'studio/drafts/{_REQUEST}\.json',
    rf'assistant/(?:plans|jobs)/{_ID}\.json',
    rf'provider-jobs/{_ID}\.json',
)

# These retained canonical settings are not applied by this host. Recovery
# drafts additionally bind an existing scoped evidence row before publication.
_TYPED_ROOT_LIMITS = {'evidence-drafts.json': 8 * 1024**2,
                      'keyboard-shortcuts.json': 64 * 1024,
                      'progress-preferences.json': 8192,
                      'desktop-preferences.json': 8192,
                      'portable-sync-state.json': 1024 * 1024}


def _present(path):
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return False
    return True


def pending_path(value):
    """Paths owned exclusively by pending_backup in composite manifests."""
    return value == 'imports.sqlite' or value.startswith((
        'import-requests/', 'import-batches/', 'pending-originals/'))


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Application state backup or restore cancelled.')


def _plain(path, *, directory=False):
    info = Path(path).lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not valid or getattr(info, 'st_file_attributes', 0) & 0x400
            or (not directory and info.st_nlink != 1)):
        raise RestoreError('sidecar paths must be plain private files or directories')
    return info


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _path(value):
    if (not isinstance(value, str) or len(value) > 400
            or not any(re.fullmatch(pattern, value) for pattern in _PATHS)):
        raise RestoreError('unsupported or unsafe sidecar path')
    return PurePosixPath(value)


def _read_json(path):
    before = _plain(path)
    if not 0 < before.st_size <= MAX_JSON_BYTES:
        raise RestoreError('sidecar JSON size limit')
    with Path(path).open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise RestoreError('sidecar changed while opening')
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) != before.st_size or _identity(_plain(path)) != _identity(before):
        raise RestoreError('sidecar changed while reading')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(RestoreError('invalid JSON number')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError('invalid sidecar JSON') from exc


def _write_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > MAX_JSON_BYTES:
        raise RestoreError('sidecar JSON size limit')
    # Only the unpublished, verified private restore stage is rewritten.
    with Path(path).open('wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _copy(source, target, *, expected=None, cancelled=lambda: False, maximum_bytes=None):
    if maximum_bytes is not None and (type(maximum_bytes) is not int
            or not 0 <= maximum_bytes <= MAX_BYTES):
        raise RestoreError('sidecar snapshot resource limit')
    _check(cancelled)
    before = _plain(source)
    if not 0 < before.st_size <= MAX_FILE_BYTES:
        raise RestoreError('sidecar file size limit')
    if maximum_bytes is not None and before.st_size > maximum_bytes:
        raise RestoreError('sidecar snapshot resource limit')
    if expected is not None and before.st_size != expected['size_bytes']:
        raise RestoreError('sidecar size mismatch')
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    count, digest = 0, sha256()
    with source.open('rb') as incoming, target.open('xb') as outgoing:
        if _identity(os.fstat(incoming.fileno())) != _identity(before):
            raise RestoreError('sidecar changed while opening')
        while block := incoming.read(min(1024**2, before.st_size + 1 - count)):
            _check(cancelled)
            count += len(block)
            if count > before.st_size:
                raise RestoreError('sidecar grew during snapshot')
            digest.update(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if count != before.st_size or _identity(_plain(source)) != _identity(before):
        raise RestoreError('sidecar changed during snapshot; retry the backup')
    checksum = digest.hexdigest()
    if expected is not None and checksum != expected['sha256']:
        raise RestoreError('sidecar digest mismatch')
    return count, checksum


def _file_record(path, relative, *, cancelled=lambda: False):
    _check(cancelled)
    before = _plain(path)
    if not 0 < before.st_size <= MAX_FILE_BYTES:
        raise RestoreError('sidecar file size limit')
    digest, count = sha256(), 0
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise RestoreError('sidecar changed while opening')
        while block := stream.read(1024**2):
            _check(cancelled)
            count += len(block)
            if count > before.st_size:
                raise RestoreError('sidecar grew during verification')
            digest.update(block)
    if count != before.st_size or _identity(_plain(path)) != _identity(before):
        raise RestoreError('sidecar changed during verification')
    return dict(path=relative, size_bytes=count, sha256=digest.hexdigest())


def _ignored(parts):
    temporary = any(part.startswith('.') or '.invalid-' in part or '.incomplete-' in part
                    or part.endswith(('.lock', '.tmp')) for part in parts)
    # Project IDs such as work-authored-project are legitimate. Only the
    # director's third-level per-project work directory is temporary.
    return temporary or (len(parts) >= 3 and parts[0] == 'projects' and parts[2].startswith('work-'))


def _walk(root, *, exclude_temporary=False, exclude_cache=False, cancelled=lambda: False):
    """Never follow a directory link, including one in an excluded temp entry."""
    _plain(root, directory=True)
    pending, files, visited = [(root, ())], [], 0
    while pending:
        _check(cancelled)
        parent, parts = pending.pop()
        _plain(parent, directory=True)
        with os.scandir(parent) as children:
            for entry in children:
                _check(cancelled)
                visited += 1
                child = parent / entry.name
                relative = (*parts, child.name)
                if visited > MAX_FILES * 3 or len(relative) > 7:
                    raise RestoreError('sidecar tree resource limit')
                info = child.lstat()
                is_directory = stat.S_ISDIR(info.st_mode)
                _plain(child, directory=is_directory)
                if exclude_temporary and _ignored(relative):
                    continue
                # Only this existing primary-project cache namespace is optional.
                # Unknown directories still pass through the typed file checks.
                if (exclude_cache and is_directory and len(relative) == 3
                        and relative[0] == 'projects' and relative[2] == 'cache'
                        and re.fullmatch(_ID, relative[1])):
                    continue
                if is_directory:
                    pending.append((child, relative))
                else:
                    files.append(('/'.join(relative), child))
                    if len(files) > MAX_FILES:
                        raise RestoreError('sidecar file count limit')
        _check(cancelled)
    _check(cancelled)
    return sorted(files)

@contextmanager
def state_locks(root):
    """Fail visibly if another accepted state publication currently owns a lock."""
    with ExitStack() as stack:
        for name in ('studio', 'assistant'):
            directory = root / name
            if os.path.lexists(directory):
                _plain(directory, directory=True)
                lock = directory / '.state.lock'
                if os.path.lexists(lock):
                    _plain(lock)
                try:
                    stack.enter_context(FileLock(lock, mode=0o600).acquire(timeout=0))
                except Timeout as exc:
                    raise RestoreError(f'{name} state is busy; retry the backup') from exc
        yield


def _journal(path):
    if not 0 < _plain(path).st_size <= MAX_JOURNAL_BYTES:
        raise RestoreError('import journal size limit')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise RestoreError('invalid import journal integrity')
        schema = tuple(db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"))
        if schema != (('table', 'checkpoint'), ('table', 'job')):
            raise RestoreError('unsupported import journal schema')
        for name, columns in (
            ('job', ('id','workspace','edition','asset','config','count','expected_active','state','error')),
            ('checkpoint', ('job','ordinal','payload','digest')),
        ):
            if tuple(row[1] for row in db.execute(f'PRAGMA table_info({name})')) != columns:
                raise RestoreError('unsupported import journal columns')
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RestoreError('invalid import journal references')
        assets = tuple(row[0] for row in db.execute('SELECT DISTINCT asset FROM job LIMIT ?', (MAX_FILES + 1,)))
        if len(assets) > MAX_FILES or any(not isinstance(value, str) or not re.fullmatch(_HASH, value) for value in assets):
            raise RestoreError('invalid import journal asset identities')
        return assets


def validate_manifest(value):
    if (type(value) is not dict or set(value) != {'version','source_root','files'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['source_root'], str) or not 0 < len(value['source_root']) <= 32768
            or '\x00' in value['source_root']
            or not isinstance(value['files'], list) or not 1 <= len(value['files']) <= MAX_FILES):
        raise RestoreError('invalid sidecar manifest')
    paths, total = set(), 0
    for row in value['files']:
        if type(row) is not dict or set(row) != {'path','size_bytes','sha256'}:
            raise RestoreError('invalid sidecar file manifest')
        _path(row['path'])
        if (row['path'] in paths or type(row['size_bytes']) is not int
                or not 0 < row['size_bytes'] <= _TYPED_ROOT_LIMITS.get(row['path'], MAX_FILE_BYTES)
                or not isinstance(row['sha256'], str) or not re.fullmatch(_HASH, row['sha256'])):
            raise RestoreError('invalid or duplicate sidecar file manifest')
        paths.add(row['path'])
        total += row['size_bytes']
        if total > MAX_BYTES:
            raise RestoreError('sidecar total byte limit')
    if [row['path'] for row in value['files']] != sorted(paths):
        raise RestoreError('sidecar manifest paths must be ordered')
    return value


def _validate_json(relative, value, source_root):
    if relative == 'reading-state.json':
        from .reader.reading_state import _decode
        _decode(json.dumps(value, ensure_ascii=False))
    elif relative == 'keybindings.json':
        from .keybindings import decode_preferences
        decode_preferences(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    elif relative == 'ocr-language-presets.json':
        from .extract.ocr_languages import validate_presets_bytes
        validate_presets_bytes(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    elif relative == 'connections.json':
        from .assistant.providers import validate_profiles_document
        validate_profiles_document(value)
    elif relative == 'studio/music-settings.json':
        from .audio.music_settings import validate_music_settings
        validate_music_settings(value, check_files=False)
    elif relative == 'studio/voice-settings.json':
        from .audio.voice_settings import validate_voice_settings_document
        validate_voice_settings_document(value)
    elif relative.startswith('import-batches/'):
        from .ingest.batches import ImportBatchStore
        # _validate uses only the string binding. Preserve the original path
        # spelling even when this backup was created on another operating system.
        store = ImportBatchStore(SimpleNamespace(root=source_root))
        store._validate(value, PurePosixPath(relative).stem)
    elif relative.startswith('import-requests/reviews/'):
        if type(value) is not dict or len(value) > 1000:
            raise RestoreError('invalid import review sidecar')
    elif relative.startswith('import-requests/'):
        if (type(value) is not dict or type(value.get('version')) is not int or value.get('version') != 1
                or value.get('request_id') != PurePosixPath(relative).stem
                or value.get('data_dir') != source_root
                or type(value.get('metadata')) is not dict
                or type(value.get('config')) is not dict
                or not isinstance(value.get('workspace'), str)
                or not isinstance(value.get('source'), str)):
            raise RestoreError('invalid import request identity')
        from .ingest.worker import validate_publication_document
        validate_publication_document(value)
    elif relative.startswith('studio/'):
        from .audio.projects import Project, ProjectStore
        parts = PurePosixPath(relative).parts
        if parts[1] == 'drafts':
            ProjectStore.validate_draft(value, standalone=True)
            if value['id'] != PurePosixPath(relative).stem:
                raise RestoreError('studio recovery draft identity mismatch')
        elif parts[-1] == 'project.json' or parts[-2] == 'revisions':
            project = Project.from_dict(value)
            if project.id != parts[2]:
                raise RestoreError('studio project identity mismatch')
            if parts[-2] == 'revisions' and project.revision != int(PurePosixPath(relative).stem):
                raise RestoreError('studio revision identity mismatch')
        elif parts[-1] == 'draft.json':
            ProjectStore.validate_draft(value)
            if value.get('version') == 3 and value['project_id'] != parts[2]:
                raise RestoreError('studio recovery draft project identity mismatch')
        elif parts[-2] == 'jobs':
            if (type(value) is not dict or value.get('version') != 1
                    or value.get('id') != PurePosixPath(relative).stem
                    or value.get('project_id') != parts[2]):
                raise RestoreError('invalid studio job identity')
            project = Project.from_dict(value['project'])
            if project.id != parts[2] or value.get('project_fingerprint') != project.fingerprint:
                raise RestoreError('studio job project changed')
        elif parts[-2] == 'cache':
            if (type(value) is not dict or value.get('key') != PurePosixPath(relative).stem
                    or not isinstance(value.get('sha256'), str) or not re.fullmatch(_HASH, value['sha256'])):
                raise RestoreError('invalid accepted studio chunk metadata')
    elif relative.startswith('assistant/'):
        if type(value) is not dict:
            raise RestoreError('invalid assistant state document')
        if '/plans/' in relative:
            from .assistant.workflows import WorkflowPlan
            plan = WorkflowPlan.from_dict(value['plan'])
            if (value.get('id') != PurePosixPath(relative).stem or value['id'] != plan.id
                    or value.get('fingerprint') != plan.fingerprint):
                raise RestoreError('assistant plan identity changed')
    elif relative.startswith('provider-jobs/'):
        from .assistant.providers import validate_ledger_document
        validate_ledger_document(value, plan_id=PurePosixPath(relative).stem)


def _validate_source(db, source, *, asset=None):
    """Bind exact saved locations to the sealed database snapshot.

    Adopted from PR98's checked backup without consulting current revisions:
    an archived workspace or historical revision remains valid backup content.
    Studio links predate an explicit asset field; their sealed revision binds
    the immutable catalog asset before its independent inventory is checked.
    """
    keys = {'workspace', 'edition', 'revision', 'kind', 'index', 'text_sha256'}
    if (type(source) is not dict or set(source) != keys
            or source['kind'] not in ('physical_page', 'logical_segment')
            or type(source['index']) is not int or source['index'] < 1):
        raise RestoreError('invalid exact source link in backup')
    row = db.execute('''SELECT r.asset FROM revision r
        JOIN sealed s ON s.revision=r.id
        JOIN page p ON p.revision=r.id
        JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition
            AND e.asset=r.asset
        WHERE r.workspace=? AND r.edition=? AND r.id=? AND p.digest=?
            AND ((?='physical_page' AND p.physical=?) OR
                 (?='logical_segment' AND p.physical IS NULL AND p.ordinal=?))''',
        (source['workspace'], source['edition'], source['revision'], source['text_sha256'],
         source['kind'], source['index'], source['kind'], source['index'])).fetchone()
    if row is None or (asset is not None and row[0] != asset):
        raise RestoreError('reading state or studio source does not match its exact saved source')


def _verify_sources(db, relative, value):
    if relative == 'reading-state.json':
        from dataclasses import asdict
        from .reader.reading_state import snapshot_locations
        for location in snapshot_locations(json.dumps(value, ensure_ascii=False).encode('utf-8')):
            source = asdict(location)
            asset = source.pop('asset_sha256')
            _validate_source(db, source, asset=asset)
    elif relative.startswith('studio/'):
        source = value.get('source', {})
        if '/jobs/' in relative:
            source = value.get('project', {}).get('source', {})
        if source:
            _validate_source(db, source)


def _verify_tree(stage, manifest, *, db, cancelled=lambda: False):
    validate_manifest(manifest)
    actual = dict(_walk(stage, cancelled=cancelled))
    if set(actual) != {row['path'] for row in manifest['files']}:
        raise RestoreError('sidecar tree differs from its manifest')
    for row in manifest['files']:
        _check(cancelled)
        path = actual[row['path']]
        if _file_record(path, row['path'], cancelled=cancelled) != row:
            raise RestoreError('sidecar digest mismatch')
        if row['path'] in _TYPED_ROOT_LIMITS:
            from .backup_compat import read_regular, validate_sidecar
            validate_sidecar(db, row['path'], read_regular(path, _TYPED_ROOT_LIMITS[row['path']]))
        elif path.suffix == '.json':
            value = _read_json(path)
            _validate_json(row['path'], value, manifest['source_root'])
            _verify_sources(db, row['path'], value)
            if row['path'].startswith('studio/drafts/') and value.get('project_id') is not None:
                # Each window retains its original base, even after another
                # window commits a newer revision. Require the saved project
                # itself, without rebasing or discarding a stale recovery draft.
                current = f"studio/projects/{value['project_id']}/project.json"
                if current not in actual:
                    raise RestoreError('studio recovery draft has no matching saved project')
            if '/cache/' in row['path']:
                paired = row['path'][:-5] + '.wav'
                if paired in actual and value['sha256'] != _file_record(actual[paired], paired, cancelled=cancelled)['sha256']:
                    raise RestoreError('accepted studio chunk digest mismatch')
        elif row['path'] == 'imports.sqlite':
            _journal(path)
        if row['path'].startswith('pending-originals/') or '/assets/' in row['path']:
            if path.stem != row['sha256']:
                raise RestoreError('retained sidecar asset digest mismatch')
    projects = {PurePosixPath(name).parts[2] for name in actual if name.startswith('studio/projects/')}
    if any(f'studio/projects/{identity}/project.json' not in actual for identity in projects):
        raise RestoreError('studio state has no current project revision')
    if any('import-requests/' + PurePosixPath(name).name not in actual
           for name in actual if name.startswith('import-requests/reviews/')):
        raise RestoreError('import review has no matching request')


def snapshot(app, stage, registered_assets, *, db, include_pending=True,
             include_generated=True, cancelled=lambda: False, max_bytes=None):
    """Return None for a legacy-shaped library; otherwise seal all accepted state."""
    if type(include_pending) is not bool or type(include_generated) is not bool or not callable(cancelled):
        raise ValueError('Invalid application sidecar snapshot options.')
    selected_limit = max_bytes is not None
    if max_bytes is None:
        max_bytes = MAX_BYTES
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BYTES:
        raise RestoreError('sidecar snapshot resource limit must only lower the existing byte cap')
    _check(cancelled)
    root, target = app.root, stage / DIRECTORY
    selected = []
    for name in ('reading-state.json', 'connections.json', 'keybindings.json', 'ocr-language-presets.json',
                 *_TYPED_ROOT_LIMITS):
        _check(cancelled)
        path = root / name
        if _present(path):
            selected.append((name, path))
    directories = ['studio', 'assistant', 'provider-jobs']
    if include_pending:
        directories.extend(('import-requests', 'import-batches'))
    for name in directories:
        _check(cancelled)
        path = root / name
        if _present(path):
            selected.extend((name + '/' + relative, item)
                            for relative, item in _walk(path, exclude_temporary=True,
                                exclude_cache=(name == 'studio' and not include_generated), cancelled=cancelled))
    for relative, path in selected:
        _path(relative)
    if not include_generated:
        selected = [(name, path) for name, path in selected if '/cache/' not in name]
    if len(selected) > MAX_FILES:
        raise RestoreError('sidecar snapshot file count limit')
    meaningful_imports = any(relative.startswith(('import-requests/', 'import-batches/'))
                             for relative, _ in selected)
    if not selected:
        return None
    target.mkdir(mode=0o700)
    total, records, pending = 0, [], set()
    for relative, source in sorted(selected):
        _check(cancelled)
        size = _plain(source).st_size
        if not 0 < size <= _TYPED_ROOT_LIMITS.get(relative, MAX_FILE_BYTES):
            raise RestoreError('sidecar exceeds its data-format size limit')
        total += size
        if total > max_bytes or len(records) >= MAX_FILES:
            raise RestoreError('sidecar snapshot resource limit')
        size, checksum = _copy(source, target / relative, cancelled=cancelled,
            **({'maximum_bytes': size} if selected_limit else {}))
        records.append(dict(path=relative, size_bytes=size, sha256=checksum))
        if relative.startswith('import-requests/') and '/reviews/' not in relative:
            value = _read_json(target / relative)
            _validate_json(relative, value, str(root))
            if value.get('asset_sha256'):
                pending.add(value['asset_sha256'])
    journal = root / 'imports.sqlite'
    if meaningful_imports and os.path.lexists(journal):
        journal_size = _plain(journal).st_size
        total += journal_size
        if total > max_bytes or len(records) >= MAX_FILES:
            raise RestoreError('sidecar snapshot resource limit')
        _journal(journal)
        journal_copy = target / 'imports.sqlite'
        with closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True)) as incoming:
            remaining_bytes = max_bytes - (total - journal_size)
            if selected_limit:
                page_size = incoming.execute('PRAGMA page_size').fetchone()[0]
                page_count = incoming.execute('PRAGMA page_count').fetchone()[0]
                if page_size * page_count > remaining_bytes:
                    raise RestoreError('sidecar snapshot resource limit')
                _check(cancelled)
            with closing(sqlite3.connect(journal_copy)) as outgoing:
                if not selected_limit:
                    incoming.backup(outgoing)
                else:
                    def progress(status, remaining, total_pages):
                        _check(cancelled)
                        if total_pages * page_size > remaining_bytes:
                            raise RestoreError('sidecar snapshot resource limit')
                    pages = max(1, min(256, remaining_bytes // page_size))
                    incoming.backup(outgoing, pages=pages, progress=progress)
        pending.update(_journal(journal_copy))
        record = _file_record(journal_copy, 'imports.sqlite', cancelled=cancelled)
        if selected_limit:
            total += record['size_bytes'] - journal_size
            if total > max_bytes:
                raise RestoreError('sidecar snapshot resource limit')
        records.append(record)
    for digest in sorted(pending - set(registered_assets)):
        _check(cancelled)
        source = app.assets.resolve(digest)
        size = _plain(source).st_size
        total += size
        if total > max_bytes or len(records) >= MAX_FILES:
            raise RestoreError('sidecar snapshot resource limit')
        relative = 'pending-originals/' + source.name
        size, checksum = _copy(source, target / relative, cancelled=cancelled,
            **({'maximum_bytes': size} if selected_limit else {}))
        records.append(dict(path=relative, size_bytes=size, sha256=checksum))
    manifest = dict(version=1, source_root=str(root), files=sorted(records, key=lambda row: row['path']))
    _verify_tree(target, manifest, db=db, cancelled=cancelled)
    return manifest


def _restore_studio_job(value):
    """Revoke the existing primary job authority only in a verified private copy."""
    value.update(restore_requires_new_output=True,
        restored_from_output_dir=value.get('output_dir', ''), output_dir='',
        state='paused', result=None, outputs=[],
        message='Restored production: choose a new output folder and create a new job. Accepted cached chunks remain available.')


def restore(source, stage, manifest, destination, *, db, cancelled=lambda: False):
    """Verify source bytes, then apply only explicit restore-safe state changes."""
    validate_manifest(manifest)
    _check(cancelled)
    if dict(_walk(source, cancelled=cancelled)).keys() != {row['path'] for row in manifest['files']}:
        raise RestoreError('sidecar tree differs from its manifest')
    target = stage / DIRECTORY
    target.mkdir(mode=0o700)
    for row in manifest['files']:
        _copy(source / row['path'], target / row['path'], expected=row, cancelled=cancelled)
    _verify_tree(target, manifest, db=db, cancelled=cancelled)
    rebound = []
    for row in manifest['files']:
        _check(cancelled)
        relative, path = row['path'], target / row['path']
        changed = False
        if path.suffix == '.json':
            value = _read_json(path)
            if (relative.startswith('import-batches/') or
                    (relative.startswith('import-requests/') and '/reviews/' not in relative)):
                if relative.startswith('import-requests/'):
                    # _verify_tree already checked the original receipt hash.
                    # Root/source rebinding requires a fresh import attempt.
                    value.pop('publication', None)
                    value.pop('attempt_id', None)
                value['data_dir'] = str(destination)
                changed = True
            elif relative == 'connections.json':
                for profile in value['profiles']:
                    profile['credential_ref'] = None
                changed = True
            elif relative == 'studio/voice-settings.json':
                value['restored'] = True
                changed = True
            elif relative.startswith('studio/') and '/jobs/' in relative:
                _restore_studio_job(value)
                changed = True
            elif relative.startswith(('assistant/plans/', 'assistant/jobs/')):
                value['restore_requires_review'] = True
                changed = True
            elif relative.startswith('provider-jobs/'):
                value['restored'] = True
                changed = True
            if changed:
                _write_json(path, value)
        rebound.append(_file_record(path, relative, cancelled=cancelled) if changed else row)
    updated = dict(version=1, source_root=str(destination), files=rebound)
    _verify_tree(target, updated, db=db, cancelled=cancelled)
    return updated


def publish(stage, destination, manifest, *, backup, cancelled=lambda: False):
    """Publish only an already validated stage into the caller's new directory."""
    base = destination / DIRECTORY if backup else destination
    if backup:
        base.mkdir(mode=0o700)
    projects = set()
    for row in manifest['files']:
        _check(cancelled)
        relative = row['path']
        source = stage / DIRECTORY / relative
        target_relative = ('originals/' + PurePosixPath(relative).name
                           if not backup and relative.startswith('pending-originals/') else relative)
        target = base / target_relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.link(source, target)
        if not backup and relative.startswith('studio/projects/'):
            projects.add(PurePosixPath(relative).parts[2])
    for identity in projects:
        for name in ('revisions', 'cache', 'jobs', 'assets'):
            (destination / 'studio' / 'projects' / identity / name).mkdir(mode=0o700, exist_ok=True)
    if os.name != 'nt':
        directories = {base}
        for row in manifest['files']:
            relative = row['path']
            relative = ('originals/' + PurePosixPath(relative).name
                        if not backup and relative.startswith('pending-originals/') else relative)
            parent = (base / relative).parent
            while parent.is_relative_to(base):
                directories.add(parent)
                if parent == base:
                    break
                parent = parent.parent
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            _check(cancelled)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
