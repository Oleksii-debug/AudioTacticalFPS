"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
from contextlib import closing
from dataclasses import asdict, replace
from functools import lru_cache
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import stat
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from ..ingest.bundle import _unique_object
from ..ingest.restore import RestoreError


DIRECTORY = 'recovery'


MAX_FILES = 100_000


MAX_BYTES = 64 * 1024**3


MAX_FILE_BYTES = 32 * 1024**3


MAX_DATABASE = 512 * 1024**2


MAX_JSON = 32 * 1024**2


_HASH = r'[0-9a-f]{64}'


_ID = r'[0-9a-f]{32}'


_PROFILE = r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}'


_DATABASES = {'imports.sqlite', 'provider-settings/providers.sqlite', 'agent-jobs/agent.sqlite'}


_JOURNAL_STATES = frozenset(('pending', 'complete', 'failed', 'cancelled', 'conflict', 'quarantined', 'superseded'))


_REQUEST_STATES = _JOURNAL_STATES | {'running', 'timed_out'}


def _plain(path, *, directory=False, allow_links=False):
    info = Path(path).lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or (not directory and not allow_links and info.st_nlink != 1)):
        raise RestoreError('Recovery paths must be plain private files and directories.')
    return info


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _local_absolute(value):
    """Validate an inert original-machine declaration, without host path access."""
    return (type(value) is str and 0 < len(value) <= 32768 and '\0' not in value
            and '://' not in value and (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()))


def _path(value):
    if (type(value) is not str or not value or len(value) > 500 or '\\' in value
            or value.startswith('/') or ':' in value or len(value.split('/')) > 8
            or any(part in ('', '.', '..') or part.rstrip(' .') != part
                   or PureWindowsPath(part).is_reserved()
                   or any(ord(c) < 32 or c in '<>"|?*' for c in part) for part in value.split('/'))):
        raise RestoreError('Unsafe recovery component path.')
    from .formats import SUFFIXES
    suffixes = '|'.join(re.escape(s) for s in set(SUFFIXES.values()))
    if (value not in _DATABASES
            and not re.fullmatch(rf'import-batches/{_ID}\.json', value)
            and not re.fullmatch(rf'import-requests/(?:reviews/)?{_ID}\.json', value)
            and not re.fullmatch(rf'pending-originals/{_HASH}(?:{suffixes})', value)
            and not re.fullmatch(rf'projects/{_ID}/.+', value)):
        raise RestoreError('Unsupported recovery component path.')
    return value


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Legacy recovery verification cancelled.')


def _walk(root, *, temporary=False, allow_links=False, cancelled=lambda: False):
    _plain(root, directory=True)
    pending, files, visited = [root], {}, 0
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
            _check(cancelled)
            visited += 1
            relative = path.relative_to(root).as_posix()
            if visited > MAX_FILES * 3 or len(relative.split('/')) > 8:
                raise RestoreError('Recovery tree exceeds its entry budget.')
            info = path.lstat()
            directory = stat.S_ISDIR(info.st_mode)
            _plain(path, directory=directory, allow_links=allow_links)
            if temporary and (path.name.endswith(('.lock', '.tmp')) or path.name.startswith('.')):
                continue
            if directory:
                pending.append(path)
            else:
                files[relative] = path
                if len(files) > MAX_FILES:
                    raise RestoreError('Recovery tree exceeds its file budget.')
    return files


def _record(path, relative, *, cancelled=lambda: False):
    _check(cancelled)
    before = _plain(path)
    limit = MAX_DATABASE if relative in _DATABASES else MAX_FILE_BYTES
    if not 0 < before.st_size <= limit:
        raise RestoreError('Recovery file exceeds its size budget.')
    digest, count = sha256(), 0
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise RestoreError('Recovery file changed while opening.')
        while block := stream.read(1024**2):
            _check(cancelled)
            count += len(block)
            if count > before.st_size:
                raise RestoreError('Recovery file grew during verification.')
            digest.update(block)
    if count != before.st_size or _identity(_plain(path)) != _identity(before):
        raise RestoreError('Recovery file changed during verification.')
    return dict(path=relative, size_bytes=count, sha256=digest.hexdigest())


def _json(path):
    before = _plain(path)
    if not 0 < before.st_size <= MAX_JSON:
        raise RestoreError('Recovery JSON exceeds its size budget.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) != before.st_size or _identity(_plain(path)) != _identity(before):
        raise RestoreError('Recovery JSON changed while reading.')
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(RestoreError('Nonfinite recovery value.')))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RestoreError('Invalid recovery JSON.') from exc


def _schema_shape(db):
    rows = []
    for kind, name, table, sql in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'):
        # Existing provider databases may have acquired credential_ref through
        # ALTER TABLE. SQLite preserves harmless whitespace from that command.
        normalized = re.sub(r'\s+', '', sql).casefold() if sql is not None else None
        rows.append((kind, name, table, normalized))
    return db.execute('PRAGMA user_version').fetchone()[0], tuple(rows)


def _canonical_schemas():
    from .schemas import LEGACY_SCHEMAS
    return LEGACY_SCHEMAS


def _schema(db, kind):
    db.execute('PRAGMA trusted_schema=OFF')
    if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
        raise RestoreError('Recovery database integrity failure.')
    if _schema_shape(db) != _canonical_schemas()[kind]:
        raise RestoreError('Unsupported recovery database schema.')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RestoreError('Recovery database has invalid references.')


def _text(value, *, maximum=MAX_JSON, empty=False):
    return type(value) is str and (empty or bool(value)) and len(value) <= maximum and '\0' not in value


def _providers(path, *, restoring=False, cancelled=lambda: False):
    from .profiles import ProviderProfile
    with closing(sqlite3.connect(path.as_uri() + ('?mode=rw' if restoring else '?mode=ro&immutable=1'), uri=True)) as db:
        db.set_progress_handler(lambda: int(cancelled()), 10_000)
        _schema(db, 'providers')
        if db.execute('SELECT id,mode FROM policy').fetchall() not in ([(1, 'LOCAL_ONLY')], [(1, 'HYBRID')]):
            raise RestoreError('Invalid provider policy snapshot.')
        for identity, raw, spent, reference in db.execute('SELECT id,document,spent_cents,credential_ref FROM profiles'):
            _check(cancelled)
            if not _text(raw) or type(spent) is not int or spent < 0:
                raise RestoreError('Invalid provider budget snapshot.')
            profile = ProviderProfile(**json.loads(raw, object_pairs_hook=_unique_object))
            if profile.id != identity or (reference is not None and (not isinstance(reference, str)
                    or not re.fullmatch(rf'{_HASH}/{re.escape(identity)}/{_ID}', reference))):
                raise RestoreError('Invalid provider credential reference.')
            if restoring:
                db.execute('UPDATE profiles SET document=?,credential_ref=NULL WHERE id=?',
                    (json.dumps(asdict(replace(profile, enabled=False)), sort_keys=True, ensure_ascii=False), identity))
        for digest, identity, state, reserved, response, updated in db.execute('SELECT * FROM requests'):
            _check(cancelled)
            if (not isinstance(digest, str) or not re.fullmatch(_HASH, digest)
                    or not isinstance(identity, str) or not re.fullmatch(_PROFILE, identity)
                    or state not in ('reserved', 'complete', 'uncertain')
                    or type(reserved) is not int or reserved < 0
                    or response is not None and (not isinstance(response, str) or not re.fullmatch(_HASH, response))
                    or not isinstance(updated, (int, float)) or not math.isfinite(updated)):
                raise RestoreError('Invalid provider receipt snapshot.')
        if restoring:
            db.execute("UPDATE policy SET mode='LOCAL_ONLY'")
            db.execute("UPDATE requests SET state='uncertain' WHERE state='reserved'")
            db.commit()


def _agent(path, *, restoring=False, cancelled=lambda: False):
    from .schemas import TOOLS
    with closing(sqlite3.connect(path.as_uri() + ('?mode=rw' if restoring else '?mode=ro&immutable=1'), uri=True)) as db:
        db.set_progress_handler(lambda: int(cancelled()), 10_000)
        _schema(db, 'agent')
        for plan, step, tool, state, result, error, updated in db.execute('SELECT * FROM receipts'):
            _check(cancelled)
            if (not isinstance(plan, str) or not re.fullmatch(_HASH, plan) or not _text(step, maximum=80)
                    or tool not in TOOLS or state not in ('running', 'complete', 'needs_review')
                    or not isinstance(updated, (int, float)) or not math.isfinite(updated)
                    or result is not None and not _text(result)
                    or error is not None and not _text(error, empty=True)):
                raise RestoreError('Invalid local-agent receipt snapshot.')
            if result is not None:
                json.loads(result, object_pairs_hook=_unique_object)
        if restoring:
            db.execute("UPDATE receipts SET state='needs_review',error=? WHERE tool NOT IN ('library.list','library.search') OR state!='complete'",
                       ('Restored historical receipt: inspect the prior result and create a newly approved plan before writing.',))
            db.commit()


def _journal(path, workspaces, *, cancelled=lambda: False):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.set_progress_handler(lambda: int(cancelled()), 10_000)
        _schema(db, 'imports')
        assets, jobs = set(), {}
        for identity, workspace, edition, asset, config, count, active, state, error in db.execute('SELECT * FROM job'):
            _check(cancelled)
            if (not isinstance(identity, str) or not re.fullmatch(_HASH, identity)
                    or workspace not in workspaces or not _text(edition, maximum=200)
                    or not isinstance(asset, str) or not re.fullmatch(_HASH, asset)
                    or not _text(config) or type(count) is not int or not 1 <= count <= 1000
                    or active is not None and not _text(active, maximum=200)
                    or state not in _JOURNAL_STATES
                    or error is not None and not _text(error, empty=True)):
                raise RestoreError('Invalid or out-of-scope import journal.')
            settings = json.loads(config, object_pairs_hook=_unique_object)
            if type(settings) is not dict or settings.get('kind') not in ('physical_page', 'logical_segment'):
                raise RestoreError('Invalid import journal configuration.')
            jobs[identity] = (asset, count, settings['kind'])
            assets.add(asset)
            if len(jobs) > MAX_FILES:
                raise RestoreError('Import journal exceeds its job budget.')
        ordinals = {}
        for job, ordinal, payload, digest in db.execute('SELECT * FROM checkpoint ORDER BY job,ordinal'):
            _check(cancelled)
            if job not in jobs or type(ordinal) is not int or ordinal != ordinals.get(job, 0) + 1:
                raise RestoreError('Invalid import checkpoint sequence.')
            asset, count, kind = jobs[job]
            if (ordinal > count or not _text(payload) or sha256(payload.encode('utf-8')).hexdigest() != digest):
                raise RestoreError('Import checkpoint digest mismatch.')
            value = json.loads(payload, object_pairs_hook=_unique_object)
            if (type(value) is not dict or value.get('physical_index') != (ordinal if kind == 'physical_page' else None)
                    or type(value.get('provenance')) is not dict or value['provenance'].get('asset_sha256') != asset
                    or value.get('printed_label') is not None or value.get('printed_label_verified') is not False
                    or not _text(value.get('raw_text'), empty=True)):
                raise RestoreError('Import checkpoint source identity mismatch.')
            ordinals[job] = ordinal
        return assets


def _import_json(relative, value, source_root, workspaces):
    from .ocr_config import Config
    from ..ingest.worker import FIELDS, MAX_REQUEST, MAX_REVIEWS
    if relative.startswith('import-batches/'):
        from .batches import validate_batch
        validate_batch(value, PurePosixPath(relative).stem, source_root)
        if value['workspace'] not in workspaces:
            raise RestoreError('Import batch is outside the saved library scope.')
    elif relative.startswith('import-requests/reviews/'):
        if type(value) is not dict or len(value) > 1000 or len(json.dumps(value).encode('utf-8')) > MAX_REVIEWS:
            raise RestoreError('Invalid import review receipt.')
    else:
        if (type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1
                or value.get('request_id') != PurePosixPath(relative).stem or value.get('data_dir') != source_root
                or value.get('workspace') not in workspaces or not _local_absolute(value.get('source'))
                or type(value.get('metadata')) is not dict or set(value['metadata']) != FIELDS
                or type(value.get('config')) is not dict or len(json.dumps(value).encode('utf-8')) > MAX_REQUEST
                or value.get('state') not in _REQUEST_STATES):
            raise RestoreError('Invalid import request identity or scope.')
        Config(**value['config'])
        if value.get('asset_sha256') is not None and (not isinstance(value['asset_sha256'], str)
                or not re.fullmatch(_HASH, value['asset_sha256'])):
            raise RestoreError('Invalid retained import identity.')


def validate_manifest(value):
    fields = {'version', 'source_root', 'files', 'projects'}
    if (type(value) is not dict or set(value) != fields or type(value['version']) is not int
            or value['version'] != 1 or not _local_absolute(value['source_root'])
            or type(value['files']) is not list or not 1 <= len(value['files']) <= MAX_FILES
            or type(value['projects']) is not list or len(value['projects']) > 1000):
        raise RestoreError('Invalid recovery manifest.')
    names, total = set(), 0
    for row in value['files']:
        if (type(row) is not dict or set(row) != {'path', 'size_bytes', 'sha256'}
                or type(row['size_bytes']) is not int or not 0 < row['size_bytes'] <= MAX_FILE_BYTES
                or not isinstance(row['sha256'], str) or not re.fullmatch(_HASH, row['sha256'])):
            raise RestoreError('Invalid recovery file record.')
        _path(row['path'])
        if row['path'] in names:
            raise RestoreError('Duplicate recovery path.')
        names.add(row['path'])
        total += row['size_bytes']
        if total > MAX_BYTES:
            raise RestoreError('Recovery files exceed their total byte budget.')
    projects = set()
    for row in value['projects']:
        if (type(row) is not dict or set(row) != {'id', 'manifest_sha256'}
                or not isinstance(row['id'], str) or not re.fullmatch(_ID, row['id']) or row['id'] in projects
                or not isinstance(row['manifest_sha256'], str) or not re.fullmatch(_HASH, row['manifest_sha256'])):
            raise RestoreError('Invalid studio recovery manifest.')
        projects.add(row['id'])
    if {name.split('/')[1] for name in names if name.startswith('projects/')} != projects:
        raise RestoreError('Studio recovery inventory differs from its manifest.')
    if [row['path'] for row in value['files']] != sorted(names):
        raise RestoreError('Recovery paths must be ordered.')
    return value


def _verify(root, manifest, workspaces, registered_assets, *, cancelled=lambda: False):
    validate_manifest(manifest)
    files = _walk(root, cancelled=cancelled)
    if set(files) != {row['path'] for row in manifest['files']}:
        raise RestoreError('Recovery file inventory mismatch.')
    pending, available, retained = set(), set(registered_assets), set()
    for row in manifest['files']:
        path, relative = files[row['path']], row['path']
        if _record(path, relative, cancelled=cancelled) != row:
            raise RestoreError('Recovery file digest mismatch.')
        if relative == 'provider-settings/providers.sqlite':
            _providers(path, cancelled=cancelled)
        elif relative == 'agent-jobs/agent.sqlite':
            _agent(path, cancelled=cancelled)
        elif relative == 'imports.sqlite':
            pending.update(_journal(path, workspaces, cancelled=cancelled))
        elif relative.startswith(('import-requests/', 'import-batches/')):
            value = _json(path)
            _import_json(relative, value, manifest['source_root'], workspaces)
            if relative.startswith('import-requests/') and '/reviews/' not in relative and value.get('asset_sha256'):
                pending.add(value['asset_sha256'])
        elif relative.startswith('pending-originals/'):
            if path.stem != row['sha256'] or path.stem in available:
                raise RestoreError('Pending original identity duplicates or differs from its bytes.')
            available.add(path.stem)
            retained.add(path.stem)
    if pending - available:
        raise RestoreError('A pending import original is missing from the backup.')
    if retained != pending - set(registered_assets):
        raise RestoreError('A retained recovery original has no pending import reference.')
    if any('import-requests/' + Path(name).name not in files for name in files
           if name.startswith('import-requests/reviews/')):
        raise RestoreError('Import review has no corresponding request.')
    return files
