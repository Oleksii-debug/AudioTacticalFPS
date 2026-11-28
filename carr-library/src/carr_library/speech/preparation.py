"""Explicit deterministic speech preparation for the existing reading derivative.

This finite fallback names digits, unambiguous ISO date fields and selected
symbols. It does not infer pronunciation, stress, grammar or punctuation, choose
a voice, download a model, or change authenticated source text.
"""
from dataclasses import dataclass
from datetime import date
import math
import re
import time

from ._io import check_cancelled
from .reading import ReadingDocument
from .reading_tools import ReadingPreview, ReadingSnapshot

POLICY = 'reviewed-reading-preparation-v1'
LANGUAGES = (('en', 'English'), ('uk', 'Українська'), ('sk', 'Slovenčina'))
MAX_INPUT_CHARACTERS = 200_000
MAX_OUTPUT_CHARACTERS = 400_000
MAX_TRANSFORMATIONS = 10_000
DEFAULT_TIMEOUT = 3.0

_DIGITS = {
    'en': ('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine'),
    'uk': ('нуль', 'один', 'два', 'три', 'чотири', 'п’ять', 'шість', 'сім', 'вісім', 'дев’ять'),
    'sk': ('nula', 'jeden', 'dva', 'tri', 'štyri', 'päť', 'šesť', 'sedem', 'osem', 'deväť'),
}
_DATE_FIELDS = {
    'en': ('year', 'month', 'day'),
    'uk': ('рік', 'місяць', 'день'),
    'sk': ('rok', 'mesiac', 'deň'),
}
_SYMBOLS = {
    'en': {'+': 'plus', '−': 'minus', '×': 'times', '÷': 'divided by',
           '=': 'equals', '≠': 'not equal to', '≤': 'less than or equal to',
           '≥': 'greater than or equal to', '%': 'percent sign', '°': 'degree sign',
           '€': 'euro sign', '£': 'pound sign', '$': 'dollar sign', '&': 'ampersand',
           '@': 'at sign', '#': 'number sign', '*': 'asterisk', '/': 'slash'},
    'uk': {'+': 'плюс', '−': 'мінус', '×': 'помножити на', '÷': 'поділити на',
           '=': 'дорівнює', '≠': 'не дорівнює', '≤': 'менше або дорівнює',
           '≥': 'більше або дорівнює', '%': 'знак відсотка', '°': 'знак градуса',
           '€': 'знак євро', '£': 'знак фунта', '$': 'знак долара', '&': 'амперсанд',
           '@': 'равлик', '#': 'решітка', '*': 'зірочка', '/': 'коса риска'},
    'sk': {'+': 'plus', '−': 'mínus', '×': 'krát', '÷': 'delené',
           '=': 'rovná sa', '≠': 'nerovná sa', '≤': 'menšie alebo rovné',
           '≥': 'väčšie alebo rovné', '%': 'znak percenta', '°': 'znak stupňa',
           '€': 'znak eura', '£': 'znak libry', '$': 'znak dolára', '&': 'ampersand',
           '@': 'zavináč', '#': 'mriežka', '*': 'hviezdička', '/': 'lomka'},
}
_TOKEN = re.compile(r'\S+|\s+')
_INTEGER = re.compile(r'[0-9]+\Z')
_ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')
_SIMPLE_MATH = re.compile(r'[0-9]+(?:[+=−×÷][0-9]+)+\Z')
_MATH_PART = re.compile(r'[0-9]+|[+=−×÷]')
_OPENING = frozenset('([{"\'“‘«')
_CLOSING = frozenset(')]}"\'”’»,;.!?')


@dataclass(frozen=True)
class PreparationOptions:
    language: str
    numbers: bool = False
    iso_dates: bool = False
    symbols: bool = False
    normalize_spaces: bool = False
    join_hyphenated: bool = False
    remove_soft_hyphens: bool = False

    def __post_init__(self):
        if type(self.language) is not str or self.language not in _DIGITS:
            raise ValueError('Choose English, Ukrainian or Slovak explicitly; there is no language fallback.')
        for name in ('numbers', 'iso_dates', 'symbols', 'normalize_spaces',
                     'join_hyphenated', 'remove_soft_hyphens'):
            if type(getattr(self, name)) is not bool:
                raise ValueError('Reading-preparation choices must be boolean.')


@dataclass(frozen=True)
class PreparationResult:
    preview: ReadingPreview
    language: str
    counts: tuple
    retained_iso_dates: int = 0

    def description(self):
        changes = ', '.join(name + ': ' + str(count) for name, count in self.counts) or 'No changes'
        return (f'Explicit language: {dict(LANGUAGES)[self.language]}; policy: {POLICY}.\n'
                + changes + '.\n'
                + (f'{self.retained_iso_dates} invalid or unselected ISO date token(s) retained verbatim.\n'
                   if self.retained_iso_dates else '')
                + 'Numbers are spoken digit by digit. ISO dates use year/month/day labels, not inferred local order. '
                'Stress, phonemes, natural grammar, punctuation restoration and voice switching are not inferred.\n\n'
                + self.preview.description())


def _digits(value, language):
    words = _DIGITS[language]
    size = sum(len(words[ord(character) - 48]) for character in value) + max(0, len(value) - 1)
    if size > MAX_OUTPUT_CHARACTERS:
        raise ValueError('The proposed spoken digits exceed the reading-preparation output budget.')
    return ' '.join(words[ord(character) - 48] for character in value)


def _token_parts(token):
    first, end = 0, len(token)
    while first < end and token[first] in _OPENING:
        first += 1
    while end > first and token[end - 1] in _CLOSING:
        end -= 1
    return token[:first], token[first:end], token[end:]


def preview_preparation(snapshot, options, *, scope=None, cancelled=lambda: False,
                        timeout=DEFAULT_TIMEOUT):
    """Return a source-bound preview; no document or file is changed here."""
    if not isinstance(snapshot, ReadingSnapshot) or not isinstance(options, PreparationOptions):
        raise ValueError('Capture the reading copy and explicit preparation settings first.')
    if not callable(cancelled):
        raise ValueError('A reading-preparation cancellation callback is required.')
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0.01 <= timeout <= 5):
        raise ValueError('The reading-preparation deadline must be between 0.01 and 5 seconds.')
    deadline = time.monotonic() + timeout

    def check():
        check_cancelled(cancelled)
        if time.monotonic() >= deadline:
            raise TimeoutError('Reading preparation reached its time budget. Select a smaller range.')

    check()
    if scope is None:
        start, end = 0, len(snapshot.text)
    elif (type(scope) is tuple and len(scope) == 2
            and all(type(value) is int for value in scope)
            and 0 <= scope[0] < scope[1] <= len(snapshot.text)):
        start, end = scope
    else:
        raise ValueError('Select a nonempty current reading-copy range.')
    fragment = snapshot.text[start:end]
    if len(fragment) > MAX_INPUT_CHARACTERS:
        raise ValueError('Select at most 200,000 characters for one reading-preparation preview.')
    counts = {}
    total_changes = 0

    def record(name, count=1):
        nonlocal total_changes
        if count:
            total_changes += count
            if total_changes > MAX_TRANSFORMATIONS:
                raise ValueError('Too many reading-preparation changes; select a smaller range.')
            counts[name] = counts.get(name, 0) + count

    # These are the existing ReadingDocument.cleanup rules, now explicit in a
    # preview. They preserve newline spelling and never execute a user regex.
    if options.join_hyphenated:
        fragment, count = re.subn(r'(?<=\w)-\r?\n(?=\w)', '', fragment)
        record('joined line-break hyphens', count)
        check()
    if options.normalize_spaces:
        def normalized(match):
            if match.group() != ' ':
                record('normalized whitespace groups')
            return ' '
        fragment = re.sub(r'[^\S\r\n]+', normalized, fragment)
        check()
    if options.remove_soft_hyphens:
        count = fragment.count('\u00ad')
        fragment = fragment.replace('\u00ad', '')
        record('removed soft hyphens', count)
        check()

    pieces, output_size, retained_dates = [], 0, 0
    symbols = _SYMBOLS[options.language]
    for match in _TOKEN.finditer(fragment):
        check()
        token = match.group()
        leading, core, trailing = _token_parts(token)
        replacement, kind = core, None
        if _ISO_DATE.fullmatch(core):
            try:
                date.fromisoformat(core)
                valid_date = True
            except ValueError:
                valid_date = False
            if options.iso_dates and valid_date:
                replacement = ', '.join(label + ' ' + _digits(value, options.language)
                    for label, value in zip(_DATE_FIELDS[options.language], core.split('-')))
                kind = 'ISO dates with explicit field labels'
            else:
                retained_dates += 1
        elif options.numbers and _INTEGER.fullmatch(core):
            replacement = _digits(core, options.language)
            kind = 'digit-by-digit integers'
        elif options.symbols and core in symbols:
            replacement = symbols[core]
            kind = 'named symbols'
        elif options.symbols and _SIMPLE_MATH.fullmatch(core):
            chunks = []
            for part in _MATH_PART.findall(core):
                check()
                chunks.append(symbols[part] if part in symbols else
                              _digits(part, options.language) if options.numbers else part)
            replacement = ' '.join(chunks)
            kind = 'explicit simple operator sequences'
        if kind is not None:
            record(kind)
        rendered = leading + replacement + trailing
        output_size += len(rendered)
        if output_size > MAX_OUTPUT_CHARACTERS:
            raise ValueError('The reading-preparation output budget was exceeded.')
        pieces.append(rendered)
    check()
    replacement = ''.join(pieces)
    combined = snapshot.text[:start] + replacement + snapshot.text[end:]
    # Reuse the existing edit reduction: unchanged prefixes/suffixes retain
    # their original source mapping. The accepted preview is one undo item.
    probe = ReadingDocument(snapshot.text)
    if probe.set_text(combined):
        edit = probe._edits[-1]
        first, last, replacement = edit['start'], edit['end'], edit['replacement']
    else:
        first = last = 0
        replacement = ''
    check()
    preview = ReadingPreview(snapshot, first, last, replacement,
                             'Reviewed language-aware reading preparation',
                             total_changes, 'explicit preparation change(s)')
    return PreparationResult(preview, options.language, tuple(counts.items()), retained_dates)
