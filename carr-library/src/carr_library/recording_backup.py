"""Typed retained recordings in the existing complete-library sidecar format.

Only relative recording receipts and final owned WAV names are admitted.
Unreceipted finals remain inert originals; no device or external runtime is
opened while validating a snapshot or restoring it.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re

from .backup_files import local_path, regular
from .durable_state import read_json, state_lock
from .audio.recording import RecordingService, RecordingOptions, validate_recording_receipt

DIRECTORY = 'state/studio/recordings'
PREFIX = DIRECTORY + '/'
_NAME = re.compile(r'[0-9a-f]{32}\.(wav|json)\Z')
_PENDING = re.compile(r'\.pending-recording-[A-Za-z0-9_-]+\Z')
MAX_FILES = 20000
MAX_BYTES = 2 * 1024**3
JSON_LIMIT = 64 * 1024


def state_path(value):
    return (isinstance(value, str) and value.startswith(PREFIX)
            and _NAME.fullmatch(value[len(PREFIX):]) is not None)


def state_limit(value):
    if not state_path(value):
        raise ValueError('Invalid retained recording path.')
    return JSON_LIMIT if value.endswith('.json') else MAX_BYTES


def state_paths(library_root, *, cancelled=lambda: False):
    library = Path(library_root)
    directory = library / DIRECTORY
    if not os.path.lexists(directory):
        return ()
    directory = local_path(directory, root=library, exists=True)
    regular(directory, directory=True)
    result = []
    for count, path in enumerate(directory.iterdir()):
        if cancelled():
            raise InterruptedError('Recording snapshot inventory was cancelled.')
        if count >= MAX_FILES * 2:
            raise ValueError('The recording inventory exceeds its item allowance.')
        if path.name == '.recording.lock':
            regular(local_path(path, root=directory, exists=True))
            continue
        if _PENDING.fullmatch(path.name):
            # Unfinished captures never become a completed original merely
            # because an idle/crashed staging folder is present.
            regular(local_path(path, root=directory, exists=True), directory=True)
            continue
        if _NAME.fullmatch(path.name) is None:
            raise ValueError('Unknown retained recording state must not be silently omitted.')
        path = local_path(path, root=directory, exists=True)
        info = regular(path)
        limit = state_limit(PREFIX + path.name)
        if not 0 <= info.st_size <= limit or len(result) >= MAX_FILES:
            raise ValueError('The recording inventory exceeds its file or byte allowance.')
        result.append(path)
    return tuple(sorted(result))


@contextmanager
def snapshot_guard(library_root):
    directory = Path(library_root) / DIRECTORY
    if not os.path.lexists(directory):
        yield
        return
    regular(local_path(directory, root=library_root, exists=True), directory=True)
    with state_lock(directory, 'recording'):
        yield


def validate_state_tree(library_root, *, cancelled=lambda: False):
    directory = Path(library_root) / DIRECTORY
    paths = state_paths(library_root, cancelled=cancelled)
    names = {path.name: path for path in paths}
    receipts = {}
    for path in paths:
        if cancelled():
            raise InterruptedError('Recording snapshot validation was cancelled.')
        if path.suffix != '.json':
            continue
        value = validate_recording_receipt(read_json(path), recording_id=path.stem)
        output = names.get(value['filename'])
        if output is None:
            raise ValueError('A recording receipt lost its retained original WAV.')
        if RecordingService._audio(output, RecordingOptions(**value['limits']), cancelled) != value['audio']:
            raise ValueError('A recording receipt differs from its retained original PCM.')
        RecordingService._verify_capture_parts(output, value, cancelled)
        receipts[value['id']] = value
    # Validate each edge once, then walk the parent graph with a shared memo.
    parents = {}
    for identity, value in receipts.items():
        if value.get('retake') is None:
            continue
        relation = value['retake']
        previous = receipts.get(relation['previous_id'])
        if (previous is None or previous['audio']['sha256'] != relation['previous_sha256']
                or relation['origin_id'] != (previous.get('retake') or {}).get('origin_id', previous['id'])):
            raise ValueError('A recording retake lost its retained original identity.')
        parents[identity] = previous['id']
    checked = set()
    for identity in receipts:
        branch = set()
        while identity not in checked:
            if cancelled():
                raise InterruptedError('Recording retake validation was cancelled.')
            if identity in branch:
                raise ValueError('Recording retakes contain a cycle.')
            branch.add(identity)
            if identity not in parents:
                break
            identity = parents[identity]
        checked.update(branch)
    # A final canonical WAV without a receipt is retained byte-for-byte as an
    # unverified original. It is not decoded, activated, or declared successful.
    return tuple(receipts)
