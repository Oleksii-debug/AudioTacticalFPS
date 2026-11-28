"""Ordered reader drafts; no source, engine, path or playback authority."""
from dataclasses import dataclass
from hashlib import sha256
import os
import re
from uuid import uuid4

from ..durable_state import atomic_json, encode, local_path, read_json_snapshot, regular, state_lock
from .session import ReaderDraftStore

SCHEMA = 'carr.reader-tabs.v1'
MAX_TABS = 16
MAX_BYTES = 16384
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

    def save(self, value, *, expected_token):
        if not isinstance(value, ReadingTabs):
            raise ValueError('Use a validated ordered reading-tab index.')
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
            for tab in value.tabs:
                if self.draft(tab).load() is None:
                    raise ValueError('Save every reading draft before publishing its tab index.')
            atomic_json(self.path, value.to_dict(), replace=token is not None)
        return sha256(raw).hexdigest()


def validate_collection(stage, entries):
    inventory = {entry['filename'] for entry in entries}
    if NAME not in inventory:
        return
    value, _ = read_json_snapshot(stage / NAME, max_bytes=MAX_BYTES)
    index = ReadingTabs.from_dict(value)
    if any(tab.filename not in inventory for tab in index.tabs):
        raise ValueError('Reading-tab recovery references a missing retained draft.')
