"""Bounded source-range reading through the existing speech and player APIs.

The reader renders one utterance at a time. Offsets identify exact input text;
they are not synthesized word timings or quotation approval. The caller owns
playback and acknowledges a completed utterance before advancing its cursor.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from uuid import uuid4

from ..audio.engines import Voice, _request
from ..audio.ffmpeg import wav_info
from ..audio.jobs import (atomic_json, checked_directory, file_identity, read_json,
                          regular_file, sha256_file, verified_reader)
from ..audio.production import plan_narration
from ..backup_files import optional_present
from ..ingest.writer_guard import exclusive_writer
from ..operations import ResourceBroker
from ..operations.admission import production_request
from .reading_state import ReadingLocation, ReadingState, _location, _now, _timestamp, _unique


FILENAME = 'reading-speech.json'
MAX_STATE_BYTES = 8 * 1024 ** 2
MAX_POSITIONS = 10000
MAX_TEXT_CHARACTERS = 2_000_000
MAX_UTTERANCE_CHARACTERS = 1200
MODES = ('selection', 'cursor', 'word', 'line', 'sentence', 'paragraph', 'page')


def text_hash(value):
    return sha256(value.encode('utf-8')).hexdigest()


def _text(value):
    if (not isinstance(value, str) or not value.strip() or '\x00' in value
            or len(value) > MAX_TEXT_CHARACTERS):
        raise ValueError('Choose a nonempty reading section of at most two million characters.')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Reading requires valid Unicode text.') from exc
    return value


@dataclass(frozen=True)
class SpeechSelection:
    start: int
    end: int


def select_range(text, mode, *, cursor=0, selection=None):
    """Resolve a finite reading command without changing the source text."""
    _text(text)
    if mode not in MODES or type(cursor) is not int or not 0 <= cursor <= len(text):
        raise ValueError('Choose a supported reading command and a cursor within the text.')
    if mode == 'page':
        start, end = 0, len(text)
    elif mode == 'selection':
        if (not isinstance(selection, (tuple, list)) or len(selection) != 2
                or any(type(value) is not int for value in selection)
                or not 0 <= selection[0] < selection[1] <= len(text)):
            raise ValueError('Select a nonempty range of text before reading the selection.')
        start, end = selection
    elif mode == 'cursor':
        start, end = cursor, len(text)
    else:
        # Sentence boundaries are an explicit punctuation-based navigation aid,
        # not a linguistic claim for abbreviations or every supported language.
        patterns = {'word': r'\S+', 'line': r'[^\r\n]+',
                    'sentence': r'[^.!?。！？\n]+(?:[.!?。！？]+|(?=\n|$))',
                    'paragraph': r'(?s).+?(?=\r?\n[ \t]*\r?\n|\Z)'}
        point = min(cursor, len(text) - 1)
        found = None
        for match in re.finditer(patterns[mode], text):
            if match.end() > point:
                found = match
                break
        if found is None:
            raise ValueError('There is no readable unit at this cursor.')
        start, end = found.span()
    if not text[start:end].strip():
        raise ValueError('The selected range contains no text to speak.')
    return SpeechSelection(start, end)


def next_utterance(text, start, end, *, maximum=MAX_UTTERANCE_CHARACTERS):
    """Return an exact bounded slice, preferring punctuation then whitespace."""
    if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text)
            or type(maximum) is not int or not 32 <= maximum <= MAX_UTTERANCE_CHARACTERS):
        raise ValueError('Invalid bounded speech range.')
    while start < end and text[start].isspace():
        start += 1
    if start == end:
        return None
    stop = min(end, start + maximum)
    candidates = list(re.finditer(r'[.!?。！？]+(?=\s|$)|\n', text[start:stop]))
    if candidates:
        stop = start + candidates[0].end()
    elif stop < end:
        spaces = list(re.finditer(r'\s+', text[start:stop]))
        if spaces and spaces[-1].start() > 0:
            stop = start + spaces[-1].start()
    while stop > start and text[stop - 1].isspace():
        stop -= 1
    return SpeechSelection(start, stop)


@dataclass(frozen=True)
class SpeechSettings:
    voice: str
    language: str
    rate: float = 0
    pitch: float = 0
    volume: float = .8

    def __post_init__(self):
        if (not isinstance(self.voice, str) or ':' not in self.voice or len(self.voice) > 2048
                or any(ord(char) < 32 for char in self.voice)):
            raise ValueError('Explicitly choose an enumerated local voice.')
        _request('Selected reading text', self.language, self.rate, self.pitch, self.volume)

    def check_voice(self, voice):
        if type(voice) is not Voice or voice.id != self.voice or not voice.available:
            raise ValueError('The selected local voice is unavailable; refresh and choose it explicitly.')
        for control, value, default in (('rate', self.rate, 0), ('pitch', self.pitch, 0),
                                        ('volume', self.volume, 1)):
            if control not in voice.controls and value != default:
                raise ValueError('The selected voice does not support ' + control + '.')


def _key(digest, location):
    # A long book has one resume position, not one row for every page. The
    # complete location/digest remain in that row and must match on load.
    identity = ({'workspace': location.workspace, 'edition': location.edition}
                if location is not None else {'text_sha256': digest, 'location': None})
    return text_hash(json.dumps(identity, sort_keys=True, ensure_ascii=False))


def decode_positions(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_STATE_BYTES:
        raise ValueError('Live reading state is empty or exceeds its byte budget.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number.')))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Live reading state must be bounded UTF-8 JSON.') from exc
    if (type(value) is not dict or set(value) != {'schema', 'positions'}
            or value['schema'] != 'carr-live-reading/1' or type(value['positions']) is not list
            or len(value['positions']) > MAX_POSITIONS):
        raise ValueError('Unsupported live reading state.')
    rows = {}
    for row in value['positions']:
        if (type(row) is not dict or set(row) != {'key', 'text_sha256', 'characters', 'cursor',
                                                 'location', 'settings', 'updated_at'}
                or not isinstance(row['text_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', row['text_sha256'])
                or type(row['characters']) is not int or not 1 <= row['characters'] <= MAX_TEXT_CHARACTERS
                or type(row['cursor']) is not int or not 0 <= row['cursor'] <= row['characters']
                or type(row['settings']) is not dict or set(row['settings']) != {
                    'voice', 'language', 'rate', 'pitch', 'volume'}):
            raise ValueError('Invalid live reading position.')
        location = None if row['location'] is None else _location(row['location'])
        SpeechSettings(**row['settings'])
        _timestamp(row['updated_at'])
        if (row['key'] != _key(row['text_sha256'], location) or row['key'] in rows
                or location is not None and row['text_sha256'] != location.text_sha256):
            raise ValueError('The live reading position does not match its exact source.')
        rows[row['key']] = dict(row)
    return rows


def saved_locations(raw):
    return tuple(_location(row['location']) for row in decode_positions(raw).values()
                 if row['location'] is not None)


class SpeechPositionStore:
    """Reuse the existing application writer and atomic JSON publisher."""
    def __init__(self, root):
        self.root = checked_directory(root)
        self.path = self.root / FILENAME

    def _read(self):
        if not optional_present(self.path):
            return {}
        value = read_json(self.path, limit=MAX_STATE_BYTES)
        return decode_positions(json.dumps(value, ensure_ascii=False).encode('utf-8'))

    def load(self, text, location=None):
        _text(text)
        if location is not None and type(location) is not ReadingLocation:
            raise ValueError('A validated source location is required.')
        digest = text_hash(text)
        row = self._read().get(_key(digest, location))
        expected = asdict(location) if location is not None else None
        if row is not None and row['text_sha256'] == digest and row['location'] == expected:
            return row
        return None

    def remember(self, text, cursor, settings, location=None, *, guard=lambda: None):
        _text(text)
        if type(settings) is not SpeechSettings:
            raise ValueError('Validated local speech settings are required.')
        if location is not None and type(location) is not ReadingLocation:
            raise ValueError('A validated source location is required.')
        digest = text_hash(text)
        row = dict(key=_key(digest, location), text_sha256=digest, characters=len(text), cursor=cursor,
                   location=asdict(location) if location is not None else None,
                   settings=asdict(settings), updated_at=_now())
        decode_positions(json.dumps({'schema': 'carr-live-reading/1', 'positions': [row]}).encode())
        with exclusive_writer(self.root):
            guard()
            rows = self._read()
            rows[row['key']] = row
            value = {'schema': 'carr-live-reading/1', 'positions': list(rows.values())}
            decode_positions(json.dumps(value, ensure_ascii=False).encode('utf-8'))
            guard()
            atomic_json(self.path, value)
        return row


@dataclass(frozen=True)
class RenderedUtterance:
    selection: SpeechSelection
    path: Path
    sha256: str
    frames: int
    sample_rate: int
    text_sha256: str
    identity: tuple


class SpeechSession:
    """One selected source, voice and bounded in-flight utterance.

    Heavy work stays on a caller-owned worker. Cancellation settles the shared
    engine process family before this method returns and releases its lease.
    Temporary PCM is retained until the caller stops playback and releases it.
    """
    def __init__(self, root, text, settings, selection, *, registry=None, guard=lambda: None,
                 location=None, broker=None, temporary_parent=None, validate_source=lambda: None):
        self.root, self.text = checked_directory(root), _text(text)
        if type(settings) is not SpeechSettings or type(selection) is not SpeechSelection:
            raise ValueError('A selected source range and speech settings are required.')
        if not 0 <= selection.start < selection.end <= len(text):
            raise ValueError('The speech range is outside its source.')
        self.settings, self.selection, self.guard, self.location = settings, selection, guard, location
        self.validate_source = validate_source
        if location is not None and (type(location) is not ReadingLocation or location.text_sha256 != text_hash(text)):
            raise ValueError('The selected speech text does not match its immutable source location.')
        self.registry = registry
        self.broker = broker
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self._temporary = tempfile.TemporaryDirectory(prefix='carr-live-reading-', dir=temporary_parent)
        self.directory = Path(self._temporary.name).resolve()
        self.cursor, self.current, self.closed = selection.start, None, False
        self.identity = uuid4().hex
        self._voice_identity = None
        self._playback_reader = None

    def _check(self):
        self.guard()
        if self.closed or self.cancel.is_set():
            raise InterruptedError('Live reading was stopped.')
        return False

    def render_next(self):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('A speech utterance is already being prepared.')
        try:
            self._check()
            self.validate_source()
            if self.current is not None:
                raise RuntimeError('Stop and release the previous utterance before preparing the next one.')
            selected = next_utterance(self.text, self.cursor, self.selection.end)
            if selected is None:
                self.cursor = self.selection.end
                return None
            if self.registry is None:
                from ..runtime_settings import configured_voice_registry
                self.registry = configured_voice_registry(self.root)
            voices = self.registry.voices(cancelled=self._check)
            voice = next((voice for voice in voices if voice.id == self.settings.voice), None)
            self.settings.check_voice(voice)
            identity = self.registry.cache_identity(self.settings.voice, cancelled=self._check)
            if self._voice_identity is not None and self._voice_identity != identity:
                raise ValueError('The selected voice/runtime changed. Start a new reviewed reading session.')
            self._voice_identity = identity
            if self.broker is None:
                self.broker = ResourceBroker.for_app(self.root, cancelled=self._check)
            value = self.text[selected.start:selected.end]
            plan = plan_narration(value, voice=self.settings.voice, language=self.settings.language)
            request = production_request(plan, self.directory, self.broker.limits)
            path = self.directory / (uuid4().hex + '.wav')
            try:
                with self.broker.reserve(request, operation_id='reader:' + self.identity,
                                         cancelled=self._check) as lease:
                    def stopped():
                        self._check()
                        lease.checkpoint()
                        return False
                    self.registry.synthesize(value, path, **asdict(self.settings), cancelled=stopped)
                    stopped()
                    payload_identity = file_identity(regular_file(path))
                    info = wav_info(path)
                    digest = sha256_file(path, cancelled=stopped, expected_identity=payload_identity)
                    if self.registry.cache_identity(self.settings.voice, cancelled=stopped) != identity:
                        raise ValueError('The selected voice/runtime changed while rendering.')
                    self.validate_source()
                    stopped()
                    self.current = RenderedUtterance(selected, path, digest, info.frames, info.rate,
                                                     text_hash(value), payload_identity)
                    return self.current
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        finally:
            self._lock.release()

    def pin_playback(self):
        """Validate on the worker and retain the exact immutable payload lease.

        On Windows the shared reader excludes writers/renames until playback
        has actually stopped. The caller must never release it merely because
        the native player was asked to stop. POSIX changes are detected when
        the reader is released, before acknowledging completed speech.
        """
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('Wait for the current speech operation before playing.')
        try:
            self._check()
            self.validate_source()
            excerpt = self.current
            if excerpt is None:
                raise ValueError('Prepare an utterance before playing it.')
            if self._playback_reader is not None:
                return excerpt
            owner = verified_reader(excerpt.path, expected_identity=excerpt.identity)
            stream = owner.__enter__()
            try:
                digest = sha256()
                remaining = os.fstat(stream.fileno()).st_size
                while remaining:
                    self._check()
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError('The prepared utterance was truncated before playback.')
                    digest.update(block)
                    remaining -= len(block)
                if digest.hexdigest() != excerpt.sha256:
                    raise ValueError('The prepared utterance changed before playback. Stop and prepare it again.')
                self.validate_source()
                self._check()
            except BaseException:
                owner.__exit__(None, None, None)
                raise
            self._playback_reader = owner
            return excerpt
        finally:
            self._lock.release()

    def _unpin_playback(self):
        owner, self._playback_reader = self._playback_reader, None
        if owner is not None:
            owner.__exit__(None, None, None)

    def check_playback(self):
        """Cheap UI handoff check while the already verified reader stays open."""
        self._check()
        if self.current is None or self._playback_reader is None:
            raise ValueError('The prepared utterance has no verified playback owner.')
        if file_identity(regular_file(self.current.path)) != self.current.identity:
            raise ValueError('The prepared utterance changed while waiting for playback.')

    def release(self, *, completed=False):
        """Call only after the existing player has released its audio handle."""
        if type(completed) is not bool:
            raise ValueError('Playback completion must be explicit.')
        self._unpin_playback()
        if self.current is not None:
            if completed:
                self.cursor = self.current.selection.end
            self.current.path.unlink(missing_ok=True)
            self.current = None

    def checkpoint(self):
        row = SpeechPositionStore(self.root).remember(self.text, self.cursor, self.settings,
                                                      self.location, guard=self.guard)
        if self.location is not None:
            def stopped():
                self.guard()
                return False
            ReadingState(self.root).remember(self.location, cancelled=stopped)
        return row

    def close(self):
        self.cancel.set()
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('Wait for speech cancellation before releasing its files.')
        try:
            try:
                self._unpin_playback()
            except (OSError, RuntimeError, ValueError):
                # A stopped, private derivative may be discarded even if its
                # validation failed. Completed-playback release stays strict
                # and never advances a cursor after such a failure.
                pass
            self.release()
            self._temporary.cleanup()
            self.closed = True
        finally:
            self._lock.release()
