"""Additive EPUB contents over the existing sealed section-reader contract.

Older imports keep their original settings and expose only chapter starts.
New imports retain the legacy chapters/alternatives fields and add a bounded
contents descriptor. Neither route reparses originals or grants quotation
approval. Every visit rechecks scope, the active revision and retained bytes.
"""
from hashlib import sha256
import json
import re

from ..catalog.membership import Catalog
from ..catalog.read_views import get_edition
from ..citations.anchors import Identity
from ..ingest.pipeline import canonical
from ..research.topic_tree import TopicTree
from .ebook import _chapters as _legacy_chapters


ADAPTER = 'epub-spine-v1'
MAX_SETTINGS = 4 * 1024 * 1024
MAX_NAVIGATION = 2 * 1024 * 1024
MAX_ITEMS = 2000
MAX_TEXT = 10_000_000
_DIGEST = re.compile('[0-9a-f]{64}')


def _check_cancelled(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Reading the electronic book was cancelled.')


def _label(value, *, empty=False, limit=4096):
    if (not isinstance(value, str) or len(value) > limit or '\x00' in value
            or (not empty and not value.strip())):
        raise ValueError('Invalid electronic-book contents label.')
    return value


def _warnings(value, *, limit=1000):
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError('Invalid electronic-book extraction warnings.')
    return [_label(item, limit=1024) for item in value]


def _descriptions(entries):
    # Keep authored alternatives separate from the extracted quotation text.
    return [text for _, text in entries if text.strip()]


def _navigation(value):
    """Validate controls and offsets; the persisted byte bound is separate."""
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1):
        raise ValueError('Unsupported electronic-book contents version.')
    records = value.get('chapters')
    if not isinstance(records, list) or not 1 <= len(records) <= 1000:
        raise ValueError('Invalid electronic-book chapter count.')
    total, navigation_count, chapters = 0, 0, []
    for ordinal, record in enumerate(records, 1):
        if (not isinstance(record, dict) or type(record.get('ordinal')) is not int
                or record['ordinal'] != ordinal or type(record.get('linear')) is not bool
                or type(record.get('characters')) is not int or record['characters'] < 0
                or not isinstance(record.get('text_sha256'), str)
                or not _DIGEST.fullmatch(record['text_sha256'])):
            raise ValueError('Invalid electronic-book chapter descriptor.')
        total += record['characters']
        if total > MAX_TEXT:
            raise ValueError('Electronic-book text exceeds its budget.')
        headings = record.get('headings', [])
        descriptions = record.get('image_descriptions', [])
        if (not isinstance(headings, list) or not isinstance(descriptions, list)
                or len(descriptions) > 10000):
            raise ValueError('Invalid electronic-book heading or image-description list.')
        checked_headings = []
        for heading in headings:
            if (not isinstance(heading, dict) or type(heading.get('offset')) is not int
                    or not 0 <= heading['offset'] <= record['characters']
                    or type(heading.get('level')) is not int or not 1 <= heading['level'] <= 6):
                raise ValueError('Invalid electronic-book heading location.')
            checked_headings.append(dict(title=_label(heading.get('title')),
                                         offset=heading['offset'], level=heading['level']))
        navigation_count += len(checked_headings)
        chapters.append(dict(ordinal=ordinal, title=_label(record.get('title')),
            member=_label(record.get('member')), linear=record['linear'],
            characters=record['characters'], text_sha256=record['text_sha256'],
            headings=checked_headings, warnings=_warnings(record.get('warnings', [])),
            image_descriptions=[_label(item) for item in descriptions]))

    def links(rows, *, grouping=False):
        nonlocal navigation_count
        if not isinstance(rows, list):
            raise ValueError('Invalid electronic-book navigation list.')
        navigation_count += len(rows)
        checked = []
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('depth', 0)) is not int
                    or not 0 <= row.get('depth', 0) <= 64
                    or type(row.get('heading_only', False)) is not bool):
                raise ValueError('Invalid electronic-book navigation depth or heading.')
            title = _label(row.get('title'))
            if row.get('heading_only', False):
                if not grouping or row.get('ordinal') is not None or row.get('offset') is not None:
                    raise ValueError('A contents heading cannot supply a chapter location.')
                checked.append(dict(title=title, ordinal=None, offset=None,
                                    depth=row.get('depth', 0), heading_only=True))
                continue
            ordinal, offset = row.get('ordinal'), row.get('offset')
            if (type(ordinal) is not int or not 1 <= ordinal <= len(chapters)
                    or type(offset) is not int
                    or not 0 <= offset <= chapters[ordinal - 1]['characters']):
                raise ValueError('Electronic-book contents point outside their chapter.')
            checked.append(dict(title=title, ordinal=ordinal, offset=offset,
                                depth=row.get('depth', 0)))
        return checked

    toc = links(value.get('toc', []), grouping=True)
    page_hints = links(value.get('page_hints', []))
    if navigation_count > MAX_ITEMS:
        raise ValueError('Electronic-book navigation exceeds its budget.')
    hints = value.get('metadata_hints', {})
    if not isinstance(hints, dict):
        raise ValueError('Invalid electronic-book metadata hints.')
    metadata = {}
    for key in ('titles', 'creators', 'languages', 'identifiers', 'publishers'):
        items = hints.get(key, [])
        if not isinstance(items, (list, tuple)) or len(items) > 100:
            raise ValueError('Invalid electronic-book metadata hint values.')
        metadata[key] = [_label(item) for item in items]
    return dict(version=1, chapters=chapters, toc=toc, page_hints=page_hints,
                metadata_hints=metadata,
                # Legacy settings permit 1000 warnings; the honest fallback
                # adds one local notice without changing that stored contract.
                warnings=_warnings(value.get('warnings', []), limit=1001))


def _alternatives(values):
    if not isinstance(values, list) or len(values) > 10000:
        raise ValueError('Invalid electronic-book image alternatives.')
    checked, previous = [], 0
    for value in values:
        if (not isinstance(value, dict) or value.get('kind') != 'image_alt'
                or type(value.get('image_index')) is not int or value['image_index'] <= previous):
            raise ValueError('Invalid electronic-book image alternative identity.')
        text = _label(value.get('text'), empty=True)
        previous = value['image_index']
        checked.append(dict(kind='image_alt', image_index=previous, text=text))
    return checked


def extraction_settings(extracted):
    """Retain canonical section settings and add hash-bound contents metadata."""
    records = extracted.get('records') if isinstance(extracted, dict) else None
    if not isinstance(records, list) or not 1 <= len(records) <= 1000:
        raise ValueError('Invalid electronic-book extraction records.')
    provenance = extracted.get('provenance')
    if (not isinstance(provenance, dict) or not isinstance(provenance.get('asset_sha256'), str)
            or not _DIGEST.fullmatch(provenance['asset_sha256'])):
        raise ValueError('Invalid electronic-book source provenance.')
    locators, alternatives, chapters = [], [], []
    for ordinal, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise ValueError('Invalid electronic-book extraction record.')
        raw, locator = record.get('raw_text'), record.get('logical_locator')
        if (not isinstance(raw, str) or '\x00' in raw
                or sha256(raw.encode('utf-8')).hexdigest() != record.get('text_sha256')
                or record.get('physical_index') is not None
                or record.get('printed_label') is not None
                or record.get('printed_label_verified', False) is not False):
            raise ValueError('Electronic-book extraction text or logical location changed.')
        if (not isinstance(locator, dict) or type(locator.get('spine_index')) is not int
                or locator['spine_index'] != ordinal or type(locator.get('linear')) is not bool):
            raise ValueError('Invalid electronic-book spine location.')
        title = _label(locator.get('title'), empty=True)
        member = _label(locator.get('href'))
        locators.append(dict(locator))
        entries = _alternatives(record.get('alternatives', []))
        alternatives.append(entries)
        chapters.append(dict(ordinal=ordinal, title=title or f'Section {ordinal}',
            member=member, linear=locator['linear'], characters=len(raw),
            text_sha256=record['text_sha256'], headings=record.get('headings', []),
            warnings=record.get('warnings', []),
            image_descriptions=_descriptions([(item['image_index'], item['text']) for item in entries])))
    warnings = _warnings(extracted.get('warnings', []))
    ebook = _navigation(dict(version=1, chapters=chapters, toc=extracted.get('toc', []),
        page_hints=extracted.get('page_hints', []), metadata_hints=extracted.get('metadata_hints', {}),
        warnings=warnings))
    if len(canonical(ebook).encode('utf-8')) > MAX_NAVIGATION:
        raise ValueError('Electronic-book contents exceed the navigation budget.')
    settings = canonical(dict(adapter=ADAPTER, kind='logical_segment', provenance=provenance,
        chapters=locators, alternatives=alternatives, warnings=warnings, ebook=ebook))
    if len(settings.encode('utf-8')) > MAX_SETTINGS:
        raise ValueError('Electronic-book extraction settings exceed the storage budget.')
    return settings


def _page_shapes(db, revision, count):
    rows = db.execute('SELECT ordinal,physical,printed,verified,length(raw),digest,typeof(raw) '
        'FROM page WHERE revision=? ORDER BY ordinal LIMIT 1001', (revision,)).fetchall()
    if len(rows) != count:
        raise ValueError('Electronic-book chapters differ from their sealed extraction.')
    total, shapes = 0, []
    for ordinal, row in enumerate(rows, 1):
        position, physical, printed, verified, characters, digest, kind = tuple(row)
        if (type(position) is not int or position != ordinal or physical is not None
                or printed is not None or type(verified) is not int or verified != 0
                or kind != 'text' or type(characters) is not int or characters < 0
                or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            raise ValueError('Invalid electronic-book stored chapter identity.')
        total += characters
        if total > MAX_TEXT:
            raise ValueError('Electronic-book text exceeds its budget.')
        shapes.append((ordinal, None, None, 0, characters, digest))
    return tuple(shapes)


def _fallback(chapters, warnings, descriptions, shapes):
    """Describe only stored chapter starts; do not reconstruct missing anchors."""
    records = []
    for chapter, entries, shape in zip(chapters, descriptions, shapes):
        records.append(dict(ordinal=chapter.position, title=chapter.title, member=chapter.resource,
            linear=chapter.linear, characters=shape[4], text_sha256=shape[5], headings=[],
            warnings=[], image_descriptions=_descriptions(entries)))
    warnings = list(warnings)
    if 'embedded_contents_unavailable' not in warnings:
        warnings.append('embedded_contents_unavailable')
    return _navigation(dict(version=1, chapters=records,
        toc=[dict(title=chapter.title, ordinal=chapter.position, offset=0, depth=0)
             for chapter in chapters], page_hints=[], metadata_hints={}, warnings=warnings))


def _selected(app, db, workspace, edition, revision, *, authorized_topic, cancelled):
    _check_cancelled(cancelled)
    if authorized_topic is not None:
        TopicTree(db, workspace)._visible(authorized_topic)
    view = get_edition(Catalog(db, workspace), edition)
    row = db.execute('SELECT r.id,r.asset,r.config,a.mime FROM active x '
        'JOIN revision r ON r.id=x.revision AND r.workspace=x.workspace AND r.edition=x.edition '
        'JOIN sealed s ON s.revision=r.id JOIN catalog_asset a ON a.digest=r.asset '
        'WHERE x.workspace=? AND x.edition=?', (workspace, edition)).fetchone()
    if row is None or row['mime'] != 'application/epub+zip' or row['asset'] != view.asset:
        raise ValueError('Select an imported EPUB book in Books and Collections first.')
    if revision is not None and revision != row['id']:
        raise ValueError('The electronic-book revision changed. Open its contents again.')
    config = row['config']
    if not isinstance(config, str) or len(config.encode('utf-8')) > MAX_SETTINGS:
        raise ValueError('Electronic-book extraction settings have changed.')
    try:
        settings = json.loads(config)
    except (ValueError, RecursionError):
        raise ValueError('Electronic-book extraction settings are unreadable.') from None
    if not isinstance(settings, dict) or settings.get('adapter') != ADAPTER:
        raise ValueError('Unsupported electronic-book extraction settings.')
    identity = Identity(workspace, edition, row['asset'], row['id'])
    # Reuse the accepted canonical schema and deterministic sealed-baseline
    # proof. Its UUID metadata revisions remain valid without rewriting them.
    legacy = _legacy_chapters(db, identity)
    if legacy is None:
        raise ValueError('Unsupported electronic-book extraction settings.')
    chapters, warnings, descriptions, baseline = legacy
    app._retained_source(db, workspace, identity, cancelled=cancelled)
    shapes = _page_shapes(db, identity.revision, len(chapters))
    if baseline != identity.revision and _page_shapes(db, baseline, len(chapters)) != shapes:
        raise ValueError('Electronic-book chapters differ from their original extraction.')
    if 'ebook' in settings:
        if len(canonical(settings['ebook']).encode('utf-8')) > MAX_NAVIGATION:
            raise ValueError('Electronic-book contents exceed the navigation budget.')
        book = _navigation(settings['ebook'])
        if len(book['chapters']) != len(chapters) or book['warnings'] != list(warnings):
            raise ValueError('Electronic-book contents differ from their section index.')
        for record, chapter, entries in zip(book['chapters'], chapters, descriptions):
            if (record['ordinal'], record['title'], record['member'], record['linear'],
                    record['image_descriptions']) != (
                    chapter.position, chapter.title, chapter.resource, chapter.linear,
                    _descriptions(entries)):
                raise ValueError('Electronic-book contents differ from their section index.')
    else:
        book = _fallback(chapters, warnings, descriptions, shapes)
    expected = tuple((record['ordinal'], None, None, 0, record['characters'], record['text_sha256'])
                     for record in book['chapters'])
    if shapes != expected:
        raise ValueError('Electronic-book chapters differ from their sealed extraction.')
    _check_cancelled(cancelled)
    return dict(book, workspace=workspace, edition_id=edition, revision=identity.revision,
                asset_sha256=identity.asset_sha256, title=view.title or 'Untitled EPUB',
                authors=list(view.authors), verified=False), baseline


def open_ebook(app, workspace, edition, *, authorized_topic=None, cancelled=lambda: False):
    """Read contents or an honest chapter-only fallback for an older import."""
    _check_cancelled(cancelled)
    with app.read(workspace) as db:
        book, _ = _selected(app, db, workspace, edition, None,
                           authorized_topic=authorized_topic, cancelled=cancelled)
        return book


def read_chapter(app, workspace, edition, revision, ordinal, *, authorized_topic=None,
                 cancelled=lambda: False):
    """Return exact stored text; never mint an Anchor or quotation approval."""
    _check_cancelled(cancelled)
    if type(ordinal) is not int:
        raise ValueError('Choose a valid electronic-book chapter.')
    with app.read(workspace) as db:
        book, baseline = _selected(app, db, workspace, edition, revision,
                                   authorized_topic=authorized_topic, cancelled=cancelled)
        if not 1 <= ordinal <= len(book['chapters']):
            raise ValueError('Choose a valid electronic-book chapter.')
        row = db.execute('SELECT raw,digest FROM page WHERE revision=? AND ordinal=?',
                         (revision, ordinal)).fetchone()
        chapter = book['chapters'][ordinal - 1]
        if (row is None or not isinstance(row['raw'], str) or '\x00' in row['raw']
                or sha256(row['raw'].encode('utf-8')).hexdigest() != chapter['text_sha256']
                or row['digest'] != chapter['text_sha256']):
            raise ValueError('The extracted chapter text has changed.')
        if baseline != revision:
            original = db.execute('SELECT raw,digest FROM page WHERE revision=? AND ordinal=?',
                                  (baseline, ordinal)).fetchone()
            if original is None or tuple(original) != tuple(row):
                raise ValueError('The extracted chapter differs from its original extraction.')
        _check_cancelled(cancelled)
        return dict(chapter, raw_text=row['raw'], revision=revision, verified=False)
