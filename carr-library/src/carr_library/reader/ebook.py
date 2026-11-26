"""Freshly authorized, unverified EPUB section reading without parsing markup.

Section numbers describe the package spine, never physical or printed pages.
The retained EPUB, extraction settings and source revision remain separate from
mutable catalog metadata. This service cannot grant quotation approval.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json

from ..citations.anchors import Identity
from ..ingest.pipeline import canonical
from ..research.topic_tree import TopicTree


@dataclass(frozen=True)
class EbookBook:
    workspace: str
    edition_id: str
    revision: str
    title: str
    author: str
    edition: str
    section_count: int


@dataclass(frozen=True)
class EbookChapter:
    position: int
    title: str
    resource: str
    linear: bool


@dataclass(frozen=True)
class EbookSection:
    identity: Identity
    book_title: str
    title: str
    resource: str
    position: int
    count: int
    linear: bool
    raw_text: str
    chapters: tuple[EbookChapter, ...]
    warnings: tuple[str, ...]
    alternatives: tuple[tuple[int, str], ...]
    verified: bool = field(default=False, init=False)


def _cancelled(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('EPUB reading cancelled.')


def _topic(db, workspace, authorized_topic):
    if authorized_topic is not None:
        TopicTree(db, workspace)._visible(authorized_topic)


def _chapters(db, identity):
    row = db.execute('SELECT r.config FROM revision r JOIN sealed s ON s.revision=r.id '
        'WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=?',
        (identity.workspace, identity.edition_id, identity.revision, identity.asset_sha256)).fetchone()
    if row is None:
        raise ValueError('The EPUB source revision is unavailable.')
    if len(row[0].encode('utf-8')) > 4 * 1024 * 1024:
        raise ValueError('The EPUB section index is too large.')
    try:
        settings = json.loads(row[0])
    except (ValueError, TypeError):
        raise ValueError('The EPUB section index is invalid.') from None
    if not isinstance(settings, dict) or settings.get('adapter') != 'epub-spine-v1':
        return None
    baseline = sha256(canonical([identity.workspace, identity.edition_id,
                                 identity.asset_sha256, row[0]]).encode('utf-8')).hexdigest()
    provenance = settings.get('provenance')
    values = settings.get('chapters')
    warnings = settings.get('warnings')
    alternatives = settings.get('alternatives')
    if (settings.get('kind') != 'logical_segment'
            or not isinstance(provenance, dict)
            or provenance.get('asset_sha256') != identity.asset_sha256
            or not isinstance(values, list) or not 1 <= len(values) <= 1000
            or not isinstance(alternatives, list) or len(alternatives) != len(values)
            or not isinstance(warnings, list) or len(warnings) > 1000
            or any(not isinstance(w, str) or len(w) > 1024 for w in warnings)):
        raise ValueError('The EPUB section index differs from its source revision.')
    # A metadata edit creates a new source-metadata revision while retaining the
    # extraction settings. Keep its original deterministic import as the text
    # and section-index reference instead of mistaking the new ID for corruption.
    if not db.execute('SELECT 1 FROM revision r JOIN sealed s ON s.revision=r.id '
            'WHERE r.id=? AND r.workspace=? AND r.edition=? AND r.asset=? AND r.config=?',
            (baseline, identity.workspace, identity.edition_id,
             identity.asset_sha256, row[0])).fetchone():
        raise ValueError('The original EPUB extraction revision is unavailable.')
    chapters = []
    descriptions = []
    for index, value in enumerate(values, 1):
        if (not isinstance(value, dict) or type(value.get('spine_index')) is not int
                or value['spine_index'] != index or type(value.get('linear')) is not bool
                or not isinstance(value.get('href'), str) or not value['href']
                or len(value['href']) > 4096 or not isinstance(value.get('title'), str)
                or len(value['title']) > 4096):
            raise ValueError('The EPUB chapter location is invalid.')
        chapters.append(EbookChapter(index, value['title'] or f'Section {index}',
                                     value['href'], value['linear']))
        entries = alternatives[index - 1]
        if not isinstance(entries, list) or len(entries) > 10000:
            raise ValueError('The EPUB image description index is invalid.')
        chapter_descriptions = []
        previous = 0
        for entry in entries:
            if (not isinstance(entry, dict) or entry.get('kind') != 'image_alt'
                    or type(entry.get('image_index')) is not int or entry['image_index'] <= previous
                    or not isinstance(entry.get('text'), str) or len(entry['text']) > 4096):
                raise ValueError('The EPUB image description is invalid.')
            previous = entry['image_index']
            chapter_descriptions.append((previous, entry['text']))
        descriptions.append(tuple(chapter_descriptions))
    count = db.execute('SELECT count(*) FROM page WHERE revision=?',
                       (identity.revision,)).fetchone()[0]
    if count != len(chapters):
        raise ValueError('The EPUB section count differs from its source revision.')
    return tuple(chapters), tuple(warnings), tuple(descriptions), baseline


def section_description(db, identity, position):
    """Describe an already authorized search preview; never infer page labels."""
    result = _chapters(db, identity)
    if result is None:
        return ''
    chapters, _, _, _ = result
    if type(position) is not int or not 1 <= position <= len(chapters):
        raise ValueError('Choose an existing EPUB section.')
    chapter = chapters[position - 1]
    extra = ' (supplementary)' if not chapter.linear else ''
    return f'EPUB section {position} of {len(chapters)}: {chapter.title}{extra}'


def list_ebooks(app, workspace, *, authorized_topic=None, cancelled=lambda: False):
    """List at most 500 EPUB editions in this library, using stable identities."""
    _cancelled(cancelled)
    with app.read(workspace) as db:
        _topic(db, workspace, authorized_topic)
        rows = db.execute('''SELECT e.id,a.revision,m.title,m.author,m.edition_label,
                    (SELECT count(*) FROM page p WHERE p.revision=a.revision)
            FROM catalog_edition e JOIN catalog_asset f ON f.digest=e.asset
            JOIN active a ON a.workspace=e.workspace AND a.edition=e.id
            JOIN revision r ON r.id=a.revision AND r.asset=e.asset
            JOIN sealed s ON s.revision=r.id
            JOIN source_metadata m ON m.workspace=e.workspace AND m.edition=e.id AND m.revision=r.id
            WHERE e.workspace=? AND f.mime='application/epub+zip'
            ORDER BY m.title COLLATE NOCASE,e.id COLLATE BINARY LIMIT 501''', (workspace,)).fetchall()
        if len(rows) > 500:
            raise ValueError('This EPUB list supports at most 500 editions per library.')
        _cancelled(cancelled)
        return tuple(EbookBook(workspace, row[0], row[1], row[2], row[3] or 'Unknown author',
                              row[4] or '', row[5]) for row in rows)


def read_ebook_section(app, workspace, edition_id, revision, position, *,
                      authorized_topic=None, cancelled=lambda: False):
    """Read one exact published EPUB section after checking scope and originals.

    The requested revision stays fixed while the reader is open. A metadata
    update or a new edition cannot silently retarget the displayed book.
    """
    if type(position) is not int or not 1 <= position <= 1000:
        raise ValueError('Choose an existing EPUB section from 1 onwards.')
    if any(not isinstance(v, str) or not v for v in (workspace, edition_id, revision)):
        raise ValueError('Choose an EPUB book in the active library.')
    _cancelled(cancelled)
    with app.read(workspace) as db:
        _topic(db, workspace, authorized_topic)
        row = db.execute('''SELECT r.asset,m.title FROM revision r
            JOIN sealed s ON s.revision=r.id
            JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition AND e.asset=r.asset
            JOIN catalog_asset f ON f.digest=r.asset AND f.mime='application/epub+zip'
            JOIN source_metadata m ON m.workspace=r.workspace AND m.edition=r.edition AND m.revision=r.id
            WHERE r.workspace=? AND r.edition=? AND r.id=?''',
            (workspace, edition_id, revision)).fetchone()
        if row is None:
            raise ValueError('The EPUB book is unavailable in this library.')
        identity = Identity(workspace, edition_id, row[0], revision)
        app._retained_source(db, workspace, identity, cancelled=cancelled)
        result = _chapters(db, identity)
        if result is None:
            raise ValueError('This source does not have a supported EPUB section index.')
        chapters, warnings, descriptions, baseline = result
        if position > len(chapters):
            raise ValueError('Choose an existing EPUB section.')
        page = db.execute('SELECT raw,digest,physical,printed,verified FROM page '
                          'WHERE revision=? AND ordinal=?', (revision, position)).fetchone()
        if (page is None or page[2] is not None or page[3] is not None or page[4] != 0
                or sha256(page[0].encode('utf-8')).hexdigest() != page[1]):
            raise ValueError('The EPUB section text or location changed.')
        if baseline != revision:
            original = db.execute('SELECT raw,digest,physical,printed,verified FROM page '
                                  'WHERE revision=? AND ordinal=?', (baseline, position)).fetchone()
            if original is None or tuple(original) != tuple(page):
                raise ValueError('The EPUB section differs from its original extraction.')
        _cancelled(cancelled)
        chapter = chapters[position - 1]
        return EbookSection(identity, row[1], chapter.title, chapter.resource, position,
                            len(chapters), chapter.linear, page[0], chapters, warnings,
                            descriptions[position - 1])
