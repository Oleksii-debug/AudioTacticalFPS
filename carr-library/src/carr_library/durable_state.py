"""Atomic, bounded private JSON state. Original source databases are not edited."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import tempfile

from .windows_state import replace_file

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


def _open_windows_read(path):
    """Allow the native state publisher to replace an open read snapshot."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    # GENERIC_READ; share read/write/delete; OPEN_EXISTING. Opening the reparse
    # point itself prevents a final-component link swap from following a link.
    handle = create(str(path), 0x80000000, 0x00000007, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close(handle)
        raise
    # open_osfhandle transfers ownership: only the CRT descriptor may close the
    # handle now, including when construction of the Python stream fails.
    try:
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def _open_json_read(path):
    return _open_windows_read(path) if os.name == 'nt' else path.open('rb')


def read_json(path):
    return read_json_snapshot(path)[0]


def read_json_snapshot(path, *, max_bytes=None):
    """Read one stable JSON snapshot and the digest of its exact encoded bytes."""
    maximum = MAX_JSON_BYTES if max_bytes is None else max_bytes
    if type(maximum) is not int or not 0 < maximum <= MAX_JSON_BYTES:
        raise ValueError('Use a positive bounded JSON read budget.')
    path = local_path(path, exists=True)
    if regular(path).st_size > maximum:
        raise ValueError('State exceeds its configured JSON budget.')
    with _open_json_read(path) as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or opened.st_nlink not in (0, 1)):
            raise ValueError('State must use regular local files, without links.')
        if opened.st_size > maximum:
            raise ValueError('State exceeds its configured JSON budget.')
        raw = stream.read(maximum + 1)
        finished = os.fstat(stream.fileno())
    if len(raw) > maximum:
        raise ValueError('State grew beyond its JSON budget.')
    # A replacement may unlink this old snapshot while it is open. Its path,
    # link count and change time can change; its bytes and file identity cannot.
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
    if any(getattr(opened, name) != getattr(finished, name) for name in fields):
        raise ValueError('State changed while it was being read.')
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
            replace_file(temporary, path)
        elif os.name == 'nt':
            # Windows rename refuses an existing destination and leaves no
            # second hard link if the process exits immediately after success.
            # POSIX rename can overwrite, so it must not use this branch.
            os.rename(temporary, path)
            temporary = None
        else:
            # The portable create-only fallback is atomic against a competing
            # destination. Process death between link and unlink can still leave
            # a residual link; this branch does not claim crash recovery.
            os.link(temporary, path, follow_symlinks=False)
            published_stage, temporary = temporary, None
            try:
                published_stage.unlink()
            except OSError:
                try:
                    published_stage.unlink(missing_ok=True)
                except OSError as exc:
                    raise OSError('File was published, but its temporary link '
                                  'could not be removed.') from exc
        sync_directory(path.parent)
    finally:
        if temporary is not None:
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
