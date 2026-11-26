"""Offline thematic BM25 candidate. One scope per invocation; no network or models.

The extract_query_terms function below is from cairnsearch, MIT licensed,
commit eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93, search/snippets.py.
The original license is retained at the end of this module.
"""
from dataclasses import dataclass
from hashlib import sha256
import re
import sqlite3
from time import monotonic
from unicodedata import category, normalize


@dataclass(frozen=True)
class Passage:
    workspace: str
    work_id: str
    edition_id: str
    revision_id: str
    passage_id: str
    physical_index: int | None
    start: int
    end: int
    raw_page: str
    page_sha256: str
    segment_index: int | None = None


# Conservative transliterations, not inferred identities or translations.
ALIASES = (("Herzen", "Gertsen"), ("Ogarev", "Ogaryov"),
           ("Bakunin", "Bakounine"), ("Belinsky", "Belinski"))


def _groups(query):
    if (not isinstance(query, str) or len(query) > 500 or any(
            category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query)):
        raise ValueError("invalid thematic query")
    normalized = normalize("NFC", query)
    terms = extract_query_terms(normalized)
    # The reused parser recognizes content:/filename: fields and drops every
    # other colon token. In this plain-language adapter a time (10:30), ratio
    # (1:2), or heading (railway:) is still a required search concept. Preserve
    # those tokens as quoted FTS text, excluding already extracted phrases.
    # Keep the upstream parser itself unchanged and retain its known prefixes.
    unquoted = re.sub(r'"([^"]+)"', '', normalized)
    terms.extend(word for word in unquoted.split()
                 if ':' in word and word.split(':', 1)[0].lower()
                 not in ('filename', 'content'))
    groups = []
    for term in terms:
        # The upstream splitter is not a security boundary. Every token is quoted
        # independently below, never interpreted as FTS5 operators or columns.
        phrase = term.strip()
        if not any(ch.isalnum() for ch in phrase):
            continue
        # Let native unicode61 tokenize the full term, including combining marks.
        # Only the ASCII alias lookup strips surrounding punctuation.
        alias_key = re.sub(r"^[\W_]+|[\W_]+$", "", phrase)
        variants = next((g for g in ALIASES if alias_key.casefold() in
                         {v.casefold() for v in g}), (phrase,))
        if variants not in groups:
            groups.append(variants)
    if len(groups) > 24:
        raise ValueError("too many query concepts")
    return groups


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def search(workspace, passages, query, limit=20, *, timeout=5.0, cancelled=lambda: False):
    """Rank active, authorized source spans supplied by the caller.

    The caller must resolve active revisions and collection/topic memberships
    in storage before invoking this adapter. Mixed workspace input fails closed.
    Scores are scope-local; the temporary FTS5 table is discarded after use.
    Returns exact code-point spans, never transformed snippets or page labels.
    Cancellation is cooperative; a blocking caller iterator or native operation
    may exceed the deadline, but a stopped search never returns partial results.
    """
    if (not isinstance(workspace, str) or not workspace or type(limit) is not int
            or not 1 <= limit <= 100 or type(timeout) not in (int, float)
            or not 0 < timeout <= 60 or not callable(cancelled)):
        raise ValueError("explicit workspace and bounded search budget required")
    deadline = monotonic() + timeout

    def stopped():
        return cancelled() or monotonic() >= deadline

    def check():
        if stopped():
            raise TimeoutError("thematic search cancelled or timed out")

    check()
    groups = _groups(query)
    check()
    if not groups:
        return []
    expressions = ["(" + " OR ".join(map(_quote, g)) + ")" for g in groups]
    match = " AND ".join(expressions)
    refs = []
    seen = set()
    size = 0
    db = sqlite3.connect(":memory:")
    try:
        db.set_progress_handler(lambda: int(bool(stopped())), 1000)
        db.execute("""CREATE VIRTUAL TABLE scoped USING fts5(text,
            tokenize="unicode61 remove_diacritics 0 categories 'L* N* Co M*'")""")
        for p in passages:
            check()
            if p.workspace != workspace:
                raise ValueError("foreign workspace passage")
            if (not all((p.work_id, p.edition_id, p.revision_id, p.passage_id))
                    or not ((type(p.physical_index) is int and p.physical_index >= 1 and p.segment_index is None)
                            or (p.physical_index is None and type(p.segment_index) is int and p.segment_index >= 1))
                    or type(p.start) is not int or type(p.end) is not int
                    or not isinstance(p.raw_page, str) or '\x00' in p.raw_page
                    or not 0 <= p.start < p.end <= len(p.raw_page)
                    or sha256(p.raw_page.encode('utf-8')).hexdigest() != p.page_sha256):
                raise ValueError("unverified passage anchor")
            identity = (p.edition_id, p.revision_id, p.passage_id)
            if identity in seen:
                raise ValueError("duplicate passage identity")
            seen.add(identity)
            quote = p.raw_page[p.start:p.end]
            size += len(p.raw_page)
            if len(refs) >= 20000 or size > 10_000_000:
                raise ValueError("scope exceeds temporary ranking budget")
            refs.append(p)
            db.execute("INSERT INTO scoped(rowid,text) VALUES (?,?)", (len(refs), normalize("NFC", quote)))
        check()
        # Native SQLite BM25: lower is better. All concepts must occur in the
        # same contiguous span, with OR only for controlled spelling variants.
        ranked = db.execute("SELECT rowid,bm25(scoped) FROM scoped WHERE scoped MATCH ? ORDER BY 2,rowid", (match,)).fetchall()
        check()
        ranked.sort(key=lambda r: (r[1], refs[r[0]-1].edition_id, refs[r[0]-1].revision_id, refs[r[0]-1].passage_id))
        hits, deferred, works = [], [], set()
        for rowid, rank in ranked:
            check()
            p = refs[rowid-1]
            if p.work_id in works:
                deferred.append((rowid, rank))
            else:
                works.add(p.work_id)
                hits.append((rowid, rank))
        result = []
        for rowid, rank in (hits + deferred)[:limit]:
            check()
            p = refs[rowid-1]
            matched = tuple(v for g in groups for v in g if db.execute(
                "SELECT 1 FROM scoped WHERE rowid=? AND scoped MATCH ?", (rowid, _quote(v))).fetchone())
            result.append({"passage_id": p.passage_id, "work_id": p.work_id,
                "edition_id": p.edition_id, "revision_id": p.revision_id,
                "physical_index": p.physical_index, "start": p.start, "end": p.end,
                "page_sha256": p.page_sha256, "raw_quote": p.raw_page[p.start:p.end],
                "score": -rank, "matched_terms": matched,
                "reason": "lexical BM25 with controlled variants; not semantic verification"})
            if p.physical_index is None:
                result[-1].update(locator_kind="logical_segment", segment_index=p.segment_index)
        check()
        return result
    except sqlite3.OperationalError as exc:
        if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
            raise TimeoutError("thematic search cancelled or timed out") from exc
        raise
    finally:
        db.close()


def search_active(db, workspace, edition_to_work, query, *, limit=20,
                  timeout=5.0, cancelled=lambda: False):
    """Candidate handoff for the PR21 active_page view and PR23 locator V2.

    The caller supplies a catalog-authorized edition-to-work mapping for this
    workspace/collection/topic; no implicit global scope. Use a dedicated trusted
    storage reader; its own native SQL timeout/interrupt remains caller-owned.
    This additive candidate does not change the legacy physical search response.
    Full contiguous pages/segments are ranked; no inferred printed page labels.
    """
    if (not isinstance(workspace, str) or not workspace or
            not isinstance(edition_to_work, dict) or len(edition_to_work) > 500 or
            any(not isinstance(v, str) or not v for pair in edition_to_work.items() for v in pair) or
            type(limit) is not int or not 1 <= limit <= 100 or
            type(timeout) not in (int, float) or not 0 < timeout <= 60 or
            not callable(cancelled)):
        raise ValueError('invalid active thematic scope or budget')
    allowed = dict(edition_to_work)
    deadline = monotonic() + timeout
    latched = False

    def stopped():
        nonlocal latched
        latched = latched or cancelled() or monotonic() >= deadline
        return latched

    metadata = {}

    def passages():
        if not allowed:
            return
        placeholders = ','.join('?' for _ in allowed)
        sql = ('SELECT id,workspace,edition,revision,asset,ordinal,physical,printed,'
               'verified,raw,digest FROM active_page WHERE workspace=? AND edition IN ('
               + placeholders + ') ORDER BY edition,ordinal,id')
        cursor = db.execute(sql, (workspace, *allowed))
        try:
            for count, row in enumerate(cursor, 1):
                if count > 20000:
                    raise ValueError('scope exceeds temporary ranking budget')
                if stopped():
                    raise TimeoutError('active thematic search cancelled or timed out')
                page_id, ws, edition, revision, asset, ordinal, physical, printed, verified, raw, digest = row
                if (ws != workspace or edition not in allowed or type(page_id) is not int or page_id < 1
                        or type(ordinal) is not int or ordinal < 1
                        or (physical is not None and (type(physical) is not int or physical < 1))
                        or not isinstance(asset, str) or not re.fullmatch('[0-9a-f]{64}', asset)
                        or type(verified) is not int or verified not in (0, 1)
                        or (physical is None and (printed is not None or verified))
                        or (verified and (not isinstance(printed, str) or not printed.strip()))
                        or not isinstance(raw, str) or '\x00' in raw
                        or sha256(raw.encode('utf-8')).hexdigest() != digest):
                    raise ValueError('invalid active source locator or digest')
                if not raw:
                    continue
                key = str(page_id)
                if key in metadata:
                    raise ValueError('duplicate active page')
                metadata[key] = (page_id, asset, printed if verified else None, bool(verified))
                yield Passage(ws, allowed[edition], edition, revision, key, physical,
                              0, len(raw), raw, digest, ordinal if physical is None else None)
        finally:
            cursor.close()

    source = passages()
    try:
        hits = search(workspace, source, query, limit=limit, timeout=timeout, cancelled=stopped)
    except sqlite3.OperationalError as exc:
        if stopped():
            raise TimeoutError('active thematic search cancelled or timed out') from exc
        raise
    finally:
        source.close()
    for hit in hits:
        page_id, asset, printed, verified = metadata[hit['passage_id']]
        hit.update(workspace_id=workspace, page_id=page_id, asset_sha256=asset,
                   locator_kind='logical_segment' if hit['physical_index'] is None else 'physical_page',
                   segment_index=hit.get('segment_index'), printed_label=printed,
                   printed_label_verified=verified, raw_text_sha256=hit['page_sha256'])
    if stopped():
        raise TimeoutError('active thematic search cancelled or timed out')
    return hits


def search_catalog(db, workspace, query, *, collection=None, limit=20,
                   timeout=5.0, cancelled=lambda: False):
    """Candidate PR53/PR21 handoff on a caller-owned SQLite read snapshot.

    Catalog memberships authorize editions; the active revision must reference
    the same immutable asset. No transaction is opened, committed or rolled back
    here. Use a dedicated trusted reader; the caller owns its native SQL budget.
    """
    from carr_library.catalog.membership import Catalog

    if (not db.in_transaction or not isinstance(workspace, str) or not workspace
            or type(limit) is not int or not 1 <= limit <= 100
            or type(timeout) not in (int, float) or not 0 < timeout <= 60
            or not callable(cancelled)):
        raise ValueError('explicit read snapshot and bounded search budget required')
    deadline = monotonic() + timeout
    latched = False

    def stopped():
        nonlocal latched
        latched = latched or cancelled() or monotonic() >= deadline
        return latched

    def check():
        if stopped():
            raise TimeoutError('catalog thematic search cancelled or timed out')

    check()
    rows = Catalog(db, workspace).editions(collection)
    check()
    if len(rows) > 500:
        raise ValueError('catalog scope exceeds edition budget')
    allowed = {}
    assets = {}
    for edition, work, asset, *_ in rows:
        check()
        if edition in allowed or not isinstance(asset, str) or not re.fullmatch('[0-9a-f]{64}', asset):
            raise ValueError('invalid catalog edition identity')
        allowed[edition] = work
        assets[edition] = asset
    if allowed:
        placeholders = ','.join('?' for _ in allowed)
        sql = ('SELECT a.edition,r.asset FROM active a JOIN revision r ON r.id=a.revision '
               'WHERE a.workspace=? AND a.edition IN (' + placeholders + ')')
        cursor = db.execute(sql, (workspace, *allowed))
        try:
            for edition, asset in cursor:
                check()
                if assets[edition] != asset:
                    raise ValueError('active revision asset differs from catalog edition')
        finally:
            cursor.close()
    check()
    # Keep the entire operation within the original deadline and snapshot.
    remaining = min(timeout, deadline - monotonic())
    if remaining <= 0:
        raise TimeoutError('catalog thematic search cancelled or timed out')
    hits = search_active(db, workspace, allowed, query, limit=limit,
                         timeout=remaining, cancelled=stopped)
    for hit in hits:
        check()
        if assets.get(hit['edition_id']) != hit['asset_sha256']:
            raise ValueError('returned source asset differs from catalog edition')
    check()
    return hits


def extract_query_terms(query: str) -> list[str]:
    """Extract searchable terms from query string."""
    terms = []
    
    # Extract quoted phrases
    phrase_pattern = re.compile(r'"([^"]+)"')
    for match in phrase_pattern.finditer(query):
        terms.append(match.group(1))
    
    # Remove phrases from query
    remaining = phrase_pattern.sub("", query)
    
    # Extract words (skip operators and field prefixes)
    skip_words = {"and", "or", "not"}
    for word in remaining.split():
        word = word.strip()
        if not word:
            continue
        if word.lower() in skip_words:
            continue
        if ":" in word:
            # Check if it's a searchable field
            field, value = word.split(":", 1)
            if field.lower() in ("filename", "content"):
                terms.append(value)
            continue
        terms.append(word)
    
    return terms


# MIT License
#
# Copyright (c) 2026 Muhammad Waqas
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


def fuse_scoped_rankings(workspace, authorized, lexical, semantic, *, limit=20, work_ids=None):
    """Fuse already-authorized locator keys; never construct or resolve quotes.

    Each key is (workspace, edition, revision, passage). The trusted caller
    must authorize active source spans in one storage snapshot before fusion
    and re-resolve selected keys from that same snapshot. A missing semantic
    backend is represented by an empty ranking, not synthetic vectors.
    Optional trusted work_ids diversify the entire fused ranking before the
    result cap: one per work, then one per unused edition, then repeats.
    """
    from ._vendor.rifflux.rrf import rrf_fuse

    if (not isinstance(workspace, str) or not workspace or len(workspace) > 256
            or '\x00' in workspace or type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError('explicit workspace and bounded fusion limit required')

    def validated(keys):
        if not isinstance(keys, (tuple, list)) or len(keys) > 20000:
            raise ValueError('bounded locator sequence required')
        seen = set()
        for key in keys:
            if (not isinstance(key, tuple) or len(key) != 4
                    or any(not isinstance(v, str) or not v or len(v) > 256
                           or '\x00' in v for v in key) or key[0] != workspace):
                raise ValueError('invalid or foreign locator key')
            if key in seen:
                raise ValueError('duplicate locator key')
            seen.add(key)
        return seen

    allowed = validated(authorized)
    for ranking in (lexical, semantic):
        if not validated(ranking) <= allowed:
            raise ValueError('ranking contains unauthorized locator key')
    # Use opaque IDs for upstream RRF; stable tie-break is the full locator.
    keys = sorted(allowed)
    tokens = {key: str(i) for i, key in enumerate(keys)}
    scores = rrf_fuse({'lexical': [tokens[key] for key in lexical],
                       'semantic': [tokens[key] for key in semantic]})
    ranked = [(keys[int(token)], score) for token, score in scores.items()]
    ranked.sort(key=lambda pair: (-pair[1], pair[0]))
    if work_ids is None:
        return ranked[:limit]
    if (not isinstance(work_ids, dict) or set(work_ids) != allowed
            or any(not isinstance(work, str) or not work or len(work) > 256
                   or '\x00' in work for work in work_ids.values())):
        raise ValueError('invalid trusted work identities')
    first, deferred, second, rest = [], [], [], []
    works, editions = set(), set()
    for pair in ranked:
        key = pair[0]
        work = work_ids[key]
        if work in works:
            deferred.append(pair)
        else:
            works.add(work)
            editions.add(key[1])
            first.append(pair)
    for pair in deferred:
        edition = pair[0][1]
        if edition in editions:
            rest.append(pair)
        else:
            editions.add(edition)
            second.append(pair)
    return (first + second + rest)[:limit]


def search_hybrid(workspace, passages, query, *, semantic_ranker=None, limit=20,
                  timeout=5.0, cancelled=lambda: False):
    """Candidate hybrid handoff over one caller-authorized active snapshot.

    The optional local ranker receives (query, ((locator_key, exact_text), ...))
    and returns only relevance-filtered locator keys, not text or citations.
    It must use an independently verified offline model; no model is loaded
    here. Absence of a ranker preserves the original lexical API. Scores in
    hybrid mode are RRF ranks, not probabilities of relevance. The bounded
    synchronous ranker must cooperate with the caller's runtime budget.
    """
    if semantic_ranker is None:
        return search(workspace, passages, query, limit=limit, timeout=timeout,
                      cancelled=cancelled)
    if (not callable(semantic_ranker) or not isinstance(workspace, str)
            or not workspace or type(limit) is not int or not 1 <= limit <= 100
            or type(timeout) not in (int, float) or not 0 < timeout <= 60
            or not callable(cancelled)):
        raise ValueError('invalid hybrid search scope or budget')
    deadline = monotonic() + timeout
    latched = False

    def stopped():
        nonlocal latched
        latched = latched or cancelled() or monotonic() >= deadline
        return latched

    def check():
        if stopped():
            raise TimeoutError('hybrid search cancelled or timed out')

    check()
    if not _groups(query):
        return []
    refs = []
    size = 0
    for p in passages:
        check()
        if type(p) is not Passage or not isinstance(p.raw_page, str):
            raise ValueError('expected immutable Passage')
        size += len(p.raw_page)
        if len(refs) >= 500 or size > 10_000_000:
            raise ValueError('scope exceeds semantic candidate budget')
        refs.append(p)
    check()
    if not refs:
        return []
    remaining = min(timeout, deadline - monotonic())
    if remaining <= 0:
        raise TimeoutError('hybrid search cancelled or timed out')
    # Native BM25 also verifies every source anchor before the model sees text.
    lexical = search(workspace, refs, query, limit=100, timeout=remaining,
                     cancelled=stopped)
    check()
    by_key = {(p.workspace, p.edition_id, p.revision_id, p.passage_id): p for p in refs}
    keys = list(by_key)
    # Reuse the fusion boundary's validation before invoking the backend.
    lex_keys = [(workspace, h['edition_id'], h['revision_id'], h['passage_id']) for h in lexical]
    fuse_scoped_rankings(workspace, keys, lex_keys, [], limit=limit)
    check()
    semantic = semantic_ranker(query, tuple((key, p.raw_page[p.start:p.end])
                                            for key, p in by_key.items()))
    check()
    fused = fuse_scoped_rankings(workspace, keys, lex_keys, semantic, limit=limit,
                                  work_ids={key: p.work_id for key, p in by_key.items()})
    if not semantic:
        return lexical[:limit]
    by_hit = dict(zip(lex_keys, lexical))
    results = []
    for key, score in fused:
        check()
        p = by_key[key]
        hit = dict(by_hit.get(key, {}))
        hit.update(passage_id=p.passage_id, work_id=p.work_id,
                   edition_id=p.edition_id, revision_id=p.revision_id,
                   physical_index=p.physical_index, start=p.start, end=p.end,
                   page_sha256=p.page_sha256, raw_quote=p.raw_page[p.start:p.end],
                   score=score, matched_terms=hit.get('matched_terms', ()),
                   reason='hybrid RRF rank; not semantic verification')
        if p.physical_index is None:
            hit.update(locator_kind='logical_segment', segment_index=p.segment_index)
        results.append(hit)
    check()
    return results
