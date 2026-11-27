"""Typed preservation of microphone originals, receipts and inert crash stages.

A restored receipt authorizes no microphone or executable. Opening a completed
recording revalidates its PCM bytes. Orphan and unfinished originals remain
opaque recovery inputs, including files without a usable WAV header.
"""
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import re

from filelock import FileLock, Timeout

from ..operations._state import local_path, regular
from .jobs import read_json
from .recording import RecordingOptions, RecordingService, validate_recording_receipt

DIRECTORY = 'state/studio/recordings'
MAX_RECORDINGS = 10000
MAX_FILES = 30000
MAX_SNAPSHOT_BYTES = 32 * 1024**3
MAX_AUDIO_BYTES = 2 * 1024**3
MAX_RECEIPT_BYTES = 65536
_FINAL = re.compile(r'[0-9a-f]{32}\.(?:wav|json)\Z')
_PENDING = re.compile(r'\.pending-recording-[a-z0-9_]{8}\Z')
_METADATA_STAGE = re.compile(r'\.pending-[a-z0-9_]{8}\Z')


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Recording snapshot verification cancelled.')


def _parts(relative):
    if type(relative) is not str or '\\' in relative or '\x00' in relative:
        return ()
    parts = PurePosixPath(relative).parts
    if '/'.join(parts) != relative or parts[:3] != ('state', 'studio', 'recordings'):
        return ()
    return parts[3:]


def is_state_path(relative):
    parts = _parts(relative)
    return bool(len(parts) == 1 and _FINAL.fullmatch(parts[0])
                or len(parts) == 2 and _PENDING.fullmatch(parts[0]) and parts[1] == 'capture.wav')


def state_limit(relative):
    if not is_state_path(relative):
        raise ValueError('Unsupported recording backup path.')
    return MAX_RECEIPT_BYTES if relative.endswith('.json') else MAX_AUDIO_BYTES


def allows_empty(relative):
    return is_state_path(relative) and relative.endswith('.wav')


def state_paths(root, *, cancelled=lambda: False):
    root = local_path(root, exists=True)
    directory = root / DIRECTORY
    if not os.path.lexists(directory):
        return ()
    local_path(directory, root=root, exists=True)
    regular(directory, directory=True)
    result, entries_seen, total, identities = [], 0, 0, set()
    with os.scandir(directory) as entries:
        for entry in entries:
            _cancel(cancelled)
            entries_seen += 1
            if entries_seen > MAX_FILES + MAX_RECORDINGS + 1:
                raise ValueError('The recording inventory exceeds its entry allowance.')
            path = local_path(entry.path, root=directory, exists=True)
            if _PENDING.fullmatch(entry.name):
                regular(path, directory=True)
                with os.scandir(path) as children:
                    for child in children:
                        _cancel(cancelled)
                        entries_seen += 1
                        if entries_seen > MAX_FILES + MAX_RECORDINGS + 1 or child.name != 'capture.wav':
                            raise ValueError('An unfinished recording contains an unsupported artifact.')
                        candidate = local_path(child.path, root=directory, exists=True)
                        info = regular(candidate)
                        if not 0 <= info.st_size <= MAX_AUDIO_BYTES:
                            raise ValueError('An unfinished recording exceeds its byte allowance.')
                        total += info.st_size
                        result.append(candidate)
                identities.add(entry.name)
            else:
                info = regular(path)
                if entry.name == '.recording.lock':
                    if info.st_size > 1:
                        raise ValueError('The recording lock is not a bounded lock file.')
                    continue
                if _METADATA_STAGE.fullmatch(entry.name):
                    if info.st_size > MAX_RECEIPT_BYTES:
                        raise ValueError('A pending recording receipt exceeds its byte allowance.')
                    continue  # No audio is stored in this exact transient JSON stage.
                relative = path.relative_to(root).as_posix()
                if not is_state_path(relative):
                    raise ValueError('The recording namespace contains an unregistered file.')
                if not (0 if allows_empty(relative) else 1) <= info.st_size <= state_limit(relative):
                    raise ValueError('A saved recording exceeds its file allowance.')
                total += info.st_size
                result.append(path)
                identities.add(entry.name.rsplit('.', 1)[0])
            if len(result) > MAX_FILES or len(identities) > MAX_RECORDINGS or total > MAX_SNAPSHOT_BYTES:
                raise ValueError('Saved recordings exceed the snapshot count or byte allowance.')
    _cancel(cancelled)
    return tuple(sorted(result))


def validate_state_tree(db, root, *, cancelled=lambda: False):
    """Validate the exact typed producer; the database grants no device authority."""
    root = local_path(root, exists=True)
    paths = state_paths(root, cancelled=cancelled)
    inventory = {path.relative_to(root).as_posix() for path in paths}
    for path in paths:
        _cancel(cancelled)
        if path.suffix != '.json':
            continue
        value = validate_recording_receipt(read_json(path, limit=MAX_RECEIPT_BYTES),
                                           recording_id=path.stem)
        audio = path.with_suffix('.wav')
        if audio.relative_to(root).as_posix() not in inventory:
            raise ValueError('A recording receipt is missing its retained original.')
        if RecordingService._audio(audio, RecordingOptions(**value['limits']), cancelled) != value['audio']:
            raise ValueError('A recording original differs from its verified receipt.')
    _cancel(cancelled)


@contextmanager
def snapshot_guard(root, *, cancelled=lambda: False):
    """Borrow only the recorder's actual guard under the outer library writer."""
    root = local_path(root, exists=True)
    directory = root / DIRECTORY
    _cancel(cancelled)
    if not os.path.lexists(directory):
        yield
        return
    local_path(directory, root=root, exists=True)
    regular(directory, directory=True)
    path = directory / '.recording.lock'
    if os.path.lexists(path):
        if regular(path).st_size > 1:
            raise ValueError('The recording lock is not a bounded lock file.')
    lock = FileLock(path, timeout=0, mode=0o600, thread_local=False)
    try:
        lock.acquire()
    except Timeout as exc:
        raise RuntimeError('A microphone recording or owned cleanup is active. Retry the backup after it stops.') from exc
    try:
        _cancel(cancelled)
        yield
    finally:
        lock.release()
