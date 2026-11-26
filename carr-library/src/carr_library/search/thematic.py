"""Offline thematic BM25 candidate. One scope per invocation; no network or models.

The extract_query_terms function below is from cairnsearch, MIT licensed,
commit eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93, search/snippets.py.
The original license is retained at the end of this module.
"""
from dataclasses import dataclass
from hashlib import sha256
import re
import sqlite3


@dataclass(frozen=True)
class Passage:
    workspace: str
    work_id: str
    edition_id: str
    revision_id: str
    passage_id: str
    physical_index: int
    start: int
    end: int
    raw_page: str
    page_sha256: str


# Conservative transliterations, not inferred identities or translations.
ALIASES = (("Herzen", "Gertsen"), ("Ogarev", "Ogaryov"),
           ("Bakunin", "Bakounine"), ("Belinsky", "Belinski"))


def _groups(query):
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("invalid thematic query")
    groups = []
    for term in extract_query_terms(query):
        # The upstream splitter is not a security boundary. Every token is quoted
        # independently below, never interpreted as FTS5 operators or columns.
        words = re.findall(r"[^\W_]+", term, re.UNICODE)
        if not words:
            continue
        phrase = " ".join(words)
        variants = next((g for g in ALIASES if phrase.casefold() in
                         {v.casefold() for v in g}), (phrase,))
        if variants not in groups:
            groups.append(variants)
    if len(groups) > 24:
        raise ValueError("too many query concepts")
    return groups


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def search(workspace, passages, query, limit=20):
    """Rank active, authorized source spans supplied by the caller.

    The caller must resolve active revisions and collection/topic memberships
    in storage before invoking this adapter. Mixed workspace input fails closed.
    Scores are scope-local; the temporary FTS5 table is discarded after use.
    Returns exact code-point spans, never transformed snippets or page labels.
    """
    if not isinstance(workspace, str) or not workspace or not 1 <= limit <= 100:
        raise ValueError("explicit workspace and bounded limit required")
    groups = _groups(query)
    if not groups:
        return []
    expressions = ["(" + " OR ".join(map(_quote, g)) + ")" for g in groups]
    match = " AND ".join(expressions)
    refs = []
    seen = set()
    size = 0
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE scoped USING fts5(text, tokenize='unicode61 remove_diacritics 0')")
        for p in passages:
            if p.workspace != workspace:
                raise ValueError("foreign workspace passage")
            if (not all((p.work_id, p.edition_id, p.revision_id, p.passage_id))
                    or p.physical_index < 1 or not 0 <= p.start < p.end <= len(p.raw_page)
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
            db.execute("INSERT INTO scoped(rowid,text) VALUES (?,?)", (len(refs), quote))
        # Native SQLite BM25: lower is better. All concepts must occur in the
        # same contiguous span, with OR only for controlled spelling variants.
        ranked = db.execute("SELECT rowid,bm25(scoped) FROM scoped WHERE scoped MATCH ? ORDER BY 2,rowid", (match,)).fetchall()
        ranked.sort(key=lambda r: (r[1], refs[r[0]-1].edition_id, refs[r[0]-1].revision_id, refs[r[0]-1].passage_id))
        hits, deferred, works = [], [], set()
        for rowid, rank in ranked:
            p = refs[rowid-1]
            if p.work_id in works:
                deferred.append((rowid, rank))
            else:
                works.add(p.work_id)
                hits.append((rowid, rank))
        result = []
        for rowid, rank in (hits + deferred)[:limit]:
            p = refs[rowid-1]
            matched = tuple(v for g in groups for v in g if db.execute(
                "SELECT 1 FROM scoped WHERE rowid=? AND scoped MATCH ?", (rowid, _quote(v))).fetchone())
            result.append({"passage_id": p.passage_id, "work_id": p.work_id,
                "edition_id": p.edition_id, "revision_id": p.revision_id,
                "physical_index": p.physical_index, "start": p.start, "end": p.end,
                "page_sha256": p.page_sha256, "raw_quote": p.raw_page[p.start:p.end],
                "score": -rank, "matched_terms": matched,
                "reason": "lexical BM25 with controlled variants; not semantic verification"})
        return result
    finally:
        db.close()


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
