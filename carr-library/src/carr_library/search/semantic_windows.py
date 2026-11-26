"""Candidate MiniLM source windows using the existing tokenizers encode API.

A separately loaded, approved local tokenizer must have padding and truncation
disabled. Only the prefixless 128-token MiniLM contract is supported here.
"""
from dataclasses import replace
from hashlib import sha256
from time import monotonic
from unicodedata import category

TOKEN_LIMIT = 128
MAX_WINDOWS = 500


def _encoding(tokenizer, text):
    if tokenizer.truncation is not None or tokenizer.padding is not None:
        raise ValueError('untruncated, unpadded tokenizer required')
    enc = tokenizer.encode(text, add_special_tokens=True)
    if (tokenizer.truncation is not None or tokenizer.padding is not None
            or enc.overflowing or not len(enc.ids) == len(enc.offsets) == len(enc.special_tokens_mask)):
        raise ValueError('invalid untruncated encoding')
    starts = []
    for (a, b), special in zip(enc.offsets, enc.special_tokens_mask):
        if (type(a) is not int or type(b) is not int or not 0 <= a <= b <= len(text)
                or special not in (0, 1)):
            raise ValueError('invalid source character offsets')
        if not special and a < b:
            if starts and a < starts[-1]:
                raise ValueError('nonmonotonic source offsets')
            starts.append(a)
    return len(enc.ids), starts


def source_windows(tokenizer, raw, *, max_windows=MAX_WINDOWS, check=lambda: None):
    """Return a complete nonoverlapping partition of exact Unicode codepoints.

    Native tokenizer offsets propose cuts; every final raw slice is independently
    re-encoded with special tokens. Unrepresentable budgets fail explicitly.
    """
    if (not isinstance(raw, str) or not raw or len(raw) > 200_000
            or '\x00' in raw or any(category(c) == 'Cs' for c in raw)
            or type(max_windows) is not int or not 1 <= max_windows <= MAX_WINDOWS):
        raise ValueError('invalid source window budget')
    pending = [(0, len(raw))]
    result = []
    while pending:
        check()
        a, b = pending.pop()
        count, starts = _encoding(tokenizer, raw[a:b])
        check()
        if count <= TOKEN_LIMIT:
            result.append((a, b))
            continue
        # Use existing tokenizer offsets, not a new word/token segmenter.
        cuts = [i for i in set(starts) if 0 < i < b-a
                and not category(raw[a+i]).startswith('M')
                and raw[a+i] != '\u200d' and raw[a+i-1] != '\u200d']
        if not cuts or len(result) + len(pending) + 2 > max_windows:
            raise ValueError('source cannot fit semantic window budget')
        cut = a + min(cuts, key=lambda i: (abs(2*i-(b-a)), i))
        pending.extend(((cut, b), (a, cut)))
    return tuple(result)


def search_windowed(workspace, passages, query, *, tokenizer=None,
                    semantic_ranker=None, limit=20, timeout=5.0,
                    cancelled=lambda: False):
    """Opt-in bounded candidate; existing BM25/RRF performs the actual ranking.

    The caller owns snapshot authorization and model approval. This adapter
    accepts no arbitrary model prefixes. On budget errors the caller must show
    an explicit degraded status before retrying ordinary lexical search.
    """
    from .thematic import Passage, _groups, search_hybrid
    if semantic_ranker is None:
        return search_hybrid(workspace, passages, query, limit=limit,
                             timeout=timeout, cancelled=cancelled)
    if (tokenizer is None or not callable(semantic_ranker) or not callable(cancelled)
            or not isinstance(workspace, str) or not workspace
            or type(limit) is not int or not 1 <= limit <= 100
            or type(timeout) not in (int, float) or not 0 < timeout <= 60):
        raise ValueError('invalid semantic window scope or budget')
    deadline = monotonic() + timeout
    latched = False

    def stopped():
        nonlocal latched
        latched = latched or cancelled() or monotonic() >= deadline
        return latched

    def check():
        if stopped():
            raise TimeoutError('semantic windows cancelled or timed out')

    check()
    if not _groups(query):
        return []
    refs, seen = [], set()
    size = 0
    # Validate every input before sending any source text to the tokenizer.
    for p in passages:
        check()
        if (type(p) is not Passage or p.workspace != workspace
                or any(not isinstance(v, str) or not v or len(v) > 256 or '\x00' in v
                       for v in (p.workspace, p.work_id, p.edition_id, p.revision_id, p.passage_id))
                or not isinstance(p.raw_page, str) or '\x00' in p.raw_page
                or type(p.start) is not int or type(p.end) is not int
                or not 0 <= p.start < p.end <= len(p.raw_page)
                or not ((type(p.physical_index) is int and p.physical_index >= 1 and p.segment_index is None)
                        or (p.physical_index is None and type(p.segment_index) is int and p.segment_index >= 1))
                or sha256(p.raw_page.encode('utf-8')).hexdigest() != p.page_sha256):
            raise ValueError('unverified or foreign passage')
        key = (p.edition_id, p.revision_id, p.passage_id)
        size += len(p.raw_page)
        if key in seen or len(refs) >= MAX_WINDOWS or size > 10_000_000:
            raise ValueError('duplicate or excessive source scope')
        seen.add(key)
        refs.append(p)
    check()
    if not refs:
        return []
    if _encoding(tokenizer, query)[0] > TOKEN_LIMIT:
        raise ValueError('query exceeds model token budget; lexical fallback required')
    windows, parents = [], {}
    total = 0
    for p in refs:
        check()
        if len(windows) >= MAX_WINDOWS:
            raise ValueError('scope exceeds semantic window budget')
        for a, b in source_windows(tokenizer, p.raw_page[p.start:p.end],
                                   max_windows=MAX_WINDOWS-len(windows), check=check):
            a, b = a+p.start, b+p.start
            identity = 'window-v1:' + sha256(repr((p.workspace, p.edition_id,
                p.revision_id, p.passage_id, p.page_sha256, a, b)).encode()).hexdigest()
            if identity in parents:
                raise ValueError('duplicate semantic window identity')
            total += len(p.raw_page)
            if total > 10_000_000:
                raise ValueError('expanded scope exceeds hybrid budget')
            parents[identity] = (p.edition_id, p.revision_id, p.passage_id)
            windows.append(replace(p, passage_id=identity, start=a, end=b))
    check()
    # One bounded ranking call; no new retrieval engine or generated quotations.
    remaining = deadline-monotonic()
    if remaining <= 0:
        raise TimeoutError('semantic windows cancelled or timed out')
    hits = search_hybrid(workspace, windows, query, semantic_ranker=semantic_ranker,
                         limit=100, timeout=remaining, cancelled=stopped)
    result, returned = [], set()
    for hit in hits:
        check()
        parent = parents[hit['passage_id']]
        if parent in returned:
            continue
        returned.add(parent)
        result.append(dict(hit, parent_passage_id=parent[2]))
        if len(result) == limit:
            break
    check()
    return result
