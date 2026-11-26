"""Orchestrate trusted local revision backups, not a complete workspace backup."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from carr_library.ingest.restore import RestoreError, restore_revision_snapshot

SNAPSHOT = 'revisions.sqlite'
MANIFEST = 'manifest.json'
MAX_MANIFEST = 4096
DEFAULT_MAX_BYTES = 512 * 1024 * 1024


def _plain(path, *, directory=False):
    info = path.lstat()
    mode = stat.S_ISDIR if directory else stat.S_ISREG
    if not mode(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise RestoreError('bundle path must be a regular local file or directory')
    return info


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError('duplicate manifest key')
        result[key] = value
    return result


def create_revision_bundle(storage, directory, *, include_all_workspaces=False,
                           max_bytes=DEFAULT_MAX_BYTES):
    """Return the manifest SHA256 for independent, trusted retention.

    Explicit opt-in is required: the storage snapshot includes ALL workspaces.
    Requires a stable private parent and a filesystem supporting hard links.
    The manifest is published last. A failed/interrupted publish may leave an
    incomplete directory; never overwrite or automatically remove it. The size
    limit applies to published snapshots, not temporary backup disk usage.
    """
    if include_all_workspaces is not True:
        raise RestoreError('explicit all-workspaces backup consent required')
    if type(max_bytes) is not int or max_bytes < 1:
        raise RestoreError('invalid snapshot size limit')
    directory = Path(directory)
    if os.path.lexists(directory):
        raise FileExistsError(directory)
    with TemporaryDirectory(prefix='.carr-bundle-', dir=directory.parent) as tmp:
        stage = Path(tmp)
        snapshot = stage / SNAPSHOT
        storage.backup(snapshot)
        size = _plain(snapshot).st_size
        if not 0 < size <= max_bytes:
            raise RestoreError('snapshot exceeds bundle size limit')
        manifest = dict(version=1, kind='revision-storage', schema_version=1,
                        size_bytes=size, sha256=hash_file(snapshot))
        raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        with (stage / MANIFEST).open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory.mkdir(mode=0o700)
        os.link(snapshot, directory / SNAPSHOT)
        os.link(stage / MANIFEST, directory / MANIFEST)
        return sha256(raw).hexdigest()


def restore_revision_bundle(directory, destination, *, expected_manifest_sha256,
                            max_bytes=DEFAULT_MAX_BYTES):
    """Restore a trusted closed bundle to a fresh path, never in-place.

    The expected manifest digest must be retained independently. It does not
    authenticate a bundle if obtained from the same untrusted source.
    The source and destination parents must be private and stable.
    """
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(
            r'[0-9a-f]{64}', expected_manifest_sha256):
        raise RestoreError('independent manifest SHA256 required')
    directory, destination = Path(directory), Path(destination)
    _plain(directory, directory=True)
    if set(p.name for p in directory.iterdir()) != {SNAPSHOT, MANIFEST}:
        raise RestoreError('incomplete bundle or unexpected files')
    if destination.resolve().is_relative_to(directory.resolve()):
        raise RestoreError('restore destination must be outside bundle')
    _plain(directory / SNAPSHOT)
    if _plain(directory / MANIFEST).st_size > MAX_MANIFEST:
        raise RestoreError('manifest size limit')
    with (directory / MANIFEST).open('rb') as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST or sha256(raw).hexdigest() != expected_manifest_sha256:
        raise RestoreError('manifest digest mismatch')
    try:
        manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError('invalid manifest') from exc
    return restore_revision_snapshot(directory / SNAPSHOT, destination, manifest,
                                     max_bytes=max_bytes)
