"""Stream bounded source windows while preserving the host's opaque cursors.

The current literal implementation remains in use for its compatible scopes.
This route reuses PR99's catalog keyset traversal and local-window ranking, but
validates the complete raw-source snapshot before candidate pruning, as the host
already requires. Every displayed result still passes LibraryApp's retained
original/metadata attestation. No serialized cursor or persistent index exists.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import unicodedata

from ..catalog.membership import Catalog
from ..catalog.read_views import _selection
from . import literal, thematic

MAX_PAGE_RESULTS = 100
MAX_PAGE_SOURCES = 20_000
MAX_PAGE_CHARACTERS = 5_000_000
DEFAULT_PAGE_SOURCES = 500
MAX_PRESENTED_SPANS = 50_000
_HASH = re.compile('[0-9a-f]{64}\\Z')


@dataclass(frozen=True)
class WindowPage:
    hits: tuple
    snapshot: str
    after: tuple | None
    scanned_segments: int
    scope_editions: int
    ordering: str
    kind: str = 'window'


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _identity(value):
    return type(value) is str and 0 < len(value) <= 256 and '\x00' not in value


def validate_budgets(page_budget, character_budget):
    if (type(page_budget) is not int or not 1 <= page_budget <= MAX_PAGE_SOURCES
            or type(character_budget) is not int or not 1 <= character_budget <= MAX_PAGE_CHARACTERS):
        raise ValueError('Choose bounded source-window and character budgets.')


def scope_statistics(db, workspace, filters, budget):
    """Metadata only; select the new route without changing old literal reads."""
    _, selected, params = _selection(Catalog(db, workspace), **filters)
    budget.check()
    editions = db.execute('SELECT count(*) FROM (' + selected + ')', params).fetchone()[0]
    row = db.execute('WITH selected AS (' + selected + ') '
        'SELECT count(*),coalesce(sum(length(p.raw)),0) FROM selected s '
        'JOIN active_page p ON p.workspace=s.workspace AND p.edition=s.id',
        params).fetchone()
    budget.check()
    return editions, row[0], row[1]


def _scope_snapshot(db, selected, params, check, character_budget):
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
    # Preserve the host's complete raw-source integrity check. Stream one
    # admitted segment at a time; do not retain a corpus-wide text index.
    segments = 0
    source_rows = _metadata_rows(db, selected, params, None, None)
    try:
        for row in source_rows:
            check()
            _admit_source(row, character_budget)
            _read_source(db, row, params['scope'], check)
            payload = _canonical(tuple(row))
            digest.update(len(payload).to_bytes(8, 'big'))
            digest.update(payload)
            segments += 1
    finally:
        source_rows.close()
    check()
    return digest.hexdigest(), count, segments


def _metadata_rows(db, selected, params, position, maximum):
    # Read source length before its text, so an oversized row cannot make the
    # bounded candidate list allocate its contents before admission.
    tail = ''
    params = dict(params)
    limit = ''
    if maximum is not None:
        params['rows'] = maximum + 1
        limit = ' LIMIT :rows'
    if position is not None:
        edition, ordinal, page, start = position
        relation = '>=' if start is not None else '>'
        tail = (' AND (s.id COLLATE BINARY>:last_edition OR '
                '(s.id=:last_edition AND (p.ordinal>:last_ordinal OR '
                '(p.ordinal=:last_ordinal AND p.id' + relation + ':last_page))))')
        params.update(last_edition=edition, last_ordinal=ordinal, last_page=page)
    return db.execute('WITH selected AS (' + selected + ') '
        'SELECT p.id,p.workspace,p.edition,p.revision,p.asset,p.ordinal,p.physical,'
        'p.printed,p.verified,length(p.raw),p.digest,s.work,'
        'length(CAST(p.raw AS BLOB)),typeof(p.raw),instr(p.raw,char(0)) '
        'FROM selected s JOIN active_page p ON p.workspace=s.workspace '
        'AND p.edition=s.id AND p.asset=s.asset WHERE 1=1' + tail +
        ' ORDER BY s.id COLLATE BINARY,p.ordinal,p.id' + limit, params)


def _admit_source(row, character_budget):
    # SQLite length(TEXT) stops at NUL. Its byte length and storage type must
    # be admitted before selecting the value into Python, even for corruption.
    if len(row) != 15 or row[13] != 'text' or row[14] != 0:
        raise ValueError('Invalid source text before search-page admission.')
    characters, byte_count = row[9], row[12]
    if (type(characters) is not int or not 0 <= characters <= character_budget
            or type(byte_count) is not int or not characters <= byte_count <= 4 * character_budget):
        raise ValueError('One source segment exceeds this search page character budget.')


def _read_source(db, row, workspace, check):
    (page, ws, edition, revision, asset, ordinal, physical, printed,
     verified, length, digest, work) = tuple(row[:12])
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


def candidates(db, workspace, query, *, continuation, mode='phrase',
               filters=None, limit=100, page_budget=DEFAULT_PAGE_SOURCES,
               character_budget=MAX_PAGE_CHARACTERS, result_mode=None,
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
    if result_mode not in (None, 'short', 'thematic'):
        raise ValueError('Choose Short quotations or Thematic excerpts.')
    if continuation.kind not in ('literal', 'window') or (
            continuation.kind == 'literal' and continuation.after is not None):
        raise ValueError('This source-window continuation belongs to another search route.')
    _, selected, params = _selection(Catalog(db, workspace), **(filters or {}))
    signature, count, _ = _scope_snapshot(db, selected, params, budget.check, character_budget)
    if continuation.snapshot is not None and continuation.snapshot != signature:
        raise ValueError('Search sources or catalog filters changed. Start a new search.')
    position = continuation.after
    if position is not None and (
            type(position) is not tuple or len(position) != 4 or not _identity(position[0])
            or type(position[1]) is not int or position[1] < 1
            or type(position[2]) is not int or position[2] < 1
            or position[3] is not None and (type(position[3]) is not int or position[3] < 0)):
        raise ValueError('Invalid internal source-window continuation position.')
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
            _admit_source(row, character_budget)
            length = row[9]
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
                presented = set()
                for spans, reason in _literal_matches(raw, query, mode, compiled, budget.check):
                    start, end = spans[0]
                    hit = dict(base, start=start, end=end, raw_quote=raw[start:end],
                               match_spans=spans, score=1.0, reason=reason)
                    if result_mode is not None:
                        from .presentation import presentation_span
                        display = presentation_span(raw, hit, query, result_mode, check=budget.check)
                        if display in presented:
                            continue
                        if len(presented) >= MAX_PRESENTED_SPANS:
                            raise ValueError('One source exceeds this search presentation budget.')
                        presented.add(display)
                    # Reconstruct deduplication before the previous raw offset,
                    # exactly as the existing literal route does.
                    if position is not None and tuple(current[:3]) == tuple(position[:3]) and position[3] is not None:
                        if start <= position[3]:
                            continue
                    hits.append(hit)
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
    ordering = ('Source windows in edition/segment order; relevance is local to each window.'
                if ranked else 'Source edition, segment and raw offset.')
    return WindowPage(tuple(hits), signature, tuple(last) if more else None,
                      scanned, count, ordering)


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
