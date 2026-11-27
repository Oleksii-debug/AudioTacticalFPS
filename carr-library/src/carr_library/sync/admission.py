"""Size-based admission on the application's existing cooperative broker.

These are declared peak estimates, not a memory sandbox. The source is measured
again after waiting. No secret, transfer label, or filesystem path becomes an
operation identifier. The passive listing/confirmation paths never create a broker.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
from uuid import uuid4

from ..audio.cleanup import retained_context
from ..operations import ResourceBroker, ResourceRequest
from .archive import MAX_FILES, MAX_TOTAL, cancelled_check, local_path

MIB = 1024 ** 2
# 128 MiB scrypt working set plus bounded crypto/archive/SQLite buffers.
MEMORY_BYTES = 256 * MIB
RESERVE_BYTES = 64 * MIB


def tree_bytes(root, *, cancelled=lambda: False, roots=None, with_count=False):
    """Read metadata only, with finite traversal and no linked-file admission."""
    root = local_path(root, exists=True, directory=True)
    total, seen, files = 0, 0, 0
    pending = [root] if roots is None else [root / name for name in roots]
    while pending:
        cancelled_check(cancelled)
        path = pending.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            if roots is not None and path.parent == root:
                continue
            raise
        seen += 1
        if seen > MAX_FILES:
            raise ValueError('Portable preparation inventory exceeds its entry budget.')
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Portable preparation cannot follow linked inputs.')
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as iterator:
                for entry in iterator:
                    cancelled_check(cancelled)
                    # Budget discovery before retaining directory entries.
                    if seen + len(pending) >= MAX_FILES:
                        raise ValueError('Portable preparation inventory exceeds its entry budget.')
                    pending.append(Path(entry.path))
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            total += info.st_size
            files += 1
            if total > MAX_TOTAL:
                raise ValueError('Portable preparation exceeds its byte budget.')
        else:
            raise ValueError('Portable preparation needs regular private inputs.')
    return (total, files) if with_count else total


def library_bytes(app, *, cancelled=lambda: False):
    # Current full-backup producer families only. Runtime/model directories and
    # external references are not traversed. The authoritative backup still
    # performs its own exact typed inventory, hashes, locks and source checks.
    from ..backup_sidecars import _TYPED_ROOT_LIMITS
    names = ('library.sqlite', 'library.sqlite-wal', 'originals', 'state',
             'reading-state.json', 'connections.json', 'keybindings.json',
             'ocr-language-presets.json', 'studio', 'assistant', 'provider-jobs',
             'imports.sqlite', 'imports.sqlite-wal', 'import-requests',
             'import-batches', 'pending-originals', *_TYPED_ROOT_LIMITS)
    return tree_bytes(app.root, roots=tuple(dict.fromkeys(names)), cancelled=cancelled)


@contextmanager
def admitted(app, *, measured_bytes, staging_parent, additional=(),
             broker=None, cancelled=lambda: False, recheck=None):
    if type(measured_bytes) is not int or not 0 <= measured_bytes <= MAX_TOTAL:
        raise ValueError('A bounded measured transfer size is required.')
    # Stored-only ZIP and the authenticated envelope cannot expand without
    # bound. This covers backup, archive, decrypted/unpacked and checked copies.
    peak = 8 * measured_bytes + RESERVE_BYTES
    request = ResourceRequest(cpu_slots=1, memory_bytes=MEMORY_BYTES,
        disk_bytes=peak, disk_path=staging_parent,
        additional_disks=tuple((path, amount) for path, amount in additional))
    owner = broker if broker is not None else ResourceBroker.for_app(app.root)
    with retained_context(owner.reserve(request, operation_id='sync-' + uuid4().hex,
                                       cancelled=cancelled, timeout=60)) as lease:
        lease.checkpoint()
        if recheck is not None and recheck() > measured_bytes:
            raise ValueError('Portable source grew while waiting; review and retry.')
        yield lease
