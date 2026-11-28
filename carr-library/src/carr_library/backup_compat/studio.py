"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
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
from .pcm import _local_path
from .studio_models import PlanError, Project, unique_object, render_identity
from .studio_assets import _verify_report


DATABASE = 'studio.sqlite'


MANIFEST = 'manifest.json'


MAX_MANIFEST = 16 * 1024**2


MAX_DATABASE = 512 * 1024**2


MAX_METADATA = 64 * 1024**2


MAX_FILES = 100000


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


def _same_file(first, second):
    return (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) == (
        second.st_dev, second.st_ino, second.st_size, second.st_mtime_ns)


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


def _canonical_schema(parent=None):
    """Return the frozen schema, without opening a runtime or source store."""
    from .schemas import STUDIO_SCHEMA
    return STUDIO_SCHEMA


def _readonly(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA query_only=ON')
    return db


def _records(snapshot, canonical, cancelled):
    try:
        with closing(_readonly(snapshot)) as db:
            db.set_progress_handler(lambda: int(bool(cancelled and cancelled())), 10_000)
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
        manifest_path = _owned_path(result['manifest_path'], original_root)
        render_key = PurePosixPath(manifest_path).parent.name
        if render_key not in {revision, render_identity(revision, result['pipeline'],
                result['voice_fingerprints'], result['master']['sha256'])}:
            return None
        # Revision-only directories remain a supported legacy publication.
        directory = f'renders/{render_key}/'
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
        render_key = PurePosixPath(production['master']['path']).parent.name
        if render_key in productions and productions[render_key] != production:
            raise BackupError('Completed jobs disagree about one production record.')
        productions[render_key] = production
        for name in ('production.json', 'captions.srt'):
            files[f'renders/{render_key}/{name}'] = dict(kind='render-metadata')
        if include_audio:
            files[f'renders/{render_key}/master.wav'] = dict(kind='render-audio', sha256=production['master']['sha256'])
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
