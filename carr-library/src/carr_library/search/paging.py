"""Bounded source windows over the existing catalog and active-page snapshot.

A cursor is passive navigation data, never source authorization. Every page
resolves the current catalog/collection and exact active revisions again.
No new index, job queue or database is created.
"""
from dataclasses import dataclass
from hashlib import sha256
import json

from ..catalog.read_views import _edition_keys

MAX_TIMEOUT_SECONDS = 24 * 60 * 60
LIBRARIES_PER_PAGE = 20
MAX_SELECTED_SCOPE_BYTES = 1024 * 1024
MAX_SOURCE_PAGES = 100
MAX_SOURCE_CHARACTERS = 5_000_000
CATALOG_PAGE_SIZE = 500
_CURSOR_KEYS = {'schema', 'scope', 'edition_after', 'after_page'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def scope_identity(root, workspace, query, mode, result_mode, filters):
    return sha256(canonical([str(root), workspace, query, mode, result_mode, filters]).encode('utf-8')).hexdigest()


def checked_page_ids(values):
    if values is None:
        return '', ()
    if (type(values) not in (tuple, list) or len(values) > MAX_SOURCE_PAGES
            or any(type(value) is not int or value < 1 for value in values)
            or len(set(values)) != len(values)):
        raise ValueError('Invalid bounded source-page selection.')
    if not values:
        return ' AND 0', ()
    return ' AND id IN (' + ','.join('?' for _ in values) + ')', tuple(values)


def _text(value):
    return type(value) is str and 0 < len(value) <= 256 and '\x00' not in value


def _cursor(value, scope):
    if value is None:
        return {'schema': 'carr-source-search-cursor/1', 'scope': scope,
                'edition_after': None, 'after_page': None}
    if (type(value) is not dict or set(value) != _CURSOR_KEYS
            or value['schema'] != 'carr-source-search-cursor/1' or value['scope'] != scope
            or value['edition_after'] is not None and not _text(value['edition_after'])):
        raise ValueError('This search cursor belongs to different options or source scope. Restart search.')
    previous = value['after_page']
    if previous is not None and (type(previous) not in (list, tuple) or len(previous) != 4
            or not _text(previous[0]) or not _text(previous[1])
            or any(type(item) is not int or item < 1 for item in previous[2:])):
        raise ValueError('Invalid source search continuation.')
    return json.loads(canonical(value))


@dataclass(frozen=True)
class SourceWindow:
    editions: dict
    page_ids: tuple
    cursor: dict
    next_cursor: dict | None
    identity: str
    characters: int


@dataclass(frozen=True)
class SearchPage:
    results: tuple
    cursor: dict
    next_source_cursor: dict | None
    has_more_results: bool
    offset: int
    source_pages: int
    source_editions: int
    source_characters: int
    window_identity: str


def _active_revision(db, workspace, edition):
    # Resolve the existing composite active key before reading its page range.
    # Joining active_page by revision alone cannot use that composite key.
    source = db.execute(
        'SELECT a.revision,r.workspace,r.edition FROM active a '
        'LEFT JOIN revision r ON r.id=a.revision '
        'WHERE a.workspace=? AND a.edition=?', (workspace, edition))
    try:
        row = source.fetchone()
    finally:
        source.close()
    if row is None:
        return None
    revision, actual_workspace, actual_edition = row
    if (not _text(revision) or actual_workspace != workspace
            or actual_edition != edition):
        raise ValueError('Invalid active source navigation metadata.')
    return revision


def select_window(db, catalog, *, scope, cursor=None, check=lambda: None, **filters):
    """Select <=100 source pages/5M characters from <=500 catalog identities.

    Catalog and raw-page cursors are independent: an edition larger than this
    window continues from its exact active revision instead of being rejected.
    Empty catalog windows still advance. Results are ranked within this source
    window, not presented as global relevance over unvisited material.
    """
    check()
    current = _cursor(cursor, scope)
    keys = _edition_keys(catalog, after=current['edition_after'],
                         limit=CATALOG_PAGE_SIZE, **filters)
    check()
    allowed = dict(keys)
    if not keys:
        return SourceWindow({}, (), current, None, sha256(b'[]').hexdigest(), 0)
    previous = current['after_page']
    if previous is not None:
        edition, revision, ordinal, page_id = previous
        check()
        if (edition not in allowed
                or _active_revision(db, catalog.workspace, edition) != revision):
            raise ValueError('The active source changed since this cursor. Restart search.')
        source = db.execute(
            'SELECT id FROM page WHERE revision=? AND ordinal=? AND id=?',
            (revision, ordinal, page_id))
        try:
            row = source.fetchone()
        finally:
            source.close()
        if row is None:
            raise ValueError('The active source changed since this cursor. Restart search.')
    rows, characters, more_pages = [], 0, False
    reached_previous = previous is None
    # Catalog order is already the exact BINARY edition order. Resolve only
    # enough keyed revision/page ranges to fill this bounded source window;
    # unrelated later editions must not be scanned or sorted first.
    for edition in allowed:
        if not reached_previous:
            if edition != previous[0]:
                continue
            reached_previous = True
        check()
        revision = _active_revision(db, catalog.workspace, edition)
        if revision is None:
            continue
        sql = ('SELECT id,revision,ordinal,length(raw),digest FROM page '
               'WHERE revision=?')
        parameters = [revision]
        if previous is not None and edition == previous[0]:
            # UNIQUE(revision,ordinal) makes an equal-ordinal ID tie
            # impossible after the exact previous page was validated above.
            sql += ' AND ordinal>?'
            parameters.append(previous[2])
        sql += ' ORDER BY ordinal,id LIMIT ?'
        parameters.append(MAX_SOURCE_PAGES + 1 - len(rows))
        source = db.execute(sql, parameters)
        try:
            for selected in source:
                check()
                page_id, actual_revision, ordinal, count, digest = selected
                row = (page_id, edition, actual_revision, ordinal, count, digest)
                if (type(page_id) is not int or page_id < 1
                        or actual_revision != revision or not _text(actual_revision)
                        or type(ordinal) is not int or ordinal < 1
                        or type(count) is not int or count < 0 or type(digest) is not str):
                    raise ValueError('Invalid active source navigation metadata.')
                if count > MAX_SOURCE_CHARACTERS:
                    raise ValueError('One extracted source page exceeds the bounded search window; split or re-import its reading segments.')
                if len(rows) == MAX_SOURCE_PAGES or characters + count > MAX_SOURCE_CHARACTERS:
                    more_pages = True
                    break
                rows.append(row)
                characters += count
        finally:
            source.close()
        if more_pages:
            break
    check()
    if more_pages:
        last = rows[-1]
        following = {**current, 'after_page': [last[1], last[2], last[3], last[0]]}
    else:
        # An unfinished page range already determines its continuation. Only
        # a completed range needs the next catalog key, in this same snapshot.
        next_catalog = _edition_keys(catalog, after=keys[-1][0], limit=1, **filters)
        check()
        following = ({**current, 'edition_after': keys[-1][0], 'after_page': None}
                     if next_catalog else None)
    identity = sha256(canonical([scope, tuple(allowed.items()), rows]).encode('utf-8')).hexdigest()
    return SourceWindow(allowed, tuple(row[0] for row in rows), current, following, identity, characters)
