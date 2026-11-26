"""Typed live-reader recovery under the library's existing exclusive writer lock.

Only this known sidecar is written. Backup adapters can validate its captured
bytes through validate_sessions_bytes; no arbitrary state directory is copied.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re

from ..audio.jobs import atomic_json, checked_directory
from ..ingest.writer_guard import exclusive_writer
from ..reader.narration_text import decode_json, read_document
from .reading import ReadingDocument
from .runtime import SpeechSettings
from ._io import text_value

FILENAME = 'voice-reader-state.json'
MAX_BYTES = 8 * 1024 * 1024
MAX_SESSIONS = 64


@dataclass(frozen=True)
class ReaderResume:
    document: ReadingDocument
    voice_id: str = ''
    rate: int = 0
    volume: int = 80
    cursor: int = 0
    audio_seconds: float = 0
    audio_key: str | None = None
    audio_sha256: str | None = None

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
        for value in (self.audio_key, self.audio_sha256):
            if value is not None and (not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)):
                raise ValueError('Invalid playback source or audio identity.')
        if (self.audio_key is None) != (self.audio_sha256 is None):
            raise ValueError('Playback resume requires both source settings and exact WAV hash.')

    def to_dict(self):
        return {'document': self.document.to_dict(), 'voice_id': self.voice_id,
                'rate': self.rate, 'volume': self.volume, 'cursor': self.cursor,
                'audio_seconds': self.audio_seconds, 'audio_key': self.audio_key, 'audio_sha256': self.audio_sha256}

    @classmethod
    def from_dict(cls, value, *, private_root=None):
        if type(value) is not dict or set(value) != {
                'document', 'voice_id', 'rate', 'volume', 'cursor', 'audio_seconds', 'audio_key', 'audio_sha256'}:
            raise ValueError('Unsupported live-reader resume state.')
        return cls(ReadingDocument.from_dict(value['document'], private_root=private_root),
                   value['voice_id'], value['rate'], value['volume'], value['cursor'],
                   value['audio_seconds'], value['audio_key'], value['audio_sha256'])


def validate_sessions_bytes(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError('Live-reader recovery exceeds its 8 MiB budget.')
    value = decode_json(raw.decode('utf-8'))
    if (type(value) is not dict or set(value) != {'kind', 'version', 'sessions'}
            or value['kind'] != 'research-live-reader-state' or type(value['version']) is not int
            or value['version'] != 1 or type(value['sessions']) is not dict
            or len(value['sessions']) > MAX_SESSIONS):
        raise ValueError('Unsupported live-reader recovery sidecar.')
    for key, entry in value['sessions'].items():
        if type(entry) is not dict or set(entry) != {'slot', 'resume'}:
            raise ValueError('Invalid live-reader recovery entry.')
        slot = text_value(entry['slot'], 'reading source identity', maximum=2048)
        if key != sha256(slot.encode('utf-8')).hexdigest():
            raise ValueError('The live-reader recovery source identity changed.')
        resume = ReaderResume.from_dict(entry['resume'])
        if slot != 'manual' and resume.document.source_id != slot:
            raise ValueError('The reading draft belongs to a different source revision.')
    return value


def session_source_bindings(raw):
    """Validated exact source positions for copied-database backup verification."""
    value = validate_sessions_bytes(raw)
    return tuple(entry['resume']['document']['source_binding'] for entry in value['sessions'].values()
                 if entry['resume']['document']['source_binding'] is not None)


class ReaderDraftStore:
    def __init__(self, library_root, *, slot='manual'):
        self.root = checked_directory(library_root, create=True)
        self.slot = text_value(slot, 'reading source identity', maximum=2048)
        self.key = sha256(self.slot.encode('utf-8')).hexdigest()
        self.path = self.root / FILENAME

    def _read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {'kind': 'research-live-reader-state', 'version': 1, 'sessions': {}}
        return validate_sessions_bytes(read_document(self.path, byte_budget=MAX_BYTES))

    def save(self, resume):
        if not isinstance(resume, ReaderResume):
            raise ValueError('A typed live-reader recovery snapshot is required.')
        with exclusive_writer(self.root):
            value = self._read()
            value['sessions'][self.key] = {'slot': self.slot, 'resume': resume.to_dict()}
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
            validate_sessions_bytes(raw)
            atomic_json(self.path, value)
        return self.path

    def load(self):
        entry = self._read()['sessions'].get(self.key)
        return ReaderResume.from_dict(entry['resume'], private_root=self.root) if entry else None
