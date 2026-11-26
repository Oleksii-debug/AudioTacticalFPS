"""Cooperative local writer session; not a hostile-filesystem lock or storage CAS."""
from contextlib import contextmanager, closing
from importlib.metadata import version
from pathlib import Path
import re
import stat
import sys

LOCK_NAME = '.carr-ingest-writer.lock'


class WriterGuardError(ValueError):
    pass


class WriterBusy(WriterGuardError):
    pass


def _private_root(root):
    path = Path(root)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise WriterGuardError('writer root must be an existing local directory')
    return path.resolve(strict=True)


def _regular_if_present(path, *, empty=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            getattr(info, 'st_file_attributes', 0) & 0x400 or
            (empty and info.st_size != 0)):
        raise WriterGuardError('unexpected writer namespace entry')


@contextmanager
def exclusive_writer(root, *, timeout=0):
    """Serialize cooperating writers using the same private local root.

    All callers must use this guard before opening a writable database. Local
    trusted stable directories only; never use network filesystems. Direct
    storage calls and hostile external writers are not protected.
    """
    if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
        raise WriterGuardError('timeout must be between 0 and 30 seconds')
    root = _private_root(root)
    path = root / LOCK_NAME
    _regular_if_present(path, empty=True)
    try:
        from filelock import FileLock, Timeout, UnixFileLock, WindowsFileLock
    except ImportError as exc:
        raise WriterGuardError('optional filelock dependency unavailable') from exc
    if version('filelock') != '3.29.0':
        raise WriterGuardError('unreviewed filelock version')
    expected = WindowsFileLock if sys.platform == 'win32' else UnixFileLock
    if FileLock is not expected:
        raise WriterGuardError('native hard file locking unavailable')
    lock = FileLock(path, mode=0o600)
    try:
        acquired = lock.acquire(timeout=timeout)
    except Timeout as exc:
        raise WriterBusy('another library writer holds the lock') from exc
    with acquired:
        # filelock can switch to a soft lock if flock is not supported.
        if type(lock) is not expected:
            raise WriterGuardError('native hard file locking unavailable')
        yield root


@contextmanager
def open_ingest_session(root, storage_path, journal_path, *, timeout=0):
    """Open existing candidate services under a single cooperative guard.

    Paths must be distinct direct children of the chosen private root, with
    explicit caller-supplied names. Creating a new database is intentional
    when a supplied path does not exist. Close both before releasing the lock.
    """
    with exclusive_writer(root, timeout=timeout) as base:
        paths = [Path(p).absolute() for p in (storage_path, journal_path)]
        if any(p.parent.resolve(strict=True) != base for p in paths):
            raise WriterGuardError('database must be inside writer root')
        paths = [base / p.name for p in paths]
        # Internal filenames are deliberately portable, including on Windows.
        reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                    *(f'LPT{i}' for i in range(1, 10))}
        if (paths[0].name.casefold() == paths[1].name.casefold() or
                any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.(?:sqlite|db)', p.name)
                    or p.stem.upper() in reserved for p in paths)):
            raise WriterGuardError('database names must be distinct and portable')
        for path in paths:
            _regular_if_present(path)
            for suffix in ('-wal', '-shm', '-journal'):
                _regular_if_present(Path(str(path) + suffix))
        from carr_library.storage.revisions import Storage
        from carr_library.ingest.pipeline import Ingest
        with closing(Storage(paths[0])) as storage:
            with closing(Ingest(paths[1], storage)) as ingest:
                yield ingest
