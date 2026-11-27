"""Captured source headings/bookmarks for BookPlan; never a new navigation store."""
from dataclasses import asdict
from hashlib import sha256

from ..durable_state import encode
from ..reader.ebook_contents import MAX_NAVIGATION, open_ebook
from ..reader.reading_state import Bookmark, ReadingLocation, ReadingState

KINDS = ('headings', 'bookmarks')


def _plain(value, maximum):
    if type(value) is not str or not value.strip() or len(value) > maximum or '\0' in value:
        raise ValueError('Invalid retained book landmark text.')
    value.encode('utf-8', 'strict')
    return value


def entries(source, snapshot):
    """Passively validate exact captured metadata and derive source offsets."""
    if (type(snapshot) is not dict or set(snapshot) != {'schema', 'kind', 'records'}
            or snapshot['schema'] != 'carr-book-landmarks/1' or snapshot['kind'] not in KINDS
            or type(snapshot['records']) is not list or len(encode(snapshot)) > MAX_NAVIGATION):
        raise ValueError('Invalid bounded captured book landmarks.')
    anchor = source['source_anchor']
    positions = anchor['positions']
    result = []
    if snapshot['kind'] == 'headings':
        if len(snapshot['records']) != len(positions):
            raise ValueError('Heading metadata must cover the exact selected source positions.')
        count = 0
        for record, position in zip(snapshot['records'], positions):
            if (type(record) is not dict or set(record) != {
                    'ordinal', 'title', 'member', 'characters', 'text_sha256', 'headings'}
                    or position['kind'] != 'logical_segment' or type(record['ordinal']) is not int
                    or record['ordinal'] != position['ordinal'] or type(record['characters']) is not int
                    or record['characters'] != position['end'] - position['start']
                    or record['text_sha256'] != position['text_sha256']
                    or type(record['headings']) is not list):
                raise ValueError('Heading metadata differs from its exact captured source position.')
            _plain(record['title'], 4096)
            _plain(record['member'], 4096)
            count += len(record['headings'])
            if count > 2000:
                raise ValueError('The retained heading inventory exceeds its existing bound.')
            for index, heading in enumerate(record['headings'], 1):
                if (type(heading) is not dict or set(heading) != {'title', 'offset', 'level'}
                        or type(heading['offset']) is not int or not 0 <= heading['offset'] <= record['characters']
                        or type(heading['level']) is not int or not 1 <= heading['level'] <= 6):
                    raise ValueError('Invalid retained EPUB heading boundary.')
                _plain(heading['title'], 4096)
                origin = {'ordinal': record['ordinal'], 'heading_index': index,
                          'chapter_title': record['title'], 'member': record['member'],
                          'text_sha256': record['text_sha256'], 'heading': heading}
                identity = sha256(encode([anchor['workspace'], anchor['edition'], anchor['revision'],
                                          anchor['asset_sha256'], origin])).hexdigest()
                result.append({'id': identity, 'start': position['start'] + heading['offset'],
                               'label': heading['title'], 'origin': origin})
        if not result:
            raise ValueError('No retained EPUB heading offsets are available for this selection. Choose chapters or another split.')
    else:
        if not 1 <= len(snapshot['records']) <= 256:
            raise ValueError('Choose between one and 256 existing reading bookmarks explicitly.')
        seen = set()
        by_page = {(position['kind'], position['index']): position for position in positions}
        for record in snapshot['records']:
            if type(record) is not dict or set(record) != {'id', 'label', 'location', 'created_at', 'updated_at'}:
                raise ValueError('Invalid captured reading bookmark.')
            try:
                bookmark = Bookmark(record['id'], record['label'], ReadingLocation(**record['location']),
                                    record['created_at'], record['updated_at'])
            except (TypeError, KeyError) as exc:
                raise ValueError('Invalid captured reading bookmark location.') from exc
            location = bookmark.location
            position = by_page.get((location.kind, location.index))
            if (bookmark.id in seen or position is None
                    or (location.workspace, location.edition, location.revision, location.asset_sha256)
                    != (anchor['workspace'], anchor['edition'], anchor['revision'], anchor['asset_sha256'])
                    or location.text_sha256 != position['text_sha256']):
                raise ValueError('The selected bookmark is stale, duplicated or outside this exact source range.')
            seen.add(bookmark.id)
            result.append({'id': bookmark.id, 'start': position['start'],
                           'label': bookmark.label, 'origin': asdict(bookmark)})
    return tuple(sorted(result, key=lambda item: (item['start'], item['id'])))


def capture(library, source, kind, *, bookmark_ids=None, cancelled=lambda: False, guard=lambda: None):
    """Capture only the existing retained descriptor or explicitly selected IDs."""
    def check():
        guard()
        if cancelled():
            raise InterruptedError('Book landmark review was cancelled.')
    check()
    anchor = source['source_anchor']
    if kind == 'headings':
        if bookmark_ids is not None:
            raise ValueError('Bookmark identities apply only to bookmark splitting.')
        contents = open_ebook(library, anchor['workspace'], anchor['edition'],
                              authorized_topic=source['source_topic'], cancelled=cancelled)
        if (contents['revision'], contents['asset_sha256']) != (anchor['revision'], anchor['asset_sha256']):
            raise ValueError('The retained EPUB heading inventory changed during review.')
        chapters = {row['ordinal']: row for row in contents['chapters']}
        records = []
        for position in anchor['positions']:
            check()
            chapter = chapters.get(position['ordinal'])
            if chapter is None:
                raise ValueError('A selected source position has no retained EPUB chapter.')
            records.append({key: chapter[key] for key in
                            ('ordinal', 'title', 'member', 'characters', 'text_sha256', 'headings')})
    elif kind == 'bookmarks':
        if (type(bookmark_ids) not in (tuple, list) or not 1 <= len(bookmark_ids) <= 256
                or any(type(identity) is not str for identity in bookmark_ids)
                or len(bookmark_ids) != len(set(bookmark_ids))):
            raise ValueError('Choose the exact existing bookmark IDs for this source split.')
        state = ReadingState(library.root)
        available = {bookmark.id: bookmark for bookmark in state.bookmarks(anchor['workspace'], anchor['edition'])}
        try:
            records = [asdict(available[identity]) for identity in sorted(bookmark_ids)]
        except KeyError as exc:
            raise ValueError('A selected reading bookmark is unavailable or changed. Review its current location again.') from exc
    else:
        raise ValueError('Choose retained EPUB headings or existing reading bookmarks.')
    snapshot = {'schema': 'carr-book-landmarks/1', 'kind': kind, 'records': records}
    entries(source, snapshot)
    check()
    return snapshot


def validate_current(library, source, snapshot, *, cancelled=lambda: False, guard=lambda: None):
    entries(source, snapshot)
    identities = [record['id'] for record in snapshot['records']] if snapshot['kind'] == 'bookmarks' else None
    current = capture(library, source, snapshot['kind'], bookmark_ids=identities,
                      cancelled=cancelled, guard=guard)
    if current != snapshot:
        raise ValueError('The reviewed heading or bookmark inventory changed. Capture and approve a new plan.')


def available_bookmarks(library, workspace, edition, *, topic=None, revision=None,
                        cancelled=lambda: False):
    """Return passive choices; capture revalidates selected positions and bytes."""
    from ..reader.reading_desk import ReadingDesk
    desk = ReadingDesk(library, workspace, edition, topic=topic, revision=revision, cancelled=cancelled)
    rows = ReadingState(library.root).bookmarks(workspace, edition)
    if cancelled():
        raise InterruptedError('Book bookmark discovery was cancelled.')
    # Stale entries remain visible as stale; they can never pass capture.
    return tuple({'bookmark': bookmark,
                  'current_revision': bookmark.location.revision == desk.revision
                    and bookmark.location.asset_sha256 == desk.identity.asset_sha256} for bookmark in rows)

