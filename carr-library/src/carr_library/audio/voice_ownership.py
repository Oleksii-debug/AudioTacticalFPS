"""Hold the selected owner's existing package lock without changing its bytes.

This is the same native lock protocol used by the reviewed owner API: flock on
POSIX and byte zero with msvcrt.locking on Windows. It is not another resource
broker. Explicit configuration establishes the file through the owner API;
discovery and synthesis never create it. Run the waiting context on the caller's
existing operation worker, and retain it until native stream cleanup settles.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import errno
import math
import os
from pathlib import Path
import stat
import threading
import time

from ..durable_state import local_path, regular
from .cleanup import CleanupOwner
from .models import AudioCleanupPending


_LOCK_NAME = '.voice-packages.lock'
_POLL_SECONDS = .05
_IDENTITY_SCHEMA = 'carr-voice-ownership/1'


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Voice package ownership admission was cancelled.')


def _identity(info):
    return int(info.st_dev), int(info.st_ino)


def _row(path, identity):
    return {'path': str(path), 'device': identity[0], 'inode': identity[1]}


def _directories(path):
    return tuple(reversed((path, *path.parents)))


@dataclass(frozen=True)
class VoiceOwnershipTicket:
    """Inert identities captured from an explicitly selected local installation."""
    runtime: Path
    references: Path
    packages: Path
    lock: Path
    runtime_identity: tuple[int, int]
    references_identity: tuple[int, int]
    packages_identity: tuple[int, int]
    lock_identity: tuple[int, int]
    directory_identities: tuple[tuple[Path, tuple[int, int]], ...]

    @property
    def identity(self):
        # Each caller gets a detached, JSON-compatible value for its own CAS.
        return {
            'schema': _IDENTITY_SCHEMA,
            'runtime': _row(self.runtime, self.runtime_identity),
            'references': _row(self.references, self.references_identity),
            'packages': _row(self.packages, self.packages_identity),
            'lock': _row(self.lock, self.lock_identity),
        }


def _check_ticket(ticket):
    if type(ticket) is not VoiceOwnershipTicket:
        raise TypeError('A captured voice ownership ticket is required.')
    if (ticket.lock != ticket.packages / _LOCK_NAME
            or ticket.packages != ticket.references / 'packages'
            or not ticket.references.is_relative_to(ticket.runtime)):
        raise ValueError('The selected voice ownership paths changed.')
    expected_paths = _directories(ticket.packages)
    if tuple(path for path, _ in ticket.directory_identities) != expected_paths:
        raise ValueError('The voice ownership directory inventory is incomplete.')
    # Recheck the caller's lexical path as well as every captured ancestor.
    if local_path(ticket.packages, exists=True) != ticket.packages:
        raise ValueError('The selected voice package directory changed.')
    for path, expected in ticket.directory_identities:
        if _identity(regular(path, directory=True)) != expected:
            raise ValueError('The selected voice installation directory changed.')
    by_path = dict(ticket.directory_identities)
    if (by_path.get(ticket.runtime) != ticket.runtime_identity
            or by_path.get(ticket.references) != ticket.references_identity
            or by_path.get(ticket.packages) != ticket.packages_identity):
        raise ValueError('The voice ownership identity is inconsistent.')
    info = regular(ticket.lock)
    if _identity(info) != ticket.lock_identity or info.st_size not in (0, 1):
        raise ValueError('The selected owner package lock changed.')
    return info


def capture_voice_ownership(runtime_root, *, references=None, expected=None,
                            cancelled=lambda: False):
    """Capture an already configured owner lock; do not create directories/files.

    The production provider uses models/voices. Management may supply a reviewed
    references directory inside this exact runtime. expected is an optional
    previously saved identity dictionary, compared in full before admission.
    """
    if not callable(cancelled):
        raise TypeError('Voice ownership requires a cancellation callback.')
    _cancel(cancelled)
    runtime = local_path(runtime_root, exists=True)
    runtime_info = regular(runtime, directory=True)
    selected = runtime / 'models' / 'voices' if references is None else Path(references)
    if not selected.is_absolute():
        selected = runtime / selected
    references_path = local_path(selected, root=runtime, exists=True)
    references_info = regular(references_path, directory=True)
    packages = local_path(references_path / 'packages', root=runtime, exists=True)
    packages_info = regular(packages, directory=True)
    lock = packages / _LOCK_NAME
    lock_info = regular(lock)
    if lock_info.st_size not in (0, 1):
        raise ValueError('The existing owner package lock has invalid contents.')
    directories = tuple((path, _identity(regular(path, directory=True)))
                        for path in _directories(packages))
    ticket = VoiceOwnershipTicket(runtime, references_path, packages, lock,
        _identity(runtime_info), _identity(references_info), _identity(packages_info),
        _identity(lock_info), directories)
    _cancel(cancelled)
    _check_ticket(ticket)
    if expected is not None and (type(expected) is not dict or ticket.identity != expected):
        raise ValueError('The reviewed voice ownership identity changed.')
    return ticket


def _try_lock(descriptor):
    if os.name == 'nt':
        import msvcrt
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno == errno.EACCES:
                return False
            raise
        return True
    if os.name == 'posix':
        import fcntl
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise
        return True
    raise RuntimeError('This platform has no reviewed owner package lock protocol.')


def settle_voice_cleanup(error):
    """Wait for the existing native owner; interruption cannot release it early.

    No cleanup thread, replacement owner, process kill, or elapsed-time reclaim
    is introduced here. A deferred interruption is raised only after the owner's
    actual quiescent event, just as at ResourceLease.settle_cleanup.
    """
    if not isinstance(error, AudioCleanupPending):
        raise TypeError('An actual pending audio cleanup error is required.')
    owner = error.cleanup_owner
    if not isinstance(owner, CleanupOwner):
        raise TypeError('The current native CleanupOwner is required.')
    interrupted = None
    while True:
        try:
            if owner.quiescent_event.is_set():
                break
            owner.close()
            if not owner.quiescent_event.is_set():
                owner.quiescent_event.wait(.25)
        except (KeyboardInterrupt, SystemExit) as current:
            if interrupted is None:
                interrupted = current
    if interrupted is not None:
        raise interrupted


class VoiceOwnershipLease:
    """One held descriptor; closing never resolves, rewrites, or unlinks its path."""
    def __init__(self, ticket, descriptor, cancelled):
        self.ticket = ticket
        self._descriptor = descriptor
        self._cancelled = cancelled
        self._pid = os.getpid()
        self._held = False
        self._guard = threading.RLock()

    @property
    def identity(self):
        return self.ticket.identity

    @property
    def held(self):
        with self._guard:
            return (self._pid == os.getpid() and self._held
                    and self._descriptor is not None)

    def _verify_descriptor(self, *, contents=False):
        if self._pid != os.getpid() or self._descriptor is None:
            raise ValueError('The voice package lease is not owned by this process.')
        named = _check_ticket(self.ticket)
        opened = os.fstat(self._descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or _identity(opened) != self.ticket.lock_identity
                or _identity(named) != _identity(opened)
                or opened.st_size not in (0, 1)):
            raise ValueError('The opened owner package lock changed.')
        if contents:
            if not self._held:
                raise ValueError('The owner package lock is not held.')
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            if os.read(self._descriptor, 2) not in (b'', b'0'):
                raise ValueError('The existing owner package lock has invalid contents.')
            # A descriptor alone cannot authorize a renamed/replaced installation.
            _check_ticket(self.ticket)

    def checkpoint(self):
        with self._guard:
            if not self.held:
                raise ValueError('The voice package lease is no longer held.')
            _cancel(self._cancelled)
            self._verify_descriptor(contents=True)
            _cancel(self._cancelled)
            self._verify_descriptor()

    def lock_sha256(self):
        """Hash only the held fd; Windows excludes a second byte-zero reader."""
        with self._guard:
            self.checkpoint()
            before = os.fstat(self._descriptor)
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            raw = os.read(self._descriptor, 2)
            after = os.fstat(self._descriptor)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            if (raw not in (b'', b'0')
                    or any(getattr(before, key) != getattr(after, key) for key in fields)):
                raise ValueError('The held owner package lock changed while hashing.')
            self._verify_descriptor()
            _cancel(self._cancelled)
            self._verify_descriptor()
            return sha256(raw).hexdigest()

    def close(self):
        with self._guard:
            descriptor, self._descriptor = self._descriptor, None
            self._held = False
            if descriptor is not None:
                # Closing an inherited copy also leaves the parent's flock alone.
                # Do not issue an explicit unlock or operate on the named path.
                os.close(descriptor)


@contextmanager
def hold_voice_ownership(ticket, *, timeout=60, cancelled=lambda: False):
    """Wait finitely for an existing owner lock, then retain it through cleanup."""
    if not callable(cancelled):
        raise TypeError('Voice ownership requires a cancellation callback.')
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 <= timeout <= 600):
        raise ValueError('Choose a finite voice lock admission timeout from 0 to 600 seconds.')
    _cancel(cancelled)
    _check_ticket(ticket)
    flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(ticket.lock, flags)  # No CREATE, TRUNC, or implicit mkdir.
    try:
        lease = VoiceOwnershipLease(ticket, descriptor, cancelled)
    except BaseException:
        os.close(descriptor)
        raise
    try:
        os.set_inheritable(descriptor, False)
        deadline, attempted = time.monotonic() + float(timeout), False
        while True:
            _cancel(cancelled)
            lease._verify_descriptor()
            if attempted and time.monotonic() >= deadline:
                raise TimeoutError('Timed out waiting for the active voice package operation.')
            if _try_lock(descriptor):
                lease._held = True
                break
            attempted = True
            # The callback may revoke/replace the selected root while we wait.
            _cancel(cancelled)
            lease._verify_descriptor()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Timed out waiting for the active voice package operation.')
            time.sleep(min(_POLL_SECONDS, remaining))
        lease.checkpoint()
        try:
            yield lease
        except AudioCleanupPending as error:
            settle_voice_cleanup(error)
            raise
    finally:
        lease.close()
