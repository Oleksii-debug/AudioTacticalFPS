"""Bounded atomic local keymap storage, separate from directory preferences."""
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from filelock import FileLock, Timeout

from .ui.command_registry import MAX_COMMANDS, ShortcutError, _identifier, normalize_shortcut


MAX_BYTES = 64 * 1024
_UNOBSERVED = object()


class KeymapError(ValueError):
    """Keyboard preferences could not be safely read or saved."""


class KeymapChangedError(KeymapError):
    """Another process changed the keymap after this instance loaded it."""


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KeymapError('The keyboard settings contain a duplicate JSON key.')
        result[key] = value
    return result


def _check_path(path, *, regular=False):
    for ancestor in reversed((path, *path.parents)):
        try:
            metadata = ancestor.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None and (stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise KeymapError('Keyboard settings and their folders must not be symbolic links.')
        if ancestor != path and ancestor.exists() and not ancestor.is_dir():
            raise KeymapError('The keyboard settings folder is invalid.')
    if regular and path.exists():
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise KeymapError('The keyboard settings path must be a regular file.')
        if metadata.st_nlink != 1:
            raise KeymapError('The keyboard settings file must not have hard links.')


def _file_state(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path, *, maximum=MAX_BYTES):
    _check_path(path, regular=True)
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_size > maximum):
            raise KeymapError(f'The keyboard settings file is invalid or exceeds {maximum // 1024} KiB.')
        if _file_state(before) != _file_state(metadata):
            raise KeymapChangedError('Keyboard settings changed while opening them. Try again.')
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    _check_path(path, regular=True)
    if len(raw) > maximum:
        raise KeymapError(f'The keyboard settings file exceeds {maximum // 1024} KiB.')
    try:
        current = path.lstat()
    except FileNotFoundError as exc:
        raise KeymapChangedError('Keyboard settings changed while reading them. Try again.') from exc
    if (_file_state(after) != _file_state(before) or _file_state(current) != _file_state(before)
            or len(raw) != before.st_size):
        raise KeymapChangedError('Keyboard settings changed while reading them. Try again.')
    return raw


def _digest(raw):
    return hashlib.sha256(raw).digest() if raw is not None else None


def _decode(raw):
    if raw is None:
        return {}
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates)
    except KeymapError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise KeymapError('The keyboard settings are not valid UTF-8 JSON.') from exc
    if (not isinstance(value, dict) or set(value) != {'version', 'bindings'}
            or type(value['version']) is not int or value['version'] != 1):
        raise KeymapError('The keyboard settings format is unsupported.')
    bindings = value['bindings']
    if not isinstance(bindings, dict) or len(bindings) > MAX_COMMANDS:
        raise KeymapError('The keyboard settings must contain at most 512 command bindings.')
    try:
        return {_identifier(command_id, 'Command ID'): normalize_shortcut(shortcut)
                for command_id, shortcut in bindings.items()}
    except ShortcutError as exc:
        raise KeymapError(str(exc)) from exc


def validate_keymap(raw):
    """Validate a bounded JSON payload for backup or import without executing it.

    Unknown future-module IDs are retained as inert strings. Applying the map
    still requires CommandRegistry.validate to check installed commands and
    context conflicts. This helper neither reads nor writes the filesystem.
    """
    if not isinstance(raw, bytes):
        raise KeymapError('Keyboard settings validation requires immutable bytes.')
    if len(raw) > MAX_BYTES:
        raise KeymapError('The keyboard settings file exceeds 64 KiB.')
    return _decode(raw)


class KeymapStore:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self._observed = _UNOBSERVED

    def load(self):
        """Read without modifying any file; malformed settings remain intact.

        Command availability and contextual conflicts are checked separately by
        CommandRegistry.validate, after all installed commands are registered.
        """
        try:
            raw = _read(self.path)
            # Even malformed, bounded JSON has an observed identity: an
            # explicit later repair must not erase another window's changes.
            self._observed = _digest(raw)
            return {} if raw is None else validate_keymap(raw)
        except OSError as exc:
            raise KeymapError(f'Keyboard settings could not be read: {exc}') from exc

    def save(self, overrides, registry):
        """Validate the complete map before replacing the previous file.

        A failed validation, interrupted write or failed replacement preserves
        the preceding settings. A previously loaded store also detects external
        edits rather than silently overwriting another window's newer changes.
        """
        normalized = registry.validate(overrides)
        raw = (json.dumps({'version': 1, 'bindings': normalized}, ensure_ascii=False,
                          sort_keys=True, indent=2) + '\n').encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise KeymapError('The keyboard settings exceed 64 KiB.')
        temporary = self.path.with_name('.' + self.path.name + '-' + uuid4().hex + '.tmp')
        lock_path = self.path.with_name(self.path.name + '.lock')
        try:
            _check_path(self.path, regular=True)
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _check_path(lock_path, regular=True)
            with FileLock(lock_path, timeout=5, mode=0o600):
                _check_path(self.path, regular=True)
                current = _read(self.path)
                if self._observed is not _UNOBSERVED and _digest(current) != self._observed:
                    raise KeymapChangedError('Keyboard settings changed in another window. '
                                             'Reload them before saving your changes.')
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if _digest(_read(self.path)) != _digest(current):
                    raise KeymapChangedError('Keyboard settings changed before saving. '
                                             'Reload them before saving your changes.')
                os.replace(temporary, self.path)
                self._observed = _digest(raw)
                # Directory fsync is not supported by every Windows filesystem.
                # The file itself was flushed before the atomic replacement.
                if hasattr(os, 'O_DIRECTORY'):
                    try:
                        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                    except OSError:
                        pass
        except Timeout as exc:
            raise KeymapError('Keyboard settings are being saved by another window. Try again.') from exc
        except OSError as exc:
            raise KeymapError(f'Keyboard settings could not be saved: {exc}') from exc
        finally:
            temporary.unlink(missing_ok=True)
        return normalized
