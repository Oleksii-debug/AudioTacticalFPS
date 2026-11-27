"""Atomic JSON checkpoints; OS locks release on crash and are not backup data."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
from threading import get_ident
from uuid import uuid4

from filelock import FileLock, Timeout
from .models import canonical, local_path


@dataclass
class _HeldGuard:
    path: Path
    process: int
    thread: int
    active: bool = True


_held_guards = ContextVar('carr_assistant_native_guards', default=())


@contextmanager
def _native_guard(path, *, snapshot=False):
    """A snapshot may borrow only this process/thread's actual live lock."""
    path = local_path(path, must_exist=False)
    regular(path, missing=True)
    current = _held_guards.get()
    if snapshot and any(held.active and held.path == path
                        and held.process == os.getpid()
                        and held.thread == get_ident() for held in current):
        yield
        return
    try:
        acquired = FileLock(path, mode=0o600).acquire(timeout=0)
    except Timeout as exc:
        raise RuntimeError('This assistant job is already running or its state is being saved. Retry after the active operation stops.') from exc
    with acquired:
        held = _HeldGuard(path, os.getpid(), get_ident())
        token = _held_guards.set((*current, held))
        try:
            yield
        finally:
            held.active = False
            _held_guards.reset(token)


def _job_guard(directory, identity, *, snapshot=False):
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError('Invalid assistant job identity.')
    return _native_guard(local_path(directory) / (identity + '.lock'), snapshot=snapshot)


def snapshot_job_guard(directory, identity):
    """Snapshot one job, borrowing a live same-thread execution lock if held."""
    return _job_guard(directory, identity, snapshot=True)


def state_write_guard(directory):
    """Short publication guard shared by job saves, controls and backups."""
    return _native_guard(local_path(directory) / '.state.lock')


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


def decode_state_json(raw):
    """Reject ambiguous or non-finite JSON before interpreting checkpoint fields."""
    if not isinstance(raw, (str, bytes)) or len(raw) > 64 * 1024 * 1024:
        raise ValueError('Assistant backup sidecar exceeds its byte budget.')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate assistant checkpoint field.')
            result[key] = value
        return result

    def finite(value):
        raise ValueError('Non-finite number in assistant checkpoint.')

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            finite(value)
        return parsed

    try:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        return json.loads(raw, object_pairs_hook=unique, parse_constant=finite, parse_float=number)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError('Invalid assistant checkpoint JSON.') from exc


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
