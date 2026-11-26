"""Choose source-exact display spans without changing retrieval or pagination.

The existing literal adapter supplies Unicode/casefold offset mapping. This
module only selects a contiguous slice of the same already-authorized page.
Quotes, saved evidence and clipboard operations must use the returned offsets,
never a separately shortened display string.
"""
from heapq import merge
from hashlib import sha256
import re
import sqlite3
from unicodedata import category, normalize

from .literal import _CompactOffsets, _mapped
from .thematic import _groups, _quote


SHORT_CHARS = 360
THEMATIC_CHARS = 1600
_BREAK = re.compile(r'''([.!?。！？]["'”’»)]*)(?:\s+|$)|\n[ \t]*\n''')


def _cluster_start(raw, position):
    while position > 0 and position < len(raw) and (
            category(raw[position]).startswith('M') or raw[position] == '\u200d'
            or raw[position - 1] == '\u200d'
            or raw[position - 1:position + 1] == '\r\n'):
        position -= 1
    return position


def _cluster_end(raw, position):
    while 0 < position < len(raw) and (
            category(raw[position]).startswith('M') or raw[position] == '\u200d'
            or raw[position - 1] == '\u200d'
            or raw[position - 1:position + 1] == '\r\n'):
        position += 1
    return position


def _reverse_offsets(offsets, length, check):
    """Invert cluster boundaries without recreating long-source dictionaries."""
    if not isinstance(offsets, _CompactOffsets):
        return {original: folded for folded, original in offsets.items()}
    result = _CompactOffsets(length)
    for index, (folded, original) in enumerate(offsets.items()):
        if index % 1024 == 0:
            check()
        result[original] = folded
    check()
    return result


def _lexical_focus(raw, start, end, query, budget, check):
    """Smallest observed source interval covering query concepts, when possible.

    Alias groups come from the existing thematic query adapter. Stream matching
    offsets rather than accumulating every occurrence in a long source page.
    The result is a display focus, not a replacement ranking algorithm.
    """
    source = raw[start:end]
    normalized = normalize('NFC', source)
    # ASCII is unchanged by NFC. Native highlight offsets already address
    # the original slice; do not invert the literal engine's sparse maps.
    ascii_source = source.isascii()
    if not ascii_source:
        folded, raw_starts, raw_ends = _mapped(source, check)
        normalized_folded, normalized_starts, normalized_ends = _mapped(normalized, check)
        if folded != normalized_folded:
            raise ValueError('Unsupported source normalization for excerpts.')
        normalized_starts = _reverse_offsets(normalized_starts, len(normalized), check)
        normalized_ends = _reverse_offsets(normalized_ends, len(normalized), check)
    groups = _groups(query)
    marker = '\x1eCARR-' + sha256(normalized.encode('utf-8')).hexdigest()
    while marker in normalized:
        check()
        marker += '-'
    opening, closing = marker + '-OPEN\x1f', marker + '-CLOSE\x1f'
    index = sqlite3.connect(':memory:')
    try:
        # Use the ranker's exact NFC/unicode61/phrase semantics. Native highlight
        # yields actual phrase occurrences, preserving order, adjacency and
        # repeated tokens; an unordered bag of query words is not sufficient.
        index.execute('''CREATE VIRTUAL TABLE source USING fts5(text,
            tokenize="unicode61 remove_diacritics 0 categories 'L* N* Co M*'")''')
        index.execute('INSERT INTO source(rowid,text) VALUES(1,?)', (normalized,))
        check()

        def positions(variant, group):
            check()
            row = index.execute('SELECT highlight(source,0,?,?) FROM source WHERE source MATCH ?',
                                (opening, closing, _quote(variant))).fetchone()
            check()
            if row is None:
                return
            highlighted = row[0]
            if highlighted.replace(opening, '').replace(closing, '') != normalized:
                raise ValueError('Native excerpt highlighting changed the source.')
            cursor = offset = 0
            while (left := highlighted.find(opening, cursor)) != -1:
                check()
                a = offset + left - cursor
                text_start = left + len(opening)
                right = highlighted.find(closing, text_start)
                if right < 0:
                    raise ValueError('Incomplete native excerpt highlight.')
                b = a + right - text_start
                if ascii_source:
                    source_start, source_end = start + a, start + b
                else:
                    try:
                        source_start = raw_starts[normalized_starts[a]] + start
                        source_end = raw_ends[normalized_ends[b]] + start
                    except KeyError as exc:
                        raise ValueError('Native phrase boundary splits a source character cluster.') from exc
                yield source_start, source_end, group
                cursor, offset = right + len(closing), b

        streams = [positions(variant, group) for group, variants in enumerate(groups)
                   for variant in variants]
        latest, best, best_single, first = {}, None, None, None
        for a, b, group in merge(*streams):
            check()
            if first is None:
                first = (a, b)
            latest[group] = (a, b)
            if len(latest) == len(groups):
                left = min(value[0] for value in latest.values())
                right = max(value[1] for value in latest.values())
                candidate = (right - left, left, right)
                if best is None or candidate < best:
                    best = candidate
                    best_single = min(latest.values())
        check()
        if best is not None:
            # An entire native phrase remains intact even when its punctuation
            # makes it longer than the preferred excerpt size. Distant concepts
            # are not joined into a fabricated short quotation.
            return (best[1], best[2]) if best[0] <= budget else best_single
        return first
    finally:
        index.close()


def _window(raw, start, end, budget, *, sentence):
    # Never truncate an exact literal match or a token-bounded semantic window.
    # A short semantic result can therefore exceed the preferred character size.
    budget = max(budget, end - start)
    before = (budget - (end - start)) // 2
    left = max(0, start - before)
    right = min(len(raw), left + budget)
    left = max(0, min(left, right - budget))

    if sentence:
        preceding = list(_BREAK.finditer(raw, left, start))
        following = _BREAK.search(raw, end, right)
        sentence_left = preceding[-1].end() if preceding else (0 if left == 0 else None)
        sentence_right = (following.end(1) if following and following.group(1)
                          else following.start() if following else
                          len(raw) if right == len(raw) else None)
        if (sentence_left is not None and sentence_right is not None
                and sentence_left <= start < end <= sentence_right):
            return _cluster_start(raw, sentence_left), _cluster_end(raw, sentence_right)

    # Keep words at the outer boundaries when space permits, without losing the
    # required source span. All whitespace inside the final slice stays intact.
    if left and not raw[left - 1].isspace():
        boundary = re.search(r'\s+', raw[left:start])
        if boundary:
            left += boundary.end()
    if right < len(raw) and not raw[right].isspace():
        boundaries = list(re.finditer(r'\s+', raw[end:right]))
        if boundaries:
            right = end + boundaries[-1].start()
    return _cluster_start(raw, left), _cluster_end(raw, right)


def presentation_span(raw, hit, query, result_mode, *, check=lambda: None):
    """Return immutable page offsets for explicit short/thematic presentation.

    Existing literal spans and source windows are reused. Lexical thematic
    results may initially cover a full page, so focus them around real query
    occurrences instead of silently showing the page's first few characters.
    No span crosses a page/segment or joins non-adjacent passages.
    """
    if result_mode not in ('short', 'thematic'):
        raise ValueError('Choose Short quotations or Thematic excerpts.')
    start, end = hit['start'], hit['end']
    if (not isinstance(raw, str) or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(raw) or raw[start:end] != hit['raw_quote']):
        raise ValueError('Search quotation differs from the original source.')
    check()
    budget = SHORT_CHARS if result_mode == 'short' else THEMATIC_CHARS
    spans = hit.get('match_spans')
    if spans:
        if any(type(a) is not int or type(b) is not int or not 0 <= a < b <= len(raw)
               for a, b in spans):
            raise ValueError('Invalid query match offsets.')
        first, last = min(a for a, _ in spans), max(b for _, b in spans)
        # All-words mode promises a matching page, not a fabricated combined
        # quote. Widely separated occurrences stay separate; More shows the page.
        if last - first <= budget:
            start, end = first, last
    elif hit.get('matched_terms'):
        focus = _lexical_focus(raw, start, end, query, budget, check)
        if focus is not None:
            start, end = focus
    check()
    result = _window(raw, start, end, budget, sentence=result_mode == 'short')
    check()
    return result
