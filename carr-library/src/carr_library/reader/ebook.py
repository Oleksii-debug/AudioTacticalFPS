"""Authorized navigation through sealed, explicitly unverified EPUB extraction.

Chapter positions describe the extracted text, not rendered or printed pages.
The EPUB stays in the existing original-byte store and backup namespace. Every
operation rechecks the library/topic, active revision and retained source hash.
"""
from hashlib import sha256
import json
import re

from ..catalog.membership import Catalog
from ..catalog.read_views import get_edition
from ..citations.anchors import Identity
from ..ingest.pipeline import canonical
from ..research.topic_tree import TopicTree


ADAPTER = 'epub-spine-v1'
MAX_NAVIGATION = 2 * 1024 * 1024
MAX_ITEMS = 2000
MAX_TEXT = 10_000_000


def _label(value, *, empty=False, limit=1024):
    if (not isinstance(value, str) or len(value) > limit
            or (not empty and not value.strip()) or '\x00' in value):
        raise ValueError('Invalid electronic-book contents label.')
    return value


def _warnings(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError('Invalid electronic-book extraction warnings.')
    return [_label(item, limit=256) for item in value]


def _navigation(value):
    """Check bounded persisted descriptors before exposing native controls."""
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1):
        raise ValueError('Unsupported electronic-book contents version.')
    records = value.get('chapters')
    if not isinstance(records, list) or not 1 <= len(records) <= 1000:
        raise ValueError('Invalid electronic-book chapter count.')
    total_characters = 0
    navigation_count = 0
    chapters = []
    for ordinal, record in enumerate(records, 1):
        if (not isinstance(record, dict) or type(record.get('ordinal')) is not int
                or record['ordinal'] != ordinal or type(record.get('linear')) is not bool
                or type(record.get('characters')) is not int or record['characters'] < 0
                or not isinstance(record.get('text_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', record['text_sha256'])):
            raise ValueError('Invalid electronic-book chapter descriptor.')
        total_characters += record['characters']
        if total_characters > MAX_TEXT:
            raise ValueError('Electronic-book text exceeds its budget.')
        headings = record.get('headings', [])
        if not isinstance(headings, list):
            raise ValueError('Invalid electronic-book heading list.')
        checked_headings = []
        for heading in headings:
            if (not isinstance(heading, dict) or type(heading.get('offset')) is not int
                    or not 0 <= heading['offset'] <= record['characters']
                    or type(heading.get('level')) is not int or not 1 <= heading['level'] <= 6):
                raise ValueError('Invalid electronic-book heading location.')
            checked_headings.append(dict(title=_label(heading.get('title')),
                                         offset=heading['offset'], level=heading['level']))
        navigation_count += len(checked_headings)
        descriptions = record.get('image_descriptions', [])
        if not isinstance(descriptions, list) or len(descriptions) > 100:
            raise ValueError('Invalid electronic-book image descriptions.')
        descriptions = [_label(item) for item in descriptions]
        chapters.append(dict(ordinal=ordinal, title=_label(record.get('title')),
            member=_label(record.get('member')), linear=record['linear'],
            characters=record['characters'], text_sha256=record['text_sha256'],
            headings=checked_headings, warnings=_warnings(record.get('warnings', [])),
            image_descriptions=descriptions))

    def links(rows, *, grouping=False):
        nonlocal navigation_count
        if not isinstance(rows, list):
            raise ValueError('Invalid electronic-book navigation list.')
        navigation_count += len(rows)
        checked = []
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('depth', 0)) is not int
                    or not 0 <= row.get('depth', 0) <= 64):
                raise ValueError('Invalid electronic-book navigation depth.')
            title = _label(row.get('title'))
            if grouping and row.get('heading_only') is True:
                if row.get('ordinal') is not None or row.get('offset') is not None:
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
    # Only the existing EPUB hint fields are exposed. They do not replace the
    # independently supplied catalog title/authors or establish attribution.
    metadata = {}
    for key in ('titles', 'creators', 'languages', 'identifiers', 'publishers'):
        items = hints.get(key, [])
        if not isinstance(items, (list, tuple)) or len(items) > 100:
            raise ValueError('Invalid electronic-book metadata hint values.')
        metadata[key] = [_label(item, limit=4096) for item in items]
    return dict(version=1, chapters=chapters, toc=toc, page_hints=page_hints,
                metadata_hints=metadata, warnings=_warnings(value.get('warnings', [])))


def extraction_settings(extracted):
    """Bind imported navigation and text digests into the existing revision ID."""
    chapters = []
    for ordinal, record in enumerate(extracted['records'], 1):
        raw = record['raw_text']
        if (not isinstance(raw, str) or sha256(raw.encode('utf-8')).hexdigest()
                != record['text_sha256']):
            raise ValueError('Electronic-book extraction text digest changed.')
        chapters.append(dict(ordinal=ordinal, title=record['title'], member=record['member'],
            linear=record['linear'], characters=len(raw), text_sha256=record['text_sha256'],
            headings=record.get('headings', []), warnings=record.get('warnings', []),
            image_descriptions=record.get('image_descriptions', [])))
    ebook = _navigation(dict(version=1, chapters=chapters, toc=extracted.get('toc', []),
        page_hints=extracted.get('page_hints', []), metadata_hints=extracted.get('metadata', {}),
        warnings=extracted.get('warnings', [])))
    settings = canonical(dict(adapter=ADAPTER, kind='logical_segment',
                              provenance=extracted['provenance'], ebook=ebook))
    if len(settings.encode('utf-8')) > MAX_NAVIGATION:
        raise ValueError('Electronic-book contents exceed the storage budget.')
    return settings


def _selected(app, db, workspace, edition, revision, *, authorized_topic, cancelled):
    if cancelled():
        raise InterruptedError('Reading the electronic book was cancelled.')
    if authorized_topic is not None:
        TopicTree(db, workspace)._visible(authorized_topic)
    view = get_edition(Catalog(db, workspace), edition)
    row = db.execute('SELECT r.id,r.asset,r.config,a.mime FROM active x '
        'JOIN revision r ON r.id=x.revision AND r.workspace=x.workspace AND r.edition=x.edition '
        'JOIN sealed s ON s.revision=r.id JOIN catalog_asset a ON a.digest=r.asset '
        'WHERE x.workspace=? AND x.edition=?', (workspace, edition)).fetchone()
    if row is None or row['mime'] != 'application/epub+zip':
        raise ValueError('Select an imported EPUB book in Books and Collections first.')
    if revision is not None and revision != row['id']:
        raise ValueError('The electronic-book revision changed. Open its contents again.')
    config = row['config']
    if not isinstance(config, str) or len(config.encode('utf-8')) > MAX_NAVIGATION:
        raise ValueError('Electronic-book extraction settings have changed.')
    # Catalog edits legitimately create a new UUID revision while carrying the
    # same immutable extraction configuration and chapter text. Authenticate
    # that configuration against its original deterministic sealed extraction,
    # then validate the active revision's pages below. A cached reader remains
    # bound to its exact active revision and must reopen after a metadata edit.
    baseline_id = sha256(canonical([workspace, edition, row['asset'], config])
                         .encode('utf-8')).hexdigest()
    baseline = db.execute('SELECT r.workspace,r.edition,r.asset,r.config FROM revision r '
        'JOIN sealed s ON s.revision=r.id WHERE r.id=?', (baseline_id,)).fetchone()
    if baseline is None or tuple(baseline) != (workspace, edition, row['asset'], config):
        raise ValueError('Electronic-book extraction settings have changed.')
    try:
        settings = json.loads(config)
    except (ValueError, RecursionError):
        raise ValueError('Electronic-book extraction settings are unreadable.') from None
    if (not isinstance(settings, dict) or settings.get('adapter') != ADAPTER
            or settings.get('kind') != 'logical_segment'
            or not isinstance(settings.get('provenance'), dict)
            or settings['provenance'].get('asset_sha256') != row['asset']):
        raise ValueError('Unsupported electronic-book extraction settings.')
    identity = Identity(workspace, edition, row['asset'], row['id'])
    app._retained_source(db, workspace, identity, cancelled=cancelled)
    book = _navigation(settings.get('ebook'))
    pages = db.execute('SELECT ordinal,physical,printed,verified,length(raw),digest '
                       'FROM page WHERE revision=? ORDER BY ordinal', (identity.revision,)).fetchall()
    expected = tuple((r['ordinal'], None, None, 0, r['characters'], r['text_sha256'])
                     for r in book['chapters'])
    if tuple(tuple(p) for p in pages) != expected:
        raise ValueError('Electronic-book chapters differ from their sealed extraction.')
    if cancelled():
        raise InterruptedError('Reading the electronic book was cancelled.')
    return dict(book, workspace=workspace, edition_id=edition, revision=identity.revision,
                asset_sha256=identity.asset_sha256, title=view.title or 'Untitled EPUB',
                authors=list(view.authors), verified=False)


def open_ebook(app, workspace, edition, *, authorized_topic=None, cancelled=lambda: False):
    """Read a bounded chapter/contents list for the freshly authorized edition."""
    with app.read(workspace) as db:
        return _selected(app, db, workspace, edition, None,
                         authorized_topic=authorized_topic, cancelled=cancelled)


def read_chapter(app, workspace, edition, revision, ordinal, *, authorized_topic=None,
                 cancelled=lambda: False):
    """Return one exact stored chapter; no verified quotation or Anchor is minted."""
    if type(ordinal) is not int:
        raise ValueError('Choose a valid electronic-book chapter.')
    with app.read(workspace) as db:
        book = _selected(app, db, workspace, edition, revision,
                         authorized_topic=authorized_topic, cancelled=cancelled)
        if not 1 <= ordinal <= len(book['chapters']):
            raise ValueError('Choose a valid electronic-book chapter.')
        row = db.execute('SELECT raw,digest FROM page WHERE revision=? AND ordinal=?',
                         (revision, ordinal)).fetchone()
        chapter = book['chapters'][ordinal - 1]
        if (row is None or not isinstance(row['raw'], str)
                or sha256(row['raw'].encode('utf-8')).hexdigest() != chapter['text_sha256']
                or row['digest'] != chapter['text_sha256']):
            raise ValueError('The extracted chapter text has changed.')
        if cancelled():
            raise InterruptedError('Reading the electronic book was cancelled.')
        return dict(chapter, raw_text=row['raw'], revision=revision, verified=False)
