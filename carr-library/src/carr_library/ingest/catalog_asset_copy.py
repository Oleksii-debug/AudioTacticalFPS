"""Catalog-bound original inventory for a trusted combined database snapshot.

Not a migration, authorization API or complete backup. Caller must hold the
shared writer guard and independently validate a closed immutable snapshot,
including schema, FK, revision seals, evidence and FTS. Stable private local
parents only. Existing revision-only asset_copy stays unchanged.
"""
from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import re

from carr_library.ingest.retained_assets import (
    MAX_BYTES, SUFFIXES, RetainedAssets, RetentionError,
)


@dataclass(frozen=True)
class AssetEntry:
    digest: str
    mime: str
    size: int

    @property
    def filename(self):
        return self.digest + SUFFIXES[self.mime]


def catalog_inventory(db, *, include_all_workspaces=False,
                      max_assets=10000, max_bytes=8 * 1024**3):
    """Preserve all registered assets, including not-yet-indexed editions.

    Require the proposed V2 authoritative composite revision binding; reject
    old revision-only snapshots. Include registered but unassigned assets too.
    Limits apply to registered originals, not SQLite size or temporary space.
    """
    if include_all_workspaces is not True:
        raise RetentionError('explicit all-workspaces consent required')
    if type(max_assets) is not int or not 0 < max_assets <= 100000:
        raise RetentionError('invalid asset count limit')
    if type(max_bytes) is not int or max_bytes < 1:
        raise RetentionError('invalid asset byte limit')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RetentionError('snapshot foreign key violation')
    if db.execute('SELECT 1 FROM revision r LEFT JOIN sealed s ON s.revision=r.id '
                  'LEFT JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition '
                  'AND e.asset=r.asset WHERE s.revision IS NULL OR e.id IS NULL LIMIT 1').fetchone():
        raise RetentionError('unsealed or unbound revision')
    if db.execute('SELECT 1 FROM catalog_edition e LEFT JOIN catalog_asset a '
                  'ON a.digest=e.asset WHERE a.digest IS NULL LIMIT 1').fetchone():
        raise RetentionError('unbound catalog edition')
    rows = db.execute('SELECT digest,mime,size FROM catalog_asset ORDER BY digest LIMIT ?',
                      (max_assets + 1,)).fetchall()
    if len(rows) > max_assets:
        raise RetentionError('asset count limit exceeded')
    entries = []
    total = 0
    for digest, mime, size in rows:
        if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or mime not in SUFFIXES or type(size) is not int or not 0 < size <= MAX_BYTES):
            raise RetentionError('invalid or unsupported catalog asset manifest')
        total += size
        if total > max_bytes:
            raise RetentionError('asset byte limit exceeded')
        entries.append(AssetEntry(digest, mime, size))
    return tuple(entries)


def _check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Catalog asset operation cancelled.')


def _checked_path(store, entry, *, cancelled=lambda: False):
    _check_cancelled(cancelled)
    path = store.resolve(entry.digest)  # existing hash, file type and link checks
    _check_cancelled(cancelled)
    if path.name != entry.filename or path.stat().st_size != entry.size:
        raise RetentionError('retained asset disagrees with catalog MIME or size')
    return path


def verify_catalog_assets(db, store, *, cancelled=lambda: False, **limits):
    """Verify exact copied namespace and return catalog-derived manifest entries."""
    _check_cancelled(cancelled)
    entries = catalog_inventory(db, **limits)
    if {p.name for p in store.root.iterdir()} != {e.filename for e in entries}:
        raise RetentionError('unexpected or incomplete catalog asset namespace')
    for entry in entries:
        _checked_path(store, entry, cancelled=cancelled)
    return entries


def copy_catalog_assets(db, source, destination, *, cancelled=lambda: False, **limits):
    """Copy originals with existing retention API; never overwrite.

    Partial output on failure remains explicitly incomplete until independently
    verified. A future outer manifest must bind the DB bytes and these entries.
    Poll cancellation between bounded source reads and around the existing
    retention API. Its verification/publication processes at most one original
    (MAX_BYTES) without a callback; keep that producer's checks unchanged.
    """
    _check_cancelled(cancelled)
    entries = catalog_inventory(db, **limits)
    paths = [_checked_path(source, entry, cancelled=cancelled) for entry in entries]
    destination = Path(destination)
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    resolved = destination.resolve()
    if resolved.is_relative_to(source.root) or source.root.is_relative_to(resolved):
        raise RetentionError('asset copy destination overlaps source')
    _check_cancelled(cancelled)
    destination.mkdir(mode=0o700)
    target = RetainedAssets(destination)
    for entry, path in zip(entries, paths):
        _check_cancelled(cancelled)
        with path.open('rb') as stream, BytesIO() as buffered:
            count = 0
            while count <= entry.size:
                _check_cancelled(cancelled)
                block = stream.read(min(1024 * 1024, entry.size + 1 - count))
                _check_cancelled(cancelled)
                if not block:
                    break
                buffered.write(block)
                count += len(block)
            if count != entry.size:
                raise RetentionError('retained asset size changed')
            _check_cancelled(cancelled)
            target.retain(buffered.getvalue(), entry.mime, entry.digest)
        _check_cancelled(cancelled)
    return verify_catalog_assets(db, target, cancelled=cancelled, **limits)
