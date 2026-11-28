"""One cooperative import slice and its measured storage-growth allowance.

This is admission/accounting under the existing writer guard, not a filesystem
quota. All original-byte and publication integrity checks remain independent.
"""
from contextlib import contextmanager
from pathlib import Path
from itertools import chain
import os
import shutil
import threading

from .resource_budget import checked_budget, effective_budget
from .streamed_ingest import MAX_RECORD_BYTES
from ..durable_state import local_path, regular

# Reserve bounded room for one maximum checkpoint plus SQLite's page/index/WAL
# amplification. The declared growth allowance remains independently enforced.
WRITE_HEADROOM = 16 * MAX_RECORD_BYTES
MAX_SLICE_RECORDS = 1000
_TOKEN = object()


class ImportBudgetExceeded(ValueError):
    """This slice cannot publish beyond its exact reviewed resource allowance."""


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_nlink, getattr(info, 'st_file_attributes', 0)


def _directory_identity(info):
    # Ordinary subdirectory creation changes a POSIX directory's link count.
    # Its owned directory identity is the actual inode, mode and attributes;
    # regular files still retain their independently checked single-link count.
    return info.st_dev, info.st_ino, info.st_mode, getattr(info, 'st_file_attributes', 0)


def _present(path):
    try:
        return regular(path)
    except FileNotFoundError:
        # Validate the existing parent even when this optional SQLite sidecar is
        # absent; a denied/replaced parent is not an empty file.
        regular(local_path(path.parent), directory=True)
        return None


class ImportSession:
    """Internal capability for exactly one app, workspace, Storage and thread."""
    def __init__(self, token, app, workspace, storage, budget, cancelled, minimum_free):
        if token is not _TOKEN:
            raise TypeError('Import sessions must be opened by the current library owner.')
        checked_budget(budget)
        if not callable(cancelled):
            raise TypeError('A current import cancellation callback is required.')
        self.app, self.workspace, self.storage = app, workspace, storage
        self.budget = budget
        self.policy = effective_budget(budget)
        self.cancelled = cancelled
        self.root = local_path(app.root)
        self._root_identity = _directory_identity(regular(self.root, directory=True))
        self._thread = threading.get_ident()
        self._active = True
        self._minimum_free = minimum_free
        self._journal = None
        self._originals = {}
        self._original_bytes = 0
        self._current_original = None
        self._pending_originals = {}
        self._book_count = 0
        self._paths = tuple(self.root / (name + suffix)
            for name in ('library.sqlite', 'imports.sqlite')
            for suffix in ('', '-wal', '-shm', '-journal'))
        self._before = {}
        self._main_identity = {}
        for path in self._paths:
            info = _present(path)
            self._before[path.name] = 0 if info is None else info.st_size
            if path.name in ('library.sqlite', 'imports.sqlite') and info is not None:
                self._main_identity[path.name] = _identity(info)
        self._logical_before = {'library.sqlite': self._allocated(storage.db)}
        self.check()

    @staticmethod
    def _allocated(db):
        pages = db.execute('PRAGMA page_count').fetchone()[0]
        size = db.execute('PRAGMA page_size').fetchone()[0]
        if type(pages) is not int or type(size) is not int or pages < 0 or not 512 <= size <= 65536:
            raise ValueError('Invalid SQLite allocation accounting.')
        value = pages * size
        if value > 2 ** 63 - 1:
            raise ValueError('SQLite allocation accounting exceeded its integer range.')
        return value

    def _check_root(self):
        # The constructor resolved this exact path once. Recheck every actual
        # ancestor on every invocation; only the canonical path spelling is
        # reused, never a stat result or a previous successful validation.
        for parent in reversed(self.root.parents):
            regular(parent, directory=True)
        if _directory_identity(regular(self.root, directory=True)) != self._root_identity:
            raise ValueError('The current import root was replaced.')

    def validate(self, app, workspace, budget):
        checked_budget(budget)
        if (not self._active or threading.get_ident() != self._thread
                or app is not self.app or workspace != self.workspace
                or budget != self.budget or Path(app.root) != self.root
                or Path(app.database) != self.root / 'library.sqlite'):
            raise ValueError('The current import session owner or reviewed budget changed.')
        self._check_root()
        app._authorize(self.storage.db, workspace)
        return self.storage

    def _live(self):
        if not self._active or threading.get_ident() != self._thread:
            raise ValueError('The import session is closed or belongs to another thread.')
        if self.cancelled():
            raise InterruptedError('Import cancelled at a storage accounting boundary.')
        self._check_root()

    def begin_original(self, asset, target):
        """Reserve one source while RetainedAssets owns exact authentication.

        Existing originals are conservatively charged too. Accounting never
        preempts the canonical publisher's exact two-link crash recovery by
        opening its target before that publisher has authenticated it.
        """
        self._live()
        if self._book_count >= MAX_SLICE_RECORDS:
            raise ImportBudgetExceeded('This bounded import slice is full; continue the saved batch.')
        self._book_count += 1
        target = Path(target)
        if target.parent != self.app.assets_dir or target.name != asset.digest + target.suffix:
            raise ValueError('Original accounting is outside this session.')
        if asset.digest not in self._originals:
            self._pending_originals[asset.digest] = (target, asset.size)
        self.check()

    def finish_original(self, asset, target):
        self._live()
        info = regular(local_path(target))
        if info.st_size != asset.size:
            raise ValueError('The retained original size changed after publication.')
        self._current_original = (Path(target), asset.size, _identity(info))
        pending = self._pending_originals.pop(asset.digest, None)
        if pending is not None:
            if pending != (Path(target), asset.size):
                raise ValueError('The accounted original path changed.')
            self._originals[asset.digest] = self._current_original
            self._original_bytes += asset.size
        self.check()

    @contextmanager
    def journal(self, db):
        if self._journal is not None:
            raise ValueError('Only the current import journal may join this session.')
        self._live()
        self._journal = db
        if 'imports.sqlite' not in self._logical_before:
            # The pre-open physical size is the baseline: schema creation and
            # unflushed journal pages are charged to this slice.
            self._logical_before['imports.sqlite'] = self._before['imports.sqlite']
        try:
            self.check()
            yield
            self.check()
        finally:
            self._journal = None

    def check(self, *, final=False):
        if type(final) is not bool:
            raise TypeError('Final import accounting must be explicit.')
        self._live()
        physical = {}
        growth = 0
        for path in self._paths:
            # These eight leaves are fixed direct children of the root checked
            # above and again before return. Missing sidecars do not repeat the
            # identical ancestor traversal; all actual leaf lstats stay live.
            try:
                info = regular(path)
            except FileNotFoundError:
                info = None
            size = 0 if info is None else info.st_size
            if path.name in ('library.sqlite', 'imports.sqlite'):
                previous = self._main_identity.get(path.name)
                if previous is not None and (info is None or _identity(info) != previous):
                    raise ValueError('A current import database was replaced.')
                if previous is None and info is not None:
                    self._main_identity[path.name] = _identity(info)
            physical[path.name] = size
            growth += max(0, size - self._before[path.name])
        for name, db in (('library.sqlite', self.storage.db), ('imports.sqlite', self._journal)):
            if db is None:
                continue
            allocated = self._allocated(db)
            logical_growth = max(0, allocated - self._logical_before[name])
            main_growth = max(0, physical[name] - self._before[name])
            # WAL is counted independently above; counting both is conservative
            # and also catches SQLite pages not yet flushed to either file.
            growth += max(0, logical_growth - main_growth)
        # Only our current source participates in per-checkpoint metadata work.
        # Prior allocations are cumulative accounting, never cached authority to
        # read their text; the finite whole set is rechecked when the slice exits.
        current = () if self._current_original is None else (self._current_original,)
        retained = chain(self._originals.values(), current) if final else current
        original_parent = self.root / 'originals'
        original_directory = None
        for path, size, identity in retained:
            if path.parent != original_parent:
                raise ValueError('Original accounting is outside this session.')
            if original_directory is None:
                original_directory = _directory_identity(regular(original_parent, directory=True))
            info = regular(path)
            if info.st_size != size or _identity(info) != identity:
                raise ValueError('A newly retained original changed during this slice.')
        if (original_directory is not None
                and _directory_identity(regular(original_parent, directory=True)) != original_directory):
            raise ValueError('The retained original directory changed during this accounting check.')
        growth += self._original_bytes
        growth += sum(size for _, size in self._pending_originals.values())
        if growth > self.policy.library_bytes:
            raise ImportBudgetExceeded('The import reached its reviewed library-growth allowance; saved checkpoints remain resumable.')
        # Keep the separate one-write headroom available after actual allocations.
        if shutil.disk_usage(self.root).free < self._minimum_free + WRITE_HEADROOM:
            raise ImportBudgetExceeded('The library volume lacks the reserved import-write headroom.')
        # Bracket the complete fixed-path/accounting observation. A missing or
        # replaced ancestor is never accepted as zero-size SQLite state, and a
        # cancellation arriving during the real checks is observed before grant.
        self._live()
        return growth

    def _close(self):
        self._active = False
        self._journal = None


@contextmanager
def import_accounting(app, workspace, storage, budget, *, cancelled=lambda: False,
                      minimum_free=0):
    """Borrow the current writer lock and canonical Storage, without publishing."""
    checked_budget(budget)
    if budget is None:
        yield None
        return
    if type(minimum_free) is not int or not 0 <= minimum_free <= 2 ** 63 - 1:
        raise ValueError('Invalid free-disk reserve.')
    app._authorize(storage.db, workspace)
    session = ImportSession(_TOKEN, app, workspace, storage, budget, cancelled, minimum_free)
    try:
        yield session
        session.check(final=True)
    finally:
        session._close()
