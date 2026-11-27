"""Bounded review of an explicitly chosen session playlist; never autoplay.

These records are process-local review results, not restored file authority.
Reading and hashing run on the existing desktop worker. WavePlayer rechecks the
same local file identity before each native admission.
"""
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import stat
import time
import wave

from ._io import local_file

MAX_ITEMS = 256
MAX_FILE_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 32 * 1024**3
MAX_REVIEW_SECONDS = 120


@dataclass(frozen=True)
class PlaylistItem:
    path: Path
    identity: tuple
    sha256: str
    frames: int
    sample_rate: int
    channels: int
    sample_width: int

    @property
    def duration_seconds(self):
        return self.frames / self.sample_rate


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def prepare_playlist(paths, *, cancelled=lambda: False):
    """Review at most 256 user-selected local PCM files with bounded memory."""
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= MAX_ITEMS:
        raise ValueError(f'Choose from 1 to {MAX_ITEMS} local PCM WAV files.')
    deadline = time.monotonic() + MAX_REVIEW_SECONDS
    def check():
        if cancelled():
            raise InterruptedError('Playlist review was cancelled; no audio was started.')
        if time.monotonic() >= deadline:
            raise TimeoutError('Playlist review exceeded two minutes. Choose fewer files.')
    result, total = [], 0
    for path in paths:
        check()
        path = local_file(path)
        before = path.stat()
        identity = _stamp(before)
        if not 0 < before.st_size <= MAX_FILE_BYTES:
            raise ValueError('A playlist recording must be nonempty and at most 4 GiB.')
        total += before.st_size
        if total > MAX_TOTAL_BYTES:
            raise ValueError('The selected playlist exceeds its 32 GiB review budget.')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or _stamp(opened) != identity or _stamp(local_file(path).stat()) != identity):
                raise ValueError('A selected playlist file changed before it could be reviewed.')
            with wave.open(stream, 'rb') as audio:
                frames, rate = audio.getnframes(), audio.getframerate()
                channels, width = audio.getnchannels(), audio.getsampwidth()
                if (audio.getcomptype() != 'NONE' or frames <= 0 or rate <= 0
                        or channels not in (1, 2) or width not in (1, 2, 3, 4)
                        or frames * channels * width > before.st_size - stream.tell()):
                    raise ValueError('Playlists require complete nonempty mono or stereo PCM WAV files.')
            stream.seek(0)
            digest = sha256()
            remaining = before.st_size
            while remaining:
                check()
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError('A selected playlist file was truncated during review.')
                digest.update(block)
                remaining -= len(block)
            check()
            if (stream.read(1) or _stamp(os.fstat(stream.fileno())) != identity
                    or _stamp(local_file(path).stat()) != identity):
                raise ValueError('A selected playlist file changed during review.')
        result.append(PlaylistItem(path, identity, digest.hexdigest(), frames, rate, channels, width))
    check()
    return tuple(result)
