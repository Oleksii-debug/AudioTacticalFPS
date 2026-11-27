"""Small bounded local-file primitives for publisher-controlled client state.

Parents must be private, stable local directories. These checks reject link and
reparse redirection; they do not claim to defeat an administrator replacing the
running application or its trusted configuration.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from ..audio.jobs import checked_directory, verified_reader


class TrustError(ValueError):
    pass


class BusyError(TrustError):
    pass


def component(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 240
            or value in ('.', '..') or value[-1] in ' .'
            or any(ord(c) < 32 or c in '/\\:<>"|?*' for c in value)
            or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I)):
        raise TrustError('Unsafe local filename.')
    return value


def directory(path, *, create=False) -> Path:
    raw = os.fspath(path)
    if raw.startswith(('//', '\\\\')):
        raise TrustError('Use a local directory, without network shares.')
    path = Path(raw).absolute()
    for name in path.parts[1:]:
        component(name)
    for candidate in (*reversed(path.parents), path):
        if os.path.lexists(candidate):
            info = candidate.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise TrustError('Directory is linked, redirected, or not a directory.')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return directory(path)
    if not path.is_dir():
        raise TrustError('Local directory does not exist.')
    try:
        return checked_directory(path)
    except (ValueError, RuntimeError) as error:
        raise TrustError('Use an ordinary local directory without links or mapped network paths.') from error


def file_info(path):
    path = Path(path)
    directory(path.parent)
    component(path.name)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise TrustError('Expected an ordinary, unlinked local file.')
    return info


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink


def read_bytes(path, maximum=65536) -> bytes:
    path = Path(path)
    before = file_info(path)
    if before.st_size > maximum:
        raise TrustError('Document exceeds its size budget.')
    try:
        # The current host's verified reader denies native write/delete sharing
        # and validates both descriptor and pathname identity on every exit.
        with verified_reader(path) as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise TrustError('Local file changed while opening.')
            raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise TrustError('Local file changed or exceeded its size budget.')
    except (ValueError, RuntimeError) as error:
        raise TrustError('Local file changed or could not be verified.') from error
    if _identity(file_info(path)) != _identity(before):
        raise TrustError('Local file changed while reading.')
    return raw


def sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_write(path, raw: bytes):
    path = Path(path)
    parent = directory(path.parent)
    component(path.name)
    if os.path.lexists(path):
        file_info(path)
    fd, name = tempfile.mkstemp(prefix='.trust-', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.lexists(path):
            file_info(path)
        os.replace(name, path)
        sync_directory(parent)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('ascii')


def json_value(raw: bytes, *, maximum=65536):
    if not isinstance(raw, bytes) or len(raw) > maximum:
        raise TrustError('Document exceeds its size budget.')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise TrustError('Duplicate JSON field.')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(TrustError('Non-finite JSON.')))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise TrustError('Invalid UTF-8 JSON document.') from None


@contextmanager
def locked(root, name='.trust.lock'):
    root = directory(root, create=True)
    component(name)
    path = root / name
    if os.path.lexists(path):
        file_info(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise TrustError('Unsafe client lock.')
        if not info.st_size:
            os.write(fd, b'0')
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BusyError('Another trusted-client operation is running.') from None
        yield root
    finally:
        os.close(fd)
