"""Typed retained recordings in the existing complete-library state component.

Adapted from PR123 recording_backup.py, blob
973586e369a563a207edca2075e2416ae485d7cf. The existing composite format 5,
schema 6 and streaming state verifier remain authoritative. Final WAV files
without receipts are inert originals; no recording device/runtime is activated.
"""
from contextlib import contextmanager
from pathlib import Path
import re

from .durable_state import local_path, regular, read_json
from .audio.recording import (AudioCancelled, RecordingService, RecordingOptions,
                             validate_recording_receipt, verify_recording_segments)

DIRECTORY = 'state/studio/recordings'
PREFIX = DIRECTORY + '/'
STATE_DIRECTORY = 'studio/recordings'
STATE_PREFIX = STATE_DIRECTORY + '/'
_NAME = re.compile(r'[0-9a-f]{32}\.(wav|json)\Z')
_PENDING = re.compile(r'\.pending-recording-[A-Za-z0-9_-]+\Z')
MAX_FILES = 20000
MAX_BYTES = 2 * 1024**3
JSON_LIMIT = 64 * 1024


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Recording snapshot verification was cancelled.')


def state_path(value):
    return (isinstance(value, str) and value.startswith(PREFIX)
            and _NAME.fullmatch(value[len(PREFIX):]) is not None)


def is_audio(value):
    return state_path(value) and value.endswith('.wav')


def state_limit(value):
    if not state_path(value):
        raise ValueError('Invalid retained recording path.')
    return JSON_LIMIT if value.endswith('.json') else MAX_BYTES


def validate_receipt(name, value):
    if not state_path(name) or not name.endswith('.json'):
        raise ValueError('Invalid retained recording receipt path.')
    return validate_recording_receipt(value, recording_id=Path(name).stem)


def validate_state_entries(entries, *, cancelled=lambda: False):
    """Classify the reserved namespace without normalizing user filenames."""
    count = 0
    for entry in entries:
        _check(cancelled)
        name = entry['filename']
        if not (name.casefold() == STATE_DIRECTORY or name.casefold().startswith(STATE_PREFIX)):
            continue
        # Aliased spellings are active on Windows but distinct on POSIX. A
        # portable backup rejects them before publication rather than silently
        # treating an active receipt as unknown opaque state.
        if not state_path('state/' + name):
            raise ValueError('Noncanonical or unknown retained recording path.')
        count += 1
        if (count > MAX_FILES or type(entry['size_bytes']) is not int
                or not 0 <= entry['size_bytes'] <= state_limit('state/' + name)
                or name.endswith('.json') and entry['size_bytes'] == 0):
            raise ValueError('The recording inventory exceeds its file or byte allowance.')


def _recording_directory(state_root, cancelled):
    root = Path(state_root)
    for path in (root, root / 'studio', root / STATE_DIRECTORY):
        _check(cancelled)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        # Only genuine absence is optional. Permission, reparse and directory
        # enumeration errors must never turn retained originals into omissions.
        regular(path, directory=True)
    return local_path(root / STATE_DIRECTORY, root=root, exists=True)


def _state_paths(state_root, *, cancelled=lambda: False):
    directory = _recording_directory(state_root, cancelled)
    if directory is None:
        return ()
    result = []
    for count, path in enumerate(directory.iterdir()):
        _check(cancelled)
        if count >= MAX_FILES * 2:
            raise ValueError('The recording inventory exceeds its item allowance.')
        if path.name == '.recording.lock':
            regular(local_path(path, root=directory, exists=True))
            continue
        if _PENDING.fullmatch(path.name):
            # A crashed private capture directory is not a completed recording.
            regular(local_path(path, root=directory, exists=True), directory=True)
            continue
        if _NAME.fullmatch(path.name) is None:
            raise ValueError('Unknown retained recording state must not be silently omitted.')
        path = local_path(path, root=directory, exists=True)
        info = regular(path)
        limit = state_limit(PREFIX + path.name)
        if (not 0 <= info.st_size <= limit or len(result) >= MAX_FILES
                or path.suffix == '.json' and info.st_size == 0):
            raise ValueError('The recording inventory exceeds its file or byte allowance.')
        result.append(path)
    return tuple(sorted(result))


def state_paths(library_root, *, cancelled=lambda: False):
    return _state_paths(Path(library_root) / 'state', cancelled=cancelled)


@contextmanager
def snapshot_guard(library_root, *, cancelled=lambda: False):
    """Share the capture reservation while the existing writer takes a snapshot."""
    from filelock import FileLock, Timeout
    root = Path(library_root) / 'state'
    directory = _recording_directory(root, cancelled)
    if directory is None:
        yield
        return
    lock_path = directory / '.recording.lock'
    try:
        lock_path.lstat()
    except FileNotFoundError:
        pass
    else:
        regular(lock_path)
    try:
        with FileLock(lock_path, timeout=0, mode=0o600, thread_local=False):
            # Validate even entries the generic state copier excludes, so an
            # unknown final file cannot disappear behind a temporary suffix.
            _state_paths(root, cancelled=cancelled)
            yield
    except Timeout:
        raise RuntimeError('A recording is still active or awaiting owned cleanup; stop it before backup.') from None


def validate_state_root(state_root, *, cancelled=lambda: False):
    """Verify copied bytes/PCM/retake graph; orphan finals remain unverified."""
    paths = _state_paths(state_root, cancelled=cancelled)
    names = {path.name: path for path in paths}
    receipts = {}
    for path in paths:
        _check(cancelled)
        if path.suffix != '.json':
            continue
        value = validate_receipt(PREFIX + path.name, read_json(path))
        output = names.get(value['filename'])
        if output is None:
            raise ValueError('A recording receipt lost its retained original WAV.')
        try:
            measured = RecordingService._audio(output, RecordingOptions(**value['limits']), cancelled)
            if measured != value['audio']:
                raise ValueError('A recording receipt differs from its retained original PCM.')
            verify_recording_segments(output, value, cancelled=cancelled)
        except AudioCancelled:
            raise InterruptedError('Recording snapshot verification was cancelled.') from None
        receipts[value['id']] = value
    parents = {}
    for identity, value in receipts.items():
        _check(cancelled)
        if 'retake' not in value:
            continue
        relation = value['retake']
        previous = receipts.get(relation['previous_id'])
        if (previous is None or previous['audio']['sha256'] != relation['previous_sha256']
                or relation['origin_id'] != previous.get('retake', {}).get('origin_id', previous['id'])):
            raise ValueError('A recording retake lost its retained original identity.')
        parents[identity] = previous['id']
    checked = set()
    for identity in receipts:
        branch = set()
        while identity not in checked:
            _check(cancelled)
            if identity in branch:
                raise ValueError('Recording retakes contain a cycle.')
            branch.add(identity)
            if identity not in parents:
                break
            identity = parents[identity]
        checked.update(branch)
    return tuple(receipts)


def validate_state_tree(library_root, *, cancelled=lambda: False):
    return validate_state_root(Path(library_root) / 'state', cancelled=cancelled)
