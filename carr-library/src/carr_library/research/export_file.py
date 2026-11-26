"""Publish user-requested exports without modifying private library files.

The caller obtains overwrite confirmation before calling ``write_export``.
Checks cover ordinary desktop use and detect destination changes while the
temporary file is written; they are not a sandbox against a process that can
maliciously replace directory entries at the instant of publication.
"""
from pathlib import Path, PureWindowsPath
import os
import stat
from tempfile import NamedTemporaryFile


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


def _destination(path, private_root):
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
        state = _file_state(info)
    return target, _identity(parent.stat()), state


def write_export(path, content, *, private_root, cancelled=lambda: False, require_absent=False):
    """Atomically write exact UTF-8 text to an explicitly selected destination.

    A normal existing file may be replaced after caller-side confirmation.
    Library paths, linked ancestors/targets and hard-linked files are rejected.
    Failure or cancellation before publication preserves an existing target
    and removes the temporary file. Once atomic replacement completes, the
    save is complete. CR, LF and CRLF sequences remain unchanged.
    """
    if not isinstance(content, str):
        raise ValueError('Text export required.')
    _write_payload(path, content.encode('utf-8'), private_root=private_root,
                   cancelled=cancelled, require_absent=require_absent)


def write_binary_export(path, content, *, private_root, cancelled=lambda: False, require_absent=False):
    """Atomically publish a rendered document using the text export safeguards.

    The payload must be immutable bytes. As for ``write_export``, the caller
    selects the destination and confirms replacement of an existing file.
    Cancellation before replacement preserves the previous destination.
    """
    if not isinstance(content, bytes):
        raise ValueError('Binary document export required.')
    _write_payload(path, content, private_root=private_root,
                   cancelled=cancelled, require_absent=require_absent)


def _write_payload(path, payload, *, private_root, cancelled, require_absent=False):
    if not callable(cancelled):
        raise ValueError('An export cancellation callback is required.')
    if type(require_absent) is not bool:
        raise ValueError('The export replacement policy must be boolean.')

    def check_cancelled():
        if cancelled():
            raise TimeoutError('Export cancelled; the destination was not changed.')

    check_cancelled()
    raw_path = os.fspath(path)
    if (not isinstance(raw_path, str) or not raw_path or raw_path in ('.', '..')
            or raw_path.endswith(('/', '\\'))):
        raise ValueError('Choose an export filename, not an empty path or folder.')
    requested = Path(raw_path).absolute()
    _filename(requested.name)
    private = Path(private_root).absolute()
    target, parent_identity, previous = _destination(requested, private)
    if require_absent and previous is not None:
        raise FileExistsError('The export destination already exists; replacement was not authorized.')
    temporary = None
    try:
        check_cancelled()
        with NamedTemporaryFile(mode='wb', prefix='.research-export-',
                                dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())

        check_cancelled()
        checked_target, checked_parent, current = _destination(requested, private)
        if (checked_target != target or checked_parent != parent_identity or
                current != previous):
            raise ValueError('The export destination changed. Choose the destination again.')
        check_cancelled()
        if require_absent:
            # This is atomic even if a file appears after the preceding check.
            # A filesystem lacking hard links fails without replacing anything.
            os.link(temporary, target, follow_symlinks=False)
            os.unlink(temporary)
        else:
            os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
