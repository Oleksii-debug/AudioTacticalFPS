"""Bounded, presentation-only pacing over exact retained source spans.

A "word" here is one whitespace-delimited source token, not a linguistic claim.
Slow GUI dispatch never skips unseen groups or attempts to catch up in a burst.
"""
from dataclasses import dataclass
from hashlib import sha256
import math
import re


MAX_TEXT_CHARACTERS = 200_000
MAX_TOKENS = 50_000


@dataclass(frozen=True)
class PaceOptions:
    words_per_minute: int = 240
    words_per_group: int = 1

    def __post_init__(self):
        if type(self.words_per_minute) is not int or not 40 <= self.words_per_minute <= 1200:
            raise ValueError('Choose a pacing rate between 40 and 1200 words per minute.')
        if type(self.words_per_group) is not int or not 1 <= self.words_per_group <= 5:
            raise ValueError('Choose one to five source words per displayed group.')


@dataclass(frozen=True)
class PaceFrame:
    index: int
    total: int
    start: int
    end: int
    text: str
    words: int


class PacedText:
    """One explicit source range; it has no file, audio or dispatch side effect."""
    def __init__(self, text, start, end, options=PaceOptions()):
        if (type(text) is not str or not 0 < len(text) <= MAX_TEXT_CHARACTERS
                or '\0' in text):
            raise ValueError('Choose a current text section of at most 200,000 characters.')
        try:
            encoded = text.encode('utf-8')
        except UnicodeError as exc:
            raise ValueError('Paced reading requires valid Unicode source text.') from exc
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or type(options) is not PaceOptions):
            raise ValueError('Choose a nonempty exact source range and reviewed pacing settings.')
        tokens = []
        for match in re.finditer(r'\S+', text[start:end]):
            if len(tokens) >= MAX_TOKENS:
                raise ValueError('Choose a smaller range with at most 50,000 source words.')
            tokens.append((start + match.start(), start + match.end()))
        if not tokens:
            raise ValueError('The selected source range contains no visible words.')
        self.text, self.start, self.end, self.options = text, start, end, options
        self.source_sha256 = sha256(encoded).hexdigest()
        self.groups = tuple((tokens[index][0], tokens[min(index + options.words_per_group, len(tokens)) - 1][1],
                             min(options.words_per_group, len(tokens) - index))
                            for index in range(0, len(tokens), options.words_per_group))
        self.index, self.state, self.deadline = None, 'ready', None

    @staticmethod
    def _time(now):
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now < 10**12:
            raise ValueError('A finite monotonic presentation time is required.')
        return float(now)

    @property
    def current(self):
        if self.index is None:
            return None
        start, end, words = self.groups[self.index]
        return PaceFrame(self.index, len(self.groups), start, end, self.text[start:end], words)

    def _duration(self):
        return 60 * self.current.words / self.options.words_per_minute

    def play(self, now):
        now = self._time(now)
        if self.index is None or self.state == 'complete':
            self.index = 0
        self.state = 'playing'
        self.deadline = now + self._duration()
        return self.current

    def pause(self):
        if self.state == 'playing':
            self.state, self.deadline = 'paused', None

    def stop(self):
        self.index, self.state, self.deadline = None, 'ready', None

    def step(self, direction):
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError('Choose the previous or next source group.')
        if self.index is None:
            self.index = 0
        else:
            self.index = min(max(0, self.index + direction), len(self.groups) - 1)
        self.state, self.deadline = 'paused', None
        return self.current

    def tick(self, now):
        now = self._time(now)
        if self.state != 'playing' or now < self.deadline:
            return None
        if self.index + 1 == len(self.groups):
            self.state, self.deadline = 'complete', None
            return None
        self.index += 1
        # The newly shown group gets its full chosen duration even after a slow
        # timer. No source group disappears merely because the GUI was busy.
        self.deadline = now + self._duration()
        return self.current
