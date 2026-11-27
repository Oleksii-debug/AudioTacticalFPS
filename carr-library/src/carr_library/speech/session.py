"""Atomic recovery of valid reading drafts and non-secret local voice settings."""
from dataclasses import dataclass
from hashlib import sha256
import math
from pathlib import Path

from ..durable_state import atomic_json, local_path, read_json, state_lock
from .reading import ReadingDocument
from .runtime import SpeechSettings
from ._io import text_value


@dataclass(frozen=True)
class ReaderResume:
    document: ReadingDocument
    voice_id: str = ''
    rate: int = 0
    volume: int = 80
    cursor: int = 0
    audio_seconds: float = 0

    def __post_init__(self):
        if not isinstance(self.document, ReadingDocument):
            raise ValueError('Recovery requires a validated reading copy.')
        text_value(self.voice_id, 'voice identity', maximum=2048, empty=True)
        SpeechSettings(rate=self.rate, volume=self.volume)
        if type(self.cursor) is not int or not 0 <= self.cursor <= len(self.document.text):
            raise ValueError('The recovery cursor is outside the reading copy.')
        if (type(self.audio_seconds) not in (float, int) or not math.isfinite(self.audio_seconds)
                or not 0 <= self.audio_seconds <= 31_536_000):
            raise ValueError('Invalid recovery playback position.')


class ReaderDraftStore:
    def __init__(self, library_root, *, slot='manual'):
        text_value(slot, 'reading-session identity', maximum=2048)
        self.root = local_path(Path(library_root) / 'state' / 'speech')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        local_path(self.root, exists=True)
        self.key = sha256(slot.encode('utf-8')).hexdigest()
        self.path = self.root / (self.key + '.reader.json')

    def save(self, resume):
        if not isinstance(resume, ReaderResume):
            raise ValueError('A validated reader recovery snapshot is required.')
        record = {'schema': 'carr.reader-session.v1', 'document': resume.document.to_dict(),
                  'voice_id': resume.voice_id, 'rate': resume.rate, 'volume': resume.volume,
                  'cursor': resume.cursor, 'audio_seconds': resume.audio_seconds}
        with state_lock(self.root, self.key):
            atomic_json(self.path, record, replace=True)
        return self.path

    def load(self):
        if not self.path.exists():
            return None
        record = read_json(self.path)
        if (not isinstance(record, dict) or set(record) != {'schema', 'document', 'voice_id',
                'rate', 'volume', 'cursor', 'audio_seconds'} or record['schema'] != 'carr.reader-session.v1'):
            raise ValueError('Unsupported reading-session recovery state.')
        document = ReadingDocument.from_dict(record['document'])
        return ReaderResume(document, record['voice_id'], record['rate'], record['volume'],
                            record['cursor'], record['audio_seconds'])
