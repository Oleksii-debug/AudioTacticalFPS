"""Shared exact reading selections for books and structured translation.

This adapts canonical translation_source/read_source_range onto the current
ReadingDesk and the already installed non-authorizing source-anchor validator.
No second reader, translation gateway or speech document is constructed.
"""
from collections import OrderedDict
from dataclasses import asdict
from hashlib import sha256
import math
from time import monotonic_ns

from .reading_desk import ReadingDesk
from .reading_state import ReadingLocation
from .legacy_reading import validate_anchor

MAX_DOCUMENTS = 32
MAX_CHARACTERS = 2_000_000
MAX_TOTAL_CHARACTERS = 8_000_000
MAX_POSITIONS = 10_000
_SOURCE_FIELDS = {'text', 'title', 'warnings', 'source_anchor', 'source_metadata', 'source_topic'}
_METADATA_FIELDS = {'author', 'title', 'volume', 'edition'}


def _text(value, label, *, maximum=16384, empty=False):
    if (type(value) is not str or len(value) > maximum or '\x00' in value
            or not empty and not value.strip()):
        raise ValueError('Invalid ' + label + '.')
    value.encode('utf-8', 'strict')
    return value


def _bound(value, maximum, label):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError('Invalid ' + label + '.')
    return value


class _Budget:
    def __init__(self, cancelled, guard, timeout):
        if (not callable(cancelled) or not callable(guard) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not .001 <= timeout <= 3600):
            raise ValueError('Choose a finite source-reading budget from 0.001 to 3600 seconds.')
        numerator, denominator = float(timeout).as_integer_ratio()
        self.deadline = monotonic_ns() + numerator * 1_000_000_000 // denominator
        self.cancelled, self.guard = cancelled, guard

    def check(self):
        if self.cancelled():
            raise InterruptedError('Reading selection cancelled; no source was changed.')
        if monotonic_ns() >= self.deadline:
            raise TimeoutError('The selected reading sources exceeded their shared deadline.')
        self.guard()
        if self.cancelled():
            raise InterruptedError('Reading selection cancelled; no source was changed.')
        if monotonic_ns() >= self.deadline:
            raise TimeoutError('The selected reading sources exceeded their shared deadline.')

    def stopped(self):
        self.check()
        return False

    def remaining(self):
        self.check()
        return min(60.0, max(.000001, (self.deadline - monotonic_ns()) / 1_000_000_000))


def source_context(value, anchor=None, metadata=None, topic=None):
    """Pure portable lineage validation; this never grants quotation authority."""
    _text(value, 'source text', maximum=MAX_CHARACTERS, empty=True)
    if anchor is None:
        if metadata is not None or topic is not None:
            raise ValueError('Source metadata requires an exact reading-source anchor.')
        return None
    checked = validate_anchor(anchor, value)
    if (type(metadata) is not dict or set(metadata) != _METADATA_FIELDS
            or any(item is not None and
                   (type(item) is not str or len(item) > 16384 or '\x00' in item)
                   for item in metadata.values())):
        raise ValueError('Invalid original source metadata.')
    for item in metadata.values():
        if item is not None:
            item.encode('utf-8', 'strict')
    if topic is not None:
        _text(topic, 'source topic', maximum=1024)
    result = asdict(checked)
    result['positions'] = [dict(row) for row in result['positions']]
    return dict(source_anchor=result, source_metadata=dict(metadata), source_topic=topic)


def validate_capture(source):
    """Check only the shape and exact page hashes of a saved source snapshot."""
    if type(source) is not dict or set(source) != _SOURCE_FIELDS:
        raise ValueError('Invalid captured reading-source fields.')
    _text(source['title'], 'source title')
    warnings = source['warnings']
    if (type(warnings) is not list or len(warnings) > MAX_POSITIONS
            or any(type(item) is not str or len(item) > 4096 or '\x00' in item for item in warnings)):
        raise ValueError('Invalid source-reading notices.')
    for item in warnings:
        item.encode('utf-8', 'strict')
    context = source_context(source['text'], source['source_anchor'],
                             source['source_metadata'], source['source_topic'])
    if context is None:
        raise ValueError('A selected library source needs its exact non-authorizing lineage.')
    return dict(text=source['text'], title=source['title'], warnings=list(warnings), **context)


def _capture(library, workspace, edition, *, topic, first, last, chapter, revision,
             max_characters, budget):
    budget.check()
    desk = ReadingDesk(library, workspace, edition, topic=topic, revision=revision,
                       cancelled=budget.stopped, timeout=budget.remaining())
    if chapter is not None:
        if first is not None or last is not None or type(chapter) is not int:
            raise ValueError('Choose an EPUB chapter or a source-position range, not both.')
        from .ebook_contents import read_chapter
        read_chapter(library, workspace, edition, desk.revision, chapter,
                     authorized_topic=topic, cancelled=budget.stopped)
        first = last = chapter
    else:
        first = 1 if first is None else first
        last = len(desk.pages) if last is None else last
    if (type(first) is not int or type(last) is not int
            or not 1 <= first <= last <= len(desk.pages)):
        raise ValueError('Choose a valid ordered source-position range.')
    pieces, positions, warnings, offset = [], [], [], 0
    for ordinal in range(first, last + 1):
        budget.check()
        desk.timeout = budget.remaining()
        page = desk.page(ordinal)
        separator = 2 if pieces else 0
        if offset + separator + len(page.raw_text) > max_characters:
            raise ValueError('The selected source exceeds its character budget; choose fewer positions.')
        if pieces:
            pieces.append('\n\n')
            offset += 2
        start = offset
        pieces.append(page.raw_text)
        offset += len(page.raw_text)
        positions.append(dict(ordinal=page.ordinal, kind=page.location.kind,
            index=page.location.index, start=start, end=offset,
            text_sha256=page.location.text_sha256, text_verified_at_capture=page.verified))
        if not page.verified:
            warnings.append(f'Reading position {ordinal}: extracted text has not been independently approved.')
    source = dict(text=''.join(pieces), title=desk.title, warnings=warnings,
        source_anchor=dict(workspace=desk.workspace, edition=desk.edition, revision=desk.revision,
            asset_sha256=desk.identity.asset_sha256, positions=positions, quotation_authority=False),
        source_metadata=asdict(desk.metadata), source_topic=topic)
    budget.check()
    return validate_capture(source)


def _validate_current(library, source, budget):
    source = validate_capture(source)
    anchor = source['source_anchor']
    budget.check()
    desk = ReadingDesk(library, anchor['workspace'], anchor['edition'],
        topic=source['source_topic'], revision=anchor['revision'],
        cancelled=budget.stopped, timeout=budget.remaining())
    if asdict(desk.metadata) != source['source_metadata'] or desk.title != source['title']:
        raise ValueError('The selected source metadata changed.')
    for row in anchor['positions']:
        budget.check()
        location = ReadingLocation(anchor['workspace'], anchor['edition'], anchor['revision'],
            row['kind'], row['index'], row['text_sha256'], anchor['asset_sha256'])
        desk.timeout = budget.remaining()
        page = desk.validate(location)
        if page.ordinal != row['ordinal'] or page.raw_text != source['text'][row['start']:row['end']]:
            raise ValueError('The selected source bytes or reading order changed.')
        # A later independent text approval is allowed; this historical flag
        # never grants quotation authority or changes the actual source bytes.
    budget.check()
    return source


def capture_source(library, workspace, edition, *, topic=None, first=None, last=None,
                   chapter=None, revision=None, max_characters=MAX_CHARACTERS,
                   timeout=60.0, cancelled=lambda: False, guard=lambda: None):
    _bound(max_characters, MAX_CHARACTERS, 'source character budget')
    budget = _Budget(cancelled, guard, timeout)
    source = _capture(library, workspace, edition, topic=topic, first=first, last=last,
        chapter=chapter, revision=revision, max_characters=max_characters, budget=budget)
    return _validate_current(library, source, budget)


def capture_sources(library, selections, *, max_documents=MAX_DOCUMENTS,
                    max_total_characters=MAX_TOTAL_CHARACTERS, timeout=60.0,
                    cancelled=lambda: False, guard=lambda: None):
    """Capture explicit ordered document/chapter selections, completely or fail."""
    _bound(max_documents, MAX_DOCUMENTS, 'selected document count')
    _bound(max_total_characters, MAX_TOTAL_CHARACTERS, 'combined source character budget')
    if type(selections) not in (list, tuple) or not 1 <= len(selections) <= max_documents:
        raise ValueError('Choose a bounded nonempty ordered source selection.')
    budget = _Budget(cancelled, guard, timeout)
    result, total, seen = [], 0, set()
    allowed = {'workspace', 'edition', 'first', 'last', 'kind', 'revision', 'title', 'topic'}
    for item in selections:
        budget.check()
        if (type(item) is not dict or not {'workspace', 'edition'} <= set(item) <= allowed
                or item.get('kind', 'document') not in ('document', 'range', 'chapter')):
            raise ValueError('Invalid explicit document/chapter selection.')
        first, last = item.get('first'), item.get('last')
        chapter = None
        if item.get('kind', 'document') == 'range' and (type(first) is not int or type(last) is not int):
            raise ValueError('A source-location range needs explicit first and last ordinals.')
        if item.get('kind', 'document') == 'chapter':
            if type(first) is not int or last not in (None, first):
                raise ValueError('A chapter selection needs its one exact reading-order ordinal.')
            chapter, first, last = first, None, None
        remaining = max_total_characters - total
        if remaining <= 0:
            raise ValueError('The combined reading selection exceeds its character budget.')
        source = _capture(library, item['workspace'], item['edition'], topic=item.get('topic'),
            first=first, last=last, chapter=chapter, revision=item.get('revision'),
            max_characters=min(MAX_CHARACTERS, remaining), budget=budget)
        if item.get('title') is not None and item['title'] != source['title']:
            raise ValueError('The selected source title changed; review the current source.')
        anchor = source['source_anchor']
        key = (anchor['workspace'], anchor['edition'], anchor['revision'],
               anchor['positions'][0]['ordinal'], anchor['positions'][-1]['ordinal'])
        if key in seen:
            raise ValueError('The ordered selection repeats the same source range.')
        seen.add(key)
        total += len(source['text'])
        result.append(source)
    # Recheck the earliest selection after the latest capture, without retargeting
    # a new active revision or accepting an original with changed content.
    for source in result:
        _validate_current(library, source, budget)
    budget.check()
    return tuple(result)


def source_pages(source):
    """Expose exact page text and original location without copying a new snapshot."""
    source = validate_capture(source)
    anchor = source['source_anchor']
    return tuple(dict(location=asdict(ReadingLocation(anchor['workspace'], anchor['edition'],
        anchor['revision'], row['kind'], row['index'], row['text_sha256'], anchor['asset_sha256'])),
        ordinal=row['ordinal'], raw_text=source['text'][row['start']:row['end']],
        source_start=row['start'], source_end=row['end'], verified=row['text_verified_at_capture'])
        for row in anchor['positions'])


def validate_source(library, value, anchor, metadata, topic=None, *,
                    timeout=60.0, cancelled=lambda: False, guard=lambda: None):
    context = source_context(value, anchor, metadata, topic)
    if context is None:
        return None
    source = dict(text=value, title=metadata['title'] or 'Unknown title', warnings=[], **context)
    return _validate_current(library, source, _Budget(cancelled, guard, timeout))


def validate_fragments(library, origins, *, topic=None, metadata=None,
                       max_characters=MAX_TOTAL_CHARACTERS, timeout=60.0,
                       cancelled=lambda: False, guard=lambda: None):
    """Resolve exact page-relative fragments, with bounded cached source pages.

    Each origin is {location, ordinal, start, end}, optionally with exact text.
    Optional metadata maps (workspace, edition, revision) to captured metadata.
    Returned fragments preserve input order; they never authorize quotations.
    """
    _bound(max_characters, MAX_TOTAL_CHARACTERS, 'fragment character budget')
    if type(origins) not in (tuple, list) or not 1 <= len(origins) <= MAX_POSITIONS:
        raise ValueError('Choose one to 10,000 exact source fragments.')
    if metadata is not None and (type(metadata) is not dict or len(metadata) > MAX_DOCUMENTS):
        raise ValueError('Invalid bounded source metadata selection.')
    budget = _Budget(cancelled, guard, timeout)
    desks, pages, checked, results, total, cached = {}, OrderedDict(), {}, [], 0, 0
    for origin in origins:
        budget.check()
        if (type(origin) is not dict or not {'location', 'ordinal', 'start', 'end'} <= set(origin)
                <= {'location', 'ordinal', 'start', 'end', 'text'}
                or type(origin['location']) is not dict):
            raise ValueError('Invalid exact source-fragment fields.')
        try:
            location = ReadingLocation(**origin['location'])
        except (TypeError, KeyError) as error:
            raise ValueError('Invalid source-fragment location.') from error
        key = (location.workspace, location.edition, location.revision)
        if key not in desks:
            if len(desks) >= MAX_DOCUMENTS:
                raise ValueError('The fragments exceed their selected document count.')
            desk = ReadingDesk(library, location.workspace, location.edition, topic=topic,
                revision=location.revision, cancelled=budget.stopped, timeout=budget.remaining())
            if metadata is not None and (key not in metadata or metadata[key] != asdict(desk.metadata)):
                raise ValueError('The source-fragment metadata changed.')
            desks[key] = desk
        desk = desks[key]
        if (type(origin['ordinal']) is not int or type(origin['start']) is not int
                or type(origin['end']) is not int or not 0 <= origin['start'] <= origin['end']):
            raise ValueError('Invalid exact source-fragment offsets.')
        page_key = (key, location.kind, location.index, location.text_sha256, location.asset_sha256)
        checked[page_key] = (location, origin['ordinal'])
        page = pages.get(page_key)
        if page is None:
            desk.timeout = budget.remaining()
            page = desk.validate(location)
            while pages and (len(pages) >= 8 or cached + len(page.raw_text) > MAX_CHARACTERS):
                _, removed = pages.popitem(last=False)
                cached -= len(removed.raw_text)
            pages[page_key] = page
            cached += len(page.raw_text)
        else:
            pages.move_to_end(page_key)
        if page.ordinal != origin['ordinal'] or origin['end'] > len(page.raw_text):
            raise ValueError('The source-fragment reading order or range changed.')
        count = origin['end'] - origin['start']
        if total + count > max_characters:
            raise ValueError('The exact source fragments exceed their character budget.')
        value = page.raw_text[origin['start']:origin['end']]
        if 'text' in origin and origin['text'] != value:
            raise ValueError('The retained fragment differs from its exact source bytes.')
        results.append(value)
        total += count
    # Revalidate every selected page after the last fragment, not an arbitrary
    # page-one surrogate. This also catches database page changes during a long
    # selection while leaving intentionally unselected oversized pages unread.
    for location, ordinal in checked.values():
        budget.check()
        desk = desks[(location.workspace, location.edition, location.revision)]
        desk.timeout = budget.remaining()
        if desk.validate(location).ordinal != ordinal:
            raise ValueError('The selected source-fragment order changed during validation.')
    budget.check()
    return tuple(results)
