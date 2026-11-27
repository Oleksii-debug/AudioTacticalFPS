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


def regular(path, *, directory=False):
    path = Path(path)
    value = path.lstat()
    if (stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
            or (not directory and value.st_nlink != 1)):
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


def _open_json_file(path):
    """Read a non-inheritable snapshot with Windows delete sharing.

    Legacy Windows replacement can still reject an open destination. Readers
    close promptly; live writers must bound any sharing-race retry separately.
    """
    if os.name != 'nt':
        return path.open('rb')
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = (wintypes.HANDLE,), wintypes.BOOL
    # GENERIC_READ; FILE_SHARE_READ | WRITE | DELETE; OPEN_EXISTING;
    # FILE_FLAG_OPEN_REPARSE_POINT. No delete-on-close or write access is given.
    handle = create(str(path), 0x80000000, 0x7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def read_json(path):
    return read_json_snapshot(path)[0]


def read_json_snapshot(path, *, max_bytes=None):
    """Return decoded state and a digest of the same bounded captured bytes."""
    maximum = MAX_JSON_BYTES if max_bytes is None else max_bytes
    if type(maximum) is not int or not 0 < maximum <= MAX_JSON_BYTES:
        raise ValueError('Use a positive bounded JSON read budget.')
    # An atomic writer can win between lstat and open. Retry that race only;
    # linked paths and invalid opened objects still fail before reading bytes.
    for _ in range(8):
        path = local_path(path, exists=True)
        before = regular(path)
        if before.st_size > maximum:
            raise ValueError('State exceeds its configured JSON budget.')
        with _open_json_file(path) as stream:
            opened = os.fstat(stream.fileno())
            # A valid snapshot can already have zero links after replacement;
            # multiple links or reparse points are never admitted.
            if (not stat.S_ISREG(opened.st_mode)
                    or getattr(opened, 'st_file_attributes', 0) & 0x400
                    or opened.st_nlink > 1):
                raise ValueError('State must use regular local files, without links.')
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                continue
            # Recheck ancestors after opening, before consuming the bound handle.
            local_path(path, exists=True)
            if opened.st_size > maximum:
                raise ValueError('State exceeds its configured JSON budget.')
            raw = stream.read(maximum + 1)
        break
    else:
        raise ValueError('State changed repeatedly while opening; retry the operation.')
    if len(raw) > maximum:
        raise ValueError('State grew beyond its JSON budget.')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite state number.')))
    return value, sha256(raw).hexdigest()


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
