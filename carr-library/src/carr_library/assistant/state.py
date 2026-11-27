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


MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024


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
    if len(payload) > MAX_CHECKPOINT_BYTES:
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


def read_json(path, *, maximum=MAX_CHECKPOINT_BYTES):
    path = Path(path)
    local_path(path.parent)
    info = regular(path)
    if not 0 < info.st_size <= maximum:
        raise ValueError('Invalid assistant checkpoint size.')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('Assistant checkpoint exceeds its budget.')
    return json.loads(raw)


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
        return value

    def save(self, value):
        value['updated'] = now()
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
        if type(value) is not dict or value.get('command') not in ('run', 'pause', 'cancel'):
            raise ValueError('Invalid assistant control checkpoint.')
        return value['command']

    def identities(self):
        return tuple(sorted(p.stem for p in self.directory.glob('*.json')
                            if re.fullmatch('[0-9a-f]{32}', p.stem)))
