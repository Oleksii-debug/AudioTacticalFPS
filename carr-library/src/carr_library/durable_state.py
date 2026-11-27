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


def digest_file(path):
    regular(path)
    digest = sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
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
    """Open a bounded snapshot with Windows read/write/delete sharing.

    The writer can atomically replace a delete-sharing snapshot using the
    supported native rename operation. Ordinary non-delete-sharing readers or
    filesystems without that operation retain the bounded replacement retry.
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
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close(handle)
        raise
    try:
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def read_json(path):
    # An atomic writer can win between lstat and open. Retry that race only;
    # linked paths and invalid opened objects still fail before reading bytes.
    for _ in range(8):
        path = local_path(path, exists=True)
        before = regular(path)
        if before.st_size > MAX_JSON_BYTES:
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
            if opened.st_size > MAX_JSON_BYTES:
                raise ValueError('State exceeds its configured JSON budget.')
            raw = stream.read(MAX_JSON_BYTES + 1)
        break
    else:
        raise ValueError('State changed repeatedly while opening; retry the operation.')
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


def _replace_open_state(temporary, path):
    """Replace a delete-sharing Windows snapshot with POSIX rename semantics.

    MoveFileExW (used by os.replace) cannot replace an open destination even
    when it shares delete access. FileRenameInfoEx keeps the old handle valid
    while new readers see the fully flushed replacement. No unlink gap or
    permission override is used.
    """
    import ctypes
    from ctypes import wintypes

    class RenameInfo(ctypes.Structure):
        _fields_ = [('Flags', wintypes.DWORD), ('RootDirectory', wintypes.HANDLE),
                    ('FileNameLength', wintypes.DWORD), ('FileName', wintypes.WCHAR * 1)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    rename = kernel.SetFileInformationByHandle
    rename.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    rename.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes, close.restype = (wintypes.HANDLE,), wintypes.BOOL

    # Explicit UTF-16 byte length also handles non-BMP names correctly.
    name = str(Path(path).absolute()).encode('utf-16-le')
    buffer = ctypes.create_string_buffer(max(ctypes.sizeof(RenameInfo),
        RenameInfo.FileName.offset + len(name) + 2))
    info = RenameInfo.from_buffer(buffer)
    info.Flags = 0x1 | 0x2  # REPLACE_IF_EXISTS | POSIX_SEMANTICS
    info.RootDirectory = None
    info.FileNameLength = len(name)
    ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.FileName.offset, name, len(name))
    # DELETE; share READ | WRITE | DELETE; OPEN_EXISTING; OPEN_REPARSE_POINT.
    handle = create(str(temporary), 0x00010000, 0x7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not rename(handle, 22, buffer, len(buffer)):  # FileRenameInfoEx
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)


def _replace_state(temporary, path):
    """Publish over a snapshot, or wait briefly for a non-delete-sharing reader.

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
            if os.name == 'nt' and getattr(exc, 'winerror', None) == 5:
                try:
                    _replace_open_state(temporary, path)
                    return
                except OSError as native_error:
                    # Older filesystems may not implement FileRenameInfoEx.
                    # Keep the same bounded legacy retry for that case; never
                    # reinterpret an unrelated error as successful publication.
                    code = getattr(native_error, 'winerror', None)
                    if code not in (1, 5, 32, 33, 50, 87, 120):
                        raise
                    if code in (5, 32, 33):
                        exc = native_error
            delay = next(delays, None)
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or delay is None:
                raise exc
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
        elif os.name == 'nt':
            # Windows rename is create-only and leaves no second hard link.
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
