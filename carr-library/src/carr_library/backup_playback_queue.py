"""Passive saved-playlist validation over copied managed PCM only."""
from hashlib import sha256
from pathlib import Path
import unicodedata
import time

NAME = 'state/speech/playback-queue.json'
LOCK = 'state/speech/.playback-queue.lock'
MAX_BYTES = 2 * 1024**2


def reserved(name):
    value = unicodedata.normalize('NFC', name).casefold()
    return value in (NAME, LOCK) or value.startswith((NAME + '/', LOCK + '/'))


def validate_directory(name):
    if reserved(name):
        raise ValueError('A reserved playback queue record cannot be a directory.')


def collection_budgets(entries, *, cancelled=lambda: False):
    found = False
    for entry in entries:
        if cancelled():
            raise InterruptedError('Playback queue backup cancelled.')
        name = entry['filename']
        if not reserved(name):
            continue
        if (name != NAME or found or type(entry['size_bytes']) is not int
                or not 1 <= entry['size_bytes'] <= MAX_BYTES):
            raise ValueError('Invalid reserved playback queue name or byte budget.')
        found = True


def validate_record(value):
    from .speech.playlist import SavedPlaylist
    return SavedPlaylist.from_dict(value)


def validate_collection(copied_state, entries, *, cancelled=lambda: False):
    """Entries are state-relative; never resolve an original or output device."""
    from .backup_sidecars import read_regular, _json
    from .durable_state import local_path
    from .speech.playlist import MAX_REVIEW_SECONDS, SavedPlaylistItem, prepare_playlist
    copied_state = Path(copied_state)
    entries = tuple(entries)
    collection_budgets(
        ({**row, 'filename': 'state/' + row['filename']} for row in entries),
        cancelled=cancelled)
    by_name = {row['filename']: row for row in entries}
    relative = NAME[6:]
    entry = by_name.get(relative)
    if entry is None:
        return
    if cancelled():
        raise InterruptedError('Playback queue backup cancelled.')
    path = local_path(copied_state / relative, root=copied_state, exists=True)
    raw = read_regular(path, MAX_BYTES)
    if len(raw) != entry['size_bytes'] or sha256(raw).hexdigest() != entry['sha256']:
        raise ValueError('Copied playback queue differs from its recorded bytes.')
    saved = validate_record(_json(raw))
    deadline = time.monotonic() + MAX_REVIEW_SECONDS
    def check():
        if cancelled():
            raise InterruptedError('Playback queue backup cancelled.')
        if time.monotonic() >= deadline:
            raise TimeoutError('Copied playback PCM verification exceeded its two-minute allowance.')
        return False
    reviewed = {}
    for item in saved.items:
        if cancelled():
            raise InterruptedError('Playback queue backup cancelled.')
        relative = item.filename[6:]
        media = by_name.get(relative)
        if (media is None or media['sha256'] != item.sha256
                or media['size_bytes'] != item.size_bytes):
            raise ValueError('Saved playlist references missing or changed retained audio.')
        if relative not in reviewed:
            path = local_path(copied_state / relative, root=copied_state, exists=True)
            actual, = prepare_playlist((path,), cancelled=check)
            reviewed[relative] = actual
        actual = reviewed[relative]
        actual_row = SavedPlaylistItem(item.filename, item.label, actual.sha256,
            actual.identity[2], actual.frames, actual.sample_rate, actual.channels, actual.sample_width)
        if actual_row != item:
            raise ValueError('Saved playlist PCM differs from the actual copied bytes.')
