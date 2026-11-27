"""Regular local files shared by manifest-bound backup components.

These helpers do not acquire a second state lock. The enclosing backup owns the
application writer; each producer validates its own typed records and inventory.
"""
import os
from pathlib import Path
import stat


def optional_present(path):
    """Only an absent name is optional; inaccessible/invalid lookup must fail.

    ``lexists`` suppresses other OSErrors, which can turn a denied typed state
    root into a successful but incomplete snapshot. Keep dangling links present
    so the existing regular-file/directory validation rejects them explicitly.
    """
    path = Path(path)
    try:
        path.lstat()
    except FileNotFoundError:
        # Windows may report PATH_NOT_FOUND for a child of a regular file.
        # A missing descendant is optional only beneath an actual directory;
        # do not let that OS error mapping hide an invalid or dangling parent.
        for parent in path.parents:
            try:
                parent.lstat()
            except FileNotFoundError:
                continue
            regular(parent, directory=True)
            break
        return False
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
        # os.walk otherwise drops inaccessible directories, which would let
        # the caller publish a completion marker for an incomplete snapshot.
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
