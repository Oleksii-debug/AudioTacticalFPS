"""Bounded, explicit document/chapter/batch snapshots for translation.

The existing reader authenticates each retained original and sealed source.
This module keeps structural source headings and locators; it grants no
quotation authority and never reads a remote URL or mutates an original.
"""
from dataclasses import asdict, dataclass
import re

from .models import canonical, text
from .translation_state import MAX_SOURCE_CHARACTERS, text_hash
from ..citations.anchors import Locator
from ..reader.reading_desk import ReadingDesk
from ..reader.ebook_contents import open_ebook

SELECTION_SCHEMA = 'carr-translation-selection/1'
MAX_ITEMS = 100
MAX_BLOCKS = 512
MAX_SELECTION_BYTES = 16 * 1024 * 1024
_HEX = re.compile('[0-9a-f]{64}')


@dataclass(frozen=True)
class DocumentSelection:
    edition: str
    revision: str | None = None
    chapter: int | None = None

    def __post_init__(self):
        text(self.edition, 'translation edition', maximum=512)
        if self.revision is not None:
            text(self.revision, 'translation revision', maximum=512)
        if self.chapter is not None and (type(self.chapter) is not int or not 1 <= self.chapter <= 1000):
            raise ValueError('Select an exact EPUB chapter ordinal.')


def _check(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Translation selection cancelled.')


def _hash(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError('Invalid translation source digest.')


def blocks(selection):
    """Yield source slices at declared heading boundaries, including empty pages."""
    for item_index, item in enumerate(selection['items']):
        for section_index, section in enumerate(item['sections']):
            value = section['source_text']
            boundaries = sorted({0, len(value), *(h['offset'] for h in section['headings'])})
            if not value:
                boundaries = [0, 0]
            for block_index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
                yield dict(item=item_index, section=section_index, block=block_index,
                           start=start, end=end, text=value[start:end],
                           headings=[h for h in section['headings'] if h['offset'] == start])


def validate_selection(value):
    keys = {'schema', 'workspace', 'topic', 'mode', 'items'}
    if type(value) is not dict or set(value) != keys or value['schema'] != SELECTION_SCHEMA:
        raise ValueError('Invalid typed translation selection.')
    text(value['workspace'], 'translation workspace', maximum=512)
    if value['topic'] is not None:
        text(value['topic'], 'translation topic', maximum=512)
    if value['mode'] not in ('document', 'chapter', 'batch'):
        raise ValueError('Choose document, chapter or batch translation.')
    items = value['items']
    if (type(items) is not list or not 1 <= len(items) <= MAX_ITEMS
            or (value['mode'] != 'batch' and len(items) != 1)):
        raise ValueError('The translation document inventory is invalid.')
    characters, seen = 0, set()
    for item in items:
        expected = {'edition', 'revision', 'asset_sha256', 'mime', 'title', 'author',
                    'chapter', 'total_sections', 'sections'}
        if type(item) is not dict or set(item) != expected:
            raise ValueError('Invalid translation document source.')
        DocumentSelection(item['edition'], item['revision'], item['chapter'])
        if item['revision'] is None:
            raise ValueError('Translation must pin an exact source revision.')
        _hash(item['asset_sha256'])
        for key in ('mime', 'title', 'author'):
            text(item[key], 'translation source ' + key, maximum=4096)
        if type(item['total_sections']) is not int or not 1 <= item['total_sections'] <= 10000:
            raise ValueError('Invalid translation source section count.')
        chapter = item['chapter']
        if ((value['mode'] == 'document' and chapter is not None)
                or (value['mode'] == 'chapter' and chapter is None)
                or (chapter is not None and (item['mime'] != 'application/epub+zip'
                                            or chapter > item['total_sections']))):
            raise ValueError('Chapter selection requires an actual EPUB chapter.')
        key = (item['edition'], item['revision'], chapter)
        if key in seen or any(old[:2] == key[:2] and (old[2] is None or chapter is None) for old in seen):
            raise ValueError('Overlapping document selections would repeat a paid translation.')
        seen.add(key)
        sections = item['sections']
        ordinals = [chapter] if chapter is not None else list(range(1, item['total_sections'] + 1))
        if type(sections) is not list or len(sections) != len(ordinals):
            raise ValueError('Translation selection omitted a source section.')
        previous = None
        for ordinal, section in zip(ordinals, sections):
            expected = {'ordinal', 'locator', 'heading', 'heading_basis', 'headings',
                        'source_text', 'source_sha256', 'source_text_verified'}
            if (type(section) is not dict or set(section) != expected
                    or type(section['ordinal']) is not int or section['ordinal'] != ordinal
                    or type(section['source_text_verified']) is not bool):
                raise ValueError('Invalid ordered translation section.')
            location = section['locator']
            if type(location) is not dict or set(location) != {'kind', 'index', 'printed', 'verified'}:
                raise ValueError('Invalid translation source locator.')
            locator = Locator(**location)
            if locator.index > 1000000 or (previous is not None and (
                    locator.kind != previous.kind or locator.index <= previous.index)):
                raise ValueError('Translation locators are out of source order.')
            previous = locator
            if locator.printed is not None:
                text(locator.printed, 'source printed label', maximum=512, empty=True)
            text(section['heading'], 'source heading', maximum=4096)
            if section['heading_basis'] not in ('epub_chapter', 'source_locator'):
                raise ValueError('Invalid source heading basis.')
            if section['heading_basis'] == 'epub_chapter' and item['mime'] != 'application/epub+zip':
                raise ValueError('An unstructured source cannot invent an EPUB heading.')
            source = text(section['source_text'], 'immutable source text',
                          maximum=MAX_SOURCE_CHARACTERS, empty=True)
            characters += len(source)
            if characters > MAX_SOURCE_CHARACTERS or section['source_sha256'] != text_hash(source):
                raise ValueError('Translation source exceeds its bound or differs from its digest.')
            headings = section['headings']
            if type(headings) is not list or len(headings) > MAX_BLOCKS:
                raise ValueError('Invalid source heading inventory.')
            last = -1
            for heading in headings:
                if (type(heading) is not dict or set(heading) != {'title', 'offset', 'level'}
                        or type(heading['offset']) is not int or not 0 <= heading['offset'] <= len(source)
                        or heading['offset'] < last
                        or type(heading['level']) is not int or not 1 <= heading['level'] <= 6):
                    raise ValueError('Invalid structural heading boundary.')
                text(heading['title'], 'structural source heading', maximum=4096)
                last = heading['offset']
            if item['mime'] != 'application/epub+zip' and headings:
                raise ValueError('Only declared EPUB headings supply structural boundaries.')
    units = list(blocks(value))
    if not 1 <= len(units) <= MAX_BLOCKS or not any(unit['text'].strip() for unit in units):
        raise ValueError('Select bounded readable text in at most 512 structural blocks.')
    if len(canonical(value).encode('utf-8')) > MAX_SELECTION_BYTES:
        raise ValueError('Translation selection exceeds its snapshot byte budget.')
    return value


def capture_selection(app, workspace, selections, *, mode='document', topic=None,
                      cancelled=lambda: False):
    """Materialize only an explicit bounded selection, with no provider call."""
    _check(cancelled)
    if not isinstance(selections, (tuple, list)) or not 1 <= len(selections) <= MAX_ITEMS:
        raise ValueError('Choose from one to 100 explicit document selections.')
    if any(type(item) is not DocumentSelection for item in selections):
        raise ValueError('Explicit document selections are required.')
    if (mode not in ('document', 'chapter', 'batch') or (mode != 'batch' and len(selections) != 1)
            or (mode == 'document' and selections[0].chapter is not None)
            or (mode == 'chapter' and selections[0].chapter is None)):
        raise ValueError('Choose an exact document, chapter or ordered batch.')
    result = dict(schema=SELECTION_SCHEMA, workspace=workspace, topic=topic, mode=mode, items=[])
    total = 0
    for selected in selections:
        _check(cancelled)
        desk = ReadingDesk(app, workspace, selected.edition, topic=topic, revision=selected.revision,
                           cancelled=cancelled)
        with app.read(workspace) as db:
            _, mime, _ = app._retained_source(db, workspace, desk.identity, cancelled=cancelled)
            ordinals = [selected.chapter] if selected.chapter is not None else [p.ordinal for p in desk.pages]
            if any(not 1 <= n <= len(desk.pages) for n in ordinals):
                raise ValueError('The selected chapter is outside this source.')
            sizes = {row['ordinal']: row['size'] for row in db.execute(
                'SELECT ordinal,length(raw) AS size FROM page WHERE revision=? ORDER BY ordinal',
                (desk.revision,))}
            if any(type(sizes.get(n)) is not int or sizes[n] < 0 for n in ordinals):
                raise ValueError('The selected source text is unavailable.')
            total += sum(sizes[n] for n in ordinals)
            if total > MAX_SOURCE_CHARACTERS:
                raise ValueError('This selection exceeds two million source characters. Choose smaller explicit chapters.')
        book = open_ebook(app, workspace, selected.edition, authorized_topic=topic,
                          cancelled=cancelled) if mime == 'application/epub+zip' else None
        if selected.chapter is not None and book is None:
            raise ValueError('Chapter selection requires an actual EPUB chapter; pages are not inferred chapters.')
        if book is not None and book['revision'] != desk.revision:
            raise ValueError('The source chapter revision changed during selection.')
        item = dict(edition=selected.edition, revision=desk.revision,
                    asset_sha256=desk.identity.asset_sha256, mime=mime,
                    title=desk.title, author=desk.author, chapter=selected.chapter,
                    total_sections=len(desk.pages), sections=[])
        for ordinal in ordinals:
            _check(cancelled)
            page = desk.page(ordinal)
            chapter = book['chapters'][ordinal - 1] if book else None
            headings = [dict(row) for row in chapter['headings']] if chapter else []
            section = dict(ordinal=ordinal, locator=asdict(page.locator),
                heading=chapter['title'] if chapter else page.locator.display(),
                heading_basis='epub_chapter' if chapter else 'source_locator',
                headings=headings, source_text=page.raw_text,
                source_sha256=page.location.text_sha256, source_text_verified=page.verified)
            item['sections'].append(section)
            if sum(1 for _ in blocks(dict(items=[*result['items'], item]))) > MAX_BLOCKS:
                raise ValueError('This selection exceeds 512 structural blocks. Choose smaller explicit chapters.')
        result['items'].append(item)
    _check(cancelled)
    return validate_selection(result)


def revalidate_selection(app, selection, *, cancelled=lambda: False):
    """No scope/revision/source drift may borrow the previously reviewed text."""
    validate_selection(selection)
    current = capture_selection(app, selection['workspace'],
        [DocumentSelection(item['edition'], item['revision'], item['chapter'])
         for item in selection['items']], mode=selection['mode'], topic=selection['topic'], cancelled=cancelled)
    if current != selection:
        raise PermissionError('The reviewed source structure changed. Capture and approve a new selection.')
    return current
