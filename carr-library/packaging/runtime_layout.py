"""Resolve stable per-user data paths, separately from replaceable program files.

This is a layout preflight, not an installer or a deletion routine.
"""
from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = 'Universal Research Library'


@dataclass(frozen=True)
class RuntimeLayout:
    install: Path
    data: Path


def _directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('absolute directory required')
    path = path.resolve()
    for candidate in (path, *path.parents):
        if candidate.exists() and not candidate.is_dir():
            raise ValueError('expected directory, not file')
    return path


def resolve_layout(install_root):
    """No writes; reject any overlap in either direction, including resolved links."""
    install = _directory(install_root)
    dirs = PlatformDirs(APP_NAME, appauthor=False, version=None, roaming=False,
                        ensure_exists=False, use_site_for_root=False)
    data = _directory(dirs.user_data_path)
    if data.is_relative_to(install) or install.is_relative_to(data):
        raise ValueError('program and persistent data directories must be disjoint')
    return RuntimeLayout(install, data)
