"""Verified, local studio backups; no in-place restore or partial publication.

The SQLite online backup is the authority for revisions and committed cache
files. In-flight files and locks are deliberately excluded. Retain the returned
manifest SHA256 independently: a digest supplied by an untrusted backup is not
authentication. Existing source/destination parents must be private and stable.

An audio-free backup still retains every plan, original authored script, job,
and completed production record. Restoring it archives production records under
``restore-history/<backup digest>/`` and clears cache rows/results, so a new
render cannot conflict with stale immutable production records. Model runtimes,
voice installations and external assets not yet snapshotted are not bundled.
"""
from contextlib import closing
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import stat
from tempfile import TemporaryDirectory
import time

from ..audio.ffmpeg import _local_path
from ..data_paths import guard_data_directory
from .models import PlanError, Project, unique_object
from .store import ProjectStore, _verify_report


DATABASE = 'studio.sqlite'
MANIFEST = 'manifest.json'
INCOMPLETE = '.restore-incomplete'
MAX_MANIFEST = 16 * 1024**2
MAX_DATABASE = 512 * 1024**2
MAX_METADATA = 64 * 1024**2
MAX_FILES = 100000
# Streamed multi-hour masters are permitted, including RF64. This is a disk
# safety bound, not a duration restriction; nothing is truncated to fit it.
MAX_TOTAL_BYTES = 1024**4
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_STATES = {'running', 'paused', 'cancelled', 'failed', 'complete', 'interrupted'}
_RESULT_EXTRA = {'manifest_path', 'captions_path', 'job_id', 'completed_chunks', 'reused_chunks'}
_PRODUCTION_FIELDS = {'schema_version', 'pipeline', 'project_revision', 'title', 'mode',
    'path_base', 'master', 'chapters', 'captions', 'voice_fingerprints', 'music',
    'source_reference', 'source_text_unchanged', 'listening_review', 'word_alignment', 'nvda_review'}


class BackupError(PlanError):
    """A backup is incomplete, incompatible, changed, or outside local bounds."""


class BackupCancelled(RuntimeError):
    """The operation was cancelled; no completed backup/restore was reported."""


def _check(cancelled):
    if cancelled is not None and (cancelled.is_set() if hasattr(cancelled, 'is_set') else cancelled()):
        raise BackupCancelled('Studio backup or restore cancelled.')


def _digest(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _json(raw):
    try:
        def constant(value):
            raise BackupError('Non-finite JSON number in studio backup.')
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise BackupError('Invalid or duplicate JSON field in studio backup.') from exc


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                       allow_nan=False) + '\n').encode('utf-8')


def _plain(path, *, directory=False):
    """Check every existing ancestor; final symlinks and junctions never count."""
    path = Path(path).absolute()
    for ancestor in (*reversed(path.parents), path):
        info = ancestor.lstat()
        required = stat.S_ISDIR if ancestor != path or directory else stat.S_ISREG
        if not required(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise BackupError('Backup paths must be regular local files and directories without symbolic links.')
    return info


def _local(value):
    try:
        path = _local_path(value)
        if '..' in path.parts:
            raise BackupError('Parent traversal is unavailable in studio backup paths.')
        return path.absolute()
    except (ValueError, TypeError) as exc:
        raise BackupError('Choose a regular local backup path.') from exc


def _destination(value, source):
    path = _local(value)
    guard_data_directory(path, purpose='studio backup or restored project')
    _plain(path.parent, directory=True)
    if os.path.lexists(path):
        raise FileExistsError(path)
    if path.is_relative_to(source) or source.is_relative_to(path):
        raise BackupError('Backup and restore destinations must be outside their source directory.')
    return path


def _relative(value, *, windows=False):
    if type(value) is not str or not value or '\0' in value:
        raise BackupError('Invalid relative backup path.')
    if windows:
        value = value.replace('\\', '/')
    parts = value.split('/')
    if (len(parts) > 6 or len(value) > 1024 or any(not p or p in ('.', '..') for p in parts)
            or '\\' in value or ':' in value or value.startswith('/')
            or any(any(ord(c) < 32 or c in '<>"|?*' for c in p) or p.endswith((' ', '.'))
                   or p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                       *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
                   for p in parts)):
        raise BackupError('Unsafe or non-portable relative backup path.')
    return '/'.join(parts)


def _original_root(value):
    if type(value) is not str or not value or '\0' in value or value.startswith(('//', '\\\\')):
        raise BackupError('Invalid original project root.')
    path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts:
        raise BackupError('Original project root must be an absolute local path.')
    return path


def _owned_path(value, original_root):
    if type(value) is not str or not value:
        raise BackupError('A render result path is unavailable.')
    root = _original_root(original_root)
    path = type(root)(value)
    if path.is_absolute():
        try:
            value = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise BackupError('A render result points outside its original project.') from exc
    return _relative(value, windows=isinstance(root, PureWindowsPath))


def _sync_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _same_file(first, second):
    return (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) == (
        second.st_dev, second.st_ino, second.st_size, second.st_mtime_ns)


def _copy(source, target, *, limit, expected=None, cancelled=None):
    _check(cancelled)
    before = _plain(source)
    if not 0 < before.st_size <= limit:
        raise BackupError('Backup file exceeds its size limit or is empty.')
    if expected is not None and before.st_size != expected['size_bytes']:
        raise BackupError('Backup file size mismatch.')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    digest, size = sha256(), 0
    with os.fdopen(descriptor, 'rb') as incoming, target.open('xb') as output:
        if not _same_file(before, os.fstat(incoming.fileno())):
            raise BackupError('Backup source changed before it could be copied.')
        while block := incoming.read(min(1024**2, before.st_size + 1 - size)):
            _check(cancelled)
            size += len(block)
            if size > before.st_size:
                raise BackupError('Backup source grew during copying.')
            digest.update(block)
            output.write(block)
        if not _same_file(before, os.fstat(incoming.fileno())) or not _same_file(before, _plain(source)):
            raise BackupError('Backup source changed during copying.')
        output.flush()
        os.fsync(output.fileno())
    actual = digest.hexdigest()
    if size != before.st_size or (expected is not None and actual != expected['sha256']):
        raise BackupError('Backup file checksum mismatch.')
    return {'size_bytes': size, 'sha256': actual}


def _read(path, limit):
    before = _plain(path)
    if not 0 < before.st_size <= limit:
        raise BackupError('Backup metadata exceeds its size limit or is empty.')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(raw) != before.st_size or not _same_file(before, after) or not _same_file(before, _plain(path)):
        raise BackupError('Backup metadata changed during reading.')
    return raw


def _schema(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    rows = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name,tbl_name,sql').fetchall()
    return version, tuple(rows)


def _canonical_schema(parent):
    with TemporaryDirectory(prefix='.studio-schema-', dir=parent) as tmp:
        reference = ProjectStore(tmp)
        with reference.connect() as db:
            return _schema(db)


def _readonly(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA query_only=ON')
    return db


def _records(snapshot, canonical, cancelled):
    try:
        with closing(_readonly(snapshot)) as db:
            if _schema(db) != canonical:
                raise BackupError('Unsupported studio backup schema; exact layout differs.')
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise BackupError('Studio backup database integrity check failed.')
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise BackupError('Studio backup database references are incomplete.')
            projects, chunks, jobs = {}, {}, []
            for revision, document, created in db.execute('SELECT revision,document,created FROM projects ORDER BY revision'):
                _check(cancelled)
                if (not _digest(revision) or type(document) is not str or len(document.encode('utf-8')) > MAX_METADATA
                        or type(created) not in (float, int) or not math.isfinite(created)):
                    raise BackupError('Invalid saved project record.')
                project = Project.from_dict(_json(document))
                if project.revision != revision or project.canonical_json() != document:
                    raise BackupError('Saved project revision checksum mismatch.')
                projects[revision] = document
            current = db.execute('SELECT id,revision FROM current_project').fetchall()
            if (len(current) > 1 or any(i != 1 or r not in projects for i, r in current)
                    or bool(projects) != bool(current)):
                raise BackupError('Current project revision is inconsistent.')
            for key, path, digest, report in db.execute('SELECT cache_key,path,sha256,report FROM chunks ORDER BY cache_key'):
                _check(cancelled)
                path = _relative(path, windows=True)
                report = _json(report)
                if (not _digest(key) or not _digest(digest) or not path.startswith('cache/')
                        or type(report) is not dict or report.get('sha256') != digest):
                    raise BackupError('Invalid committed cache record.')
                chunks[key] = {'path': path, 'sha256': digest, 'report': report}
            for identity, revision, state, progress, result, error, updated in db.execute(
                    'SELECT id,revision,state,progress,result,error,updated FROM jobs ORDER BY id'):
                _check(cancelled)
                progress, result = _json(progress), _json(result) if result is not None else None
                if (type(identity) is not str or re.fullmatch('[0-9a-f]{32}', identity) is None
                        or revision not in projects or state not in _STATES or type(progress) is not dict
                        or (result is not None and type(result) is not dict)
                        or (error is not None and type(error) is not str)
                        or type(updated) not in (float, int) or not math.isfinite(updated)):
                    raise BackupError('Invalid saved render checkpoint.')
                jobs.append(dict(id=identity, revision=revision, state=state, progress=progress,
                                 result=result, error=error, updated=updated))
    except (sqlite3.DatabaseError, TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError('Invalid or incompatible studio database.') from exc
    return projects, chunks, jobs


def _production(job, original_root, chunks):
    """Only adapt the documented result schema and its known path fields."""
    result, revision = job['result'], job['revision']
    if job['state'] != 'complete' or type(result) is not dict:
        return None
    try:
        if (set(result) != _PRODUCTION_FIELDS | _RESULT_EXTRA or type(result['schema_version']) is not int
                or result['schema_version'] != 1 or result['path_base'] != 'project_root'
                or result['mode'] != 'LOCAL_ONLY' or result['project_revision'] != revision
                or result['job_id'] != job['id'] or type(result['master']) is not dict
                or not _digest(result['master'].get('sha256')) or type(result['chapters']) is not list):
            return None
        directory = f'renders/{revision}/'
        if (_owned_path(result['manifest_path'], original_root) != directory + 'production.json'
                or _owned_path(result['captions_path'], original_root) != directory + 'captions.srt'
                or _owned_path(result['master']['path'], original_root) != directory + 'master.wav'):
            return None
        cache = {entry['path']: entry['sha256'] for entry in chunks.values()}
        chapters = []
        for chapter in result['chapters']:
            path = _owned_path(chapter['path'], original_root)
            if cache.get(path) != chapter['sha256']:
                return None
            chapters.append({**chapter, 'path': path})
        return {**{key: result[key] for key in _PRODUCTION_FIELDS},
                'master': {**result['master'], 'path': directory + 'master.wav'}, 'chapters': chapters}
    except (BackupError, KeyError, TypeError):
        return None


def _expected(projects, chunks, jobs, original_root, include_audio):
    files, productions = {}, {}
    for revision, document in projects.items():
        raw = (document + '\n').encode('utf-8')
        files[f'revisions/{revision}.json'] = dict(kind='revision', size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    if include_audio:
        for entry in chunks.values():
            existing = files.get(entry['path'])
            if existing is not None and existing['sha256'] != entry['sha256']:
                raise BackupError('Conflicting checksums for the same cache path.')
            files[entry['path']] = dict(kind='cache', sha256=entry['sha256'])
    for job in jobs:
        production = _production(job, original_root, chunks)
        if production is None:
            continue
        revision = job['revision']
        if revision in productions and productions[revision] != production:
            raise BackupError('Completed jobs disagree about one production record.')
        productions[revision] = production
        for name in ('production.json', 'captions.srt'):
            files[f'renders/{revision}/{name}'] = dict(kind='render-metadata')
        if include_audio:
            files[f'renders/{revision}/master.wav'] = dict(kind='render-audio', sha256=production['master']['sha256'])
    if len(files) > MAX_FILES:
        raise BackupError('Studio backup file count exceeds its budget.')
    return files, productions


def _history_path(path):
    parts = path.split('/')
    return (len(parts) == 5 and parts[0] == 'restore-history' and _digest(parts[1])
            and parts[2] == 'renders' and _digest(parts[3]) and parts[4] in ('production.json', 'captions.srt'))


def _inventory(root, *, cancelled=None):
    files, directories, pending = set(), set(), [root]
    while pending:
        _check(cancelled)
        directory = pending.pop()
        _plain(directory, directory=True)
        with os.scandir(directory) as children:
            for child in children:
                _check(cancelled)
                path = Path(child.path)
                info = child.stat(follow_symlinks=False)
                relative = path.relative_to(root).as_posix()
                if getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode):
                    raise BackupError('Symbolic links and reparse points are unavailable in studio backups.')
                if stat.S_ISDIR(info.st_mode):
                    directories.add(relative)
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    files.add(relative)
                else:
                    raise BackupError('Studio backups require regular files.')
                if len(files) + len(directories) > MAX_FILES * 4:
                    raise BackupError('Studio backup inventory exceeds its file budget.')
    return files, directories


def _ancestors(files):
    return {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != '.'}


def _portable_production(value, original_root):
    if type(value) is not dict or type(value.get('master')) is not dict or type(value.get('chapters')) is not list:
        raise BackupError('Invalid production manifest.')
    try:
        return {**value, 'master': {**value['master'], 'path': _owned_path(value['master']['path'], original_root)},
                'chapters': [{**chapter, 'path': _owned_path(chapter['path'], original_root)} for chapter in value['chapters']]}
    except (TypeError, KeyError) as exc:
        raise BackupError('Invalid production manifest paths.') from exc


def _validate_assets(stage, productions, chunks, include_audio, original_root):
    if include_audio:
        for entry in chunks.values():
            try:
                _verify_report(stage / entry['path'], entry['report'], entry['sha256'])
            except (ValueError, OSError) as exc:
                raise BackupError('Committed cache report does not match the verified file.') from exc
    for revision, production in productions.items():
        actual = _portable_production(_json(_read(stage / f'renders/{revision}/production.json', MAX_METADATA)), original_root)
        if actual != production:
            raise BackupError('Production manifest differs from its completed job record.')


def _publish(stage, destination, names, *, restoring, cancelled):
    _check(cancelled)
    destination.mkdir(mode=0o700)
    if restoring:
        _sync_file(destination / INCOMPLETE, b'Studio restore is incomplete. Restore into a fresh directory.\n')
        _sync_directory(destination)
    # The database is last on restore, the completion manifest last on backup.
    last = DATABASE if restoring else MANIFEST
    for name in sorted(set(names) - {last}):
        _check(cancelled)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.link(stage / name, target)
    for directory in sorted(_ancestors(names), key=lambda p: -len(PurePosixPath(p).parts)):
        _sync_directory(destination / directory)
    _sync_directory(destination)
    _check(cancelled)
    os.link(stage / last, destination / last)
    _sync_directory(destination)
    if restoring:
        _check(cancelled)
        (destination / INCOMPLETE).unlink()
        _sync_directory(destination)
    _sync_directory(destination.parent)


def backup_project(store, destination, *, include_audio=True, cancelled=None):
    """Snapshot a live project to a new local folder; return its manifest hash."""
    if not isinstance(store, ProjectStore) or type(include_audio) is not bool:
        raise BackupError('A ProjectStore and a boolean include_audio option are required.')
    _check(cancelled)
    source = _local(store.root)
    _plain(source, directory=True)
    _plain(store.database)
    if os.path.lexists(source / INCOMPLETE):
        raise BackupError('An incomplete studio restore cannot be backed up.')
    destination = _destination(destination, source)
    canonical = _canonical_schema(destination.parent)
    with TemporaryDirectory(prefix='.studio-backup-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        snapshot = stage / DATABASE
        with closing(sqlite3.connect(store.database.as_uri() + '?mode=ro', uri=True)) as incoming:
            incoming.execute('PRAGMA trusted_schema=OFF')
            page_size = incoming.execute('PRAGMA page_size').fetchone()[0]
            def progress(status, remaining, total):
                _check(cancelled)
                if total * page_size > MAX_DATABASE:
                    raise BackupError('Studio database exceeds its backup size budget.')
            with closing(sqlite3.connect(snapshot)) as outgoing:
                incoming.backup(outgoing, pages=128, progress=progress, sleep=.01)
        if not 0 < snapshot.stat().st_size <= MAX_DATABASE:
            raise BackupError('Studio database exceeds its backup size budget.')
        with snapshot.open('r+b') as stream:
            os.fsync(stream.fileno())
        records = _records(snapshot, canonical, cancelled)
        projects, chunks, jobs = records
        expected, productions = _expected(*records, str(source), include_audio)
        history = source / 'restore-history'
        if os.path.lexists(history):
            history_files, _ = _inventory(history, cancelled=cancelled)
            for name in history_files:
                path = 'restore-history/' + name
                if not _history_path(path):
                    raise BackupError('Unknown file in restored production history.')
                expected[path] = dict(kind='history')
        files, total = [], snapshot.stat().st_size
        for name, entry in sorted(expected.items()):
            path = source / name
            size = _plain(path).st_size
            if total + size > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                raise BackupError('Studio backup exceeds its file or disk budget.')
            limit = MAX_TOTAL_BYTES if entry['kind'] in ('cache', 'render-audio') else MAX_METADATA
            metadata = _copy(path, stage / name, limit=limit, expected=(
                dict(size_bytes=entry.get('size_bytes', size), sha256=entry['sha256']) if 'sha256' in entry else None),
                cancelled=cancelled)
            total += metadata['size_bytes']
            if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                raise BackupError('Studio backup exceeds its file or disk budget.')
            files.append(dict(path=name, kind=entry['kind'], **metadata))
        _validate_assets(stage, productions, chunks, include_audio, str(source))
        database_raw = sha256()
        with snapshot.open('rb') as stream:
            while block := stream.read(1024**2):
                _check(cancelled)
                database_raw.update(block)
        manifest = dict(version=1, kind='carr-studio', schema_version=canonical[0],
            schema_fingerprint=sha256(_encoded(canonical)).hexdigest(), original_root=str(source),
            include_audio=include_audio, database=dict(path=DATABASE, size_bytes=snapshot.stat().st_size,
                sha256=database_raw.hexdigest()), files=files)
        raw = _encoded(manifest)
        if len(raw) > MAX_MANIFEST:
            raise BackupError('Studio backup manifest exceeds its size budget.')
        _sync_file(stage / MANIFEST, raw)
        _manifest(stage, sha256(raw).hexdigest(), canonical, cancelled)
        _publish(stage, destination, [DATABASE, MANIFEST, *expected], restoring=False, cancelled=cancelled)
    return sha256(raw).hexdigest()


def _manifest(source, expected_digest, canonical, cancelled):
    if not _digest(expected_digest):
        raise BackupError('An independently retained manifest SHA256 is required.')
    raw = _read(source / MANIFEST, MAX_MANIFEST)
    if sha256(raw).hexdigest() != expected_digest:
        raise BackupError('Studio backup manifest checksum mismatch.')
    value = _json(raw)
    fields = {'version', 'kind', 'schema_version', 'schema_fingerprint', 'original_root', 'include_audio', 'database', 'files'}
    if (type(value) is not dict or set(value) != fields or type(value['version']) is not int
            or value['version'] != 1 or value['kind'] != 'carr-studio'
            or type(value['schema_version']) is not int or value['schema_version'] != canonical[0]
            or value['schema_fingerprint'] != sha256(_encoded(canonical)).hexdigest()
            or type(value['include_audio']) is not bool or type(value['files']) is not list
            or len(value['files']) > MAX_FILES):
        raise BackupError('Unsupported studio backup manifest or schema.')
    _original_root(value['original_root'])
    db = value['database']
    if (type(db) is not dict or set(db) != {'path', 'size_bytes', 'sha256'} or db['path'] != DATABASE
            or type(db['size_bytes']) is not int or not 0 < db['size_bytes'] <= MAX_DATABASE or not _digest(db['sha256'])):
        raise BackupError('Invalid studio database manifest.')
    names, total = {}, db['size_bytes']
    for entry in value['files']:
        _check(cancelled)
        if type(entry) is not dict or set(entry) != {'path', 'kind', 'size_bytes', 'sha256'}:
            raise BackupError('Invalid studio backup file entry.')
        path = _relative(entry['path'])
        parts = path.split('/')
        kind = entry['kind']
        valid = ((kind == 'revision' and len(parts) == 2 and parts[0] == 'revisions'
                  and parts[1].endswith('.json') and _digest(parts[1][:-5]))
                 or (kind == 'cache' and len(parts) >= 2 and parts[0] == 'cache')
                 or (kind in ('render-metadata', 'render-audio') and len(parts) == 3 and parts[0] == 'renders'
                     and _digest(parts[1]) and parts[2] in (
                         ('production.json', 'captions.srt') if kind == 'render-metadata' else ('master.wav',)))
                 or (kind == 'history' and _history_path(path)))
        maximum = MAX_TOTAL_BYTES if kind in ('cache', 'render-audio') else MAX_METADATA
        if (not valid or type(entry['size_bytes']) is not int or not 0 < entry['size_bytes'] <= maximum
                or not _digest(entry['sha256']) or path.casefold() in names
                or (not value['include_audio'] and kind in ('cache', 'render-audio'))):
            raise BackupError('Invalid, duplicate, or oversized studio backup file.')
        names[path.casefold()] = path
        total += entry['size_bytes']
        if total > MAX_TOTAL_BYTES:
            raise BackupError('Studio backup exceeds its total disk budget.')
    expected_files = {DATABASE, MANIFEST, *names.values()}
    files, directories = _inventory(source, cancelled=cancelled)
    if files != expected_files or directories != _ancestors(expected_files):
        raise BackupError('Studio backup is incomplete or contains unexpected files.')
    return value


def _prepare_restore(snapshot, destination, records, manifest, cancelled):
    """Called only after every staged byte, schema and relationship is checked."""
    _, chunks, jobs = records
    with closing(sqlite3.connect(snapshot)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        with db:
            if manifest['include_audio']:
                for key, entry in chunks.items():
                    _check(cancelled)
                    report = {**entry['report'], 'path': str(destination / entry['path'])}
                    db.execute('UPDATE chunks SET path=?,report=? WHERE cache_key=?',
                               (entry['path'], _encoded(report).decode('utf-8').rstrip('\n'), key))
            else:
                db.execute('DELETE FROM chunks')
            for job in jobs:
                _check(cancelled)
                result, state, error, updated = job['result'], job['state'], job['error'], job['updated']
                if state == 'running':
                    state, error, result = 'interrupted', 'Restored snapshot of an active render; resume from verified chunks.', None
                    updated = time.time()
                elif state == 'complete':
                    production = _production(job, manifest['original_root'], chunks)
                    if manifest['include_audio'] and production is not None:
                        directory = destination / 'renders' / job['revision']
                        result = {**result, 'master': {**result['master'], 'path': str(directory / 'master.wav')},
                            'chapters': production['chapters'], 'manifest_path': str(directory / 'production.json'),
                            'captions_path': str(directory / 'captions.srt')}
                    else:
                        state, result = 'interrupted', None
                        error = ('Audio excluded from backup; render again from the saved plan.' if not manifest['include_audio']
                                 else 'Previous result paths could not be safely relocated; resume from verified chunks.')
                        updated = time.time()
                elif result is not None:
                    # Non-complete results are not a publication contract.
                    result = None
                db.execute('UPDATE jobs SET state=?,result=?,error=?,updated=? WHERE id=?',
                           (state, _encoded(result).decode('utf-8').rstrip('\n') if result is not None else None,
                            error, updated, job['id']))
    with snapshot.open('r+b') as stream:
        os.fsync(stream.fileno())


def restore_project(source, destination, *, expected_manifest_sha256, cancelled=None):
    """Verify all bytes/schema first, then restore into a fresh local project."""
    _check(cancelled)
    source = _local(source)
    _plain(source, directory=True)
    destination = _destination(destination, source)
    canonical = _canonical_schema(destination.parent)
    manifest = _manifest(source, expected_manifest_sha256, canonical, cancelled)
    with TemporaryDirectory(prefix='.studio-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        snapshot = stage / DATABASE
        _copy(source / DATABASE, snapshot, limit=MAX_DATABASE, expected=manifest['database'], cancelled=cancelled)
        entries = {entry['path']: entry for entry in manifest['files']}
        for name, entry in entries.items():
            _copy(source / name, stage / name,
                  limit=MAX_TOTAL_BYTES if entry['kind'] in ('cache', 'render-audio') else MAX_METADATA,
                  expected=entry, cancelled=cancelled)
        records = _records(snapshot, canonical, cancelled)
        projects, chunks, jobs = records
        expected, productions = _expected(*records, manifest['original_root'], manifest['include_audio'])
        actual = {name: entry for name, entry in entries.items() if entry['kind'] != 'history'}
        if set(actual) != set(expected) or any(any(actual[name].get(k) != v for k, v in entry.items())
                                              for name, entry in expected.items()):
            raise BackupError('Backup files disagree with the committed studio database snapshot.')
        _validate_assets(stage, productions, chunks, manifest['include_audio'], manifest['original_root'])
        _prepare_restore(snapshot, destination, records, manifest, cancelled)
        if manifest['include_audio']:
            # Adapt only the documented relative path fields, after verifying
            # the original bytes. Render metadata must use this host's native
            # separators so the director can reuse its immutable publication.
            for revision, production in productions.items():
                native = {**production, 'master': {**production['master'], 'path': str(Path(production['master']['path']))},
                    'chapters': [{**chapter, 'path': str(Path(chapter['path']))} for chapter in production['chapters']]}
                path = stage / f'renders/{revision}/production.json'
                with path.open('wb') as output:
                    output.write((json.dumps(native, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8'))
                    output.flush()
                    os.fsync(output.fileno())
        names = [DATABASE]
        for name, entry in entries.items():
            if not manifest['include_audio'] and entry['kind'] == 'render-metadata':
                archived = f'restore-history/{expected_manifest_sha256}/{name}'
                target = stage / archived
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.rename(stage / name, target)
                names.append(archived)
            else:
                names.append(name)
        _publish(stage, destination, names, restoring=True, cancelled=cancelled)
    return ProjectStore(destination)
