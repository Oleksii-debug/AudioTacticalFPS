"""Bounded Boolean and proximity queries over exact, page-local source spans.

Only uppercase AND, OR and NOT are operators; adjacency means AND. Precedence
is NEAR/N, NOT, AND, OR. Double quotes delimit a phrase and doubled quotes inside
it represent one literal quote; backslashes have no special meaning. Matching
uses literal.py's case/whitespace folding and whole Unicode word boundaries.

NEAR/0 through NEAR/100 accepts two positive words or quoted phrases, in either
source order. Its witnesses must be distinct and nonoverlapping. Distance is
the number of intervening runs of Unicode letters, numbers, marks or connector
punctuation; ordinary punctuation and whitespace do not add words. NEAR cannot
be chained or applied directly to a Boolean group. Negating a NEAR expression
is allowed when the complete query still guarantees positive source evidence.

Every successful Boolean branch must have a positive witness: NOT-only queries
and unanchored OR branches are rejected. Evaluation yields one deterministic
set of source spans per page, never a concatenated or generated quotation.
"""
from dataclasses import dataclass
from heapq import heappop, heappush, merge
from itertools import chain
import unicodedata as ud

from .literal import MAX_QUERY, _fold, _spans, _word

MAX_TERMS = 32
MAX_DEPTH = 16
MAX_OCCURRENCES = 200_000  # Per page, across all evaluated proximity operands.


def _valid_text(text):
    return isinstance(text, str) and not any(
        ud.category(char) in ('Cc', 'Cs') and char not in '\t\n\r'
        for char in text
    )


def quote_atom(text):
    """Quote a literal phrase for this grammar, including operator-like text."""
    if not _valid_text(text) or not text.strip():
        raise ValueError('advanced search terms must be nonempty valid text')
    return '"' + text.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    position: int


@dataclass(frozen=True, slots=True)
class _Atom:
    needle: str
    label: str


@dataclass(frozen=True, slots=True)
class _Near:
    left: _Atom
    right: _Atom
    distance: int


@dataclass(frozen=True, slots=True)
class _And:
    left: object
    right: object


@dataclass(frozen=True, slots=True)
class _Or:
    left: object
    right: object


@dataclass(frozen=True, slots=True)
class _Not:
    child: object


def _tokens(query, check):
    tokens, i, terms = [], 0, 0
    while i < len(query):
        check()
        if query[i].isspace():
            i += 1
            continue
        start = i
        if query[i] in '()':
            tokens.append(_Token(query[i], query[i], start))
            i += 1
            continue
        if query[i] == '"':
            i += 1
            chars = []
            while i < len(query):
                if query[i] != '"':
                    chars.append(query[i])
                    i += 1
                elif i + 1 < len(query) and query[i + 1] == '"':
                    chars.append('"')
                    i += 2
                else:
                    i += 1
                    break
            else:
                raise ValueError(f'unclosed quoted phrase at position {start + 1}')
            text, kind = ''.join(chars), 'atom'
        else:
            while i < len(query) and not query[i].isspace() and query[i] not in '()"':
                i += 1
            text = query[start:i]
            kind = text if text in ('AND', 'OR', 'NOT') else 'atom'
            if text.startswith('NEAR/'):
                number = text[5:]
                if not number.isascii() or not number.isdecimal() or not 0 <= int(number) <= 100:
                    raise ValueError('NEAR distance must be an integer from 0 to 100')
                kind = 'near'
        if kind == 'atom':
            if not _fold(text).strip():
                raise ValueError(f'empty quoted phrase at position {start + 1}')
            terms += 1
            if terms > MAX_TERMS:
                raise ValueError(f'advanced search allows at most {MAX_TERMS} terms')
        tokens.append(_Token(kind, text, start))
    tokens.append(_Token('end', '', len(query)))
    return tokens


class _Parser:
    def __init__(self, tokens, check):
        self.tokens, self.check, self.position = tokens, check, 0

    def peek(self):
        return self.tokens[self.position]

    def take(self):
        token = self.peek()
        self.position += 1
        self.check()
        return token

    def depth(self, depth):
        self.check()
        if depth > MAX_DEPTH:
            raise ValueError(f'advanced search nesting exceeds {MAX_DEPTH}')

    def parse(self):
        node = self.disjunction(0)
        if self.peek().kind != 'end':
            raise ValueError(f'unexpected {self.peek().text!r} at position {self.peek().position + 1}')
        return node

    def disjunction(self, depth):
        node = self.conjunction(depth)
        while self.peek().kind == 'OR':
            self.take()
            node = _Or(node, self.conjunction(depth))
        return node

    def conjunction(self, depth):
        node = self.negation(depth)
        while self.peek().kind in ('AND', 'atom', '(', 'NOT'):
            if self.peek().kind == 'AND':
                self.take()
            node = _And(node, self.negation(depth))
        return node

    def negation(self, depth):
        self.depth(depth)
        if self.peek().kind == 'NOT':
            self.take()
            return _Not(self.negation(depth + 1))
        return self.proximity(depth)

    def proximity(self, depth):
        node = self.primary(depth)
        if self.peek().kind == 'near':
            token = self.take()
            right = self.primary(depth)
            if not isinstance(node, _Atom) or not isinstance(right, _Atom):
                raise ValueError('NEAR requires two positive words or quoted phrases')
            node = _Near(node, right, int(token.text[5:]))
            if self.peek().kind == 'near':
                raise ValueError('NEAR cannot be chained; combine separate NEAR clauses with AND')
        return node

    def primary(self, depth):
        self.depth(depth)
        token = self.peek()
        if token.kind == 'atom':
            self.take()
            return _Atom(_fold(token.text).strip(), ' '.join(token.text.split()))
        if token.kind == '(':
            self.take()
            node = self.disjunction(depth + 1)
            if self.peek().kind != ')':
                raise ValueError(f'unclosed parenthesis at position {token.position + 1}')
            self.take()
            return node
        raise ValueError(f'expected a word, quoted phrase or group at position {token.position + 1}')


def _normal(node, check, negative=False):
    """Push NOT to leaves without expanding a query into exponential branches."""
    check()
    if isinstance(node, _Not):
        return _normal(node.child, check, not negative)
    if isinstance(node, (_And, _Or)):
        kind = (_Or if isinstance(node, _And) else _And) if negative else type(node)
        return kind(_normal(node.left, check, negative), _normal(node.right, check, negative))
    return _Not(node) if negative else node


def _anchored(node):
    if isinstance(node, _Not):
        return False
    if isinstance(node, _And):
        return _anchored(node.left) or _anchored(node.right)
    if isinstance(node, _Or):
        return _anchored(node.left) and _anchored(node.right)
    return True


def _candidate(node):
    """An FTS overapproximation only; None means all scoped pages must be read."""
    if isinstance(node, _Atom):
        return quote_atom(node.needle) if len(node.needle) >= 3 else None
    if isinstance(node, _Not):
        return None
    left, right = _candidate(node.left), _candidate(node.right)
    if isinstance(node, _Or):
        return f'({left} OR {right})' if left is not None and right is not None else None
    # AND and NEAR can safely use either available necessary positive filter.
    if left is not None and right is not None:
        return f'({left} AND {right})'
    return left if left is not None else right


@dataclass(frozen=True, slots=True)
class AdvancedMatch:
    spans: tuple
    reason: str


@dataclass(frozen=True, slots=True)
class _Evidence:
    spans: tuple
    reasons: tuple


class _WordCounter:
    """Count word starts once while increasing source positions are visited."""
    def __init__(self, raw, check):
        self.raw, self.check = raw, check
        self.position = self.count = 0
        self.inside = False

    def through(self, position):
        while self.position < position:
            if self.position % 1024 == 0:
                self.check()
            word = _word(self.raw[self.position])
            self.count += bool(word and not self.inside)
            self.inside = word
            self.position += 1
        self.check()
        return self.count


class _Matcher:
    def __init__(self, raw, folded, starts, ends, check):
        self.raw, self.folded, self.starts, self.ends = raw, folded, starts, ends
        self.check = check
        self.firsts, self.near_matches = {}, {}
        self.occurrences = 0

    def spans(self, atom):
        for span in _spans(self.folded, self.starts, self.ends, atom.needle, self.check):
            self.occurrences += 1
            if self.occurrences > MAX_OCCURRENCES:
                raise ValueError('advanced search occurrence budget exceeded; narrow the query or scope')
            self.check()
            yield span

    def first(self, atom):
        if atom.needle not in self.firsts:
            self.firsts[atom.needle] = next(self.spans(atom), None)
        return self.firsts[atom.needle]

    def near(self, node):
        if node in self.near_matches:
            return self.near_matches[node]
        # An absent operand needs no word counting or scan of the other stream.
        if self.first(node.left) is None or self.first(node.right) is None:
            self.near_matches[node] = None
            return None
        streams = (
            ((start, end, 0) for start, end in self.spans(node.left)),
            ((start, end, 1) for start, end in self.spans(node.right)),
        )
        pending, latest = [], [None, None]
        words = _WordCounter(self.raw, self.check)
        for start, end, side in merge(*streams):
            self.check()
            # Ends may overlap later starts (especially for repeated phrases).
            # Retain the most recently ending eligible witness from each side.
            # A global end heap keeps the word counter strictly forward-only.
            while pending and pending[0][0] <= start:
                prior_end, prior_start, prior_side = heappop(pending)
                latest[prior_side] = ((prior_start, prior_end), words.through(prior_end))
            count = words.through(start)
            other = latest[1 - side]
            if other is not None and count - other[1] <= node.distance:
                result = (tuple(sorted((other[0], (start, end)))), count - other[1])
                self.near_matches[node] = result
                return result
            heappush(pending, (end, start, side))
        self.near_matches[node] = None
        return None

    def leaf(self, node):
        if isinstance(node, _Atom):
            span = self.first(node)
            if span is None:
                return None
            return _Evidence((span,), ('exact ' + quote_atom(node.label),))
        result = self.near(node)
        if result is None:
            return None
        spans, distance = result
        reason = (f'{quote_atom(node.left.label)} NEAR/{node.distance} '
                  f'{quote_atom(node.right.label)}: {distance} intervening words')
        return _Evidence(spans, (reason,))

    def evaluate(self, node):
        self.check()
        if isinstance(node, _Not):
            if self.leaf(node.child) is not None:
                return None
            child = node.child
            label = (quote_atom(child.label) if isinstance(child, _Atom) else
                     f'{quote_atom(child.left.label)} NEAR/{child.distance} {quote_atom(child.right.label)}')
            return _Evidence((), ('excluded ' + label,))
        if isinstance(node, (_And, _Or)):
            left = self.evaluate(node.left)
            if isinstance(node, _Or):
                return left if left is not None else self.evaluate(node.right)
            if left is None:
                return None
            right = self.evaluate(node.right)
            if right is None:
                return None
            return _Evidence(tuple(sorted(set(chain(left.spans, right.spans)))),
                             tuple(dict.fromkeys(chain(left.reasons, right.reasons))))
        return self.leaf(node)


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    query: str
    candidate: str | None
    _root: object

    def match(self, raw, folded, starts, ends, *, check=lambda: None):
        """Verify one page using the engine's original-text boundary mapping."""
        evidence = _Matcher(raw, folded, starts, ends, check).evaluate(self._root)
        check()
        if evidence is None:
            return None
        if not evidence.spans:
            raise ValueError('advanced search has no positive source witness')
        return AdvancedMatch(evidence.spans, 'Advanced search: ' + '; '.join(evidence.reasons))


def compile_query(query, *, check=lambda: None):
    """Validate and compile a query without storage, network or source access."""
    check()
    # Reject oversized paste/input before walking Unicode characters; this
    # validator is also used directly by the desktop query builder.
    if not isinstance(query, str) or len(query) > MAX_QUERY or not _valid_text(query):
        raise ValueError(f'advanced query must be valid text of at most {MAX_QUERY} characters')
    if not query.strip():
        raise ValueError('advanced query is empty')
    root = _normal(_Parser(_tokens(query, check), check).parse(), check)
    if not _anchored(root):
        raise ValueError('every advanced search OR branch requires a positive word, phrase or NEAR clause')
    candidate = _candidate(root)
    check()
    return CompiledQuery(query, candidate, root)
