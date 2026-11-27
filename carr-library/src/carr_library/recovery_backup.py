"""Typed recovery components for version-four full library backups.

The library backup owns its normal writer lock and final publication marker.
This adapter reuses the studio backup contract, snapshots auxiliary SQLite
databases online, and preserves import receipts with their retained originals.
It never reads the OS credential vault or follows arbitrary directory trees.
"""
from contextlib import closing, ExitStack
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
from filelock import FileLock, Timeout

from .ingest.bundle import _unique_object
from .ingest.restore import RestoreError


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
# Complete state vocabulary in Ingest.start/_set/_recover and worker._invoke.
# "reviewed" belongs to a review action reply, not the persisted import job.
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
    return (type(value) is str and 0 < len(value) <= 32768 and '\0' not in value
            and '://' not in value and (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()))


def _path(value):
    if (type(value) is not str or not value or len(value) > 500 or '\\' in value
            or value.startswith('/') or ':' in value or len(value.split('/')) > 8
            or any(part in ('', '.', '..') or part.rstrip(' .') != part
                   or PureWindowsPath(part).is_reserved()
                   or any(ord(c) < 32 or c in '<>"|?*' for c in part) for part in value.split('/'))):
        raise RestoreError('Unsafe recovery component path.')
    from .ingest.retained_assets import SUFFIXES
    suffixes = '|'.join(re.escape(s) for s in set(SUFFIXES.values()))
    if (value not in _DATABASES
            and not re.fullmatch(rf'import-batches/{_ID}\.json', value)
            and not re.fullmatch(rf'import-requests/(?:reviews/)?{_ID}\.json', value)
            and not re.fullmatch(rf'pending-originals/{_HASH}(?:{suffixes})', value)
            and not re.fullmatch(rf'projects/{_ID}/.+', value)):
        raise RestoreError('Unsupported recovery component path.')
    return value


def _walk(root, *, temporary=False, allow_links=False):
    _plain(root, directory=True)
    pending, files, visited = [root], {}, 0
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
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


def _record(path, relative):
    before = _plain(path)
    limit = MAX_DATABASE if relative in _DATABASES else MAX_FILE_BYTES
    if not 0 < before.st_size <= limit:
        raise RestoreError('Recovery file exceeds its size budget.')
    digest, count = sha256(), 0
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise RestoreError('Recovery file changed while opening.')
        while block := stream.read(1024**2):
            count += len(block)
            if count > before.st_size:
                raise RestoreError('Recovery file grew during verification.')
            digest.update(block)
    if count != before.st_size or _identity(_plain(path)) != _identity(before):
        raise RestoreError('Recovery file changed during verification.')
    return dict(path=relative, size_bytes=count, sha256=digest.hexdigest())


def _copy(source, target, *, expected=None):
    before = _plain(source)
    if not 0 < before.st_size <= MAX_FILE_BYTES:
        raise RestoreError('Recovery file exceeds its size budget.')
    if expected is not None and before.st_size != expected['size_bytes']:
        raise RestoreError('Recovery file size mismatch.')
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest, count = sha256(), 0
    with source.open('rb') as incoming, target.open('xb') as outgoing:
        if _identity(os.fstat(incoming.fileno())) != _identity(before):
            raise RestoreError('Recovery file changed while opening.')
        while block := incoming.read(1024**2):
            count += len(block)
            if count > before.st_size:
                raise RestoreError('Recovery file grew during snapshot.')
            digest.update(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if (count != before.st_size or _identity(_plain(source)) != _identity(before)
            or expected is not None and digest.hexdigest() != expected['sha256']):
        raise RestoreError('Recovery file changed or its digest does not match.')


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


def _rewrite_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > MAX_JSON:
        raise RestoreError('Recovery JSON exceeds its size budget.')
    with path.open('wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _snapshot_database(source, target):
    if not 0 < _plain(source).st_size <= MAX_DATABASE:
        raise RestoreError('Recovery database exceeds its size budget.')
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as incoming:
        incoming.execute('PRAGMA trusted_schema=OFF')
        page_size = incoming.execute('PRAGMA page_size').fetchone()[0]
        def progress(status, remaining, total):
            if total * page_size > MAX_DATABASE:
                raise RestoreError('Recovery database exceeds its size budget.')
        with closing(sqlite3.connect(target)) as outgoing:
            incoming.backup(outgoing, pages=128, progress=progress, sleep=.01)
            # A sealed snapshot is a standalone file, even when its live
            # source uses WAL. Never let verification create unsealed WAL/SHM.
            outgoing.execute('PRAGMA journal_mode=DELETE')
    if not 0 < _plain(target).st_size <= MAX_DATABASE:
        raise RestoreError('Recovery database exceeds its size budget.')
    with target.open('r+b') as stream:
        os.fsync(stream.fileno())


def _schema_shape(db):
    rows = []
    for kind, name, table, sql in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'):
        # Existing provider databases may have acquired credential_ref through
        # ALTER TABLE. SQLite preserves harmless whitespace from that command.
        normalized = re.sub(r'\s+', '', sql).casefold() if sql is not None else None
        rows.append((kind, name, table, normalized))
    return db.execute('PRAGMA user_version').fetchone()[0], tuple(rows)


@lru_cache(maxsize=1)
def _canonical_schemas():
    """Use the current owners' actual schema builders, including constraints."""
    from .providers import ProviderManager
    from .agent.runtime import LocalAgent
    from .ingest.pipeline import Ingest
    with TemporaryDirectory(prefix='carr-recovery-schema-') as temporary:
        root = Path(temporary)
        providers = ProviderManager(root / 'providers', vault=object())
        agent = LocalAgent(None, root / 'agent')
        journal = root / 'imports.sqlite'
        Ingest(journal, None).close()
        paths = dict(providers=providers.database, agent=agent.database, imports=journal)
        shapes = {}
        for kind, path in paths.items():
            with closing(sqlite3.connect(path)) as db:
                shapes[kind] = _schema_shape(db)
        return shapes


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


def _providers(path, *, restoring=False):
    from .providers import ProviderProfile
    with closing(sqlite3.connect(path.as_uri() + ('?mode=rw' if restoring else '?mode=ro&immutable=1'), uri=True)) as db:
        _schema(db, 'providers')
        if db.execute('SELECT id,mode FROM policy').fetchall() not in ([(1, 'LOCAL_ONLY')], [(1, 'HYBRID')]):
            raise RestoreError('Invalid provider policy snapshot.')
        for identity, raw, spent, reference in db.execute('SELECT id,document,spent_cents,credential_ref FROM profiles'):
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


def _agent(path, *, restoring=False):
    from .agent.runtime import TOOLS
    with closing(sqlite3.connect(path.as_uri() + ('?mode=rw' if restoring else '?mode=ro&immutable=1'), uri=True)) as db:
        _schema(db, 'agent')
        for plan, step, tool, state, result, error, updated in db.execute('SELECT * FROM receipts'):
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


def _journal(path, workspaces):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        _schema(db, 'imports')
        assets, jobs = set(), {}
        for identity, workspace, edition, asset, config, count, active, state, error in db.execute('SELECT * FROM job'):
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
    from .extract.page_ocr import Config
    from .ingest.worker import FIELDS, MAX_REQUEST, MAX_REVIEWS
    if relative.startswith('import-batches/'):
        from .ingest.batches import ImportBatchStore
        # The current queue engine requires native absolute external paths.
        # Do not publish a seemingly usable queue by silently reinterpreting a
        # Windows path as a POSIX relative filename, or vice versa.
        for row in value.get('rows', []) if type(value) is dict else []:
            if type(row) is dict and not Path(str(row.get('source', ''))).is_absolute():
                raise RestoreError('Import-batch source paths require a compatible operating system; no restored folder was published.')
        ImportBatchStore(SimpleNamespace(root=source_root))._validate(value, PurePosixPath(relative).stem)
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


def _verify(root, manifest, workspaces, registered_assets):
    validate_manifest(manifest)
    files = _walk(root)
    if set(files) != {row['path'] for row in manifest['files']}:
        raise RestoreError('Recovery file inventory mismatch.')
    pending, available, retained = set(), set(registered_assets), set()
    for row in manifest['files']:
        path, relative = files[row['path']], row['path']
        if _record(path, relative) != row:
            raise RestoreError('Recovery file digest mismatch.')
        if relative == 'provider-settings/providers.sqlite':
            _providers(path)
        elif relative == 'agent-jobs/agent.sqlite':
            _agent(path)
        elif relative == 'imports.sqlite':
            pending.update(_journal(path, workspaces))
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


def snapshot(app, stage, workspaces, registered_assets, *, include_independent=True):
    """Coordinate import receipts while their library writer lock is held."""
    locks = set()
    for name in ('import-requests', 'import-batches'):
        folder = app.root / name
        if os.path.lexists(folder):
            for relative, path in _walk(folder, temporary=True).items():
                if path.suffix == '.json':
                    if not re.fullmatch(_ID, path.stem):
                        raise RestoreError('Unsupported import recovery identity.')
                    locks.add(folder / (path.stem + '.lock'))
    with ExitStack() as stack:
        for path in sorted(locks):
            if os.path.lexists(path):
                _plain(path)
            try:
                stack.enter_context(FileLock(path, mode=0o600).acquire(timeout=0))
            except Timeout as exc:
                raise RestoreError('An import receipt is being written; retry the backup after it checkpoints.') from exc
        return _snapshot(app, stage, workspaces, registered_assets, include_independent=include_independent)


def _snapshot(app, stage, workspaces, registered_assets, *, include_independent=True):
    """Seal accepted components; scoped agent backups omit independent state."""
    root, target = app.root, stage / DIRECTORY
    selected = []
    for name in ('import-requests', 'import-batches'):
        directory = root / name
        if os.path.lexists(directory):
            selected.extend((name + '/' + relative, path) for relative, path in _walk(directory, temporary=True).items())
    if selected and os.path.lexists(root / 'imports.sqlite'):
        selected.append(('imports.sqlite', root / 'imports.sqlite'))
    if include_independent:
        for relative in ('provider-settings/providers.sqlite', 'agent-jobs/agent.sqlite'):
            path = root / relative
            if os.path.lexists(path.parent):
                _plain(path.parent, directory=True)
            if os.path.lexists(path):
                selected.append((relative, path))
    projects = []
    audio = root / 'audio-projects'
    if include_independent and os.path.lexists(audio):
        _plain(audio, directory=True)
        for path in sorted(audio.iterdir()):
            _plain(path, directory=True)
            if not re.fullmatch(_ID, path.name):
                raise RestoreError('Unsupported private audio project directory.')
            projects.append(path)
            if len(projects) > 1000:
                raise RestoreError('Too many audio projects for this backup.')
    if not selected and not projects:
        return None
    planned = list(selected)
    for project in projects:
        planned.extend((f'projects/{project.name}/{relative}', path)
                       for relative, path in _walk(project, temporary=True, allow_links=True).items())
    # The studio's immutable master may hard-link an accepted cache chunk.
    # Its own snapshotter authenticates those bytes and creates independent
    # snapshot files; this preflight counts each logical file conservatively.
    planned_bytes = sum(_plain(path, allow_links=relative.startswith('projects/')).st_size
                        for relative, path in planned)
    if len(planned) > MAX_FILES or planned_bytes > MAX_BYTES:
        raise RestoreError('Recovery snapshot exceeds its file or total byte budget.')
    target.mkdir(mode=0o700)
    for relative, source in sorted(selected):
        _path(relative)
        if relative in _DATABASES:
            _snapshot_database(source, target / relative)
        else:
            _copy(source, target / relative)
    pending = set()
    for relative, _ in selected:
        if relative == 'imports.sqlite':
            pending.update(_journal(target / relative, workspaces))
        elif relative.startswith(('import-requests/', 'import-batches/')):
            value = _json(target / relative)
            _import_json(relative, value, str(root), workspaces)
            if relative.startswith('import-requests/') and '/reviews/' not in relative and value.get('asset_sha256'):
                pending.add(value['asset_sha256'])
    for digest in sorted(pending - set(registered_assets)):
        source = app.assets.resolve(digest)
        planned_bytes += _plain(source).st_size
        if planned_bytes > MAX_BYTES:
            raise RestoreError('Recovery snapshot exceeds its total byte budget.')
        _copy(source, target / 'pending-originals' / source.name)
    manifests = []
    if projects:
        from .studio.store import ProjectStore
        from .studio.backup import backup_project
        (target / 'projects').mkdir(mode=0o700)
        for path in projects:
            _plain(path / 'studio.sqlite')
            # A read-only handle avoids constructor migrations or directory
            # creation in the source; backup_project consumes root/database.
            store = ProjectStore.__new__(ProjectStore)
            store.root, store.database = path, path / 'studio.sqlite'
            digest = backup_project(store, target / 'projects' / path.name)
            manifests.append(dict(id=path.name, manifest_sha256=digest))
    records = [_record(path, relative) for relative, path in sorted(_walk(target).items())]
    manifest = dict(version=1, source_root=str(root), files=records, projects=manifests)
    _verify(target, manifest, workspaces, registered_assets)
    return manifest


def prepare_restore(source, stage, manifest, destination, workspaces, registered_assets):
    """Validate the complete recovery stage before the outer root is published."""
    _verify(source, manifest, workspaces, registered_assets)
    target = stage / DIRECTORY
    target.mkdir(mode=0o700)
    for row in manifest['files']:
        if row['path'].startswith('projects/'):
            continue
        _copy(source / row['path'], target / row['path'], expected=row)
    for relative, path in _walk(target).items():
        if relative == 'provider-settings/providers.sqlite':
            _providers(path, restoring=True)
        elif relative == 'agent-jobs/agent.sqlite':
            _agent(path, restoring=True)
        elif relative.startswith(('import-requests/', 'import-batches/')) and '/reviews/' not in relative:
            value = _json(path)
            value['data_dir'] = str(destination)
            _rewrite_json(path, value)
            _import_json(relative, value, str(destination), workspaces)
    if manifest['projects']:
        from .studio import backup as studio_backup
        (target / 'audio-projects').mkdir(mode=0o700)
        canonical = studio_backup._canonical_schema(target)
        for project in manifest['projects']:
            temporary_root = target / 'audio-projects' / project['id']
            store = studio_backup.restore_project(source / 'projects' / project['id'], temporary_root,
                expected_manifest_sha256=project['manifest_sha256'])
            records = studio_backup._records(store.database, canonical, None)
            studio_backup._prepare_restore(store.database, destination / 'audio-projects' / project['id'], records,
                {'include_audio': True, 'original_root': str(temporary_root)}, None)
    return target


def publish(stage, destination, manifest, *, backup):
    source = stage / DIRECTORY
    base = destination / DIRECTORY if backup else destination
    files = _walk(source)
    if backup:
        base.mkdir(mode=0o700)
    for relative, path in sorted(files.items()):
        if not backup and relative.startswith('pending-originals/'):
            relative = 'originals/' + Path(relative).name
        target = base / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.link(path, target)
    if os.name != 'nt':
        directories = {base}
        for relative in files:
            relative = 'originals/' + Path(relative).name if not backup and relative.startswith('pending-originals/') else relative
            parent = (base / relative).parent
            while parent.is_relative_to(base):
                directories.add(parent)
                if parent == base:
                    break
                parent = parent.parent
        for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
