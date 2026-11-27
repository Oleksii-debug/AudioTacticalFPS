"""Regular local files shared by manifest-bound backup components.

These helpers do not acquire a second state lock. The enclosing backup owns the
application writer; each producer validates its own typed records and inventory.
"""
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat


def portable_relative(value):
    """One portable relative-name boundary for typed state and its sidecars."""
    if not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value:
        raise ValueError('Invalid portable relative file path.')
    path = PurePosixPath(value)
    if (path.is_absolute() or len(path.parts) > 16
            or any(part in ('', '.', '..') for part in value.split('/'))
            or any(part.rstrip(' .') != part or PureWindowsPath(part).is_reserved()
                   or any(c in '<>:"|?*' or ord(c) < 32 or 127 <= ord(c) < 160 for c in part)
                   for part in path.parts)):
        raise ValueError('Invalid portable relative file path.')
    return path


def optional_present(path):
    """Only a genuinely absent name is optional for a typed backup producer.

    Unlike lexists/exists, this does not suppress inaccessible or invalid
    lookups. Keep links present so the existing regular-path guards reject
    them. Windows PATH_NOT_FOUND also requires a valid lexical ancestor.
    """
    path = Path(path)
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            for parent in path.parents:
                try:
                    parent.lstat()
                except FileNotFoundError:
                    continue
                regular(parent, directory=True)
                break
            return False
    except OSError as exc:
        from .ingest.restore import RestoreError
        raise RestoreError('Backup state could not be inspected; retry when its registered paths are accessible.') from exc
    return True


def regular(path, *, directory=False):
    path = Path(path)
    value = path.lstat()
    if (stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
            or (not directory and value.st_nlink != 1)):
        raise ValueError('Backup state must use regular local files, without links.')
    return value


def local_path(value, *, root=None, exists=False):
    path = Path(value).absolute()
    if str(value).startswith(('//', '\\\\')) or '\x00' in str(value):
        raise ValueError('Choose a local backup path.')
    # Inspect the lexical ancestors before resolving; resolution must not hide
    # a symlink or Windows junction supplied by a caller.
    for parent in reversed((path, *path.parents)):
        if optional_present(parent):
            regular(parent, directory=(parent != path or parent.is_dir()))
    path = path.resolve(strict=exists)
    if root is not None and not path.is_relative_to(Path(root).resolve(strict=True)):
        raise ValueError('The backup path is outside the selected directory.')
    return path


def sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def publish_tree(source, target, *, cancelled=lambda: False):
    """Publish a caller-owned, already verified staging tree without replacing."""
    source, target = Path(source), Path(target)
    regular(source, directory=True)
    def unreadable(error):
        # Never publish a completion marker after silently skipped state.
        raise error
    target.mkdir(mode=0o700)
    directories = [target]
    for directory, dirs, files in os.walk(source, followlinks=False, onerror=unreadable):
        if cancelled():
            raise InterruptedError('Library backup or restore cancelled.')
        regular(directory, directory=True)
        relative = Path(directory).relative_to(source)
        for name in sorted(dirs):
            regular(Path(directory) / name, directory=True)
            destination = target / relative / name
            destination.mkdir(mode=0o700)
            directories.append(destination)
        for name in sorted(files):
            if cancelled():
                raise InterruptedError('Library backup or restore cancelled.')
            incoming = Path(directory) / name
            regular(incoming)
            os.link(incoming, target / relative / name)
    for directory in reversed(directories):
        sync_directory(directory)
