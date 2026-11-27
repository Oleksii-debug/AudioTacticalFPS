"""Atomic JSON checkpoints; OS locks release on crash and are not backup data."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from filelock import FileLock, Timeout
from .models import canonical, local_path
from .validation import validate_job, validate_control


MAX_JOB_BYTES = 64 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def regular(path, *, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Assistant state must use private regular files.')
    return info


def atomic_json(path, value):
    path = Path(path)
    local_path(path.parent)
    regular(path, missing=True)
    payload = (canonical(value) + '\n').encode('utf-8')
    if len(payload) > MAX_JOB_BYTES:
        raise ValueError('Assistant checkpoint exceeds the storage budget.')
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        regular(path, missing=True)
        os.replace(temporary, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, *, maximum=MAX_JOB_BYTES):
    path = Path(path)
    local_path(path.parent)
    info = regular(path)
    if not 0 < info.st_size <= maximum:
        raise ValueError('Invalid assistant checkpoint size.')
    def stamp(value):
        return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
                value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        if stamp(os.fstat(stream.fileno())) != stamp(info):
            raise ValueError('Assistant checkpoint changed while opening.')
        raw = stream.read(maximum + 1)
        if stamp(os.fstat(stream.fileno())) != stamp(info):
            raise ValueError('Assistant checkpoint changed while reading.')
    if len(raw) != info.st_size or stamp(regular(path)) != stamp(info):
        raise ValueError('Assistant checkpoint changed while reading.')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate assistant checkpoint field.')
            result[key] = value
        return result
    def finite(_):
        raise ValueError('Non-finite assistant checkpoint number.')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=finite)


class JobStore:
    def __init__(self, directory):
        self.directory = local_path(directory, must_exist=False)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        local_path(self.directory)

    def path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
            raise ValueError('Invalid assistant job identity.')
        return self.directory / (identity + '.json')

    def load(self, identity):
        value = read_json(self.path(identity))
        if type(value) is not dict or value.get('id') != identity or value.get('schema') != 'carr-assistant-job/1':
            raise ValueError('Invalid assistant job checkpoint.')
        return validate_job(value, identity=identity)

    def save(self, value):
        value['updated'] = now()
        validate_job(value, identity=value['id'])
        atomic_json(self.path(value['id']), value)

    @contextmanager
    def lock(self, identity):
        path = self.path(identity).with_suffix('.lock')
        regular(path, missing=True)
        try:
            with FileLock(path, mode=0o600).acquire(timeout=0):
                yield
        except Timeout as exc:
            raise RuntimeError('This assistant job is already running.') from exc

    def control(self, identity, command=None):
        path = self.path(identity).with_suffix('.control.json')
        if command is not None:
            if command not in ('run', 'pause', 'cancel'):
                raise ValueError('Invalid assistant control.')
            atomic_json(path, {'command': command})
        if not path.exists():
            return 'run'
        value = read_json(path, maximum=1024)
        return validate_control(value)['command']

    def identities(self):
        return tuple(sorted(p.stem for p in self.directory.glob('*.json')
                            if re.fullmatch('[0-9a-f]{32}', p.stem)))
