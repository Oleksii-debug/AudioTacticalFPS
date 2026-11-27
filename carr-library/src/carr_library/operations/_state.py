"""Adapt the existing local-file and publication contracts for resource records.

The broker owns only policy and transient lease metadata. This module does not
introduce a second job store or weaken the existing local path checks.
"""
import os
from pathlib import Path
import tempfile
import time

from ..audio import jobs
from ..audio.model_manifest import local_path as _asset_path

MAX_JSON_BYTES = 32 * 1024 * 1024


def local_path(value, *, root=None, exists=False):
    path = _asset_path(value, must_exist=exists)
    ancestor = path if path.is_dir() else path.parent
    while not os.path.lexists(ancestor):
        ancestor = ancestor.parent
    jobs.checked_directory(ancestor)
    if os.path.lexists(path):
        regular(path, directory=path.is_dir())
    if root is not None and not path.is_relative_to(jobs.checked_directory(root)):
        raise ValueError('The resource record is outside the selected directory.')
    return path


def regular(path, *, directory=False):
    path = Path(path)
    if directory:
        return jobs.checked_directory(path).lstat()
    info = jobs.regular_file(path)
    if info.st_nlink != 1:
        raise ValueError('Resource records must not have hard-link aliases.')
    return info


def read_json(path):
    path = local_path(path, exists=True)
    regular(path)
    return jobs.read_json(path, limit=MAX_JSON_BYTES)


def sync_directory(path):
    return jobs._sync_directory(jobs.checked_directory(path))


def _replace_state(stage, destination):
    """Reuse the donor's bounded Windows sharing-error retry; never unlink."""
    delays = iter((0.01, 0.02, 0.04, 0.08, 0.16, 0.32))
    while True:
        try:
            jobs._replace_state_file(stage, destination)
            return
        except PermissionError as exc:
            delay = next(delays, None)
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or delay is None:
                raise
            time.sleep(delay)


def atomic_json(path, value, *, replace=False):
    path = local_path(path)
    parent = jobs.checked_directory(path.parent, create=True)
    if os.path.lexists(path):
        regular(path)
        if not replace:
            raise FileExistsError(path)
    raw = jobs._json_bytes(value)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('The resource record exceeds its JSON budget.')
    descriptor, name = tempfile.mkstemp(prefix='.pending-', dir=parent)
    stage = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            _replace_state(stage, path)
            sync_directory(parent)
        else:
            jobs.publish_noreplace(stage, path)
    finally:
        stage.unlink(missing_ok=True)
    return path
