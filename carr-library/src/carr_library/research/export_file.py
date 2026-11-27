"""Publish user-requested exports without modifying private library files.

The caller obtains overwrite confirmation before calling ``write_export``.
Checks cover ordinary desktop use and detect destination changes while the
temporary file is written; they are not a sandbox against a process that can
maliciously replace directory entries at the instant of publication.
"""
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import ctypes
import logging
from pathlib import Path, PureWindowsPath
import os
import stat
from tempfile import NamedTemporaryFile
from time import monotonic


@dataclass(frozen=True)
class ExportReceipt:
    """A durable publication, including any remaining staging cleanup notice."""
    path: Path
    cleanup_warning: str | None = None


_logger = logging.getLogger(__name__)
_MAX_OVERWRITE_BYTES = 32 * 1024 * 1024
_OVERWRITE_CHECK_SECONDS = 0.25


def export_completion(message, receipt):
    """Report a completed publication and any remaining staging cleanup notice."""
    warning = getattr(receipt, 'cleanup_warning', None)
    return message + (' ' + warning if isinstance(warning, str) and warning else '')


def _filename(name):
    # Validate the basename even when checks run on Linux: NTFS streams and
    # device aliases must not become a different destination on Windows.
    if (not name or name in ('.', '..') or name.rstrip(' .') != name
            or any(char in '<>:"/\\|?*' or ord(char) < 32
                   or 127 <= ord(char) < 160 for char in name)
            or PureWindowsPath(name).is_reserved()):
        raise ValueError('Choose an export filename without Windows device names, '
                         'reserved characters, or trailing spaces or dots.')


def _linked(info):
    # Reparse points include Windows junctions and other redirections that are
    # not necessarily reported as symbolic links by pathlib.
    return (stat.S_ISLNK(info.st_mode) or
            bool(getattr(info, 'st_file_attributes', 0) &
                 getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)))


def _identity(info):
    return info.st_dev, info.st_ino


def _file_state(info):
    return (_identity(info), info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _content_state(path, info, check_cancelled):
    """Bind replacement to the actual existing bytes, including on Windows.

    Equal-length writes can retain all of the visible metadata. Read only the
    captured length plus one EOF check, with fixed memory and cancellation
    between chunks; a growing file cannot keep this snapshot running forever.
    The opened object and final path must still match the initial regular file.
    """
    check_cancelled()
    if info.st_size > _MAX_OVERWRITE_BYTES:
        raise ValueError('The existing file is too large to verify for replacement. '
                         'Choose a new filename.')
    deadline = monotonic() + _OVERWRITE_CHECK_SECONDS

    def budget():
        check_cancelled()
        if monotonic() >= deadline:
            raise ValueError('The existing file could not be verified quickly enough. '
                             'Choose a new filename.')

    previous = _file_state(info)
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0)
             | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or _file_state(opened) != previous):
            raise ValueError('The export destination changed. Choose the destination again.')
        digest = sha256()
        remaining = info.st_size
        while remaining:
            budget()
            block = os.read(descriptor, min(128 * 1024, remaining))
            if not block:
                raise ValueError('The export destination changed. Choose the destination again.')
            digest.update(block)
            remaining -= len(block)
        budget()
        current = path.lstat()
        if (os.read(descriptor, 1) or _linked(current)
                or _file_state(os.fstat(descriptor)) != previous
                or _file_state(current) != previous):
            raise ValueError('The export destination changed. Choose the destination again.')
    finally:
        os.close(descriptor)
    budget()
    return (*previous, digest.digest())


class _WindowsFileBasicInfo(ctypes.Structure):
    # FILE_BASIC_INFO: four LARGE_INTEGER values and one DWORD. Fixed-width
    # fields also make the ABI checks meaningful on non-Windows test hosts.
    _fields_ = [('CreationTime', ctypes.c_int64), ('LastAccessTime', ctypes.c_int64),
                ('LastWriteTime', ctypes.c_int64), ('ChangeTime', ctypes.c_int64),
                ('FileAttributes', ctypes.c_uint32)]


@lru_cache(maxsize=1)
def _windows_file_api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create, query, close = (kernel.CreateFileW, kernel.GetFileInformationByHandleEx,
                            kernel.CloseHandle)
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    query.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32]
    query.restype = ctypes.c_int32
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int32
    return create, query, close


def _windows_api_error(operation):
    return OSError(f'The export destination could not be verified: {operation} '
                   f'failed (Windows error {ctypes.get_last_error()}).')


def _windows_change_time(path):
    """Read bounded native metadata without reading or locking file contents."""
    create, query, close = _windows_file_api()
    # FILE_READ_ATTRIBUTES; share read/write/delete; OPEN_EXISTING; open the
    # reparse point itself so the handle query can reject redirection.
    handle = create(str(path), 0x80, 0x7, None, 3, 0x00200000, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise _windows_api_error('CreateFileW')
    try:
        basic = _WindowsFileBasicInfo()
        if not query(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise _windows_api_error('GetFileInformationByHandleEx')
        if basic.FileAttributes & (0x400 | 0x10):  # reparse point or directory
            raise ValueError('The export destination must be a regular file without links or reparse points.')
        if basic.ChangeTime <= 0:
            raise ValueError('The export destination does not provide a usable Windows change time.')
        return basic.ChangeTime
    finally:
        if not close(handle):
            raise _windows_api_error('CloseHandle')


def _destination(path, private_root, check_cancelled=lambda: None, *, require_absent=False):
    private = private_root.resolve()
    try:
        private_info = private.stat()
    except FileNotFoundError:
        private_identity = None
    else:
        if not stat.S_ISDIR(private_info.st_mode):
            raise ValueError('The private library folder is not a directory.')
        private_identity = _identity(private_info)

    # Inspect the original path before resolving it so that a junction or
    # symlink anywhere in a user-selected route is rejected, even outside the
    # library. Existing parents are required; exporting never creates them.
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if _linked(info):
            raise ValueError('Choose an export folder without symbolic links or Windows reparse points.')
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('The export parent must be an existing directory.')
        if private_identity is not None and _identity(info) == private_identity:
            raise ValueError('Choose an export destination outside the private library folder.')

    parent = path.parent.resolve(strict=True)
    target = parent / path.name
    if target.is_relative_to(private):
        raise ValueError('Choose an export destination outside the private library folder.')
    try:
        info = target.lstat()
    except FileNotFoundError:
        state = None
    else:
        if _linked(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError('The export destination must be a regular file without links or reparse points.')
        if info.st_nlink != 1:
            raise ValueError('Choose an export destination without hard links.')
        if require_absent:
            # New-only publication has no reason to read an existing file.
            # Keep the path/link guards above, then reject before hashing a
            # potentially very large or slow user-selected destination.
            raise FileExistsError('The export destination already exists; replacement was not authorized.')
        parent_identity = _identity(parent.stat())
        # Python 3.11 exposes Windows creation time as ctime. Bind native
        # ChangeTime as well as the full bytes: either timestamp can collide,
        # while a write followed by restoration of the old bytes still changes
        # native metadata. A failed native query never falls back to less proof.
        change_time = _windows_change_time(target) if os.name == 'nt' else None
        state = _content_state(target, info, check_cancelled)
        after = target.lstat()
        if (_linked(after) or _file_state(after) != _file_state(info)
                or _identity(parent.stat()) != parent_identity):
            raise ValueError('The export destination changed. Choose the destination again.')
        if change_time is not None:
            state = (*state[:-1], change_time, state[-1])
    return target, _identity(parent.stat()), state


@dataclass(frozen=True)
class ExportDestination:
    """One destination state captured before caller-side overwrite approval."""
    requested: Path
    private_root: Path
    target: Path
    parent_identity: tuple
    state: tuple | None

    @property
    def require_absent(self):
        return self.state is None


def _export_paths(path, private_root):
    raw_path = os.fspath(path)
    if (not isinstance(raw_path, str) or not raw_path or raw_path in ('.', '..')
            or raw_path.endswith(('/', '\\'))):
        raise ValueError('Choose an export filename, not an empty path or folder.')
    requested = Path(raw_path).absolute()
    _filename(requested.name)
    return requested, Path(private_root).absolute()


def capture_export_destination(path, *, private_root, cancelled=lambda: False):
    """Inspect a chosen path before asking to replace its existing file.

    Pass this same immutable snapshot to the writer after confirmation. It
    binds approval to the selected path, parent directory and existing file
    state and contents across queued work and rendering; it grants no
    permission itself. Existing files over 32 MiB or exceeding the bounded
    content-verification time require a new filename.
    """
    if not callable(cancelled):
        raise ValueError('An export cancellation callback is required.')

    def check_cancelled():
        if cancelled():
            raise TimeoutError('Export cancelled; the destination was not changed.')

    check_cancelled()
    requested, private = _export_paths(path, private_root)
    target, parent_identity, state = _destination(requested, private, check_cancelled)
    check_cancelled()
    return ExportDestination(requested, private, target, parent_identity, state)


def write_export(path, content, *, private_root, cancelled=lambda: False,
                 require_absent=False, approval=None):
    """Atomically write exact UTF-8 text to an explicitly selected destination.

    A normal existing file may be replaced after caller-side confirmation.
    Library paths, linked ancestors/targets and hard-linked files are rejected.
    Failure or cancellation before publication preserves an existing target
    and removes the temporary file. An optional captured approval also rejects
    changes since the caller selected and confirmed the destination. A captured
    absent destination always uses atomic no-clobber publication. Once atomic
    replacement completes, the save is complete. CR, LF and CRLF remain unchanged.
    """
    if not isinstance(content, str):
        raise ValueError('Text export required.')
    return _write_payload(path, content.encode('utf-8'), private_root=private_root,
                   cancelled=cancelled, require_absent=require_absent, approval=approval)


def write_binary_export(path, content, *, private_root, cancelled=lambda: False,
                        require_absent=False, approval=None):
    """Atomically publish a rendered document using the text export safeguards.

    The payload must be immutable bytes. As for ``write_export``, the caller
    selects the destination and confirms replacement of an existing file.
    Cancellation before replacement preserves the previous destination.
    """
    if not isinstance(content, bytes):
        raise ValueError('Binary document export required.')
    return _write_payload(path, content, private_root=private_root,
                   cancelled=cancelled, require_absent=require_absent, approval=approval)


def _write_payload(path, payload, *, private_root, cancelled, require_absent=False, approval=None):
    if not callable(cancelled):
        raise ValueError('An export cancellation callback is required.')
    if type(require_absent) is not bool:
        raise ValueError('The export replacement policy must be boolean.')

    def check_cancelled():
        if cancelled():
            raise TimeoutError('Export cancelled; the destination was not changed.')

    check_cancelled()
    requested, private = _export_paths(path, private_root)
    captured_absent = type(approval) is ExportDestination and approval.require_absent
    try:
        target, parent_identity, previous = _destination(
            requested, private, check_cancelled,
            require_absent=require_absent or captured_absent)
    except FileExistsError as exc:
        if captured_absent:
            # A new occupant invalidates the captured selection. Preserve the
            # approval contract without reading the unapproved file's bytes.
            raise ValueError('The export destination changed. Choose the destination again.') from exc
        raise
    # An ordinary replacement approval cannot authorize a competing file that
    # did not exist when this write began, including for legacy callers.
    require_absent = require_absent or previous is None
    if approval is not None:
        if type(approval) is not ExportDestination:
            raise ValueError('A captured export destination approval is required.')
        if (approval.requested != requested or approval.private_root != private
                or approval.target != target or approval.parent_identity != parent_identity
                or approval.state != previous):
            raise ValueError('The export destination changed. Choose the destination again.')
        require_absent = require_absent or approval.require_absent
    if require_absent and previous is not None:
        raise FileExistsError('The export destination already exists; replacement was not authorized.')
    temporary = None
    cleanup_warning = None
    try:
        check_cancelled()
        with NamedTemporaryFile(mode='wb', prefix='.research-export-',
                                dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())

        check_cancelled()
        try:
            checked_target, checked_parent, current = _destination(
                requested, private, check_cancelled, require_absent=require_absent)
        except FileExistsError as exc:
            raise ValueError('The export destination changed. Choose the destination again.') from exc
        if (checked_target != target or checked_parent != parent_identity or
                current != previous):
            raise ValueError('The export destination changed. Choose the destination again.')
        check_cancelled()
        if require_absent and os.name == 'nt':
            # Windows rename fails if the destination exists. The single
            # same-directory move avoids the link/unlink crash window.
            os.rename(temporary, target)
        elif require_absent:
            # POSIX rename can overwrite. Preserve atomic no-clobber via
            # hard-link publication; never fall back to unconditional replace.
            os.link(temporary, target, follow_symlinks=False)
            published_stage, temporary = temporary, None
            try:
                published_stage.unlink()
            except OSError:
                try:
                    published_stage.unlink(missing_ok=True)
                except OSError:
                    cleanup_warning = ('The file is saved. Temporary-file cleanup is still pending; '
                                       'the export does not need to be repeated.')
                    try:
                        _logger.warning('%s Export destination: %s', cleanup_warning, target)
                    except Exception:
                        pass
        else:
            os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return ExportReceipt(target, cleanup_warning)
