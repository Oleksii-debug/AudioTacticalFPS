"""Ordered reader drafts; no source, engine, path or playback authority."""
from dataclasses import dataclass
from hashlib import sha256
import os
import re
from uuid import uuid4

from ..durable_state import atomic_json, encode, local_path, read_json_snapshot, regular, state_lock
from .session import ReaderDraftStore, ReaderResume

SCHEMA = 'carr.reader-tabs.v1'
MAX_TABS = 16
MAX_BYTES = 16384
# The index alone cannot bound replaying sixteen independent undo histories.
MAX_TOTAL_DRAFT_BYTES = 16 * 1024 * 1024
MAX_RETAINED_CHARACTERS = 16_000_000
NAME = 'state/speech/reading-tabs.json'
_ID = re.compile(r'(?:primary|[0-9a-f]{32})\Z')


@dataclass(frozen=True)
class ReadingTab:
    id: str
    label: str

    def __post_init__(self):
        if type(self.id) is not str or _ID.fullmatch(self.id) is None:
            raise ValueError('Invalid reading-tab identity.')
        if (type(self.label) is not str or not self.label.strip() or len(self.label) > 80
                or any(ord(char) < 32 or 127 <= ord(char) < 160
                       or 0xD800 <= ord(char) <= 0xDFFF for char in self.label)):
            raise ValueError('Use a reading-tab name of 1 to 80 printable characters.')

    @property
    def slot(self):
        return 'manual' if self.id == 'primary' else 'carr.reader-tab:' + self.id

    @property
    def filename(self):
        return 'state/speech/' + sha256(self.slot.encode('utf-8')).hexdigest() + '.reader.json'

    @classmethod
    def new(cls, label):
        return cls(uuid4().hex, label)


@dataclass(frozen=True)
class ReadingTabs:
    tabs: tuple[ReadingTab, ...]
    active: str

    def __post_init__(self):
        if (type(self.tabs) is not tuple or not 1 <= len(self.tabs) <= MAX_TABS
                or any(not isinstance(tab, ReadingTab) for tab in self.tabs)):
            raise ValueError('Keep between 1 and 16 reading tabs open.')
        ids = [tab.id for tab in self.tabs]
        if len(set(ids)) != len(ids) or self.active not in ids:
            raise ValueError('Reading-tab identities and the active selection must be exact.')

    def to_dict(self):
        return {'schema': SCHEMA, 'tabs': [{'id': tab.id, 'label': tab.label} for tab in self.tabs],
                'active': self.active}

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {'schema', 'tabs', 'active'}
                or value['schema'] != SCHEMA or type(value['tabs']) is not list):
            raise ValueError('Unsupported reading-tab recovery state; existing drafts were preserved.')
        rows = []
        for row in value['tabs']:
            if type(row) is not dict or set(row) != {'id', 'label'}:
                raise ValueError('Invalid reading-tab recovery fields.')
            rows.append(ReadingTab(**row))
        return cls(tuple(rows), value['active'])


@dataclass(frozen=True)
class PreparedTab:
    entry: ReadingTab
    resume: ReaderResume
    store: object
    token: str | None


@dataclass(frozen=True)
class PreparedTabs:
    store: object
    tabs: tuple[PreparedTab, ...]
    active: int
    token: str | None
    had_recovery: bool


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Reading-tab recovery cancelled.')


def _history_characters(record):
    """Bound every replayed text state before constructing ReadingDocument."""
    document = record.get('document') if type(record) is dict else None
    if type(document) is not dict or type(document.get('source_text')) is not str:
        raise ValueError('Invalid reading-tab document.')
    edits = document.get('edits')
    if type(edits) is not list or len(edits) > 500:
        raise ValueError('Invalid reading-tab history.')
    length = total = len(document['source_text'])
    for edit in edits:
        if (type(edit) is not dict or type(edit.get('start')) is not int
                or type(edit.get('end')) is not int or type(edit.get('replacement')) is not str
                or not 0 <= edit['start'] <= edit['end'] <= length):
            raise ValueError('Invalid reading-tab edit.')
        length += len(edit['replacement']) - (edit['end'] - edit['start'])
        total += length
        if total > MAX_RETAINED_CHARACTERS:
            raise ValueError('The reading-tab histories exceed their aggregate text budget.')
    return total


class ReadingTabsStore:
    """One passive membership index over the existing reader recovery files."""
    def __init__(self, library_root):
        self.root = local_path(library_root, exists=True)
        info = regular(self.root, directory=True)
        self.identity = info.st_dev, info.st_ino
        self.path = self.root / NAME

    def guard(self):
        info = regular(local_path(self.root, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('The library directory changed. Reopen the reading tabs.')
        local_path(self.path, root=self.root)

    def draft(self, tab):
        self.guard()
        if not isinstance(tab, ReadingTab):
            raise ValueError('Choose a validated reading tab.')
        return ReaderDraftStore(self.root, slot=tab.slot)

    def load(self):
        self.guard()
        if not os.path.lexists(self.path):
            return None, None
        value, token = read_json_snapshot(self.path, max_bytes=MAX_BYTES)
        self.guard()
        return ReadingTabs.from_dict(value), token

    def read_drafts(self, value, *, cancelled=lambda: False):
        """Preflight all bytes and history sizes before materializing any copy."""
        if not isinstance(value, ReadingTabs):
            raise ValueError('Use a validated ordered reading-tab index.')
        self.guard()
        rows, total = [], 0
        for tab in value.tabs:
            _cancel(cancelled)
            draft = self.draft(tab)
            if not os.path.lexists(draft.path):
                raise ValueError('Save every reading draft before publishing its tab index.')
            size = regular(local_path(draft.path, root=self.root, exists=True)).st_size
            total += size
            if size <= 0 or total > MAX_TOTAL_DRAFT_BYTES:
                raise ValueError('The reading tabs exceed their aggregate byte budget.')
            rows.append((tab, draft, size))
        captured, characters = [], 0
        for tab, draft, size in rows:
            _cancel(cancelled)
            # A growing or replaced file cannot consume the remainder's budget:
            # cap each captured snapshot at its own preflight size.
            record, token = read_json_snapshot(draft.path, max_bytes=size)
            characters += _history_characters(record)
            if characters > MAX_RETAINED_CHARACTERS:
                raise ValueError('The reading-tab histories exceed their aggregate text budget.')
            captured.append((tab, draft, record, token))
        result = []
        for tab, draft, record, token in captured:
            _cancel(cancelled)
            result.append(PreparedTab(tab, ReaderDraftStore.decode(record), draft, token))
        _cancel(cancelled)
        self.guard()
        return tuple(result)

    def save(self, value, *, expected_token):
        if not isinstance(value, ReadingTabs):
            raise ValueError('Use a validated ordered reading-tab index.')
        if (expected_token is not None
                and (type(expected_token) is not str or len(expected_token) != 64
                     or any(char not in '0123456789abcdef' for char in expected_token))):
            raise ValueError('A valid prior reading-tab token is required.')
        raw = encode(value.to_dict())
        if len(raw) > MAX_BYTES:
            raise ValueError('Reading-tab recovery exceeds its byte budget.')
        self.guard()
        with state_lock(self.path.parent, 'reading-tabs'):
            self.guard()
            _, token = self.load()
            if token != expected_token:
                raise ValueError('Another window changed the reading tabs. Keep this window open or Save As.')
            # Publish membership only after every referenced draft is durable.
            self.read_drafts(value)
            atomic_json(self.path, value.to_dict(), replace=token is not None)
        return sha256(raw).hexdigest()


def prepare_tabs(library_root, initial, *, explicit_text=False, cancelled=lambda: False):
    """Load passive drafts on the reader's existing worker; never start speech."""
    if not isinstance(initial, ReaderResume) or type(explicit_text) is not bool:
        raise ValueError('A validated initial reading copy is required.')
    _cancel(cancelled)
    store = ReadingTabsStore(library_root)
    index, token = store.load()
    had_recovery = index is not None
    if index is not None:
        states = list(store.read_drafts(index, cancelled=cancelled))
        active = next(i for i, state in enumerate(states) if state.entry.id == index.active)
    else:
        entry = ReadingTab('primary', 'Reading copy 1')
        draft = store.draft(entry)
        if os.path.lexists(draft.path):
            states = list(store.read_drafts(ReadingTabs((entry,), entry.id), cancelled=cancelled))
            had_recovery = True
        else:
            states = [PreparedTab(entry, initial, draft, None)]
        active = 0
    if had_recovery and explicit_text:
        if len(states) >= MAX_TABS:
            raise ValueError('Close a reading tab before opening another explicit text copy.')
        entry = ReadingTab.new('New reading copy')
        states.append(PreparedTab(entry, initial, store.draft(entry), None))
        active = len(states) - 1
    # Include a new explicit/empty copy in the same aggregate materialization
    # budget before returning the state to the native controls.
    records = [{'document': state.resume.document.to_dict()} for state in states]
    if (sum(len(encode(record)) for record in records) > MAX_TOTAL_DRAFT_BYTES
            or sum(_history_characters(record) for record in records) > MAX_RETAINED_CHARACTERS):
        raise ValueError('The reading tabs exceed their aggregate recovery budget.')
    _cancel(cancelled)
    store.guard()
    if store.load()[1] != token:
        raise ValueError('Another window changed the reading tabs during recovery. Reopen the reader.')
    return PreparedTabs(store, tuple(states), active, token, had_recovery)


def validate_collection(stage, entries):
    inventory = {entry['filename'] for entry in entries}
    if NAME not in inventory:
        return
    value, _ = read_json_snapshot(stage / NAME, max_bytes=MAX_BYTES)
    index = ReadingTabs.from_dict(value)
    if any(tab.filename not in inventory for tab in index.tabs):
        raise ValueError('Reading-tab recovery references a missing retained draft.')
