"""Explicit original-file viewing through fresh workspace authorization.

The default viewer receives a byte-exact private copy, never a retained original
that an editor could overwrite. This helper does not approve extracted text,
printed page labels or citations. Private local directories must remain stable;
it is not a sandbox against other programs running as the same user.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from uuid import uuid4

from ..catalog.intake import IntakeError
from ..catalog.streaming_pdf import FileAsset, snapshot_original
from ..citations.anchors import Identity, Locator
from ..ingest.retained_assets import RetainedAssets, SUFFIXES
from ..ingest.writer_guard import exclusive_writer
from ..research.topic_tree import TopicTree
from ..extract.document_formats import EXTENDED_MIMES


VIEW_DIRECTORY = 'source-views'


@dataclass(frozen=True)
class SourceFile:
    path: Path
    identity: Identity
    locator: Locator
    ref: str
    title: str
    mime: str


def _directory(path):
    """Reject existing aliases before resolve() can hide them."""
    path = Path(path).absolute()
    for current in (path, *path.parents):
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Source viewing requires a regular local directory.')
    return path.resolve(strict=True)


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('Source viewing requires an unlinked regular file.')


def _original(app, db, workspace, ref, revision, cancelled=lambda: False):
    _, identity, locator, _, metadata = app._selected(db, workspace, ref, revision)
    row = db.execute('SELECT a.mime,a.size FROM catalog_edition e '
        'JOIN catalog_asset a ON a.digest=e.asset WHERE e.workspace=? AND e.id=? AND e.asset=?',
        (workspace, identity.edition_id, identity.asset_sha256)).fetchone()
    if row is None or row[0] not in SUFFIXES:
        raise ValueError('The selected original has no supported source format.')
    root = _directory(app.root)
    assets = _directory(app.assets_dir)
    if assets != root / 'originals':
        raise ValueError('The original source directory changed.')
    path = RetainedAssets(assets).resolve(identity.asset_sha256, cancelled=cancelled)
    if path.suffix != SUFFIXES[row[0]]:
        raise ValueError('The original source format changed.')
    original = snapshot_original(assets, path.name, cancelled=cancelled)
    _regular(path)
    if (original.digest, original.mime, original.size) != (identity.asset_sha256, row[0], row[1]):
        raise ValueError('The original source differs from its catalog record.')
    return identity, locator, metadata.title, original


def _views(app, *, create=False):
    root = _directory(app.root)
    directory = root / VIEW_DIRECTORY
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    if _directory(directory) != root / VIEW_DIRECTORY:
        raise ValueError('The source viewing directory changed.')
    return directory


def _view_matches(directory, path, original, cancelled=lambda: False):
    if path.parent != directory or path.suffix != SUFFIXES[original.mime]:
        raise ValueError('Invalid source viewing path.')
    _regular(path)
    try:
        copied = snapshot_original(directory, path.name, cancelled=cancelled)
    except IntakeError:
        # A viewer may have saved a different format. Preserve that regular file
        # and allow prepare_source_file to create a fresh exact copy elsewhere.
        _regular(path)
        return False
    _regular(path)
    return copied.manifest() == original.manifest()


def prepare_source_file(app, workspace, ref, revision, *, authorized_topic=None,
                        cancelled=lambda: False):
    """Prepare the selected retained bytes for an explicit Open original action.

    A previously unchanged copy is reused. A viewer-edited copy is preserved and
    a new filename is used, so neither the original nor a user's edits are lost.
    Copies live in private source-views and are excluded from research backups.
    """
    if cancelled():
        raise InterruptedError('Opening the original was cancelled.')
    with exclusive_writer(app.root):
        with app.read(workspace) as db:
            if authorized_topic is not None:
                TopicTree(db,workspace)._visible(authorized_topic)
            identity, locator, title, original = _original(app, db, workspace, ref, revision, cancelled)
            if cancelled():
                raise InterruptedError('Opening the original was cancelled.')
            directory = _views(app, create=True)
            path = directory / (original.digest + SUFFIXES[original.mime])
            if os.path.lexists(path):
                if _view_matches(directory, path, original, cancelled):
                    if cancelled():
                        raise InterruptedError('Opening the original was cancelled.')
                    return SourceFile(path, identity, locator, ref, title, original.mime)
                path = directory / (original.digest + '-' + uuid4().hex + SUFFIXES[original.mime])
            # No hard links: an external editor must never gain the original inode.
            created = False
            owned = None
            try:
                if isinstance(original, FileAsset):
                    owned = original.copy_to(path, cancelled=cancelled)
                    created = True
                else:
                    with path.open('xb') as stream:
                        created = True
                        owned = os.fstat(stream.fileno())
                        stream.write(original.data)
                        stream.flush()
                        os.fsync(stream.fileno())
                if not _view_matches(directory, path, original, cancelled):
                    raise ValueError('The source viewing copy could not be verified.')
                if cancelled():
                    raise InterruptedError('Opening the original was cancelled.')
                return SourceFile(path, identity, locator, ref, title, original.mime)
            except BaseException:
                if created and owned is not None and owned.st_ino:
                    try:
                        current = path.lstat()
                        if (stat.S_ISREG(current.st_mode) and current.st_nlink == 1
                                and not getattr(current, 'st_file_attributes', 0) & 0x400
                                and (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino)):
                            path.unlink()
                    except OSError:
                        pass
                raise


def launch_source_file(app, source, opener, *, authorized_topic=None,
                       cancelled=lambda: False):
    """Recheck authorization and bytes at the explicit native-launch boundary.

    The host supplies its native file-opening callback. No shell, command line,
    webbrowser, PDF URL fragments or automatic launches are constructed here.
    The caller must also check that its displayed selection is still current.
    """
    if not isinstance(source, SourceFile) or not callable(opener):
        raise ValueError('Select an original source file to open.')
    if source.mime in EXTENDED_MIMES:
        raise ValueError('Read this document with Reading Desk (Ctrl+R). Its exact original is retained, '
                         'but external viewers may execute active content or load remote resources.')
    if cancelled():
        raise InterruptedError('Opening the original was cancelled.')
    with app.read(source.identity.workspace) as db:
        if authorized_topic is not None:
            TopicTree(db,source.identity.workspace)._visible(authorized_topic)
        identity, locator, _, original = _original(app, db, source.identity.workspace,
            source.ref, source.identity.revision, cancelled)
        if identity != source.identity or locator != source.locator or original.mime != source.mime:
            raise ValueError('The selected source changed. Search again before opening it.')
        directory = _views(app)
        if not _view_matches(directory, source.path, original, cancelled):
            raise ValueError('The source viewing copy changed. Open the source again.')
        if cancelled():
            raise InterruptedError('Opening the original was cancelled.')
        if opener(str(source.path)) is not True:
            raise ValueError('No application could open this file. Choose a default viewer for its format in Windows.')
