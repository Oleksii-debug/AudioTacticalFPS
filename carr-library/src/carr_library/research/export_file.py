"""Publish user-requested exports without modifying private library files.

The caller obtains overwrite confirmation before calling ``write_export``.
Checks cover ordinary desktop use and detect destination changes while the
temporary file is written; they are not a sandbox against a process that can
maliciously replace directory entries at the instant of publication.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PureWindowsPath
import os
import stat
from tempfile import NamedTemporaryFile
from time import monotonic


# Preserve the integrated desktop policy: inspect at most the largest research
# matrix export before asking the user to replace an existing destination.
MAX_DESTINATION_BYTES = 64 * 1024 * 1024
DESTINATION_CHECK_SECONDS = 1.0


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


def _inspect_destination(path, private_root):
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


def _destination(path, private_root, check_cancelled=lambda: None, reject_existing=None):
    snapshot = _inspect_destination(path, private_root)
    target, parent_identity, state = snapshot
    if state is None:
        return snapshot
    if reject_existing is not None:
        raise reject_existing
    if state[3] > MAX_DESTINATION_BYTES:
        raise ValueError('The existing export exceeds the 64 MiB verification budget; choose a new filename.')

    # Reuse the existing one-second cooperative budget from the academic
    # export candidate. It cannot preempt a stalled native filesystem call.
    deadline = monotonic() + DESTINATION_CHECK_SECONDS

    def check():
        check_cancelled()
        if monotonic() >= deadline:
            raise TimeoutError('The existing export file could not be verified in time. '
                               'Choose another destination; the file was not changed.')

    check()
    # Guard before opening, then bind the descriptor before reading any bytes.
    # Nonblocking/no-follow opens also reject a raced-in FIFO or final symlink
    # on platforms exposing these flags. Existing paths are already validated.
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0)
             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
    descriptor = os.open(target, flags)
    try:
        with os.fdopen(descriptor, 'rb', buffering=0, closefd=False) as source:
            check()
            opened = os.fstat(descriptor)
            if (_file_state(opened) != state
                    or _inspect_destination(path, private_root) != snapshot):
                raise ValueError('The export destination changed. Choose the destination again.')
            # Read the complete captured length with constant memory and one
            # extra byte solely to detect growth; never accept a sampled or
            # truncated digest.
            digest, remaining = sha256(), opened.st_size
            while remaining:
                check()
                block = source.read(min(1024 * 1024, remaining))
                check()
                if not block:
                    raise ValueError('The export destination changed. Choose the destination again.')
                digest.update(block)
                remaining -= len(block)
            check()
            extra = source.read(1)
            check()
            if extra:
                raise ValueError('The export destination changed. Choose the destination again.')
            if (_file_state(os.fstat(descriptor)) != state
                    or _inspect_destination(path, private_root) != snapshot):
                raise ValueError('The export destination changed. Choose the destination again.')
            if monotonic() >= deadline:
                raise TimeoutError('The existing export file could not be verified in time. '
                                   'Choose another destination; the file was not changed.')
    finally:
        os.close(descriptor)
    return target, parent_identity, (*state, digest.hexdigest())


@dataclass(frozen=True)
class ExportDestination:
    """One destination state captured before caller-side overwrite approval."""
    requested: Path
    private_root: Path
    target: Path
    parent_identity: tuple
    state: tuple | None

    @property
    def content_sha256(self):
        return self.state[-1] if self.state is not None else None

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
    binds approval to the selected path, parent directory and complete existing
    file contents across queued work and rendering; it grants no permission
    itself. Existing destinations up to 64 MiB are streamed in full with
    constant-memory reads, bounded to their captured size. Larger files require
    a new destination; cancellation and a cooperative time budget are checked
    around read blocks.
    """
    if not callable(cancelled):
        raise ValueError('An export cancellation callback is required.')

    def check_cancelled():
        if cancelled():
            raise TimeoutError('Export cancelled; the destination was not changed.')

    check_cancelled()
    requested, private = _export_paths(path, private_root)
    target, parent_identity, state = _destination(requested, private, check_cancelled)
    return ExportDestination(requested, private, target, parent_identity, state)


def write_export(path, content, *, private_root, cancelled=lambda: False,
                 require_absent=False, approval=None):
    """Atomically write exact UTF-8 text to an explicitly selected destination.

    A normal existing file may be replaced after caller-side confirmation.
    Library paths, linked ancestors/targets and hard-linked files are rejected.
    Failure or cancellation before publication preserves an existing target
    and removes the temporary file. An optional captured approval also rejects
    changes since the caller selected and confirmed the destination. An absent
    destination always uses atomic no-clobber publication. Once atomic
    replacement completes, the save is complete. CR, LF and CRLF remain unchanged.
    """
    if not isinstance(content, str):
        raise ValueError('Text export required.')
    _write_payload(path, content.encode('utf-8'), private_root=private_root,
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
    _write_payload(path, content, private_root=private_root,
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
    # The existing PR169 policy rejects create-only conflicts before opening
    # their contents, even if the unapproved file would fit the hash budget.
    reject_existing = None
    if approval is not None and type(approval) is not ExportDestination:
        raise ValueError('A captured export destination approval is required.')
    if approval is not None and approval.require_absent:
        reject_existing = ValueError('The export destination changed. Choose the destination again.')
    elif require_absent:
        reject_existing = FileExistsError('The export destination already exists; replacement was not authorized.')
    target, parent_identity, previous = _destination(requested, private, check_cancelled, reject_existing)
    # A caller approving ordinary replacement cannot approve a file that did
    # not exist at this snapshot. Protect every existing caller from a last-
    # moment competing create, even without the new explicit policy argument.
    require_absent = require_absent or previous is None
    if approval is not None:
        if (approval.requested != requested or approval.private_root != private
                or approval.target != target or approval.parent_identity != parent_identity
                or approval.state != previous):
            raise ValueError('The export destination changed. Choose the destination again.')
        require_absent = require_absent or approval.require_absent
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
        reject_existing = (ValueError('The export destination changed. Choose the destination again.')
                           if require_absent else None)
        checked_target, checked_parent, current = _destination(requested, private, check_cancelled, reject_existing)
        if (checked_target != target or checked_parent != parent_identity or
                current != previous):
            raise ValueError('The export destination changed. Choose the destination again.')
        check_cancelled()
        if require_absent:
            if os.name == 'nt':
                # Windows rename refuses an existing destination. Moving the
                # staged file also leaves no second hard link if the process
                # exits immediately after the filesystem operation succeeds.
                # POSIX rename may overwrite, so it must never use this path.
                os.rename(temporary, target)
            else:
                # Both paths share a filesystem; link fails atomically if a
                # competitor creates the destination after the final check.
                # Unsupported filesystems fail without weakening this policy.
                os.link(temporary, target, follow_symlinks=False)
                published_stage, temporary = temporary, None
                try:
                    published_stage.unlink()
                except OSError:
                    try:
                        published_stage.unlink(missing_ok=True)
                    except OSError as exc:
                        raise OSError('Export was published, but its temporary '
                                      'link could not be removed.') from exc
        else:
            os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
