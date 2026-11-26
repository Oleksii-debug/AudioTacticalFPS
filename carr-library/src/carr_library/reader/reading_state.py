"""Local page bookmarks and resume positions, separate from quotation evidence.

Locations bind the source, exact revision and page bytes. A reader must resolve
them against the authorized library before showing a page. This module neither
approves extracted text nor changes the research database. It stores a bounded
JSON sidecar using the same cooperative writer lock as the library. Explicit
workspace exports provide portable reading-state backups.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile
import unicodedata
from uuid import uuid4

from ..ingest.writer_guard import _private_root, _regular_if_present, exclusive_writer


FILENAME = 'reading-state.json'
MAX_BYTES = 8 * 1024 * 1024
MAX_POSITIONS = 10000
MAX_BOOKMARKS = 10000
_LOCATION_KEYS = frozenset(('workspace', 'edition', 'revision', 'kind', 'index',
                            'text_sha256', 'asset_sha256'))


def _text(value, label, *, limit=512):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(unicodedata.category(ch) in ('Cc', 'Cs', 'Zl', 'Zp')
                   or ch in '\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'
                   for ch in value)):
        raise ValueError(f'Invalid {label}. Use nonempty plain text up to {limit} characters.')
    return value


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z', value):
        raise ValueError('Invalid reading-state timestamp.')
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')
    except ValueError as exc:
        raise ValueError('Invalid reading-state timestamp.') from exc
    return value


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _check_cancelled(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Reading-state operation cancelled; nothing was saved.')


@dataclass(frozen=True)
class ReadingLocation:
    workspace: str
    edition: str
    revision: str
    kind: str
    index: int
    text_sha256: str
    asset_sha256: str

    def __post_init__(self):
        for name in ('workspace', 'edition', 'revision'):
            _text(getattr(self, name), name)
        if self.kind not in ('physical_page', 'logical_segment'):
            raise ValueError('Invalid reading-location kind.')
        if type(self.index) is not int or not 1 <= self.index <= 1000000:
            raise ValueError('Invalid source page or segment index.')
        for name in ('text_sha256', 'asset_sha256'):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(
                    '[0-9a-f]{64}', getattr(self, name)):
                raise ValueError('Invalid reading-location content digest.')


@dataclass(frozen=True)
class Bookmark:
    id: str
    label: str
    location: ReadingLocation
    created_at: str
    updated_at: str

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch('[0-9a-f]{32}', self.id):
            raise ValueError('Invalid bookmark identity.')
        _text(self.label, 'bookmark name', limit=200)
        if type(self.location) is not ReadingLocation:
            raise ValueError('A validated reading location is required.')
        _timestamp(self.created_at)
        _timestamp(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError('Bookmark modification precedes creation.')


def _location(value):
    if type(value) is not dict or set(value) != _LOCATION_KEYS:
        raise ValueError('Invalid reading-location fields.')
    return ReadingLocation(**value)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate reading-state field.')
        result[key] = value
    return result


def _decode(text, *, exported=False):
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Reading state exceeds its size limit.')
    try:
        value = json.loads(text, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON number.')))
    except (ValueError, RecursionError) as exc:
        raise ValueError('Reading state is invalid; the existing file was kept.') from exc
    keys = {'kind', 'version', 'positions', 'bookmarks'} | ({'workspace'} if exported else set())
    if (type(value) is not dict or set(value) != keys
            or value['kind'] != ('carr-reading-export' if exported else 'carr-reading-state')
            or type(value['version']) is not int or value['version'] != 1
            or type(value['positions']) is not list or type(value['bookmarks']) is not list
            or len(value['positions']) > MAX_POSITIONS or len(value['bookmarks']) > MAX_BOOKMARKS):
        raise ValueError('Unsupported or oversized reading state.')
    if exported:
        _text(value['workspace'], 'library identity')
    positions = {}
    for row in value['positions']:
        if type(row) is not dict or set(row) != {'location', 'updated_at'}:
            raise ValueError('Invalid saved position.')
        location = _location(row['location'])
        stamp = _timestamp(row['updated_at'])
        key = location.workspace, location.edition
        if key in positions:
            raise ValueError('Duplicate saved position for a source.')
        positions[key] = (location, stamp)
    bookmarks = {}
    for row in value['bookmarks']:
        if type(row) is not dict or set(row) != {'id', 'label', 'location', 'created_at', 'updated_at'}:
            raise ValueError('Invalid bookmark fields.')
        item = Bookmark(row['id'], row['label'], _location(row['location']),
                        row['created_at'], row['updated_at'])
        if item.id in bookmarks:
            raise ValueError('Duplicate bookmark identity.')
        bookmarks[item.id] = item
    if exported and any(location.workspace != value['workspace'] for location in
                        [row[0] for row in positions.values()] + [b.location for b in bookmarks.values()]):
        raise ValueError('Reading export contains another library.')
    return positions, bookmarks, value.get('workspace')


def _encode(positions, bookmarks, *, workspace=None):
    if len(positions) > MAX_POSITIONS or len(bookmarks) > MAX_BOOKMARKS:
        raise ValueError('Reading-state item limit reached. Export and remove unneeded bookmarks.')
    value = dict(kind='carr-reading-export' if workspace is not None else 'carr-reading-state', version=1,
        positions=[dict(location=asdict(location), updated_at=stamp)
                   for _, (location, stamp) in sorted(positions.items())],
        bookmarks=[asdict(bookmark) for _, bookmark in sorted(bookmarks.items())])
    if workspace is not None:
        value['workspace'] = workspace
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Reading state exceeds its size limit. Export and remove unneeded bookmarks.')
    return text


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class ReadingState:
    """Remember pages without recording or guessing a screen-reader cursor.

    Mutations merge the latest state under the library's cooperative lock.
    Invalid existing data is never silently reset. Imports validate every
    location before one atomic merge, preserve existing resume positions and
    reject conflicting bookmark IDs rather than overwriting personal names.
    """

    def __init__(self, root):
        self.root = _private_root(root)
        self.path = self.root / FILENAME

    def _read(self):
        _regular_if_present(self.path)
        try:
            before = self.path.lstat()
        except FileNotFoundError:
            return {}, {}, None
        if not 0 < before.st_size <= MAX_BYTES:
            raise ValueError('Reading-state file is empty or exceeds its size limit.')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(self.path, flags), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or _file_identity(opened) != _file_identity(before)):
                raise ValueError('Reading-state file changed while opening.')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) != before.st_size or _file_identity(self.path.lstat()) != _file_identity(before):
            raise ValueError('Reading-state file changed while reading.')
        try:
            text = raw.decode('utf-8')
        except UnicodeError as exc:
            raise ValueError('Reading state must be valid UTF-8. The existing file was kept.') from exc
        positions, bookmarks, _ = _decode(text)
        return positions, bookmarks, _file_identity(before)

    def _publish(self, positions, bookmarks, previous, *, cancelled=lambda: False):
        _check_cancelled(cancelled)
        raw = _encode(positions, bookmarks).encode('utf-8')
        temporary = None
        try:
            with NamedTemporaryFile(mode='wb', prefix='.reading-state-', suffix='.tmp',
                                    dir=self.root, delete=False) as output:
                temporary = Path(output.name)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            _regular_if_present(self.path)
            try:
                current = _file_identity(self.path.lstat())
            except FileNotFoundError:
                current = None
            if current != previous:
                raise ValueError('Reading state changed before saving. Try the action again.')
            _check_cancelled(cancelled)
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _scope(workspace, edition):
        return _text(workspace, 'library identity'), _text(edition, 'edition identity')

    def resume(self, workspace, edition):
        key = self._scope(workspace, edition)
        positions, _, _ = self._read()
        row = positions.get(key)
        return row[0] if row is not None else None

    def remember(self, location, *, cancelled=lambda: False):
        _check_cancelled(cancelled)
        if type(location) is not ReadingLocation:
            raise ValueError('A validated reading location is required.')
        with exclusive_writer(self.root):
            positions, bookmarks, previous = self._read()
            key = location.workspace, location.edition
            if key in positions and positions[key][0] == location:
                return
            positions[key] = (location, _now())
            self._publish(positions, bookmarks, previous, cancelled=cancelled)

    def bookmarks(self, workspace, edition):
        key = self._scope(workspace, edition)
        _, bookmarks, _ = self._read()
        return tuple(sorted((b for b in bookmarks.values()
                             if (b.location.workspace, b.location.edition) == key),
                            key=lambda b: (b.location.revision, b.location.index, b.label.casefold(), b.id)))

    def add_bookmark(self, location, label, *, cancelled=lambda: False):
        _check_cancelled(cancelled)
        if type(location) is not ReadingLocation:
            raise ValueError('A validated reading location is required.')
        stamp = _now()
        bookmark = Bookmark(uuid4().hex, _text(label, 'bookmark name', limit=200).strip(),
                            location, stamp, stamp)
        with exclusive_writer(self.root):
            positions, bookmarks, previous = self._read()
            bookmarks[bookmark.id] = bookmark
            self._publish(positions, bookmarks, previous, cancelled=cancelled)
        return bookmark

    def _selected(self, bookmarks, workspace, edition, bookmark_id):
        key = self._scope(workspace, edition)
        if not isinstance(bookmark_id, str):
            raise ValueError('Select a bookmark in this source.')
        item = bookmarks.get(bookmark_id)
        if item is None or (item.location.workspace, item.location.edition) != key:
            raise ValueError('Select a bookmark in this source.')
        return item

    def rename_bookmark(self, workspace, edition, bookmark_id, label, *, cancelled=lambda: False):
        _check_cancelled(cancelled)
        label = _text(label, 'bookmark name', limit=200).strip()
        with exclusive_writer(self.root):
            positions, bookmarks, previous = self._read()
            item = self._selected(bookmarks, workspace, edition, bookmark_id)
            updated = Bookmark(item.id, label, item.location, item.created_at,
                               max(item.updated_at, _now()))
            bookmarks[item.id] = updated
            self._publish(positions, bookmarks, previous, cancelled=cancelled)
        return updated

    def delete_bookmark(self, workspace, edition, bookmark_id, *, cancelled=lambda: False):
        _check_cancelled(cancelled)
        with exclusive_writer(self.root):
            positions, bookmarks, previous = self._read()
            item = self._selected(bookmarks, workspace, edition, bookmark_id)
            del bookmarks[item.id]
            self._publish(positions, bookmarks, previous, cancelled=cancelled)

    def export_state(self, workspace):
        _text(workspace, 'library identity')
        positions, bookmarks, _ = self._read()
        return _encode({key: row for key, row in positions.items() if key[0] == workspace},
                       {key: b for key, b in bookmarks.items() if b.location.workspace == workspace},
                       workspace=workspace)

    def import_state(self, workspace, text, validate_location, *, cancelled=lambda: False):
        """Merge a validated export, returning the number of new saved items.

        Source/workspace identities must still exist unchanged. Existing resume
        positions win over imported positions. Re-importing the same bookmarks
        is a no-op. All locations are checked even when their IDs already exist.
        """
        _check_cancelled(cancelled)
        _text(workspace, 'library identity')
        incoming_positions, incoming_bookmarks, exported_workspace = _decode(text, exported=True)
        if exported_workspace != workspace:
            raise ValueError('This reading export belongs to another library.')
        if not callable(validate_location):
            raise ValueError('Source validation is required before import.')
        locations = {row[0] for row in incoming_positions.values()} | {
            b.location for b in incoming_bookmarks.values()}
        for location in sorted(locations, key=lambda loc: (loc.edition, loc.revision, loc.index)):
            _check_cancelled(cancelled)
            validate_location(location)
        _check_cancelled(cancelled)
        with exclusive_writer(self.root):
            positions, bookmarks, previous = self._read()
            added = 0
            for key, row in incoming_positions.items():
                if key not in positions:
                    positions[key] = row
                    added += 1
            for identity, incoming in incoming_bookmarks.items():
                existing = bookmarks.get(identity)
                if existing is not None:
                    if (existing.location, existing.label) != (incoming.location, incoming.label):
                        raise ValueError('An imported bookmark ID conflicts with an existing bookmark. Nothing was changed.')
                else:
                    bookmarks[identity] = incoming
                    added += 1
            if added:
                self._publish(positions, bookmarks, previous, cancelled=cancelled)
        return added
