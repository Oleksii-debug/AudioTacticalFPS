"""Atomic recovery of valid reading drafts and non-secret local voice settings."""
from dataclasses import dataclass
from hashlib import sha256
import math
from pathlib import Path
import re
from uuid import uuid4

from ..durable_state import atomic_json, encode, local_path, read_json, regular, state_lock
from .reading import MAX_HISTORY, MAX_PROJECT_BYTES, ReadingDocument
from .runtime import SpeechSettings
from ._io import text_value


def _source_family(origin):
    return sha256(encode({'workspace': origin['workspace'], 'edition': origin['edition'],
        'first': origin['positions'][0]['ordinal'], 'last': origin['positions'][-1]['ordinal']})).hexdigest()


def _saved_digest(path, cancelled, *, phase='discovery'):
    maximum = MAX_PROJECT_BYTES + 65536
    if regular(path).st_size > maximum:
        raise ValueError('The saved reading draft exceeds its discovery budget.')
    fingerprint, count = sha256(), 0
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            if cancelled():
                raise InterruptedError(f'Saved-draft {phase} cancelled; no draft was changed.')
            count += len(block)
            if count > maximum:
                raise ValueError('The saved reading draft grew beyond its discovery budget.')
            fingerprint.update(block)
    return fingerprint.hexdigest()


@dataclass(frozen=True)
class ReaderDraftSummary:
    key: str
    title: str
    source_recorded: bool
    reading_revision: int
    source_characters: int
    sha256: str
    modified_ns: int

    @property
    def label(self):
        title = ' '.join(self.title.split())[:160]
        binding = 'source identity recorded; checked on opening' if self.source_recorded else 'unbound reading copy'
        return (f'{title}; {binding}; reading revision {self.reading_revision}; '
                f'{self.source_characters} original characters; copy {self.key[:12]}')


@dataclass(frozen=True)
class ReaderDraftList:
    copies: tuple[ReaderDraftSummary, ...]
    skipped: int
    truncated: bool


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
        self._family = None
        self.discovery_warning = ''

    def save(self, resume):
        return self._save(resume, replace=True)

    def _save(self, resume, *, replace):
        if not isinstance(resume, ReaderResume):
            raise ValueError('A validated reader recovery snapshot is required.')
        record = {'schema': 'carr.reader-session.v1', 'document': resume.document.to_dict(),
                  'voice_id': resume.voice_id, 'rate': resume.rate, 'volume': resume.volume,
                  'cursor': resume.cursor, 'audio_seconds': resume.audio_seconds}
        with state_lock(self.root, self.key):
            atomic_json(self.path, record, replace=replace)
        self._remember_published()
        return self.path

    def save_if_absent(self, resume):
        """Seed a reading draft without replacing another reader's saved work."""
        if not isinstance(resume, ReaderResume):
            raise ValueError('A validated reader recovery snapshot is required.')
        with state_lock(self.root, self.key):
            existing = self.load()
            if existing is not None:
                return existing
            record = {'schema': 'carr.reader-session.v1', 'document': resume.document.to_dict(),
                      'voice_id': resume.voice_id, 'rate': resume.rate, 'volume': resume.volume,
                      'cursor': resume.cursor, 'audio_seconds': resume.audio_seconds}
            atomic_json(self.path, record, replace=False)
        self._remember_published()
        return resume

    def for_source(self, source):
        """Keep current identity separate from revision-independent discovery."""
        origin = source.origin
        family = _source_family(origin)
        identity = sha256(encode(origin)).hexdigest()
        store = type(self)(self.root.parent.parent, slot=f'source-v2:{family}:{identity}')
        store._family = family
        return store

    def _links_path(self):
        return self.root / (self._family + '.reader-links.json')

    def _links(self):
        if self._family is None or not self._links_path().exists():
            return []
        data = read_json(self._links_path())
        if (type(data) is not dict or set(data) != {'schema', 'copies'}
                or data['schema'] != 'carr.reader-recovery-links.v1'
                or type(data['copies']) is not list or not 1 <= len(data['copies']) <= 500
                or any(not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key)
                       for key in data['copies']) or len(set(data['copies'])) != len(data['copies'])):
            raise ValueError('Invalid reading-recovery links; saved drafts were kept unchanged.')
        return data['copies']

    def _remember_published(self):
        """Keep a committed draft successful if its discovery link fails."""
        self.discovery_warning = ''
        try:
            self._remember()
        except (OSError, ValueError, RuntimeError) as exc:
            self.discovery_warning = ('Reading draft saved. Automatic source recovery could not be updated; '
                                      f'use Recover saved draft or Save As. {exc}')

    def _remember(self):
        if self._family is None:
            return
        with state_lock(self.root, 'recovery-' + self._family):
            existing = self._links()
            if existing and existing[-1] == self.key:
                return
            copies = [key for key in existing if key != self.key]
            if len(copies) >= 500:
                raise ValueError('The reading-recovery history is full. Use Save As for a portable copy.')
            atomic_json(self._links_path(), {'schema': 'carr.reader-recovery-links.v1',
                                            'copies': [*copies, self.key]}, replace=True)

    def _referenced_store(self, key):
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key):
            raise ValueError('Invalid reading-recovery copy reference.')
        store = type(self)(self.root.parent.parent, slot='validated-recovery-reference')
        store.key, store.path = key, self.root / (key + '.reader.json')
        store._family = self._family
        return store

    def latest_copy(self):
        copies = self._links()
        if not copies:
            return None
        store = self._referenced_store(copies[-1])
        if not store.path.exists():
            raise ValueError('The most recent recovery copy is missing; saved older drafts were kept unchanged.')
        return store

    def list_saved(self, *, cancelled=lambda: False, limit=100, max_bytes=64 * 1024 * 1024):
        """Bounded metadata discovery for an explicit generic recovery picker.

        Run through the reader's existing worker executor. Listing never replays
        edit history, binds an unidentified legacy draft, or follows a saved
        absolute path. Full document validation occurs only after selection.
        """
        if (type(limit) is not int or not 1 <= limit <= 100
                or type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024
                or not callable(cancelled)):
            raise ValueError('Invalid saved-draft listing budget.')
        def check():
            if cancelled():
                raise InterruptedError('Saved-draft discovery cancelled; no draft was changed.')
        check()
        inventory, skipped = [], 0
        for path in self.root.glob('*.reader.json'):
            check()
            if len(inventory) + skipped >= 10000:
                raise ValueError('Too many saved reader drafts to list in one request; no draft was changed.')
            if not re.fullmatch(r'[0-9a-f]{64}\.reader\.json', path.name):
                skipped += 1
                continue
            try:
                info = regular(path)
            except (OSError, ValueError):
                skipped += 1
                continue
            inventory.append((info.st_mtime_ns, path.stem.removesuffix('.reader'), path, info.st_size))
        copies, total, truncated = [], 0, False
        ordered = sorted(inventory, reverse=True)
        for offset, (stamp, key, path, size) in enumerate(ordered):
            check()
            if len(copies) >= limit:
                truncated = True
                skipped += len(ordered) - offset
                break
            if size > MAX_PROJECT_BYTES + 65536 or total + size > max_bytes:
                skipped += 1
                truncated = True
                continue
            total += size
            try:
                before = _saved_digest(path, cancelled)
                record = read_json(path)
                check()
                if (type(record) is not dict or record.get('schema') != 'carr.reader-session.v1'
                        or type(record.get('document')) is not dict):
                    raise ValueError('Unsupported saved reading draft.')
                document = record['document']
                schema = document.get('schema')
                if schema not in ('carr.reading-copy.v1', 'carr.reading-copy.v2'):
                    raise ValueError('Unsupported reading-copy schema.')
                title = text_value(document.get('title'), 'reading-copy title', maximum=512)
                original = text_value(document.get('source_text'), 'source text', empty=True)
                revision = document.get('revision')
                if type(revision) is not int or not 0 <= revision <= MAX_HISTORY:
                    raise ValueError('Invalid saved reading revision.')
                after = _saved_digest(path, cancelled)
                if before != after:
                    raise ValueError('The saved draft changed during discovery.')
                copies.append(ReaderDraftSummary(key, title, schema == 'carr.reading-copy.v2',
                                                revision, len(original), after, stamp))
            except InterruptedError:
                raise
            except (OSError, ValueError, TypeError, KeyError):
                skipped += 1
        check()
        return ReaderDraftList(tuple(copies), skipped, truncated)

    def open_saved(self, summary, *, cancelled=lambda: False):
        """Open the explicitly selected exact snapshot as a separate local copy."""
        if (type(summary) is not ReaderDraftSummary or not callable(cancelled)
                or not isinstance(summary.sha256, str) or not re.fullmatch('[0-9a-f]{64}', summary.sha256)):
            raise ValueError('Choose a saved draft from the recovery list.')
        if cancelled():
            raise InterruptedError('Saved-draft opening cancelled; no draft was changed.')
        store = self._referenced_store(summary.key)
        if _saved_digest(store.path, cancelled, phase='opening') != summary.sha256:
            raise ValueError('The saved draft changed. Refresh the recovery list and choose it again.')
        resumed = store.load()
        if resumed is None or _saved_digest(store.path, cancelled, phase='opening') != summary.sha256:
            raise ValueError('The saved draft changed. Refresh the recovery list and choose it again.')
        if cancelled():
            raise InterruptedError('Saved-draft opening cancelled; no draft was changed.')
        if resumed.document.source_bound:
            store._family = _source_family(resumed.document.source_origin)
        return store.separate_copy(resumed)

    def separate_copy(self, resume):
        """Preserve the saved file and open an independently recoverable copy."""
        copied = ReaderResume(ReadingDocument.from_dict(resume.document.to_dict()),
                              resume.voice_id, resume.rate, resume.volume,
                              resume.cursor, resume.audio_seconds)
        store = type(self)(self.root.parent.parent, slot='recovered-copy:' + uuid4().hex)
        store._family = self._family
        store._save(copied, replace=False)
        return copied, store

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


def open_source_draft(source, legacy_store, *, choose, guard=lambda: None):
    """Resolve source/draft conflicts with one explicit native reader choice.

    Choose 0 for a separate saved copy, 1 for the current source, or None for
    cancellation. Old files are retained in every branch. A separate copy
    preserves historical lineage and grants no live library authority.
    """
    guard()
    store = legacy_store.for_source(source)
    links = store._links()

    def changed():
        raise ValueError('The saved reading draft changed. Reopen the reader and choose it again.')

    def snapshot(candidate):
        if not candidate.path.exists():
            return None, None
        checksum = _saved_digest(candidate.path, lambda: False, phase='opening')
        resumed = candidate.load()
        if resumed is None or _saved_digest(candidate.path, lambda: False, phase='opening') != checksum:
            changed()
        return resumed, checksum

    selected_store = store.latest_copy() or store
    recovered, checksum = snapshot(selected_store)
    if recovered is None:
        recovered, checksum = snapshot(legacy_store)
        selected_store = store._referenced_store(legacy_store.key)
    if store._links() != links:
        changed()
    if recovered is None:
        return ReaderResume(source.reading_document()), store, False
    try:
        source.reading_document(recovered.document)
    except ValueError:
        selection = choose()
        if selection is None:
            raise InterruptedError('Reading-draft recovery cancelled; the saved draft was kept unchanged.')
        if type(selection) is not int or selection not in (0, 1):
            raise ValueError('Choose the saved draft or the current source.')
        if selection == 0:
            # A native choice may outlive a save from another reader. Neither
            # changed bytes nor a newer linked copy may be hidden by appending
            # the stale in-memory snapshot as the latest recovered draft.
            if (store._links() != links
                    or _saved_digest(selected_store.path, lambda: False, phase='opening') != checksum):
                changed()
            copied, separate_store = selected_store.separate_copy(recovered)
            return copied, separate_store, True
        guard()
        current = store.load()
        if current is not None:
            try:
                source.reading_document(current.document)
            except ValueError:
                pass
            else:
                return current, store, False
        fresh = ReaderResume(source.reading_document())
        if current is not None:
            # A mismatched record in an identity slot is retained for recovery,
            # too. Choosing the current source is not permission to replace it.
            fresh, store = store.separate_copy(fresh)
        return fresh, store, False
    guard()
    # Migrate a matching legacy snapshot without replacing its old file.
    return recovered, (store if selected_store.key == legacy_store.key else selected_store), False
