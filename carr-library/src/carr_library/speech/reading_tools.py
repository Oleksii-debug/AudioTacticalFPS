"""Reviewed edits on the existing reading copy, using its isolated regex worker.

Snapshots and previews are in-memory UI transactions, never source authority or
another persisted document format. The ReadingDocument owns history and mapping.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import time

from ._io import check_cancelled, run_process, text_value
from .reading import ReadingDocument, PronunciationRule

MAX_MATCHES = 10000
MAX_PREVIEW_CHARS = 65536
WORKER_TIMEOUT = 3.0


@dataclass(frozen=True)
class ReadingSnapshot:
    text: str
    signature: tuple
    source_sha256: str

    @classmethod
    def capture(cls, document):
        if not isinstance(document, ReadingDocument):
            raise ValueError('Open a reading copy before preparing edits.')
        return cls(document.text, document._signature(), document.source_sha256)

    def check(self, document):
        if (not isinstance(document, ReadingDocument)
                or document._signature() != self.signature
                or document.source_sha256 != self.source_sha256
                or document.text != self.text):
            raise ValueError('The reading copy changed. Prepare a fresh preview before applying it.')


@dataclass(frozen=True)
class ReadingMatch:
    start: int
    end: int
    replacement: str = ''


@dataclass(frozen=True)
class ReadingPreview:
    snapshot: ReadingSnapshot
    start: int
    end: int
    replacement: str
    label: str
    count: int
    count_label: str = 'match(es)'

    @property
    def changed(self):
        return self.snapshot.text[self.start:self.end] != self.replacement

    @property
    def after_selection(self):
        return self.start, self.start + len(self.replacement)

    def description(self):
        before = self.snapshot.text[self.start:self.end]
        limit = MAX_PREVIEW_CHARS // 2
        clipped = len(before) > limit or len(self.replacement) > limit
        return (f'{self.count} {self.count_label}; reading offsets {self.start}–{self.end}.\n'
                'The original source, its checksum and quotation status stay unchanged.\n'
                + ('Display shortened; the complete reviewed operation remains bounded to this reading copy.\n'
                   if clipped else '')
                + '\nCURRENT READING FRAGMENT\n' + before[:limit]
                + '\n\nPROPOSED READING FRAGMENT\n' + self.replacement[:limit])

    def apply(self, document, *, cancelled=lambda: False):
        check_cancelled(cancelled)
        self.snapshot.check(document)
        # ReadingDocument performs all validation before mutating its history.
        # One reviewed operation is one undo, including a multi-match replace.
        return document.replace(self.start, self.end, self.replacement, label=self.label)


def _range(text, scope):
    if scope is None:
        return 0, len(text)
    if (not isinstance(scope, tuple) or len(scope) != 2
            or any(type(value) is not int for value in scope)
            or not 0 <= scope[0] < scope[1] <= len(text)):
        raise ValueError('Select a nonempty reading-copy range for this operation.')
    return scope


def _word(character):
    return bool(character) and (character.isalnum() or character == '_')


def find_matches(snapshot, pattern, replacement='', *, regex=False,
                 ignore_case=False, whole_word=False, scope=None,
                 cancelled=lambda: False, timeout=WORKER_TIMEOUT):
    """Return validated code-point offsets from the existing bounded child.

    Every user regex, including Find without replacement, runs off the UI thread
    in regex_worker.py. Literal replacements retain literal backslashes.
    """
    check_cancelled(cancelled)
    if not isinstance(snapshot, ReadingSnapshot):
        raise ValueError('Capture the reading copy before searching.')
    text_value(pattern, 'find text', maximum=1024, empty=True)
    text_value(replacement, 'replacement', maximum=4096, empty=True)
    if not pattern:
        raise ValueError('Enter text or a regular expression to find.')
    if any(type(flag) is not bool for flag in (regex, ignore_case, whole_word)):
        raise ValueError('Search options must be booleans.')
    if type(timeout) not in (int, float) or not 0 < timeout <= WORKER_TIMEOUT:
        raise ValueError('Search must use the bounded worker time limit.')
    start, end = _range(snapshot.text, scope)
    selected = snapshot.text[start:end]
    payload = json.dumps(dict(text=selected,
        pattern=pattern if regex else re.escape(pattern),
        replacement=replacement if regex else replacement.replace('\\', '\\\\'),
        ignore_case=ignore_case), ensure_ascii=False).encode('utf-8')
    reply, _, _ = run_process([sys.executable, '-I', '-B', '-X', 'utf8', '-S',
        str(Path(__file__).with_name('regex_worker.py'))], input_bytes=payload,
        timeout=timeout, cancelled=cancelled, max_output=1_000_000)
    check_cancelled(cancelled)
    try:
        value = json.loads(reply.decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('The bounded search worker returned an invalid reply.') from exc
    if (type(value) is not dict or set(value) != {'matches'}
            or type(value['matches']) is not list or len(value['matches']) > MAX_MATCHES):
        raise ValueError('The search reply exceeds its supported result schema.')
    result, previous, output_size = [], 0, len(snapshot.text)
    for row in value['matches']:
        check_cancelled(cancelled)
        if (type(row) is not list or len(row) != 3
                or type(row[0]) is not int or type(row[1]) is not int
                or not previous <= row[0] <= row[1] <= len(selected)
                or type(row[2]) is not str):
            raise ValueError('The search reply contains invalid or overlapping offsets.')
        a, b, spoken = start + row[0], start + row[1], row[2]
        text_value(spoken, 'expanded replacement', empty=True)
        previous = row[1]
        if whole_word and ((_word(snapshot.text[a - 1]) if a else False)
                           or (_word(snapshot.text[b]) if b < len(snapshot.text) else False)):
            continue
        output_size += len(spoken) - (b - a)
        if output_size > 2_000_000:
            raise ValueError('The replacement exceeds the reading-copy character budget.')
        result.append(ReadingMatch(a, b, spoken))
    check_cancelled(cancelled)
    return tuple(result)


def next_match(matches, selection, *, backwards=False, wrap=True, skip_current=True):
    if (not isinstance(selection, tuple) or len(selection) != 2
            or any(type(value) is not int or value < 0 for value in selection)
            or selection[0] > selection[1]
            or any(type(flag) is not bool for flag in (backwards, wrap, skip_current))):
        raise ValueError('Invalid search direction or reading selection.')
    candidates = reversed(matches) if backwards else iter(matches)
    for match in candidates:
        if backwards:
            if match.end <= selection[0] and (not skip_current or (match.start, match.end) != selection):
                return match
        elif match.start >= selection[1] and (not skip_current or (match.start, match.end) != selection):
            return match
    return (matches[-1] if backwards else matches[0]) if wrap and matches else None


def preview_matches(snapshot, matches, *, label='Find and replace reading copy'):
    matches = tuple(matches)
    if not matches or len(matches) > MAX_MATCHES or any(not isinstance(match, ReadingMatch) for match in matches):
        raise ValueError('No matching reading text was found.')
    parts, position = [], matches[0].start
    start = position
    size = len(snapshot.text)
    for match in matches:
        if (not isinstance(match, ReadingMatch) or type(match.start) is not int
                or type(match.end) is not int
                or not position <= match.start <= match.end <= len(snapshot.text)):
            raise ValueError('Choose non-overlapping matches from the current reading copy.')
        text_value(match.replacement, 'replacement', empty=True)
        size += len(match.replacement) - (match.end - match.start)
        if size > 2_000_000:
            raise ValueError('The replacement exceeds the reading-copy character budget.')
        parts.extend((snapshot.text[position:match.start], match.replacement))
        position = match.end
    replacement = ''.join(parts)
    # Reuse the existing document validator without changing the live copy.
    probe = ReadingDocument(snapshot.text)
    probe.replace(start, position, replacement, label=label)
    return ReadingPreview(snapshot, start, position, replacement, label, len(matches))


def preview_pronunciations(snapshot, rules, *, language='', voice_id='',
                           scope=None, cancelled=lambda: False):
    check_cancelled(cancelled)
    start, end = _range(snapshot.text, scope)
    rules = tuple(rules)
    if not rules or len(rules) > 1000 or any(not isinstance(rule, PronunciationRule) for rule in rules):
        raise ValueError('Add one to 1000 validated pronunciation rules.')
    deadline = time.monotonic() + 10

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('Pronunciation preview reached its total time budget. Use fewer rules or a smaller range.')
        return cancelled()

    copy = ReadingDocument(snapshot.text[start:end])
    copy.apply_pronunciations(rules, language=language, voice_id=voice_id,
                             cancelled=check, timeout=WORKER_TIMEOUT)
    check_cancelled(check)
    # Existing language/voice scope semantics are retained exactly. The full
    # operation becomes one reversible edit in the authenticated reading copy.
    applicable = sum(not rule.language or rule.language == language
                     for rule in rules if not rule.voice_id or rule.voice_id == voice_id)
    # The ordinary set_text implementation keeps its exact unchanged prefix and
    # suffix mapping; a dictionary preview must not turn the full chapter into
    # a rewritten span merely because its scope was the entire reading copy.
    result = snapshot.text[:start] + copy.text + snapshot.text[end:]
    probe = ReadingDocument(snapshot.text)
    if probe.set_text(result):
        edit = probe._edits[-1]
        start, end, replacement = edit['start'], edit['end'], edit['replacement']
    else:
        start = end = 0
        replacement = ''
    return ReadingPreview(snapshot, start, end, replacement,
                          'Reviewed pronunciation reading layer', applicable,
                          'applicable pronunciation rule(s)')
