"""Crash-recoverable project revisions and verified local chunk cache."""
from contextlib import contextmanager
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import tempfile
import time
import uuid

from filelock import FileLock
from .models import Project, PlanError, unique_object
from ..audio.ffmpeg import _local_path


def digest_file(path):
    digest = sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _chunk_key(key):
    if type(key) is not str or re.fullmatch(r'[0-9a-f]{64}', key) is None:
        raise PlanError('A chunk cache key must be a SHA256 digest.')
    return key


def _verify_report(path, report, digest):
    """A byte checksum must also agree with the actual rendered PCM timeline."""
    if type(report) is not dict or report.get('sha256') != digest:
        raise PlanError('Chunk report checksum does not match its verified bytes.')
    if 'size_bytes' in report:
        if type(report['size_bytes']) is not int or report['size_bytes'] != path.stat().st_size:
            raise PlanError('Chunk report size does not match its verified bytes.')
    media_fields = {'frames', 'sample_rate', 'channels', 'duration_seconds'}
    if media_fields.intersection(report):
        if not media_fields <= report.keys():
            raise PlanError('Rendered PCM metadata is incomplete.')
        from ..audio.ffmpeg import _pcm_header
        try:
            rate, channels, frames, _, _ = _pcm_header(path)
        except (ValueError, OSError, struct.error) as exc:
            raise PlanError('Rendered PCM metadata could not be verified.') from exc
        actual = {'frames': frames, 'sample_rate': rate, 'channels': channels}
        if any(type(report[key]) is not int or report[key] != value for key, value in actual.items()):
            raise PlanError('Rendered PCM metadata differs from the actual recording.')
        duration = report['duration_seconds']
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or not math.isclose(duration, frames / rate, rel_tol=1e-12, abs_tol=1e-9)):
            raise PlanError('Rendered PCM duration differs from the actual recording.')
    elif 'size_bytes' not in report:
        raise PlanError('A prepared input snapshot requires its verified byte size.')
    return report


def atomic_text(path, text, *, replace=False):
    """Fsync before publication; new user output is never silently overwritten."""
    path = _local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.studio-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(text.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)


class ProjectStore:
    def __init__(self, root):
        self.root = _local_path(root).absolute()
        if os.path.lexists(self.root / '.restore-incomplete'):
            raise PlanError('Studio project restore is incomplete; choose a fresh destination.')
        if self.root.is_symlink():
            raise PlanError('A studio project folder must not be a symbolic link.')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = self.root.resolve()
        self.cache = self.root / 'cache'
        self.revisions = self.root / 'revisions'
        self.renders = self.root / 'renders'
        for directory in (self.cache, self.revisions, self.renders):
            if directory.is_symlink():
                raise PlanError('Project subfolders must not be symbolic links.')
            directory.mkdir(exist_ok=True, mode=0o700)
        self.database = self.root / 'studio.sqlite'
        if self.database.is_symlink():
            raise PlanError('Project database must not be a symbolic link.')
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise PlanError('Unsupported studio database version.')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects(revision TEXT PRIMARY KEY, document TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS current_project(id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL REFERENCES projects);
                CREATE TABLE IF NOT EXISTS chunks(cache_key TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, report TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, revision TEXT NOT NULL, state TEXT NOT NULL,
                    progress TEXT NOT NULL, result TEXT, error TEXT, updated REAL NOT NULL);
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def render_lock(self):
        return FileLock(str(self.root / 'render.lock'), timeout=0)

    def save(self, project, *, expected_revision=None):
        if not isinstance(project, Project):
            raise PlanError('Expected a validated narration project.')
        raw, revision = project.canonical_json(), project.revision
        with FileLock(str(self.root / 'save.lock'), timeout=10):
            with self.connect() as db:
                current = db.execute('SELECT revision FROM current_project WHERE id=1').fetchone()
                if expected_revision is not None and (current is None or current[0] != expected_revision):
                    raise PlanError('Project changed in another window. Reload before saving.')
                target = self.revisions / (revision + '.json')
                if target.exists():
                    if target.read_text(encoding='utf-8') != raw + '\n':
                        raise PlanError('Saved project revision failed its integrity check.')
                else:
                    atomic_text(target, raw + '\n')
                db.execute('INSERT OR IGNORE INTO projects VALUES(?,?,?)', (revision, raw, time.time()))
                db.execute('INSERT INTO current_project VALUES(1,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision', (revision,))
        return revision

    def load(self, revision=None):
        with self.connect() as db:
            if revision is None:
                row = db.execute('SELECT document,projects.revision FROM projects JOIN current_project USING(revision) WHERE id=1').fetchone()
            else:
                row = db.execute('SELECT document,revision FROM projects WHERE revision=?', (revision,)).fetchone()
        if row is None:
            raise PlanError('No saved project revision was found.')
        project = Project.from_dict(json.loads(row[0]))
        if project.revision != row[1]:
            raise PlanError('Project database revision failed its integrity check.')
        return project

    def cached(self, key):
        _chunk_key(key)
        with self.connect() as db:
            row = db.execute('SELECT path,sha256,report FROM chunks WHERE cache_key=?', (key,)).fetchone()
        if row is None:
            return None
        try:
            path = self.root / row[0]
            if (path.is_symlink() or self.cache.is_symlink()
                    or not path.resolve().is_relative_to(self.cache) or not path.is_file()):
                return None
            digest = digest_file(path)
            if digest != row[1]:
                return None
            report = _verify_report(path, json.loads(row[2], object_pairs_hook=unique_object), digest)
        except (ValueError, TypeError, OSError, RecursionError):
            # A damaged checkpoint is not reusable evidence. Preserve it and
            # let the director publish a fresh uniquely named verified chunk.
            return None
        report['path'] = str(path)
        return report

    def accept_chunk(self, key, path, report):
        _chunk_key(key)
        path = Path(path).absolute()
        if path.is_symlink() or self.cache.is_symlink():
            raise PlanError('Generated chunks must be inside this studio project, without symbolic links.')
        path = path.resolve(strict=True)
        if not path.is_relative_to(self.cache) or not path.is_file():
            raise PlanError('Generated chunks must be inside this studio project cache.')
        actual = digest_file(path)
        _verify_report(path, report, actual)
        payload = json.dumps(report, sort_keys=True, allow_nan=False, default=str)
        with self.connect() as db:
            db.execute('INSERT INTO chunks VALUES(?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,report=excluded.report',
                       (key, str(path.relative_to(self.root)), actual, payload))

    def new_chunk_path(self, key):
        # A corrupt or interrupted old file is retained; never overwrite it.
        _chunk_key(key)
        if self.cache.is_symlink() or not self.cache.resolve().is_relative_to(self.root):
            raise PlanError('Generated chunks must remain inside this studio project.')
        return self.cache / f'{key}-{uuid.uuid4().hex[:12]}.wav'

    def start_job(self, revision):
        identity = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='interrupted',error='Process ended before completion',updated=? WHERE state='running'", (time.time(),))
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', (identity, revision, 'running', '{}', None, None, time.time()))
        return identity

    def checkpoint(self, identity, *, state='running', progress=None, result=None, error=None):
        if state not in ('running', 'paused', 'cancelled', 'failed', 'complete', 'interrupted'):
            raise PlanError('Invalid render job state.')
        with self.connect() as db:
            if db.execute('UPDATE jobs SET state=?,progress=?,result=?,error=?,updated=? WHERE id=?',
                (state, json.dumps(progress or {}, default=str), json.dumps(result, default=str) if result is not None else None,
                 error, time.time(), identity)).rowcount != 1:
                raise PlanError('Unknown render job.')

    def jobs(self):
        with self.connect() as db:
            rows = db.execute('SELECT id,revision,state,progress,result,error,updated FROM jobs ORDER BY updated DESC').fetchall()
        return [dict(id=r[0], revision=r[1], state=r[2], progress=json.loads(r[3]),
                     result=json.loads(r[4]) if r[4] else None, error=r[5], updated=r[6]) for r in rows]
