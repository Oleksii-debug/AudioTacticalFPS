"""Versioned narration edits with explicit mappings to an immutable source.

This module prepares a reading copy. It has no access to the research database,
original files, quotation approval or evidence. Replacement strings are plain
text: no regex program, SSML, phoneme command or document instruction executes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from bisect import bisect_left, insort
from hashlib import sha256
import json
import re
from uuid import uuid4


MAX_SOURCE_CHARS = 2_000_000  # A chapter/reading segment, not a book/project cap.
MAX_EDITS = 5000
MAX_RULES = 1000
MAX_HISTORY = 30
_LANGUAGE = re.compile(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*\Z')


def _text(value, label, *, limit=MAX_SOURCE_CHARS, empty=False):
    if (not isinstance(value, str) or (not empty and not value)
            or len(value) > limit or '\x00' in value
            or any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)):
        raise ValueError(f'Invalid {label}; use plain Unicode text up to {limit} characters.')
    return value


def _optional(value, label):
    if value is not None:
        _text(value, label, limit=512)


@dataclass(frozen=True)
class NarrationEdit:
    id: str
    source_start: int
    source_end: int
    replacement: str
    reason: str = 'User narration edit'

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch('[0-9a-f]{32}', self.id):
            raise ValueError('Invalid narration edit identity.')
        if (type(self.source_start) is not int or type(self.source_end) is not int
                or not 0 <= self.source_start <= self.source_end <= MAX_SOURCE_CHARS):
            raise ValueError('Invalid original-text edit offsets.')
        _text(self.replacement, 'reading replacement', limit=32000, empty=True)
        _text(self.reason, 'edit reason', limit=512)


@dataclass(frozen=True)
class MappingSpan:
    derived_start: int
    derived_end: int
    source_start: int
    source_end: int
    kind: str  # unchanged, replacement, insertion, deletion
    edit_id: str | None = None


@dataclass(frozen=True)
class PronunciationRule:
    source: str
    replacement: str
    language: str | None = None
    voice_id: str | None = None
    project_id: str | None = None
    match_case: bool = True
    whole_word: bool = False

    def __post_init__(self):
        _text(self.source, 'dictionary source', limit=512)
        _text(self.replacement, 'dictionary replacement', limit=2048, empty=True)
        if self.language is not None and (
                not isinstance(self.language, str) or not _LANGUAGE.fullmatch(self.language)):
            raise ValueError('Invalid dictionary language code.')
        _optional(self.voice_id, 'dictionary voice identity')
        _optional(self.project_id, 'dictionary project identity')
        if type(self.match_case) is not bool or type(self.whole_word) is not bool:
            raise ValueError('Dictionary options must be true or false.')

    def applies(self, *, language=None, voice_id=None, project_id=None):
        return ((self.language is None or (isinstance(language, str) and
                 (self.language.casefold() == language.casefold() or (
                  '-' not in self.language and
                  self.language.casefold() == language.casefold().split('-')[0]))))
                and (self.voice_id is None or self.voice_id == voice_id)
                and (self.project_id is None or self.project_id == project_id))

    @property
    def specificity(self):
        return ((4 if self.project_id is not None else 0)
                + (2 if self.voice_id is not None else 0)
                + (1 if self.language is not None else 0))


def dictionary_to_json(rules):
    rules = tuple(rules)
    if len(rules) > MAX_RULES or any(type(rule) is not PronunciationRule for rule in rules):
        raise ValueError('Use at most 1000 validated pronunciation rules.')
    return json.dumps({'version': 1, 'kind': 'narration-dictionary',
                       'rules': [asdict(rule) for rule in rules]}, ensure_ascii=False, indent=2)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate narration document field.')
        result[key] = value
    return result


def dictionary_from_json(text):
    _text(text, 'pronunciation dictionary', limit=4 * 1024 * 1024)
    value = json.loads(text, object_pairs_hook=_unique)
    if (type(value) is not dict or set(value) != {'version', 'kind', 'rules'}
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'narration-dictionary' or type(value['rules']) is not list
            or len(value['rules']) > MAX_RULES):
        raise ValueError('Unsupported pronunciation dictionary.')
    fields = set(PronunciationRule.__dataclass_fields__)
    if any(type(row) is not dict or set(row) != fields for row in value['rules']):
        raise ValueError('Invalid pronunciation dictionary fields.')
    return tuple(PronunciationRule(**row) for row in value['rules'])


def _edits(edits, source_length):
    if not isinstance(edits, tuple) or len(edits) > MAX_EDITS:
        raise ValueError('Too many narration edits in one segment.')
    seen = set()
    end = -1
    previous = None
    for edit in edits:
        if type(edit) is not NarrationEdit or edit.source_end > source_length or edit.id in seen:
            raise ValueError('Invalid narration edit or source offset.')
        if edit.source_start < end or (previous is not None and
                edit.source_start == previous.source_start):
            raise ValueError('Narration edits overlap; resolve them in the reading copy.')
        seen.add(edit.id)
        end, previous = edit.source_end, edit
    if sum(len(edit.replacement) for edit in edits) + source_length > 2 * MAX_SOURCE_CHARS:
        raise ValueError('The narration replacement exceeds the segment text budget.')


@dataclass(frozen=True)
class NarrationDerivative:
    source_text: str
    source_sha256: str
    source_id: str | None = None
    revision: int = 0
    edits: tuple[NarrationEdit, ...] = ()
    undo_stack: tuple[tuple[NarrationEdit, ...], ...] = ()
    redo_stack: tuple[tuple[NarrationEdit, ...], ...] = ()

    def __post_init__(self):
        _text(self.source_text, 'original narration segment', empty=True)
        _optional(self.source_id, 'original source identity')
        if self.source_sha256 != sha256(self.source_text.encode('utf-8')).hexdigest():
            raise ValueError('The narration original-text checksum does not match.')
        if type(self.revision) is not int or not 0 <= self.revision <= 1_000_000:
            raise ValueError('Invalid narration revision.')
        _edits(self.edits, len(self.source_text))
        for stack in (self.undo_stack, self.redo_stack):
            if not isinstance(stack, tuple) or len(stack) > MAX_HISTORY:
                raise ValueError('Invalid narration undo history.')
            for snapshot in stack:
                _edits(snapshot, len(self.source_text))

    @classmethod
    def create(cls, source_text, source_id=None):
        _text(source_text, 'original narration segment', empty=True)
        return cls(source_text, sha256(source_text.encode('utf-8')).hexdigest(), source_id)

    def render(self):
        parts, spans = [], []
        source_cursor = derived_cursor = 0
        for edit in self.edits:
            if edit.source_start > source_cursor:
                raw = self.source_text[source_cursor:edit.source_start]
                parts.append(raw)
                spans.append(MappingSpan(derived_cursor, derived_cursor + len(raw), source_cursor,
                                         edit.source_start, 'unchanged'))
                derived_cursor += len(raw)
            kind = 'insertion' if edit.source_start == edit.source_end else (
                'deletion' if not edit.replacement else 'replacement')
            spans.append(MappingSpan(derived_cursor, derived_cursor + len(edit.replacement),
                                    edit.source_start, edit.source_end, kind, edit.id))
            parts.append(edit.replacement)
            derived_cursor += len(edit.replacement)
            source_cursor = edit.source_end
        if source_cursor < len(self.source_text):
            raw = self.source_text[source_cursor:]
            parts.append(raw)
            spans.append(MappingSpan(derived_cursor, derived_cursor + len(raw), source_cursor,
                                     len(self.source_text), 'unchanged'))
        return ''.join(parts), tuple(spans)

    @property
    def text(self):
        return self.render()[0]

    @property
    def mapping(self):
        return self.render()[1]

    def original_span(self, derived_start, derived_end):
        """Return original bounding offsets; modified words map to whole edits.

        This is a position aid, not authority to quote a modified reading copy.
        Insertions map to the zero-width original anchor.
        """
        text, mapping = self.render()
        if (type(derived_start) is not int or type(derived_end) is not int
                or not 0 <= derived_start <= derived_end <= len(text)):
            raise ValueError('Invalid reading-copy selection.')
        if derived_start == derived_end:
            candidates = [s for s in mapping if s.derived_start <= derived_start <= s.derived_end]
        else:
            candidates = [s for s in mapping if s.derived_start < derived_end and s.derived_end > derived_start]
        if not candidates:
            return len(self.source_text), len(self.source_text)
        starts, ends = [], []
        for span in candidates:
            if span.kind == 'unchanged':
                start = max(derived_start, span.derived_start) - span.derived_start + span.source_start
                end = min(derived_end, span.derived_end) - span.derived_start + span.source_start
            else:
                start, end = span.source_start, span.source_end
            starts.append(start)
            ends.append(end)
        return min(starts), max(ends)

    def _change(self, edits):
        edits = tuple(sorted(edits, key=lambda edit: (edit.source_start, edit.source_end)))
        _edits(edits, len(self.source_text))
        if edits == self.edits:
            return self
        return replace(self, revision=self.revision + 1, edits=edits,
                       undo_stack=(*self.undo_stack, self.edits)[-MAX_HISTORY:], redo_stack=())

    def apply(self, source_start, source_end, replacement, *, reason='User narration edit'):
        edit = NarrationEdit(uuid4().hex, source_start, source_end, replacement, reason)
        return self.apply_edits((edit,))

    def apply_edits(self, edits):
        return self._change((*self.edits, *tuple(edits)))

    def reject(self, edit_id):
        if edit_id not in {edit.id for edit in self.edits}:
            raise ValueError('The narration edit is not in this revision.')
        return self._change(tuple(edit for edit in self.edits if edit.id != edit_id))

    def undo(self):
        if not self.undo_stack:
            return self
        return replace(self, revision=self.revision + 1, edits=self.undo_stack[-1],
                       undo_stack=self.undo_stack[:-1],
                       redo_stack=(*self.redo_stack, self.edits)[-MAX_HISTORY:])

    def redo(self):
        if not self.redo_stack:
            return self
        return replace(self, revision=self.revision + 1, edits=self.redo_stack[-1],
                       redo_stack=self.redo_stack[:-1],
                       undo_stack=(*self.undo_stack, self.edits)[-MAX_HISTORY:])

    def preview_rules(self, rules, *, language=None, voice_id=None, project_id=None,
                      start=0, end=None, cancelled=None):
        """Return proposals on original offsets without accepting them.

        More specific project/voice/language rules win. Equal-priority rules
        keep dictionary order. Earlier accepted manual edits always win. All
        searches are escaped literals; regex characters are ordinary text.
        """
        rules = tuple(rules)
        if len(rules) > MAX_RULES or any(type(rule) is not PronunciationRule for rule in rules):
            raise ValueError('Use at most 1000 validated pronunciation rules.')
        end = len(self.source_text) if end is None else end
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(self.source_text):
            raise ValueError('Invalid dictionary application source range.')
        selected = [(order, rule) for order, rule in enumerate(rules)
                    if rule.applies(language=language, voice_id=voice_id, project_id=project_id)]
        selected.sort(key=lambda pair: (-pair[1].specificity, pair[0]))
        proposals = []
        occupied = sorted((edit.source_start, edit.source_end) for edit in self.edits)
        def check():
            if cancelled is not None:
                callback = cancelled.is_set if hasattr(cancelled, 'is_set') else cancelled
                if not callable(callback):
                    raise ValueError('Invalid narration cancellation callback.')
                if callback():
                    raise InterruptedError('Narration preview cancelled; no edits were accepted.')
        check()
        for _, rule in selected:
            check()
            pattern = re.escape(rule.source)
            if rule.whole_word:
                pattern = r'(?<!\w)' + pattern + r'(?!\w)'
            # Escaped literals and fixed single-character assertions have no
            # user-programmed repetition/backtracking or replacement expansion.
            expression = re.compile(pattern, 0 if rule.match_case else re.IGNORECASE)
            for match in expression.finditer(self.source_text, start, end):
                check()
                a, b = match.span()
                index = bisect_left(occupied, (a, -1))
                neighbours = occupied[max(0, index - 1):index + 1]
                if any(a < y and b > x or (x == y and a <= x < b) for x, y in neighbours):
                    continue
                if len(proposals) + len(self.edits) >= MAX_EDITS:
                    raise ValueError('Split the reading segment before applying more dictionary edits.')
                proposals.append(NarrationEdit(uuid4().hex, a, b, rule.replacement,
                                               'Accepted literal pronunciation dictionary'))
                insort(occupied, (a, b))
        return tuple(sorted(proposals, key=lambda edit: edit.source_start))

    def apply_rules(self, rules, **scope):
        return self.apply_edits(self.preview_rules(rules, **scope))

    def diff(self):
        return tuple({'id': edit.id, 'source_start': edit.source_start, 'source_end': edit.source_end,
                      'original': self.source_text[edit.source_start:edit.source_end],
                      'reading': edit.replacement, 'reason': edit.reason} for edit in self.edits)

    def to_dict(self):
        return {'version': 1, 'kind': 'narration-derivative', 'source_text': self.source_text,
                'source_sha256': self.source_sha256, 'source_id': self.source_id,
                'revision': self.revision, 'edits': [asdict(edit) for edit in self.edits],
                'undo_stack': [[asdict(edit) for edit in snapshot] for snapshot in self.undo_stack],
                'redo_stack': [[asdict(edit) for edit in snapshot] for snapshot in self.redo_stack]}

    @classmethod
    def from_dict(cls, value):
        fields = {'version', 'kind', 'source_text', 'source_sha256', 'source_id', 'revision',
                  'edits', 'undo_stack', 'redo_stack'}
        if (type(value) is not dict or set(value) != fields or type(value['version']) is not int
                or value['version'] != 1 or value['kind'] != 'narration-derivative'):
            raise ValueError('Unsupported narration derivative.')
        def decode_edits(rows):
            if type(rows) is not list or len(rows) > MAX_EDITS:
                raise ValueError('Invalid narration edits.')
            expected = set(NarrationEdit.__dataclass_fields__)
            if any(type(row) is not dict or set(row) != expected for row in rows):
                raise ValueError('Invalid narration edit fields.')
            return tuple(NarrationEdit(**row) for row in rows)
        def decode_stack(rows):
            if type(rows) is not list or len(rows) > MAX_HISTORY:
                raise ValueError('Invalid narration history.')
            return tuple(decode_edits(snapshot) for snapshot in rows)
        return cls(value['source_text'], value['source_sha256'], value['source_id'], value['revision'],
                   decode_edits(value['edits']), decode_stack(value['undo_stack']),
                   decode_stack(value['redo_stack']))
