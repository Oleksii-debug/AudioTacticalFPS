"""Bounded source-search pages over one authorized catalog snapshot at a time.

Continuation contains positions and a catalog/active-revision digest, never
source authority. Every request reauthorizes the workspace and catalog filters,
checks immutable source hashes, and lets LibraryApp verify retained originals.
No persistent text index or long-lived SQLite reader is introduced. Ranking is
explicitly local to each bounded candidate window, not a corpus-wide score.
"""
from base64 import b64decode, urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import unicodedata

from ..catalog.membership import Catalog
from ..catalog.read_views import _selection
from . import literal, thematic


SCHEMA = 'carr-source-search-cursor/1'
MAX_CURSOR_BYTES = 16 * 1024
MAX_PAGE_RESULTS = 100
MAX_PAGE_SOURCES = 20_000
MAX_PAGE_CHARACTERS = 5_000_000
_HASH = re.compile('[0-9a-f]{64}\\Z')


@dataclass(frozen=True)
class SearchPage:
    results: tuple
    next_cursor: str | None
    scanned_segments: int
    scope_editions: int
    ordering: str

    @property
    def complete(self):
        return self.next_cursor is None

    def __len__(self):
        return len(self.results)

    def __iter__(self):
        return iter(self.results)

    def __getitem__(self, index):
        return self.results[index]


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def query_binding(root, database, workspace, query, options):
    # File identity is not source verification; it rejects another/restored DB
    # at the same spelling. Retained bytes and current authorization are checked
    # independently for every displayed result.
    if not isinstance(query, str) or not query.strip() or len(query) > literal.MAX_QUERY:
        raise ValueError('Choose a nonblank search query within the text budget.')
    root_info, db_info = root.stat(), database.stat()
    return sha256(_canonical([str(root), [root_info.st_dev, root_info.st_ino],
        [db_info.st_dev, db_info.st_ino], workspace, query, options])).hexdigest()


def encode_cursor(value):
    raw = _canonical(value)
    if len(raw) > MAX_CURSOR_BYTES:
        raise ValueError('Search continuation exceeds its metadata budget.')
    return urlsafe_b64encode(raw).decode('ascii')


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and '\x00' not in value


def decode_cursor(value):
    if not isinstance(value, str) or not 0 < len(value) <= MAX_CURSOR_BYTES * 2:
        raise ValueError('Invalid source-search continuation.')
    try:
        raw = b64decode(value.encode('ascii'), altchars=b'-_', validate=True)
        if len(raw) > MAX_CURSOR_BYTES:
            raise ValueError('oversized')
        def pairs(rows):
            result = {}
            for key, child in rows:
                if key in result:
                    raise ValueError('duplicate continuation field')
                result[key] = child
            return result
        row = json.loads(raw, object_pairs_hook=pairs)
        if (type(row) is not dict or set(row) != {'schema', 'binding', 'snapshot', 'position', 'display'}
                or row['schema'] != SCHEMA
                or any(not isinstance(row[key], str) or not _HASH.fullmatch(row[key])
                       for key in ('binding', 'snapshot'))):
            raise ValueError('shape')
        pos = row['position']
        if (type(pos) is not list or len(pos) != 4 or not _identity(pos[0])
                or any(type(v) is not int or v < 1 for v in pos[1:3])
                or (pos[3] is not None and (type(pos[3]) is not int or pos[3] < 0))):
            raise ValueError('position')
        display = row['display']
        if display is not None and (type(display) is not list or len(display) != 5
                or not all(_identity(v) for v in display[:2])
                or any(type(v) is not int for v in display[2:])
                or display[2] < 1 or not 0 <= display[3] < display[4]):
            raise ValueError('display')
        return row
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid source-search continuation.') from exc


def _scope_snapshot(db, selected, params, check):
    """Stream catalog/immutable revision identities; memory is independent of N."""
    digest = sha256()
    count, previous = 0, None
    rows = db.execute('WITH selected AS (' + selected + ') '
        'SELECT s.id,s.work,s.asset,s.title,s.language,s.edition,s.volume,'
        'a.position,a.name,h.revision,r.asset,r.workspace,r.edition,se.revision FROM selected s '
        'LEFT JOIN catalog_author a ON a.workspace=s.workspace AND a.work=s.work '
        'LEFT JOIN active h ON h.workspace=s.workspace AND h.edition=s.id '
        'LEFT JOIN revision r ON r.id=h.revision '
        'LEFT JOIN sealed se ON se.revision=h.revision '
        'ORDER BY s.id COLLATE BINARY,a.position', params)
    try:
        for row in rows:
            check()
            if row[9] is not None and (row[10] != row[2] or row[11] != params['scope']
                    or row[12] != row[0] or row[13] != row[9]):
                raise ValueError('Active source revision differs from its catalog identity or seal.')
            if row[0] != previous:
                count += 1
                previous = row[0]
            payload = _canonical(tuple(row))
            digest.update(len(payload).to_bytes(8, 'big'))
            digest.update(payload)
    finally:
        rows.close()
    check()
    return digest.hexdigest(), count


def _metadata_rows(db, selected, params, position, maximum):
    # Read source length before its text, so an oversized row cannot make the
    # bounded candidate list allocate its contents before admission.
    tail = ''
    params = dict(params, rows=maximum + 1)
    if position is not None:
        edition, ordinal, page, start = position
        relation = '>=' if start is not None else '>'
        tail = (' AND (s.id COLLATE BINARY>:last_edition OR '
                '(s.id=:last_edition AND (p.ordinal>:last_ordinal OR '
                '(p.ordinal=:last_ordinal AND p.id' + relation + ':last_page))))')
        params.update(last_edition=edition, last_ordinal=ordinal, last_page=page)
    return db.execute('WITH selected AS (' + selected + ') '
        'SELECT p.id,p.workspace,p.edition,p.revision,p.asset,p.ordinal,p.physical,'
        'p.printed,p.verified,length(p.raw),p.digest,s.work '
        'FROM selected s JOIN active_page p ON p.workspace=s.workspace '
        'AND p.edition=s.id AND p.asset=s.asset WHERE 1=1' + tail +
        ' ORDER BY s.id COLLATE BINARY,p.ordinal,p.id LIMIT :rows', params)


def _read_source(db, row, workspace, check):
    (page, ws, edition, revision, asset, ordinal, physical, printed,
     verified, length, digest, work) = tuple(row)
    if (ws != workspace or not all(_identity(v) for v in (edition, revision, work))
            or type(page) is not int or page < 1 or type(ordinal) is not int or ordinal < 1
            or (physical is not None and (type(physical) is not int or physical < 1))
            or type(verified) is not int or verified not in (0, 1)
            or (physical is None and (printed is not None or verified))
            or not isinstance(asset, str) or not _HASH.fullmatch(asset)
            or not isinstance(digest, str) or not _HASH.fullmatch(digest)
            or type(length) is not int or length < 0):
        raise ValueError('Invalid active source identity in search page.')
    check()
    raw = db.execute('SELECT raw FROM page WHERE id=? AND revision=?', (page, revision)).fetchone()
    if raw is None or not isinstance(raw[0], str) or len(raw[0]) != length or '\x00' in raw[0]:
        raise ValueError('Invalid source text in search page.')
    raw = raw[0]
    if sha256(raw.encode('utf-8')).hexdigest() != digest:
        raise ValueError('The stored source text has been damaged.')
    check()
    hit = dict(workspace_id=workspace, edition_id=edition, revision_id=revision,
        asset_sha256=asset, page_id=page, segment_index=ordinal if physical is None else None,
        locator_kind='logical_segment' if physical is None else 'physical_page',
        physical_index=physical, printed_label=printed if verified else None,
        printed_label_verified=bool(verified), raw_text_sha256=digest)
    return raw, hit, work


def _literal_matches(raw, query, mode, compiled, check):
    folded, starts, ends = literal._mapped(raw, check)
    if compiled is not None:
        match = compiled.match(raw, folded, starts, ends, check=check)
        if match is not None:
            yield match.spans, match.reason
        return
    q = literal._fold(query).strip()
    needles = tuple(dict.fromkeys(q.split(' ') if mode == 'terms' else [q]))
    if mode == 'terms':
        spans = [next(literal._spans(folded, starts, ends, n, check), None) for n in needles]
        if all(s is not None for s in spans):
            yield tuple(sorted(set(spans))), 'literal source match'
        return
    for span in literal._spans(folded, starts, ends, needles[0], check):
        yield (span,), 'literal source match'


def candidates(db, workspace, query, *, binding, cursor=None, mode='phrase',
               filters=None, limit=100, page_budget=500, character_budget=MAX_PAGE_CHARACTERS,
               budget, ranker=None, cache=None, on_status=None):
    """Return raw hits, a continuation record, inspected segments and scope size.

    Literal order is stable edition/segment/raw offset. Ranked pages inspect at
    most ``limit`` source segments, ensuring that every qualifying parent fits
    in that window's result page. More source windows remain reachable even if
    an earlier window has zero matches. No global-rank claim is made.
    """
    if (mode not in ('phrase', 'word', 'terms', 'advanced', 'thematic', 'semantic')
            or type(limit) is not int or not 1 <= limit <= MAX_PAGE_RESULTS
            or type(page_budget) is not int or not 1 <= page_budget <= MAX_PAGE_SOURCES
            or type(character_budget) is not int or not 1 <= character_budget <= MAX_PAGE_CHARACTERS
            or not isinstance(query, str) or not query.strip() or len(query) > literal.MAX_QUERY
            or any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query)):
        raise ValueError('Choose a bounded source-search page and a nonblank query.')
    if mode == 'word' and ' ' in literal._fold(query).strip():
        raise ValueError('Word mode requires one term.')
    if mode == 'terms' and len(set(literal._fold(query).strip().split(' '))) > 32:
        raise ValueError('Too many search terms.')
    previous = decode_cursor(cursor) if cursor is not None else None
    if previous is not None and previous['binding'] != binding:
        raise ValueError('Search options or library changed. Start a new search.')
    _, selected, params = _selection(Catalog(db, workspace), **(filters or {}))
    signature, count = _scope_snapshot(db, selected, params, budget.check)
    if previous is not None and previous['snapshot'] != signature:
        raise ValueError('The catalog or active revisions changed. Start a new search.')
    position = previous['position'] if previous else None
    display = previous['display'] if previous else None
    compiled = None
    if mode == 'advanced':
        from .advanced import compile_query
        compiled = compile_query(query, check=budget.check)
    ranked = mode in ('thematic', 'semantic')
    maximum = min(page_budget, limit) if ranked else page_budget
    rows = _metadata_rows(db, selected, params, position, maximum)
    hits, passages, metadata = [], [], {}
    scanned = characters = 0
    more, last = False, position
    try:
        for row in rows:
            budget.check()
            if scanned >= maximum:
                more = True
                break
            length = row[9]
            if type(length) is not int or length < 0:
                raise ValueError('Invalid source length.')
            if length > character_budget:
                raise ValueError('One source segment exceeds this search page character budget.')
            if characters + length > character_budget:
                more = True
                break
            raw, base, work = _read_source(db, row, workspace, budget.check)
            scanned += 1
            characters += length
            current = [row[2], row[5], row[0], None]
            if ranked:
                if raw:
                    key = str(row[0])
                    metadata[key] = base
                    passages.append(thematic.Passage(workspace, work, row[2], row[3], key,
                        row[6], 0, len(raw), raw, row[10], row[5] if row[6] is None else None))
            elif raw:
                for spans, reason in _literal_matches(raw, query, mode, compiled, budget.check):
                    start, end = spans[0]
                    if position is not None and current[:3] == position[:3] and position[3] is not None:
                        if start <= position[3]:
                            continue
                    hits.append(dict(base, start=start, end=end, raw_quote=raw[start:end],
                                     match_spans=spans, score=1.0, reason=reason))
                    if len(hits) == limit:
                        last = [row[2], row[5], row[0], start]
                        more = True
                        break
                if len(hits) == limit:
                    break
            last = current
    finally:
        rows.close()
    if ranked:
        hits = _rank(workspace, passages, metadata, query, mode, limit,
                     budget, ranker, cache, on_status)
    budget.check()
    continuation = (dict(schema=SCHEMA, binding=binding, snapshot=signature,
                         position=last, display=display) if more else None)
    return hits, continuation, scanned, count


def _rank(workspace, passages, metadata, query, mode, limit, budget, ranker, cache, report):
    from .semantic_windows import search_windowed
    used_model = False
    def lexical(reason=None):
        if reason is not None and report is not None:
            report(reason)
        return thematic.search(workspace, passages, query, limit=limit,
                               timeout=min(60.0, budget.remaining()), cancelled=budget.stopped)
    if mode == 'semantic' and ranker is not None:
        from ..semantic import OfflineSemanticRanker
        if not isinstance(ranker, OfflineSemanticRanker):
            raise ValueError('A verified local semantic ranker is required.')
        def rank(value, rows):
            options = {'cache': cache} if cache is not None else {}
            return ranker.rank(value, rows, cancelled=budget.stopped,
                               timeout=min(60.0, budget.remaining()), **options)
        try:
            hits = search_windowed(workspace, passages, query, tokenizer=ranker.tokenizer,
                semantic_ranker=rank, limit=limit, timeout=min(60.0, budget.remaining()),
                cancelled=budget.stopped)
            used_model = True
        except ValueError as exc:
            if 'budget' not in str(exc).lower() and 'semantic window' not in str(exc).lower():
                raise
            hits = lexical('The local model window budget was reached; this page uses keyword ranking.')
    else:
        hits = lexical('Local model unavailable; this page uses keyword ranking.' if mode == 'semantic' else None)
    result = []
    for hit in hits:
        budget.check()
        key = hit['parent_passage_id'] if used_model else hit['passage_id']
        result.append(dict(hit, **metadata[key]))
    return result
