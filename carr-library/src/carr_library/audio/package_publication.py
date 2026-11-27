"""Publish one inspected data package through the existing create-only owner.

This boundary borrows the current export destination review and audio publisher.
It does not create a package format, runtime, registry, or background worker.
"""
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from time import monotonic

from .models import AudioCancelled, AudioError
from .process import publish_file
from ..durable_state import local_path, regular
from ..research.export_file import ExportDestination, ExportReceipt, _destination, _export_paths


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _regular(info, links):
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or info.st_nlink != links):
        raise AudioError('A reviewed package must remain this attempt\'s regular file.')


def _digest(path, expected, check, *, links):
    """Read the captured length plus one EOF check, with fixed memory."""
    check()
    local_path(path.parent, exists=True)
    before = path.lstat()
    _regular(before, links)
    if _stamp(before) != expected:
        raise AudioError('The reviewed package changed before byte verification.')
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0)
             | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    digest, count = sha256(), 0
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        _regular(opened, links)
        if _stamp(opened) != expected:
            raise AudioError('The reviewed package changed while opening.')
        while True:
            check()
            block = stream.read(min(1024 * 1024, before.st_size - count + 1))
            if not block:
                break
            count += len(block)
            if count > before.st_size:
                raise AudioError('The reviewed package grew during verification.')
            digest.update(block)
        check()
        after = path.lstat()
        _regular(after, links)
        if (_stamp(os.fstat(stream.fileno())) != expected
                or _stamp(after) != expected):
            raise AudioError('The reviewed package changed during verification.')
    if count != before.st_size:
        raise AudioError('The reviewed package changed its captured length.')
    return digest.hexdigest()


@dataclass(frozen=True)
class PackageSnapshot:
    """The exact local staged bytes around an owner package inspection."""
    path: Path
    identity: tuple
    sha256: str
    size_bytes: int


def capture_package(staged, *, maximum_bytes, cancelled=lambda: False):
    if (type(maximum_bytes) is not int or not 0 < maximum_bytes <= 8 * 1024**3
            or not callable(cancelled)):
        raise ValueError('Use a bounded reviewed package capture.')
    path = local_path(staged, exists=True)
    owner = regular(path)
    _regular(owner, 1)
    if not 0 < owner.st_size <= maximum_bytes:
        raise ValueError('The staged package exceeds its selected byte allowance.')
    deadline = monotonic() + 120
    def check():
        if cancelled():
            raise AudioCancelled('Package capture cancelled before publication.')
        if monotonic() >= deadline:
            raise AudioError('The staged package exceeded its capture time budget.')
    identity = _stamp(owner)
    checksum = _digest(path, identity, check, links=1)
    return PackageSnapshot(path, identity, checksum, owner.st_size)


def publish_package(staged, output, *, private_root, expected_sha256, maximum_bytes,
                    approval, cancelled=lambda: False):
    """Return the existing export receipt only for this attempt's exact bytes.

    The caller retains its existing resource lease and private staging context.
    A late cancellation cannot undo a proven link. An uncertain or foreign
    target is retained and never reported as a verified successful export.
    """
    if (type(maximum_bytes) is not int or not 0 < maximum_bytes <= 8 * 1024**3
            or not isinstance(expected_sha256, str)
            or re.fullmatch('[0-9a-f]{64}', expected_sha256) is None
            or not callable(cancelled)):
        raise ValueError('Use a bounded reviewed package with its exact SHA-256.')
    requested, private = _export_paths(output, private_root)
    if (type(approval) is not ExportDestination or not approval.require_absent
            or approval.requested != requested or approval.private_root != private):
        raise ValueError('Review this exact new package destination before publication.')
    staged = local_path(staged, exists=True)
    if staged == approval.target:
        raise ValueError('The private staged package must differ from its destination.')
    owner = regular(staged)
    _regular(owner, 1)
    if not 0 < owner.st_size <= maximum_bytes:
        raise ValueError('The reviewed package exceeds its selected byte allowance.')
    original = _stamp(owner)
    stage_parent = regular(local_path(staged.parent, exists=True), directory=True)
    deadline = monotonic() + 120

    def parents():
        source_parent = regular(local_path(staged.parent, exists=True), directory=True)
        target_parent = regular(local_path(approval.target.parent, exists=True), directory=True)
        if ((source_parent.st_dev, source_parent.st_ino) !=
                (stage_parent.st_dev, stage_parent.st_ino)
                or (target_parent.st_dev, target_parent.st_ino) != approval.parent_identity):
            raise AudioError('The reviewed package directory changed during publication.')

    def before_check():
        if cancelled():
            raise AudioCancelled('Package export cancelled before publication.')
        if monotonic() >= deadline:
            raise AudioError('The reviewed package exceeded its byte-verification time budget.')
        parents()

    def before_commit():
        before_check()
        current = _destination(requested, private, before_check, require_absent=True,
            expected=(approval.target, approval.parent_identity, None))
        if current != (approval.target, approval.parent_identity, None):
            raise AudioError('The reviewed package destination changed.')
        # publish_file invokes this after its blocking fsync as well. Hashing
        # catches equal-size writes even if Windows timestamps were restored.
        if _digest(staged, original, before_check, links=1) != expected_sha256:
            raise AudioError('The package differs from its inspected bytes.')
        before_check()
        return False

    failure = None
    try:
        publish_file(staged, approval.target, cancelled=before_commit)
    except Exception as exc:
        if not os.path.lexists(approval.target):
            raise
        failure = exc

    # Commit reconciliation is bounded but independent of late user Cancel.
    # It accepts only the exact two-link inode, never an existing competitor.
    deadline = monotonic() + 120

    def after_check():
        if monotonic() >= deadline:
            raise AudioError('The published package needs explicit byte verification.')
        parents()

    try:
        after_check()
        linked = approval.target.lstat()
        _regular(linked, 2)
        if ((linked.st_dev, linked.st_ino, linked.st_size, linked.st_mtime_ns)
                != (owner.st_dev, owner.st_ino, owner.st_size, owner.st_mtime_ns)):
            raise AudioError('The output is not this attempt\'s reviewed package.')
        expected = _stamp(linked)
        if _stamp(staged.lstat()) != expected:
            raise AudioError('The published package lost its exact staging link.')
        if _digest(approval.target, expected, after_check, links=2) != expected_sha256:
            raise AudioError('The published package differs from the inspected bytes.')
        after_check()
        if (_stamp(staged.lstat()) != expected
                or _stamp(approval.target.lstat()) != expected):
            raise AudioError('The published package changed after its final byte check.')
    except Exception as exc:
        if failure is not None:
            raise failure from exc
        raise AudioError('A package was linked, but its final identity could not be confirmed. '
                         'The output was retained for explicit verification.') from exc
    warning = None
    if failure is not None and not isinstance(failure, AudioCancelled):
        warning = ('The exact package was saved and verified. Later directory finalization '
                   'reported an error; retain this file and do not repeat the export.')
    return ExportReceipt(approval.target, warning)
