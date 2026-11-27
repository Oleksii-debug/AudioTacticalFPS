"""Atomic JSON checkpoints; OS locks release on crash and are not backup data."""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from filelock import FileLock, Timeout
from .models import canonical, local_path
from .validation import decode_state_json, validate_job, validate_control
from ..ingest.writer_guard import exclusive_writer

MAX_JOBS = 1000


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
    if len(payload) > 64 * 1024 * 1024:
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


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_nlink, getattr(info, 'st_file_attributes', 0))


def read_bytes(path, *, maximum=64 * 1024 * 1024):
    """Read one bounded private checkpoint, rejecting replacement during the read."""
    path = Path(path)
    local_path(path.parent)
    info = regular(path)
    if not 0 < info.st_size <= maximum:
        raise ValueError('Invalid assistant checkpoint size.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(info):
            raise ValueError('Assistant checkpoint changed while opening.')
        raw = stream.read(maximum + 1)
        opened = os.fstat(stream.fileno())
    if (len(raw) != info.st_size or _identity(opened) != _identity(info)
            or _identity(regular(path)) != _identity(info)):
        raise ValueError('Assistant checkpoint changed while reading.')
    return raw


def read_json(path, *, maximum=64 * 1024 * 1024):
    return decode_state_json(read_bytes(path, maximum=maximum))


class JobStore:
    def __init__(self, directory, *, writer_root=None):
        self.directory = local_path(directory, must_exist=False)
        self.writer_root = local_path(writer_root) if writer_root is not None else self.directory
        if not self.directory.is_relative_to(self.writer_root):
            raise ValueError('Assistant state must remain inside its writer root.')
        if writer_root is None:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with exclusive_writer(self.writer_root):
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
        # Only publication uses the application guard. Holding it while running
        # tools would deadlock the assistant's own complete-library backup step.
        validate_job(value)
        with exclusive_writer(self.writer_root):
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
            with exclusive_writer(self.writer_root):
                # A control is never allowed to outlive a missing/invalid job.
                self.load(identity)
                atomic_json(path, {'command': command})
        if not path.exists():
            return 'run'
        value = read_json(path, maximum=1024)
        return validate_control(value)['command']

    def identities(self):
        identities = []
        with os.scandir(self.directory) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_JOBS * 4 + 16:
                    raise ValueError('Assistant checkpoint directory exceeds its entry budget.')
                match = re.fullmatch(r'([0-9a-f]{32})\.json', entry.name)
                if match:
                    identities.append(match[1])
                    if len(identities) > MAX_JOBS:
                        raise ValueError('Too many saved assistant jobs; review the checkpoint directory.')
        return tuple(sorted(identities))
