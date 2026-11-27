"""Atomic, bounded private JSON state. Original source databases are not edited."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

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
    """Read one snapshot without blocking another process's atomic replacement."""
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


def _replace_state_file(source, destination, staged):
    """Publish a flushed sibling file while delete-sharing readers retain it.

    MoveFileExW, and therefore Python's os.replace on Windows, cannot replace
    an open destination using the older rename semantics. FileRenameInfoEx
    flags 1|2 keep old handles valid and direct new opens to the new file.
    See Microsoft's FILE_RENAME_INFORMATION and FILE_RENAME_INFO contracts.
    """
    source = local_path(source, exists=True)
    destination = local_path(destination)
    if source.parent != destination.parent or not source.name.startswith('.pending-'):
        raise ValueError('State replacement requires its staged file in the destination directory.')
    before = regular(source)
    expected = (staged.st_dev, staged.st_ino, staged.st_size)
    if (before.st_dev, before.st_ino, before.st_size) != expected:
        raise ValueError('Staged state changed before atomic replacement.')
    if os.name != 'nt':
        os.replace(source, destination)
        return

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
    rename = kernel.SetFileInformationByHandle
    rename.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    rename.restype = wintypes.BOOL

    class RenameInfo(ctypes.Structure):
        _fields_ = [('flags', wintypes.DWORD), ('root', wintypes.HANDLE),
                    ('length', wintypes.DWORD), ('name', wintypes.WCHAR * 1)]

    # GENERIC_READ | DELETE; shared read/write/delete; OPEN_EXISTING;
    # OPEN_REPARSE_POINT. The native operation is bound to our staged file.
    # Native calls do not emit os.rename: preserve installed audit policies.
    sys.audit('os.rename', str(source), str(destination), -1, -1)
    handle = create(str(source), 0x80010000, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    unsupported = False
    published = False
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or (opened.st_dev, opened.st_ino, opened.st_size) != expected):
            raise ValueError('State staging identity changed before publication.')
        # Revalidate the owned namespace without following substituted links.
        local_path(source, exists=True)
        observed = regular(source)
        if (observed.st_dev, observed.st_ino, observed.st_size) != expected:
            raise ValueError('State staging identity changed before publication.')
        local_path(destination)

        encoded = str(destination).encode('utf-16-le')
        buffer = ctypes.create_string_buffer(ctypes.sizeof(RenameInfo) + len(encoded))
        info = RenameInfo.from_buffer(buffer)
        # REPLACE_IF_EXISTS | POSIX_SEMANTICS; do not override read-only flags.
        info.flags, info.root, info.length = 3, None, len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.name.offset,
                       encoded, len(encoded))
        if not rename(handle, 22, buffer, len(buffer)):  # FileRenameInfoEx
            error = ctypes.get_last_error()  # capture before closing the handle
            if error not in (1, 50, 87):  # unsupported OS/filesystem/class only
                raise ctypes.WinError(error)
            unsupported = True
        else:
            published = True
    except BaseException:
        # Preserve the publication error if descriptor cleanup also fails.
        # Never retry a descriptor: close may have released it before raising.
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    else:
        try:
            # open_osfhandle transferred ownership; do not close the raw handle.
            os.close(descriptor)
        except OSError:
            if not published:
                raise
            # Native success already committed the new file. A close error
            # cannot turn that durable outcome into a failed/retryable save.
    if unsupported:
        # Preserve the prior failure behavior on unsupported filesystems. This
        # has no copy/delete fallback, target unlink, attribute change or retry.
        os.replace(source, destination)


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
            staged = os.fstat(stream.fileno())
        if replace:
            _replace_state_file(temporary, path, staged)
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
