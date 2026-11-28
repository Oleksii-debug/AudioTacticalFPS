"""One content-addressed local input namespace for portable Studio clips.

Only exact relative declarations resolve here. Backup validation can use the
same resolver on an already-copied state tree; no legacy or external paths are
opened by these helpers.
"""
from hashlib import sha256
import os
from pathlib import Path
import re

from ..durable_state import local_path, regular

MAX_INPUT_BYTES = 8 * 1024**3
_MANAGED_INPUT = re.compile(r'inputs/([0-9a-f]{64})\.audio')


def managed_input_sha256(value):
    """Validate a portable declaration without filesystem access."""
    match = _MANAGED_INPUT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError('Portable Studio clips require inputs/<64-hex-SHA256>.audio.')
    return match[1]


def project_managed_inputs(project):
    """List the exact managed references in a checked portable project."""
    if project.schema not in (2, 3, 4):
        return ()
    values = set()
    for scene in project.scenes:
        for cue in scene.cues:
            managed_input_sha256(cue.path)
            values.add(cue.path)
    return tuple(sorted(values))


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _checked_digest(source, before, cancelled=lambda: False):
    digest, count = sha256(), 0
    if cancelled():
        raise InterruptedError('Studio input verification cancelled.')
    with Path(source).open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A required Studio input changed before verification.')
        while block := stream.read(min(1024 * 1024, before.st_size + 1 - count)):
            if cancelled():
                raise InterruptedError('Studio input verification cancelled.')
            count += len(block)
            if count > before.st_size:
                raise ValueError('A required Studio input grew during verification.')
            digest.update(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A required Studio input changed during verification.')
    if count != before.st_size or _stamp(regular(source)) != _stamp(before):
        raise ValueError('A required Studio input changed during verification.')
    return digest.hexdigest()


def resolve_managed_input(studio_root, value, *, cancelled=lambda: False):
    """Resolve and hash-check only this Studio's retained input directory."""
    expected = managed_input_sha256(value)
    root = local_path(studio_root, exists=True)
    inputs = local_path(root / 'inputs', root=root, exists=True)
    source = local_path(root / value, root=inputs, exists=True)
    if source.parent != inputs:
        raise ValueError('A portable clip is outside the managed Studio inputs.')
    before = regular(source)
    if not 0 < before.st_size <= MAX_INPUT_BYTES:
        raise ValueError('A managed Studio input exceeds its byte allowance.')
    if _checked_digest(source, before, cancelled) != expected:
        raise ValueError('A managed Studio input differs from its content identity.')
    return source
