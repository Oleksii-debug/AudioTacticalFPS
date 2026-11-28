"""Atomic recovery of valid reading drafts and non-secret local voice settings."""
from dataclasses import dataclass
from hashlib import sha256
import math
import os
from pathlib import Path

from ..durable_state import atomic_json, encode, local_path, read_json_snapshot, state_lock
from .reading import ReadingDocument
from .runtime import SpeechSettings
from ._io import text_value

_UNCHECKED = object()


@dataclass(frozen=True)
class ReaderResume:
    document: ReadingDocument
    voice_id: str = ''
    rate: int = 0
    volume: int = 80
    cursor: int = 0
    audio_seconds: float = 0
    pitch: int | None = None
    punctuation: bool = False

    def __post_init__(self):
        if not isinstance(self.document, ReadingDocument):
            raise ValueError('Recovery requires a validated reading copy.')
        text_value(self.voice_id, 'voice identity', maximum=2048, empty=True)
        SpeechSettings(rate=self.rate, volume=self.volume,
                       pitch=self.pitch, punctuation=self.punctuation)
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
        return self._save(resume)[0]

    def save_with_token(self, resume, *, expected_token):
        """Save a reviewed tab only if its prior exact recovery bytes remain."""
        return self._save(resume, expected_token=expected_token)[1]

    def _save(self, resume, *, expected_token=_UNCHECKED):
        if not isinstance(resume, ReaderResume):
            raise ValueError('A validated reader recovery snapshot is required.')
        if (expected_token is not _UNCHECKED and expected_token is not None
                and (type(expected_token) is not str or len(expected_token) != 64
                     or any(char not in '0123456789abcdef' for char in expected_token))):
            raise ValueError('A valid prior reading-draft token is required.')
        record = {'schema': 'carr.reader-session.v1', 'document': resume.document.to_dict(),
                  'voice_id': resume.voice_id, 'rate': resume.rate, 'volume': resume.volume,
                  'cursor': resume.cursor, 'audio_seconds': resume.audio_seconds}
        # Keep legacy seven-key records byte-compatible for engine defaults.
        if resume.pitch is not None:
            record['pitch'] = resume.pitch
        if resume.punctuation:
            record['punctuation'] = resume.punctuation
        with state_lock(self.root, self.key):
            if resume.document.format_notice and self.path.exists():
                raise ValueError('Use Save As to preserve the original pre-release reading draft.')
            if expected_token is not _UNCHECKED:
                actual = (read_json_snapshot(self.path)[1]
                          if os.path.lexists(self.path) else None)
                if actual != expected_token:
                    raise ValueError('Another window changed this reading draft. Save As to keep your copy.')
            atomic_json(self.path, record, replace=expected_token is not None)
        return self.path, sha256(encode(record)).hexdigest()

    def load(self):
        return self.load_with_token()[0]

    def load_with_token(self, *, max_bytes=None):
        if not os.path.lexists(self.path):
            return None, None
        record, token = read_json_snapshot(self.path, max_bytes=max_bytes)
        return self.decode(record), token

    @staticmethod
    def decode(record):
        """Validate a passive captured record without opening a store or engine."""
        required = {'schema', 'document', 'voice_id', 'rate', 'volume', 'cursor', 'audio_seconds'}
        if (not isinstance(record, dict) or not required <= set(record)
                or set(record) - required - {'pitch', 'punctuation'}
                or record['schema'] != 'carr.reader-session.v1'):
            raise ValueError('Unsupported reading-session recovery state.')
        document = ReadingDocument.from_dict(record['document'])
        return ReaderResume(document, record['voice_id'], record['rate'], record['volume'],
                            record['cursor'], record['audio_seconds'],
                            pitch=record.get('pitch'), punctuation=record.get('punctuation', False))
