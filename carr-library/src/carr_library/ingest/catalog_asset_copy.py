"""Catalog-bound original inventory for a trusted combined database snapshot.

Not a migration, authorization API or complete backup. Caller must hold the
shared writer guard and independently validate a closed immutable snapshot,
including schema, FK, revision seals, evidence and FTS. Stable private local
parents only. Existing revision-only asset_copy stays unchanged.
"""
from dataclasses import dataclass
from ..catalog.intake import source_limit, source_snapshot
from ..backup_budget import check_metadata_memory, checked_budget
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
                      max_assets=10000, max_bytes=8 * 1024**3, max_source_bytes=MAX_BYTES,
                      cancelled=lambda: False, metadata_budget=None):
    """Preserve all registered assets, including not-yet-indexed editions.

    Require the proposed V2 authoritative composite revision binding; reject
    old revision-only snapshots. Include registered but unassigned assets too.
    Limits apply to registered originals, not SQLite size or temporary space.
    """
    if include_all_workspaces is not True:
        raise RetentionError('explicit all-workspaces consent required')
    if type(max_assets) is not int or not 0 < max_assets <= 2**31-1:
        raise RetentionError('invalid asset count limit')
    if type(max_bytes) is not int or max_bytes < 1:
        raise RetentionError('invalid asset byte limit')
    source_limit('text/plain', max_source_bytes)
    checked_budget(metadata_budget)
    if not callable(cancelled):
        raise TypeError('A catalog inventory cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Catalog original inventory cancelled.')
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
                      (max_assets + 1,))
    entries = []
    total, wire_bytes = 0, 0
    for index, (digest, mime, size) in enumerate(rows):
        if cancelled():
            raise InterruptedError('Catalog original inventory cancelled.')
        if index >= max_assets:
            raise RetentionError('asset count limit exceeded')
        if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or type(mime) is not str or mime not in SUFFIXES or type(size) is not int
                or not 0 < size <= source_limit(mime, max_source_bytes)):
            raise RetentionError('invalid or unsupported catalog asset manifest')
        total += size
        if total > max_bytes:
            raise RetentionError('asset byte limit exceeded')
        # Account known manifest fields and Python row/container overhead before
        # retaining another entry, rather than waiting for final JSON encoding.
        wire_bytes += 256 + len(mime)
        check_metadata_memory(wire_bytes, len(entries) + 1, metadata_budget)
        entries.append(AssetEntry(digest, mime, size))
    return tuple(entries)


def _checked_path(store, entry, *, cancelled=lambda: False, max_source_bytes=MAX_BYTES):
    path = store.resolve(entry.digest, cancelled=cancelled, max_bytes=max_source_bytes)
    if path.name != entry.filename or path.stat().st_size != entry.size:
        raise RetentionError('retained asset disagrees with catalog MIME or size')
    return path


def verify_catalog_assets(db, store, *, cancelled=lambda: False, **limits):
    """Verify exact copied namespace and return catalog-derived manifest entries."""
    entries = catalog_inventory(db, cancelled=cancelled, **limits)
    missing = {entry.filename for entry in entries}
    with os.scandir(store.root) as children:
        for child in children:
            if cancelled():
                raise InterruptedError('Catalog original inventory cancelled.')
            if child.name not in missing:
                raise RetentionError('unexpected or incomplete catalog asset namespace')
            missing.remove(child.name)
    if missing:
        raise RetentionError('unexpected or incomplete catalog asset namespace')
    for entry in entries:
        _checked_path(store, entry, cancelled=cancelled,
                      max_source_bytes=limits.get('max_source_bytes', MAX_BYTES))
    return entries


def copy_catalog_assets(db, source, destination, *, cancelled=lambda: False, **limits):
    """Copy originals with existing retention API; never overwrite.

    Partial output on failure remains explicitly incomplete until independently
    verified. A future outer manifest must bind the DB bytes and these entries.
    """
    entries = catalog_inventory(db, cancelled=cancelled, **limits)
    maximum = limits.get('max_source_bytes', MAX_BYTES)
    destination = Path(destination)
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    resolved = destination.resolve()
    if resolved.is_relative_to(source.root) or source.root.is_relative_to(resolved):
        raise RetentionError('asset copy destination overlaps source')
    destination.mkdir(mode=0o700)
    target = RetainedAssets(destination)
    for entry in entries:
        if cancelled():
            raise InterruptedError('Original copy cancelled.')
        path = _checked_path(source, entry, cancelled=cancelled, max_source_bytes=maximum)
        captured = source_snapshot(path.parent, path.name, max_bytes=maximum,
                                   file_backed=True, cancelled=cancelled)
        if captured.manifest() != dict(sha256=entry.digest, mime=entry.mime, size=entry.size):
            raise RetentionError('retained asset size or MIME changed')
        target.retain_snapshot(captured, max_bytes=maximum, cancelled=cancelled)
    return verify_catalog_assets(db, target, cancelled=cancelled, **limits)
