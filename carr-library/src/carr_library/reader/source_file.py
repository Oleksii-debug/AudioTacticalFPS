"""Explicit original-file viewing through fresh workspace authorization.

The default viewer receives a byte-exact private copy, never a retained original
that an editor could overwrite. This helper does not approve extracted text,
printed page labels or citations. Private local directories must remain stable;
it is not a sandbox against other programs running as the same user.
"""
from contextlib import contextmanager
import ctypes
import sys
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import stat
from uuid import uuid4

from ..catalog.intake import IntakeError, snapshot
from ..citations.anchors import Identity, Locator
from ..ingest.retained_assets import RetainedAssets, SUFFIXES
from ..ingest.writer_guard import exclusive_writer
from ..research.topic_tree import TopicTree
from ..extract.document_formats import EXTENDED_MIMES
from ..durable_state import sync_directory as _sync_directory
from ..ingest.streamed_text import (catalog_profile, file_identity, verified_reader, file_digest)
from ..native_file_metadata import stream_change_time


VIEW_DIRECTORY = 'source-views'


@dataclass(frozen=True)
class SourceFile:
    path: Path
    identity: Identity
    locator: Locator
    ref: str
    title: str
    mime: str



@dataclass(frozen=True)
class _FileOriginal:
    """An in-process, catalog-bound descriptor; never saved as path authority."""
    path: Path
    digest: str
    mime: str
    size: int
    profile: object
    identity: tuple

    def manifest(self):
        return dict(sha256=self.digest, mime=self.mime, size=self.size)


def _check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Opening the original was cancelled.')


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
    return info


def _original(app, db, workspace, ref, revision, *, cancelled=lambda: False):
    _, identity, locator, _, metadata = app._selected(db, workspace, ref, revision, cancelled=cancelled)
    row = db.execute('SELECT a.mime,a.size FROM catalog_edition e '
        'JOIN catalog_asset a ON a.digest=e.asset WHERE e.workspace=? AND e.id=? AND e.asset=?',
        (workspace, identity.edition_id, identity.asset_sha256)).fetchone()
    if row is None or row[0] not in SUFFIXES:
        raise ValueError('The selected original has no supported source format.')
    root = _directory(app.root)
    assets = _directory(app.assets_dir)
    if assets != root / 'originals':
        raise ValueError('The original source directory changed.')
    profile = catalog_profile(db, identity.asset_sha256, row[0], row[1])
    path = (RetainedAssets(assets).resolve(identity.asset_sha256)
            if profile is None else RetainedAssets(assets).resolve(
                identity.asset_sha256, profile=profile, cancelled=cancelled))
    if path.suffix != SUFFIXES[row[0]]:
        raise ValueError('The original source format changed.')
    if profile is None:
        original = snapshot(assets, path.name)
        _regular(path)
    else:
        info = _regular(path)
        if info.st_size != row[1]:
            raise ValueError('The original source differs from its catalog record.')
        original = _FileOriginal(path, identity.asset_sha256, row[0], row[1],
                                 profile, file_identity(info))
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


def _view_matches(directory, path, original, *, cancelled=lambda: False):
    if path.parent != directory or path.suffix != SUFFIXES[original.mime]:
        raise ValueError('Invalid source viewing path.')
    info = _regular(path)
    if isinstance(original, _FileOriginal):
        _check_cancelled(cancelled)
        if info.st_size != original.size:
            return False
        return file_digest(path, maximum_bytes=original.profile.maximum_bytes,
                           cancelled=cancelled) == original.digest
    try:
        copied = snapshot(directory, path.name)
    except IntakeError:
        # A viewer may have saved a different format. Preserve that regular file
        # and allow prepare_source_file to create a fresh exact copy elsewhere.
        _regular(path)
        return False
    _regular(path)
    return copied.manifest() == original.manifest()




@contextmanager
def _source_admission(app, workspace, ref, revision, cancelled, *, copying, authorized_topic=None):
    """Reserve the bounded streamed path before hashing or copying its bytes."""
    _check_cancelled(cancelled)
    with app.read(workspace) as db:
        if authorized_topic is not None:
            TopicTree(db, workspace)._visible(authorized_topic)
        selected = app._selections.get(ref)
        _, identity, locator, _, _ = app._stored_selection(db, workspace, selected, revision)
        row = db.execute('SELECT a.mime,a.size FROM catalog_edition e '
            'JOIN catalog_asset a ON a.digest=e.asset '
            'WHERE e.workspace=? AND e.id=? AND e.asset=?',
            (workspace, identity.edition_id, identity.asset_sha256)).fetchone()
        if row is None:
            raise ValueError('The original source is outside the selected library.')
        profile = catalog_profile(db, identity.asset_sha256, row[0], row[1])
        size = row[1]
    if profile is None:
        yield cancelled
        return
    from ..operations import ResourceBroker, ResourceRequest
    from ..operations.models import ResourceCancelled
    broker = ResourceBroker.for_app(app.root)
    # Declared incremental working memory for two bounded buffers, text/SQLite
    # validation and hashes. This is not an OS memory sandbox or measured RSS.
    request = ResourceRequest(cpu_slots=1, memory_bytes=16 * 1024**2,
        disk_bytes=size + 16 * 1024**2 if copying else 0,
        disk_path=app.root if copying else None)
    try:
        with broker.reserve(request, operation_id='document-view:' + uuid4().hex,
                            cancelled=cancelled, timeout=60) as lease:
            with app.read(workspace) as db:
                if authorized_topic is not None:
                    TopicTree(db, workspace)._visible(authorized_topic)
                current = app._stored_selection(db, workspace, app._selections.get(ref), revision)
                if current[0] != selected or current[1:3] != (identity, locator):
                    raise ValueError('The selected source changed while waiting for shared resources.')
            def stopped():
                _check_cancelled(lease.cancelled)
                lease.checkpoint()
                return False
            yield stopped
    except ResourceCancelled as exc:
        raise InterruptedError('Opening the original was cancelled before shared resource admission.') from exc


@contextmanager
def _held_verified_file(path, digest, size, cancelled):
    """Keep the exact native reader open across the final UI handoff."""
    info = _regular(path)
    identity = file_identity(info)
    if info.st_size != size:
        raise ValueError('The source file changed before native launch.')
    with verified_reader(path, expected_identity=identity) as stream:
        native = stream_change_time(stream)
        hashed, remaining = sha256(), size
        while remaining:
            _check_cancelled(cancelled)
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError('The source file was truncated before native launch.')
            hashed.update(block)
            remaining -= len(block)
        if stream.read(1) or hashed.hexdigest() != digest:
            raise ValueError('The source file changed before native launch.')

        def check():
            _check_cancelled(cancelled)
            if (file_identity(_regular(path)) != identity
                    or file_identity(os.fstat(stream.fileno())) != identity
                    or stream_change_time(stream) != native):
                raise ValueError('The verified source file changed before native launch.')

        check()
        yield check


def prepare_source_file(app, workspace, ref, revision, *, authorized_topic=None,
                        cancelled=lambda: False):
    """Prepare a separately owned viewing copy through the existing authority."""
    with _source_admission(app, workspace, ref, revision, cancelled, copying=True,
                           authorized_topic=authorized_topic) as stopped:
        return _prepare_source_file(app, workspace, ref, revision,
                                    authorized_topic=authorized_topic, cancelled=stopped)


def _publish_view(stage, target, check):
    """Donor's native atomic no-replace move, with this copy's exact guard."""
    with stage.open('r+b') as stream:
        os.fsync(stream.fileno())
    check()
    if os.name == 'nt':
        os.rename(stage, target)
    elif sys.platform.startswith('linux'):
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, 'renameat2', None)
        if rename is None:
            raise RuntimeError('This runtime does not support atomic no-replace publication.')
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(stage), -100, os.fsencode(target), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(target))
    else:
        raise RuntimeError('Atomic source viewing publication supports Windows and Linux.')


def _prepare_streamed_view(directory, path, original, cancelled):
    """Copy bounded chunks; an interrupted move never exposes partial bytes."""
    directory = _directory(directory)
    if path.parent != directory:
        raise ValueError('The viewing destination left its private directory.')
    parent = directory.stat()
    parent_identity = parent.st_dev, parent.st_ino
    stage = directory / ('.view-stage-' + uuid4().hex + '.txt')
    owned = None
    def check_parent():
        current = _directory(directory).stat()
        if (current.st_dev, current.st_ino) != parent_identity:
            raise ValueError('The source viewing directory changed.')
    def exact_stage():
        check_parent()
        info = _regular(stage)
        if file_identity(info) != owned:
            raise ValueError('The private source viewing stage changed.')
        return info
    try:
        with stage.open('xb') as output:
            owned = file_identity(os.fstat(output.fileno()))
            with verified_reader(original.path, expected_identity=original.identity) as incoming:
                remaining = original.size
                hashed = sha256()
                while remaining:
                    _check_cancelled(cancelled)
                    block = incoming.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError('The original source was truncated during viewing preparation.')
                    hashed.update(block)
                    output.write(block)
                    remaining -= len(block)
                _check_cancelled(cancelled)
                if incoming.read(1) or hashed.hexdigest() != original.digest:
                    raise ValueError('The original source changed during viewing preparation.')
            output.flush()
            os.fsync(output.fileno())
            owned = file_identity(os.fstat(output.fileno()))
        if not _view_matches(directory, stage, original, cancelled=cancelled):
            raise ValueError('The source viewing copy could not be verified.')
        def check():
            _check_cancelled(cancelled)
            exact_stage()
            if not _view_matches(directory, stage, original, cancelled=cancelled):
                raise ValueError('The source viewing stage changed before publication.')
            exact_stage()
        _publish_view(stage, path, check)
        # A committed move is complete even if cancellation arrives now. Verify
        # the exact inode and bytes before returning its viewing capability.
        check_parent()
        final = file_identity(_regular(path))
        if (final[:4] != owned[:4]
                or not _view_matches(directory, path, original)):
            raise ValueError('The published viewing copy changed; it was retained without launch authority.')
        _sync_directory(directory)
    finally:
        if owned is not None:
            try:
                current = stage.lstat()
            except FileNotFoundError:
                pass
            else:
                check_parent()
                # A partial write legitimately changes size/timestamps. Only
                # this attempt's exact unaliased inode may be removed.
                _regular(stage)
                if (current.st_dev, current.st_ino) != owned[:2]:
                    raise ValueError('A foreign source viewing stage was preserved.')
                stage.unlink()
                _sync_directory(directory)


def _prepare_source_file(app, workspace, ref, revision, *, authorized_topic=None,
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
            identity, locator, title, original = _original(app, db, workspace, ref, revision,
                                                         cancelled=cancelled)
            if cancelled():
                raise InterruptedError('Opening the original was cancelled.')
            directory = _views(app, create=True)
            path = directory / (original.digest + SUFFIXES[original.mime])
            if os.path.lexists(path):
                if _view_matches(directory, path, original, cancelled=cancelled):
                    if isinstance(original, _FileOriginal):
                        _sync_directory(directory)
                    if cancelled():
                        raise InterruptedError('Opening the original was cancelled.')
                    return SourceFile(path, identity, locator, ref, title, original.mime)
                path = directory / (original.digest + '-' + uuid4().hex + SUFFIXES[original.mime])
            if isinstance(original, _FileOriginal):
                _prepare_streamed_view(directory, path, original, cancelled)
                return SourceFile(path, identity, locator, ref, title, original.mime)
            # No hard links: an external editor must never gain the original inode.
            created = False
            try:
                with path.open('xb') as stream:
                    created = True
                    stream.write(original.data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if not _view_matches(directory, path, original):
                    raise ValueError('The source viewing copy could not be verified.')
                if cancelled():
                    raise InterruptedError('Opening the original was cancelled.')
                return SourceFile(path, identity, locator, ref, title, original.mime)
            except BaseException:
                if created:
                    path.unlink(missing_ok=True)
                raise


def launch_source_file(app, source, opener, *, authorized_topic=None,
                       cancelled=lambda: False, native_dispatch=None):
    """Validate on the caller's worker, then explicitly hand off the native open.

    A UI supplies native_dispatch(action): it must run the action on its event
    thread and wait for its result. Exact original and viewing-file readers stay
    open through that handoff. The action performs only fresh scope checks and
    native file-identity checks before calling opener; large byte hashes have
    already completed on the worker. Direct non-UI callers retain their API.
    """
    if not isinstance(source, SourceFile) or not callable(opener):
        raise ValueError('Select an original source file to open.')
    if native_dispatch is not None and not callable(native_dispatch):
        raise ValueError('Native source launch needs a callable event-thread dispatcher.')
    if source.mime in EXTENDED_MIMES:
        raise ValueError('Read this document with Reading Desk (Ctrl+R). Its exact original is retained, '
                         'but external viewers may execute active content or load remote resources.')
    with _source_admission(app, source.identity.workspace, source.ref,
                           source.identity.revision, cancelled, copying=False,
                           authorized_topic=authorized_topic) as stopped:
        with app.read(source.identity.workspace) as db:
            if authorized_topic is not None:
                TopicTree(db, source.identity.workspace)._visible(authorized_topic)
            identity, locator, _, original = _original(app, db, source.identity.workspace,
                source.ref, source.identity.revision, cancelled=stopped)
            if identity != source.identity or locator != source.locator or original.mime != source.mime:
                raise ValueError('The selected source changed. Search again before opening it.')
            directory = _views(app)
            if not _view_matches(directory, source.path, original, cancelled=stopped):
                raise ValueError('The source viewing copy changed. Open the source again.')
            selected = app._selections.get(source.ref)
        original_path = app.assets_dir / (original.digest + SUFFIXES[original.mime])
        with _held_verified_file(original_path, original.digest, original.size, stopped) as original_check:
            with _held_verified_file(source.path, original.digest, original.size, stopped) as copy_check:
                def native_action():
                    original_check()
                    copy_check()
                    # A new transaction on the event thread; no connection is
                    # borrowed from the worker and no original is read here.
                    with app.read(source.identity.workspace) as current:
                        if authorized_topic is not None:
                            TopicTree(current, source.identity.workspace)._visible(authorized_topic)
                        binding = app._stored_selection(current, source.identity.workspace,
                            app._selections.get(source.ref), source.identity.revision)
                        manifest = current.execute('SELECT a.mime,a.size FROM catalog_edition e '
                            'JOIN catalog_asset a ON a.digest=e.asset '
                            'WHERE e.workspace=? AND e.id=? AND e.asset=?',
                            (identity.workspace, identity.edition_id, identity.asset_sha256)).fetchone()
                        if (binding[0] != selected or binding[1:3] != (identity, locator)
                                or manifest is None or tuple(manifest) != (original.mime, original.size)):
                            raise ValueError('The selected source changed before native launch.')
                        original_check()
                        copy_check()
                        if opener(str(source.path)) is not True:
                            raise ValueError('No application could open this file. Choose a default viewer for its format in Windows.')
                    return True

                if native_dispatch is None:
                    native_action()
                elif native_dispatch(native_action) is not True:
                    raise ValueError('The native source launch was not acknowledged.')
