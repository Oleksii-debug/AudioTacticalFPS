"""Immutable selected library text for the existing translation jobs.

Selections reuse ReadingDesk and the narration source-anchor validator. These
records identify a derivative's original; they never authorize quotations or
a provider request and contain no local source-file paths.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import re

from .models import canonical, text
from ..catalog.membership import Catalog
from ..reader.reading_desk import ReadingDesk
from ..research.topic_tree import TopicTree
from ..speech.reading import _validated_source_anchor
from ..speech.source import read_source_range

MAX_SOURCES = 16
MAX_POSITIONS = 1000
MAX_CHARACTERS = 2_000_000
SCHEMA = 'carr-library-translation-source/1'


def _hash(value):
    return sha256(value.encode('utf-8')).hexdigest()


def _check(cancelled, guard):
    guard()
    if cancelled():
        raise InterruptedError('Library translation selection cancelled.')


@dataclass(frozen=True)
class LibrarySelection:
    edition: str
    first: int | None = None
    last: int | None = None

    def __post_init__(self):
        text(self.edition, 'selected source edition', maximum=1024)
        if (self.first is None) != (self.last is None):
            raise ValueError('Choose both first and last reading positions, or the whole document.')
        if self.first is not None and (type(self.first) is not int or type(self.last) is not int
                or not 1 <= self.first <= self.last <= 10000):
            raise ValueError('Choose a valid contiguous chapter or reading-position range.')


@dataclass(frozen=True)
class LibraryText:
    text: str
    binding: dict


def validate_source_binding(value, original):
    """Pure structural/hash validation for jobs and complete-backup recovery."""
    text(original, 'selected library text', maximum=MAX_CHARACTERS)
    keys = {'schema', 'workspace', 'topic', 'collection', 'sources',
            'characters', 'text_sha256', 'quotation_authority', 'sha256'}
    if (type(value) is not dict or set(value) != keys or value['schema'] != SCHEMA
            or value['quotation_authority'] is not False
            or type(value['characters']) is not int or value['characters'] != len(original)
            or value['text_sha256'] != _hash(original)
            or type(value['sources']) is not list or not 1 <= len(value['sources']) <= MAX_SOURCES):
        raise ValueError('Invalid library translation source binding.')
    text(value['workspace'], 'source workspace', maximum=1024)
    for name in ('topic', 'collection'):
        if value[name] is not None:
            text(value[name], 'source ' + name, maximum=1024)
    if (not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])
            or value['sha256'] != _hash(canonical({k: v for k, v in value.items() if k != 'sha256'}))):
        raise ValueError('Library translation binding checksum changed.')
    cursor, seen, count = 0, set(), 0
    for number, row in enumerate(value['sources']):
        fields = {'title', 'selection', 'first', 'last', 'start', 'end', 'anchor'}
        if (type(row) is not dict or set(row) != fields
                or row['selection'] not in ('document', 'chapter')
                or type(row['first']) is not int or type(row['last']) is not int
                or not 1 <= row['first'] <= row['last'] <= 10000
                or type(row['start']) is not int or type(row['end']) is not int
                or not 0 <= row['start'] <= row['end'] <= len(original)):
            raise ValueError('Invalid selected translation source range.')
        text(row['title'], 'source title', maximum=512)
        if number:
            if original[cursor:cursor + 2] != '\n\n':
                raise ValueError('The library translation separator changed.')
            cursor += 2
        if row['start'] != cursor:
            raise ValueError('The library translation source offsets changed.')
        anchor = _validated_source_anchor(row['anchor'], original[row['start']:row['end']])
        if (anchor['workspace'] != value['workspace'] or anchor['edition'] in seen
                or anchor['positions'][0]['ordinal'] != row['first']
                or anchor['positions'][-1]['ordinal'] != row['last']
                or row['selection'] == 'document' and row['first'] != 1):
            raise ValueError('The library translation source identity or order changed.')
        seen.add(anchor['edition'])
        count += len(anchor['positions'])
        if count > MAX_POSITIONS:
            raise ValueError('Choose at most 1000 source reading positions per translation.')
        cursor = row['end']
    if cursor != len(original):
        raise ValueError('Unbound text follows the selected library sources.')
    return deepcopy(value)


def _scope(library, workspace, topic, collection, editions):
    with library.read(workspace) as db:
        if topic is not None:
            TopicTree(db, workspace)._visible(topic)
        if collection is not None:
            Catalog(db, workspace)._active_collection(collection)
            for edition in editions:
                if not db.execute('SELECT 1 FROM catalog_membership WHERE workspace=? '
                                  'AND collection=? AND edition=?',
                                  (workspace, collection, edition)).fetchone():
                    raise ValueError('The selected source is no longer in this collection.')


def capture_library_sources(library, workspace, selections, *, topic=None, collection=None,
                            cancelled=lambda: False, guard=lambda: None):
    """Capture an explicit bounded chapter/document/batch without any networking."""
    if not isinstance(selections, (list, tuple)) or not 1 <= len(selections) <= MAX_SOURCES:
        raise ValueError('Explicitly select between 1 and 16 source documents.')
    if any(not isinstance(row, LibrarySelection) for row in selections):
        raise TypeError('Typed library source selections are required.')
    editions = [row.edition for row in selections]
    if len(set(editions)) != len(editions):
        raise ValueError('Select each edition once; choose one contiguous range per source.')
    _check(cancelled, guard)
    _scope(library, workspace, topic, collection, editions)
    pieces, sources, offset, positions = [], [], 0, 0
    for selection in selections:
        _check(cancelled, guard)
        desk = ReadingDesk(library, workspace, selection.edition, topic=topic, cancelled=cancelled)
        first = selection.first if selection.first is not None else 1
        last = selection.last if selection.last is not None else len(desk.pages)
        positions += last - first + 1
        if positions > MAX_POSITIONS:
            raise ValueError('Choose at most 1000 source reading positions per translation.')
        if pieces:
            pieces.append('\n\n')
            offset += 2
        remaining = MAX_CHARACTERS - offset
        if remaining < 1:
            raise ValueError('The selected translation exceeds the two-million-character budget.')
        source = read_source_range(desk, first, last, guard=guard, cancelled=cancelled,
                                   max_characters=remaining)
        start = offset
        pieces.append(source.text)
        offset += len(source.text)
        sources.append({'title': source.title, 'selection': 'document' if selection.first is None else 'chapter',
            'first': first, 'last': last, 'start': start, 'end': offset,
            'anchor': source.reading_document().source_anchor})
    _scope(library, workspace, topic, collection, editions)
    _check(cancelled, guard)
    original = ''.join(pieces)
    binding = {'schema': SCHEMA, 'workspace': workspace, 'topic': topic, 'collection': collection,
        'sources': sources, 'characters': len(original), 'text_sha256': _hash(original),
        'quotation_authority': False}
    binding['sha256'] = _hash(canonical(binding))
    authorize_library_binding(library, binding)
    _check(cancelled, guard)
    return LibraryText(original, validate_source_binding(binding, original))


def verify_library_sources(library, binding, original, *, cancelled=lambda: False,
                           guard=lambda: None):
    """Re-authorize the exact current source before transfer or derivative publication."""
    expected = validate_source_binding(binding, original)
    if library is None:
        raise PermissionError('Open this translation from its original library before using its selected sources.')
    selections = tuple(LibrarySelection(row['anchor']['edition'],
        None if row['selection'] == 'document' else row['first'],
        None if row['selection'] == 'document' else row['last']) for row in expected['sources'])
    current = capture_library_sources(library, expected['workspace'], selections,
        topic=expected['topic'], collection=expected['collection'], cancelled=cancelled, guard=guard)
    if current.text != original or current.binding != expected:
        raise ValueError('The selected library source changed; capture and approve a new translation.')
    return expected


def authorize_library_binding(library, binding):
    """Cheap current-scope check for shared reader Save/play handlers.

    Full byte verification is done by the cancellable source loader. This
    checks current permission and revision identity without hashing on the UI.
    """
    with library._search_read(binding['workspace'], 1.0, lambda: False) as (db, budget):
        if binding['topic'] is not None:
            TopicTree(db, binding['workspace'])._visible(binding['topic'])
        if binding['collection'] is not None:
            Catalog(db, binding['workspace'])._active_collection(binding['collection'])
        for source in binding['sources']:
            budget.check()
            anchor = source['anchor']
            row = db.execute('SELECT r.id,r.asset FROM active a JOIN revision r ON r.id=a.revision '
                'AND r.workspace=a.workspace AND r.edition=a.edition '
                'WHERE a.workspace=? AND a.edition=?', (binding['workspace'], anchor['edition'])).fetchone()
            if row is None or tuple(row) != (anchor['revision'], anchor['asset_sha256']):
                raise ValueError('The selected original revision changed. Reopen the linked translation.')
            if binding['collection'] is not None and not db.execute(
                    'SELECT 1 FROM catalog_membership WHERE workspace=? AND collection=? AND edition=?',
                    (binding['workspace'], binding['collection'], anchor['edition'])).fetchone():
                raise PermissionError('The original left the selected collection.')


def source_summary(binding):
    """Small explicit transfer summary without source text or external path leakage."""
    return {'workspace': binding['workspace'], 'topic': binding['topic'],
        'collection': binding['collection'], 'documents': len(binding['sources']),
        'positions': sum(row['last'] - row['first'] + 1 for row in binding['sources']),
        'unverified_positions': sum(not position['text_verified_at_capture']
            for row in binding['sources'] for position in row['anchor']['positions']),
        'characters': binding['characters'], 'source_binding_sha256': binding['sha256'],
        'sources': [{'title': row['title'], 'edition': row['anchor']['edition'],
                     'revision': row['anchor']['revision'], 'selection': row['selection'],
                     'first': row['first'], 'last': row['last']} for row in binding['sources']],
        'quotation_authority': False}
