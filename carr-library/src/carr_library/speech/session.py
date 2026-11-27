"""Atomic recovery of valid reading drafts and non-secret local voice settings."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import math
from pathlib import Path

from ..durable_state import atomic_json, local_path, read_json, state_lock
from .reading import ReadingDocument
from .runtime import SpeechSettings
from ._io import text_value
from ..reader.reading_state import ReadingLocation, _location


def document_source_locations(document):
    """Derive the compatibility view from canonical reading-copy lineage."""
    anchor = document.source_anchor
    if anchor is None:
        return ()
    return tuple(ReadingLocation(anchor['workspace'], anchor['edition'], anchor['revision'],
        row['kind'], row['index'], row['text_sha256'], anchor['asset_sha256'])
        for row in anchor['positions'])


@dataclass(frozen=True)
class ReaderResume:
    document: ReadingDocument
    voice_id: str = ''
    rate: int = 0
    volume: int = 80
    cursor: int = 0
    audio_seconds: float = 0
    source_locations: tuple[ReadingLocation, ...] = ()

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
        if (type(self.source_locations) is not tuple or len(self.source_locations) > 10000
                or any(type(item) is not ReadingLocation for item in self.source_locations)):
            raise ValueError('Invalid authenticated reading-source locations.')
        anchored = document_source_locations(self.document)
        if anchored:
            if self.source_locations and self.source_locations != anchored:
                raise ValueError('The legacy source envelope differs from canonical reading-copy lineage.')
            object.__setattr__(self, 'source_locations', anchored)
        if self.source_locations:
            first = self.source_locations[0]
            identity = first.workspace, first.edition, first.revision, first.asset_sha256, first.kind
            previous = 0
            for item in self.source_locations:
                if ((item.workspace, item.edition, item.revision, item.asset_sha256, item.kind) != identity
                        or item.index <= previous):
                    raise ValueError('Reading-source locations must identify one ordered exact edition.')
                previous = item.index


def decode_reader_resume(record):
    """Typed v1/v2 recovery parsing; callers authenticate v2 source references.

    Source locations come from canonical ReadingDocument.source_anchor, or from
    the strictly checked interim v2 envelope when no anchor exists. A hashed
    slot filename never grants authority or implies workspace scope.
    """
    fields = {'schema', 'document', 'voice_id', 'rate', 'volume', 'cursor', 'audio_seconds'}
    if not isinstance(record, dict):
        raise ValueError('Unsupported reading-session recovery state.')
    version = record.get('schema')
    if version == 'carr.reader-session.v2':
        fields.add('source_locations')
    elif version != 'carr.reader-session.v1':
        raise ValueError('Unsupported reading-session recovery state.')
    if set(record) != fields:
        raise ValueError('Unsupported reading-session recovery fields.')
    locations = record.get('source_locations', [])
    if type(locations) is not list or len(locations) > 10000 or (version.endswith('.v2') and not locations):
        raise ValueError('Invalid reading-session source bindings.')
    return ReaderResume(ReadingDocument.from_dict(record['document']), record['voice_id'],
                        record['rate'], record['volume'], record['cursor'], record['audio_seconds'],
                        tuple(_location(value) for value in locations))


class ReaderDraftStore:
    def __init__(self, library_root, *, slot='manual'):
        text_value(slot, 'reading-session identity', maximum=2048)
        self.root = local_path(Path(library_root) / 'state' / 'speech')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        local_path(self.root, exists=True)
        self.key = sha256(slot.encode('utf-8')).hexdigest()
        self.path = self.root / (self.key + '.reader.json')

    @staticmethod
    def _record(resume):
        if not isinstance(resume, ReaderResume):
            raise ValueError('A validated reader recovery snapshot is required.')
        record = {'schema': 'carr.reader-session.v1', 'document': resume.document.to_dict(),
                  'voice_id': resume.voice_id, 'rate': resume.rate, 'volume': resume.volume,
                  'cursor': resume.cursor, 'audio_seconds': resume.audio_seconds}
        if resume.source_locations and resume.document.source_anchor is None:
            record.update(schema='carr.reader-session.v2',
                          source_locations=[asdict(item) for item in resume.source_locations])
        return record

    def save(self, resume):
        record = self._record(resume)
        with state_lock(self.root, self.key):
            if resume.document.format_notice and self.path.exists():
                raise ValueError('Use Save As to preserve the original pre-release reading draft.')
            atomic_json(self.path, record, replace=True)
        return self.path

    def save_if_absent(self, resume):
        """Seed a draft without replacing another reader's saved work.

        Returning the original candidate identifies a newly created draft;
        returning the validated existing snapshot identifies recovery.
        """
        record = self._record(resume)
        with state_lock(self.root, self.key):
            existing = self.load()
            if existing is not None:
                return existing
            atomic_json(self.path, record, replace=False)
        return resume

    def load(self):
        if not self.path.exists():
            return None
        record = read_json(self.path)
        return decode_reader_resume(record)
