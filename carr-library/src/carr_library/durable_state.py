"""Atomic, bounded private JSON state. Original source databases are not edited."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import tempfile
import time

MAX_JSON_BYTES = 32 * 1024 * 1024


class UnlinkedRegularFile(ValueError):
    """A stat snapshot of an ordinary file concurrently unlinked."""
    def __init__(self, path):
        self.path = Path(path)
        super().__init__('State must use regular local files, without links.')


def regular(path, *, directory=False):
    path = Path(path)
    value = path.lstat()
    if (stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))):
        raise ValueError('State must use regular local files, without links.')
    if not directory and value.st_nlink != 1:
        if value.st_nlink == 0:
            raise UnlinkedRegularFile(path)
        raise ValueError('State must use regular local files, without links.')
    return value


def local_path(value, *, root=None, exists=False):
    path = Path(value).absolute()
    if str(value).startswith(('//', '\\\\')) or '\x00' in str(value):
        raise ValueError('Choose a local path.')
    for parent in reversed((path, *path.parents)):
        if os.path.lexists(parent):
            if parent != path or parent.is_dir():
                regular(parent, directory=True)
            else:
                regular(parent)
    path = path.resolve(strict=exists)
    if root is not None and not path.is_relative_to(Path(root).resolve(strict=True)):
        raise ValueError('The path is outside the selected directory.')
    return path


def digest_file(path, *, cancelled=lambda: False):
    regular(path)
    digest = sha256()
    with Path(path).open('rb') as stream:
        if cancelled():
            raise InterruptedError('File verification cancelled.')
        while block := stream.read(1024 * 1024):
            if cancelled():
                raise InterruptedError('File verification cancelled.')
            digest.update(block)
    return digest.hexdigest()


def encode(value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')) + '\n').encode('utf-8')
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('State exceeds its configured JSON budget.')
    return raw


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate state key.')
        value[key] = item
    return value


def read_json(path):
    path = local_path(path, exists=True)
    if regular(path).st_size > MAX_JSON_BYTES:
        raise ValueError('State exceeds its configured JSON budget.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('State grew beyond its JSON budget.')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite state number.')))


def sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _replace_state(temporary, path):
    """Allow a short Windows reader to release its non-delete-sharing handle.

    Reading a tiny control/state file can overlap its atomic replacement on
    Windows. Retain one fully flushed staging file and retry only the Windows
    sharing/access errors, with a bounded total delay of 0.63 seconds. A real
    denial or an unrelated filesystem error still fails without truncating the
    old state. In particular, never fall back to unlinking the destination.
    """
    delays = iter((0.01, 0.02, 0.04, 0.08, 0.16, 0.32))
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            delay = next(delays, None)
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or delay is None:
                raise
            time.sleep(delay)


def atomic_bytes(path, raw, *, replace=False):
    path = local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    local_path(path.parent, exists=True)
    if os.path.lexists(path):
        regular(path)
        if not replace:
            raise FileExistsError(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            _replace_state(temporary, path)
        else:
            # link is the portable same-filesystem no-replace publication primitive.
            os.link(temporary, path)
            temporary.unlink()
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def atomic_json(path, value, *, replace=False):
    return atomic_bytes(path, encode(value), replace=replace)


@contextmanager
def state_lock(root, name='state'):
    from filelock import FileLock, Timeout
    root = local_path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / ('.' + name + '.lock')
    if os.path.lexists(path):
        regular(path)
    try:
        with FileLock(path, timeout=0, mode=0o600):
            yield
    except Timeout:
        raise RuntimeError('This project is already being changed by another operation.') from None
