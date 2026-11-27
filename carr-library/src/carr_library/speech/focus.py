"""Passive visual pacing and explicitly approximate measured-audio following."""
from bisect import bisect_right
from dataclasses import dataclass
from hashlib import sha256
import math
from pathlib import Path

import unicodedata

from .reading import ReadingDocument


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Reading-focus preparation cancelled.')


def _word_character(value):
    return value.isalnum() or value == '_' or unicodedata.category(value).startswith('M')


def _word_ranges(text, cancelled):
    # Reused PR152 Unicode boundary rules; deterministic, not language analysis.
    cursor, next_check = 0, 0
    while cursor < len(text):
        if cursor >= next_check:
            _cancel(cancelled)
            next_check = cursor + 4096
        if not _word_character(text[cursor]):
            cursor += 1
            continue
        first = cursor
        cursor += 1
        while cursor < len(text):
            if cursor >= next_check:
                _cancel(cancelled)
                next_check = cursor + 4096
            if _word_character(text[cursor]):
                cursor += 1
            elif (text[cursor] in "'\u2019\u02bc-\u2010\u2011" and cursor + 1 < len(text)
                  and _word_character(text[cursor + 1])):
                cursor += 2
            else:
                break
        yield first, cursor


MAX_FOCUS_UNITS = 100_000


def _number(value, label, minimum=0, maximum=None):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < minimum or maximum is not None and value > maximum):
        raise ValueError(f'Invalid {label}.')
    return float(value)


class FocusPlan:
    """Bounded word navigation over one exact existing reading-copy revision."""
    def __init__(self, document, *, cancelled=lambda: False):
        if not isinstance(document, ReadingDocument):
            raise ValueError('Use the current validated reading copy.')
        _cancel(cancelled)
        self.document, self.signature, self.text = document, document._signature(), document.text
        spans = []
        for span in _word_ranges(self.text, cancelled):
            if len(spans) >= MAX_FOCUS_UNITS:
                raise ValueError('Open one chapter with at most 100000 word units for paced reading.')
            spans.append(span)
        if not spans:
            raise ValueError('This reading copy has no word units for visual pacing.')
        _cancel(cancelled)
        self.check(document)
        self.spans, self.starts = tuple(spans), tuple(start for start, _ in spans)

    def check(self, document):
        if document is not self.document or document._signature() != self.signature:
            raise ValueError('The reading text or tab changed. Reopen reading focus.')

    def index_at(self, cursor):
        if type(cursor) is not int or not 0 <= cursor <= len(self.text):
            raise ValueError('Choose a cursor within this reading copy.')
        index = max(0, bisect_right(self.starts, cursor) - 1)
        if self.spans[index][1] <= cursor and index + 1 < len(self.spans):
            index += 1
        return index

    def source_label(self, span):
        source = self.document.source_span(*span)
        if source.start is None:
            return 'No original-source position is available for this inserted text.'
        return ('Original-source characters ' + str(source.start + 1) + '–' + str(source.end)
                + ('; exact text mapping.' if source.exact else '; approximate text mapping.'))


@dataclass(frozen=True)
class ReadingAudio:
    document: object
    signature: tuple
    start: int
    end: int
    path: Path
    frames: int
    sample_rate: int

    @classmethod
    def from_render(cls, document, signature, start, end, text, result):
        document._range(start, end)
        if (start == end or document._signature() != signature
                or document.text[start:end] != text
                or result.text_sha256 != sha256(text.encode('utf-8')).hexdigest()
                or type(result.frames) is not int or result.frames <= 0
                or type(result.sample_rate) is not int or result.sample_rate <= 0):
            raise ValueError('Rendered audio no longer matches the exact reading-text snapshot.')
        return cls(document, signature, start, end, Path(result.path), result.frames, result.sample_rate)

    @property
    def duration_seconds(self):
        return self.frames / self.sample_rate

    def check(self, plan, path):
        plan.check(self.document)
        if self.signature != plan.signature or path is None or Path(path) != self.path:
            raise ValueError('The current audio or reading text changed. Render and review it again.')

    def span_at(self, plan, path, seconds):
        self.check(plan, path)
        seconds = _number(seconds, 'audio-follow position')
        fraction = min(seconds / self.duration_seconds, 1)
        cursor = min(self.end - 1, self.start + int(fraction * (self.end - self.start)))
        index = plan.index_at(cursor)
        first, last = plan.spans[index]
        first, last = max(self.start, first), min(self.end, last)
        # A selection can start/end inside a word or contain only punctuation.
        # Keep every displayed range inside the actual rendered text.
        if first >= last:
            first, last = cursor, min(cursor + 1, self.end)
        return index, (first, last)


class VisualPacer:
    """No audio engine, saved timer, background executor or autonomous start."""
    def __init__(self, plan, cursor=0):
        self.plan, self.index, self.running = plan, plan.index_at(cursor), False
        self._started, self._base, self.words_per_minute = 0.0, self.index, 250.0

    def start(self, now, words_per_minute):
        now = _number(now, 'visual reading clock')
        speed = _number(words_per_minute, 'visual reading speed', 40, 1000)
        self._started, self._base, self.words_per_minute = now, self.index, speed
        self.running = True

    def tick(self, now):
        now = _number(now, 'visual reading clock')
        if self.running:
            if now < self._started:
                raise ValueError('The visual reading clock moved backwards.')
            position = self._base + int((now - self._started) * self.words_per_minute / 60)
            self.index = min(position, len(self.plan.spans) - 1)
            if position >= len(self.plan.spans):
                self.running = False
        return self.index

    def pause(self, now):
        self.tick(now)
        self.running = False

    def move(self, direction):
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError('Choose the previous or next word.')
        self.running = False
        self.index = min(max(0, self.index + direction), len(self.plan.spans) - 1)
        return self.index
