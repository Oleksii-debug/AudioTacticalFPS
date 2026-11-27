"""Copy the originals of a trusted, already validated sealed revision snapshot.

Internal helper, not a complete backup or a database validator. The caller
must hold the cooperative writer guard, use a stable private filesystem and
an independently validated, immutable revision snapshot connection. Do not
expose this all-workspace operation directly to unscoped UI.
"""
import os
from pathlib import Path
from ..catalog.streaming_pdf import FileAsset

from carr_library.ingest.retained_assets import MAX_BYTES, SUFFIXES, RetainedAssets, RetentionError


def _references(db, consent, max_assets):
    if consent is not True:
        raise RetentionError('explicit all-workspaces consent required')
    if type(max_assets) is not int or not 0 < max_assets <= 100000:
        raise RetentionError('invalid asset count limit')
    if db.execute('SELECT 1 FROM revision r LEFT JOIN sealed s ON s.revision=r.id '
                  'WHERE s.revision IS NULL LIMIT 1').fetchone():
        raise RetentionError('unsealed revision in snapshot')
    rows = db.execute('SELECT DISTINCT r.asset FROM revision r JOIN sealed s '
                      'ON s.revision=r.id ORDER BY r.asset LIMIT ?', (max_assets + 1,)).fetchall()
    if len(rows) > max_assets:
        raise RetentionError('asset count limit exceeded')
    return [row[0] for row in rows]


def _inventory(db, store, consent, max_assets, max_bytes, cancelled=lambda: False):
    if type(max_bytes) is not int or max_bytes < 1:
        raise RetentionError('invalid asset byte limit')
    total = 0
    paths = []
    for digest in _references(db, consent, max_assets):
        if cancelled():
            raise InterruptedError('Original backup inventory cancelled.')
        path = store.resolve(digest, cancelled=cancelled)
        total += path.stat().st_size
        if total > max_bytes:
            raise RetentionError('asset byte limit exceeded')
        paths.append(path)
    return paths


def verify_revision_assets(db, store, *, include_all_workspaces=False,
                           max_assets=10000, max_bytes=8*1024**3, cancelled=lambda: False):
    """Verify exact namespace, including historical (not only active) revisions."""
    paths = _inventory(db, store, include_all_workspaces, max_assets, max_bytes, cancelled)
    if {p.name for p in store.root.iterdir()} != {p.name for p in paths}:
        raise RetentionError('unexpected or incomplete copied asset namespace')
    return len(paths)


def copy_revision_assets(db, source, destination, *, include_all_workspaces=False,
                         max_assets=10000, max_bytes=8*1024**3, cancelled=lambda: False):
    """Copy referenced originals once to a fresh directory, never overwrite.

    Existing retention handles hash validation and per-file atomic publication.
    A failure after mkdir may leave an incomplete directory; never treat it as
    a completed backup or automatically delete it. Verify before using it.
    """
    paths = _inventory(db, source, include_all_workspaces, max_assets, max_bytes, cancelled)
    destination = Path(destination)
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    resolved = destination.resolve()
    if resolved.is_relative_to(source.root) or source.root.is_relative_to(resolved):
        raise RetentionError('asset copy destination overlaps source')
    destination.mkdir(mode=0o700)
    target = RetainedAssets(destination)
    mime_for_suffix = {suffix: mime for mime, suffix in SUFFIXES.items()}
    for path in paths:
        if cancelled():
            raise InterruptedError('Original backup copy cancelled.')
        if path.suffix == '.pdf' and path.stat().st_size > MAX_BYTES:
            asset = FileAsset.capture(path, cancelled=cancelled)
            if asset.digest != path.stem:
                raise RetentionError('Original backup digest changed.')
            target.retain_file(asset, cancelled=cancelled)
            continue
        with path.open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
        target.retain(data, mime_for_suffix[path.suffix], path.stem)
    return verify_revision_assets(db, target, include_all_workspaces=include_all_workspaces,
                                  max_assets=max_assets, max_bytes=max_bytes, cancelled=cancelled)
