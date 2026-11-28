"""Scoped literal search candidate for CARR-05's active_page view.

Native SQLite FTS5 trigram is a candidate filter, never citation authority.
NFD/full casefold and whitespace folding are search-only; returned offsets are
half-open Python code-point offsets into the original immutable page. No raw
FTS syntax, stemming, cross-page stitching, or generated quotation.
"""
from .paging import MAX_TIMEOUT_SECONDS

from array import array
from bisect import bisect_right
from hashlib import sha256
import re
import sqlite3
from time import monotonic
import unicodedata as ud

MAX_PAGES = 20000
MAX_CHARS = 5_000_000
MAX_QUERY = 500
_LONG_ASCII = 64 * 1024
_SPACE_RUN = re.compile(r'\s{2,}')
_ASCII_RUN = re.compile(r'[\x00-\x7f]+')


def _fold(text):
    return re.sub(r'\s+', ' ', ud.normalize('NFD', ud.normalize('NFD', text).casefold()))


class _AsciiOffsets:
    """Raw boundaries after ASCII whitespace folding, without per-letter maps.

    ASCII case folding preserves length. Only collapsed whitespace runs change
    offsets, so each such run needs one breakpoint. The boundary after a run
    points beyond all its original whitespace, preserving source-exact slices.
    """
    def __init__(self, length, cuts, removed):
        self.length = length
        self.cuts, self.removed = tuple(cuts), tuple(removed)

    def __contains__(self, position):
        return 0 <= position <= self.length

    def __getitem__(self, position):
        if position not in self:
            raise KeyError(position)
        index = bisect_right(self.cuts, position) - 1
        return position + (self.removed[index] if index >= 0 else 0)


def _ascii_mapped(text, check):
    check()
    folded = _fold(text)
    check()
    cuts, removed = [], []
    shift = 0
    for match in _SPACE_RUN.finditer(text):
        check()
        shift += match.end() - match.start() - 1
        cuts.append(match.end() - shift)
        removed.append(shift)
    check()
    offsets = _AsciiOffsets(len(folded), cuts, removed)
    return folded, offsets, offsets


class _CompactOffsets:
    """Dense source boundaries; -1 marks a position inside a folded cluster."""
    def __init__(self, length):
        self.values = array('i', [-1]) * (length + 1)

    def __contains__(self, position):
        return 0 <= position < len(self.values) and self.values[position] >= 0

    def __getitem__(self, position):
        if position not in self:
            raise KeyError(position)
        return self.values[position]

    def __setitem__(self, position, value):
        self.values[position] = value

    def items(self):
        return ((position, value) for position, value in enumerate(self.values) if value >= 0)


def _mapped(text, check=lambda: None):
    """Map whole canonical clusters/whitespace runs, not partial casefold expansions."""
    # TXT books need not contain form-feed page boundaries. Mapping millions of
    # ordinary ASCII letters into two dictionaries exhausted the search deadline
    # and hundreds of megabytes even when the source was within MAX_CHARS.
    # Long ASCII segments need only sparse whitespace offsets. Long Unicode
    # segments keep the same cluster rules in compact integer arrays, avoiding
    # two per-character dictionaries and millions of retained small strings.
    if len(text) >= _LONG_ASCII and text.isascii():
        return _ascii_mapped(text, check)
    compact = len(text) >= _LONG_ASCII
    if compact:
        check()
        folded = _fold(text)
        check()
        starts, ends = _CompactOffsets(len(folded)), _CompactOffsets(len(folded))
        parts = None
    else:
        parts, starts, ends = [], {}, {}
    i = pos = next_check = 0
    while i < len(text):
        if i >= next_check:
            check()
            next_check = i + 1024
        # A small Unicode fragment must not turn a long ASCII remainder into
        # millions of Python normalization/map assignments. Limit every bulk run
        # to the existing cancellation interval and leave a cross-boundary
        # whitespace run or base-plus-mark cluster to the canonical path below.
        if ord(text[i]) < 128:
            match = _ASCII_RUN.match(text, i, min(len(text), next_check))
            j = match.end()
            if (j < len(text) and not text[j - 1].isspace()
                    and ud.category(text[j]).startswith('M')):
                j -= 1
            if j < len(text) and text[j].isspace():
                while j > i and text[j - 1].isspace():
                    j -= 1
            if j > i:
                part, offsets, _ = _ascii_mapped(text[i:j], check)
                if compact and not folded.startswith(part, pos):
                    raise ValueError('unsupported normalization mapping')
                boundaries = array('i')
                begin = shift = 0
                for cut, removed in zip(offsets.cuts, offsets.removed):
                    boundaries.extend(range(i + begin + shift, i + cut + shift))
                    begin, shift = cut, removed
                boundaries.extend(range(i + begin + shift, i + len(part) + 1 + shift))
                if compact:
                    starts.values[pos:pos + len(part)] = boundaries[:-1]
                    ends.values[pos + 1:pos + len(part) + 1] = boundaries[1:]
                else:
                    starts.update(zip(range(pos, pos + len(part)), boundaries))
                    ends.update(zip(range(pos + 1, pos + len(part) + 1), boundaries[1:]))
                    parts.append(part)
                pos += len(part)
                i = j
                continue
        j = i + 1
        if text[i].isspace():
            while j < len(text) and text[j].isspace():
                j += 1
                if j % 1024 == 0:
                    check()
        else:
            while j < len(text) and ud.category(text[j]).startswith('M'):
                j += 1
                if j % 1024 == 0:
                    check()
        part = _fold(text[i:j])
        if compact and not folded.startswith(part, pos):
            raise ValueError('unsupported normalization mapping')
        starts[pos] = i
        pos += len(part)
        ends[pos] = j
        if not compact:
            parts.append(part)
        i = j
    check()
    if compact:
        if pos != len(folded):
            raise ValueError('unsupported normalization mapping')
    else:
        folded = ''.join(parts)
        if folded != _fold(text):
            raise ValueError('unsupported normalization mapping')
    return folded, starts, ends


def _word(char):
    return ud.category(char)[0] in 'LNM' or ud.category(char) == 'Pc'


def _spans(folded, starts, ends, needle, check=lambda: None):
    pos = 0
    while (i := folded.find(needle, pos)) != -1:
        check()
        j = i + len(needle)
        pos = i + 1
        if i not in starts or j not in ends:
            continue
        if _word(needle[0]) and i and _word(folded[i-1]):
            continue
        if _word(needle[-1]) and j < len(folded) and _word(folded[j]):
            continue
        yield starts[i], ends[j]


def search(db, workspace, query, *, mode='phrase', edition_ids=None,
           limit=50, offset=0, timeout=5.0, cancelled=lambda: False, page_ids=None, resources=None):
    """One active workspace only; edition_ids is the catalog-resolved filter.

    phrase: literal punctuation and whitespace-equivalent contiguous phrase;
    word: one whitespace-free literal; terms: all whitespace-separated literals
    on the same page, one hit per page (first occurrence of each term).
    advanced: explicit bounded Boolean/NEAR syntax, one source witness per page.
    Other modes have no wildcard/operator syntax. Exact matches share score 1; tie-break is
    edition, segment ordinal, page id, raw offset. Results never cross pages.
    Only active_page rows from PR #21 are accepted; no persistent second index.
    Scope caps fail explicitly, not silently with incomplete results. Offset
    only skips matches in this bounded scope; it never sizes an allocation.
    """
    if not isinstance(workspace, str) or not workspace:
        raise ValueError('explicit workspace required')
    if mode not in ('word', 'phrase', 'terms', 'advanced'):
        raise ValueError('invalid literal mode')
    if not isinstance(query, str) or len(query) > MAX_QUERY or any(
        ud.category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query
    ):
        raise ValueError('invalid query')
    if not (type(limit) is int and type(offset) is int and type(timeout) in (int, float)
            and 1 <= limit <= 100
            and offset >= 0 and 0 < timeout <= MAX_TIMEOUT_SECONDS):
        raise ValueError('invalid search budget')
    if resources is not None:
        from .paging import SearchResources
        if type(resources) is not SearchResources or page_ids is None:
            raise ValueError('A paged search needs a validated resource budget.')
    character_limit = resources.source_characters if resources is not None else MAX_CHARS
    q = _fold(query).strip()
    if not q:
        return []
    if mode == 'word' and ' ' in q:
        raise ValueError('word mode requires one term')
    needles = tuple(dict.fromkeys(q.split(' ') if mode == 'terms' else [q]))
    if len(needles) > 32:
        raise ValueError('too many terms')
    deadline = monotonic() + timeout
    latched = False

    def stopped():
        nonlocal latched
        latched = latched or cancelled() or monotonic() >= deadline
        return latched

    def check():
        if stopped():
            raise TimeoutError('literal search cancelled or timed out')

    check()
    advanced = None
    if mode == 'advanced':
        # Lazy import keeps the parser reusable with the exact normalization and
        # span helpers above, while all scope/integrity/budget logic stays here.
        from .advanced import compile_query
        advanced = compile_query(query, check=check)
    params = [workspace]
    where = 'workspace=?'
    if edition_ids is not None:
        from itertools import islice
        if isinstance(edition_ids, (str, bytes, bytearray)):
            raise ValueError('edition filter must be an iterable of IDs, not a scalar')
        ids = []
        for item in islice(edition_ids, 501):
            check()
            if len(ids) == 500 or not isinstance(item, str) or not item:
                raise ValueError('invalid edition filter')
            ids.append(item)
        check()
        if not ids:
            return []
        where += ' AND edition IN (' + ','.join('?' for _ in ids) + ')'
        params.extend(ids)
    from .paging import checked_page_ids
    page_clause, page_parameters = checked_page_ids(page_ids)
    where += page_clause
    params.extend(page_parameters)
    sql = ('SELECT id,workspace,edition,revision,asset,ordinal,physical,printed,'
           'verified,raw,digest FROM active_page WHERE ' + where +
           ' ORDER BY edition,ordinal,id')
    index = sqlite3.connect(':memory:')
    rows, result = [], []
    size = seen = 0
    try:
        index.set_progress_handler(lambda: int(stopped()), 1000)
        index.execute("CREATE VIRTUAL TABLE scoped USING fts5(text, tokenize='trigram case_sensitive 1')")
        for row in db.execute(sql, params):
            check()
            if row[1] != workspace:
                raise ValueError('foreign workspace row')
            raw = row[9]
            size += len(raw)
            if len(rows) >= MAX_PAGES or size > character_limit:
                raise ValueError('scope exceeds literal search budget')
            # Validate every scoped source before candidate pruning or pagination.
            if sha256(raw.encode('utf-8')).hexdigest() != row[10]:
                raise ValueError('raw page digest mismatch')
            rows.append(tuple(row))
            index.execute('INSERT INTO scoped(rowid,text) VALUES(?,?)', (len(rows), _fold(raw)))
        check()
        # Trigram cannot match strings shorter than three code points. Fall back
        # to the scoped row list, then verify all literal needles against raw.
        candidate = next((n for n in needles if len(n) >= 3), None)
        expression = (advanced.candidate if advanced is not None else
                      '"' + candidate.replace('"', '""') + '"' if candidate is not None else None)
        if expression is None:
            hits = range(1, len(rows)+1)
        else:
            hits = (r[0] for r in index.execute('SELECT rowid FROM scoped WHERE scoped MATCH ? ORDER BY rowid', (expression,)))
        for rowid in hits:
            check()
            page_id, ws, edition, revision, asset, ordinal, physical, printed, verified, raw, digest = rows[rowid-1]
            folded, starts, ends = _mapped(raw, check)
            check()
            if advanced is not None:
                match = advanced.match(raw, folded, starts, ends, check=check)
                groups = [match.spans] if match is not None else []
            elif mode == 'terms':
                spans = [next(_spans(folded, starts, ends, n, check), None) for n in needles]
                groups = [tuple(sorted(set(spans)))] if all(s is not None for s in spans) else []
            else:
                groups = ((s,) for s in _spans(folded, starts, ends, needles[0], check))
            for spans in groups:
                check()
                if seen < offset:
                    seen += 1
                    continue
                start, end = spans[0]
                result.append(dict(workspace_id=ws, edition_id=edition, revision_id=revision,
                    asset_sha256=asset, page_id=page_id, segment_index=ordinal if physical is None else None,
                    locator_kind='logical_segment' if physical is None else 'physical_page',
                    physical_index=physical, printed_label=printed if physical is not None and verified else None,
                    printed_label_verified=bool(physical is not None and verified),
                    raw_text_sha256=digest, start=start, end=end, raw_quote=raw[start:end],
                    match_spans=spans, score=1.0))
                if advanced is not None:
                    result[-1]['reason'] = match.reason
                if len(result) == limit:
                    check()
                    return result
        check()
        return result
    except sqlite3.OperationalError as exc:
        if stopped():
            raise TimeoutError('literal search cancelled or timed out') from exc
        raise
    finally:
        index.close()


def search_file(path, workspace, query, *, timeout=5.0,
                cancelled=lambda: False, **options):
    """Low-level search of a trusted local file, with explicit edition IDs.

    For a live collection or catalog selection, use search_catalog_file instead;
    IDs resolved in another transaction are not a collection authorization.
    """
    return _search_file(path, workspace, query, timeout, cancelled, options)


def search_catalog_file(path, workspace, query, *, collection=None, timeout=5.0,
                        cancelled=lambda: False, **options):
    """Resolve catalog scope and search in one read-only SQLite snapshot.

    None means all cataloged editions in the explicit workspace, not global
    search. A collection must be active in that workspace. Never accept cached
    edition_ids as a substitute for catalog resolution. The existing 500-edition
    search cap applies; oversized scopes fail explicitly. Results represent the
    snapshot at the first read; subsequent actions need fresh authorization.
    """
    if 'edition_ids' in options:
        raise ValueError('catalog search resolves edition IDs in its own snapshot')
    return _search_file(path, workspace, query, timeout, cancelled, options,
                        catalog_scope=True, collection=collection)


def _search_file(path, workspace, query, timeout, cancelled, options, *,
                 catalog_scope=False, collection=None):
    """One outer deadline and sticky cancellation across all source and FTS SQL.

    Never change a caller's writer/progress handler. Lock contention fails
    immediately; no file creation or immutable URI. WAL sidecars and filesystem
    stalls remain subject to SQLite/OS behavior; not a hard real-time deadline.
    """
    from pathlib import Path

    if type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError('invalid search budget')
    deadline = monotonic() + timeout
    latched = False

    def stopped():
        nonlocal latched
        latched = latched or cancelled() or monotonic() >= deadline
        return latched

    def check():
        if stopped():
            raise TimeoutError('literal search cancelled or timed out')

    check()
    db = None
    try:
        uri = Path(path).absolute().as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=True, timeout=0.0)
        db.set_progress_handler(lambda: int(stopped()), 1000)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        check()
        if catalog_scope:
            from carr_library.catalog.membership import Catalog
            db.execute('BEGIN')
            # Catalog's first SELECT establishes the snapshot, and all active
            # page reads below use that same connection and transaction.
            rows = Catalog(db, workspace).editions(collection)
            check()
            options = dict(options, edition_ids=(row[0] for row in rows))
        hits = search(db, workspace, query, timeout=timeout,
                      cancelled=stopped, **options)
        check()
        return hits
    except sqlite3.OperationalError as exc:
        if stopped():
            raise TimeoutError('literal search cancelled or timed out') from exc
        raise
    finally:
        if db is not None:
            db.close()
