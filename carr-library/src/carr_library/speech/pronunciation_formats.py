"""Bounded conversion of reviewed text dictionaries into existing reading rules.

No dictionary is executable input, a voice profile, or quotation authority.
Imported rules still require the existing isolated preview and explicit apply.
The supported foreign syntax is documented in PRONUNCIATION_DICTIONARIES.md.
"""
from dataclasses import dataclass
import codecs
from io import StringIO
import re

from ._io import check_cancelled
from .reading import PronunciationRule

MAX_BYTES = 8_000_000
MAX_RULES = 1000
MAX_LINES = 4096
MAX_LINE = 16_384
ENCODINGS = ('unicode', 'cp1251', 'cp1252')


@dataclass(frozen=True)
class DictionaryImport:
    rules: tuple[PronunciationRule, ...]
    format: str
    disabled: int = 0

    @property
    def notice(self):
        order = ('case-sensitive rules first, then longest pattern'
                 if self.format == 'dic' else 'file order retained')
        skipped = f'; {self.disabled} disabled BXD rules omitted' if self.disabled else ''
        return (f'{self.format.upper()} converted for review; {order}{skipped}. '
                'Only the displayed rules are retained when you save. '
                'Preview uses the current bounded Unicode substitution engine.')


def _error(kind, line, detail):
    raise ValueError(f'{kind.upper()} dictionary line {line}: {detail}')


def _decode(raw, encoding):
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise ValueError('Use a captured dictionary of at most 8,000,000 bytes.')
    if type(encoding) is not str or encoding not in ENCODINGS:
        raise ValueError('Choose Unicode, Windows-1251 or Windows-1252 explicitly.')
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise ValueError('UTF-32 dictionaries are unsupported; use UTF-8 or BOM-marked UTF-16.')
    if encoding == 'unicode':
        selected = ('utf-16' if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                    else 'utf-8-sig')
    else:
        if raw.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            raise ValueError('A Unicode BOM conflicts with the selected legacy encoding.')
        selected = encoding
    try:
        text = raw.decode(selected, 'strict')
        text.encode('utf-8', 'strict')
    except UnicodeError as exc:
        raise ValueError('The dictionary does not use the selected encoding; choose its actual encoding.') from exc
    if '\0' in text:
        raise ValueError('Pronunciation dictionaries cannot contain NUL characters.')
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _lines(text, kind, cancelled):
    source = StringIO(text)
    number = 0
    while True:
        check_cancelled(cancelled)
        line = source.readline(MAX_LINE + 2)
        if not line:
            return
        number += 1
        if number > MAX_LINES:
            _error(kind, number, 'the file exceeds 4096 lines, including comments.')
        line = line[:-1] if line.endswith('\n') else line
        if len(line) > MAX_LINE:
            _error(kind, number, 'the line exceeds 16384 characters.')
        yield number, line


def _split(line, kind, number):
    """Recognize only the format's documented left-side delimiter escaping."""
    left, index = [], 0
    while index < len(line):
        char = line[index]
        if kind == 'rex' and char == '\\':
            if index + 1 == len(line):
                _error(kind, number, 'an incomplete pattern escape precedes the missing separator.')
            left.append(line[index:index + 2])
            index += 2
            continue
        if char == '=':
            if kind == 'dic' and line[index:index + 2] == '==':
                left.append('==')
                index += 2
                continue
            return ''.join(left), line[index + 1:]
        left.append(char)
        index += 1
    _error(kind, number, 'expected one unescaped = between pattern and replacement.')


def _literal_rule(pattern, replacement, *, sensitive, left=False, right=False):
    if left or right:
        # The author's text-template boundary includes letters/digits, but an
        # underscore is a separator. This is observed on the authored native
        # corpus, including Latin, Greek, CJK, decimal digits and emoji.
        word = r'[^\W_]'
        expression = ('(?<!' + word + ')' if left else '') + re.escape(pattern) + ('(?!' + word + ')' if right else '')
        return PronunciationRule(expression, replacement.replace('\\', '\\\\'),
                                 regex=True, ignore_case=not sensitive)
    return PronunciationRule(pattern, replacement, ignore_case=not sensitive)


def _dic(line, number):
    sensitive = line.startswith('$') and not line.startswith('$$')
    if sensitive:
        line = line[1:]
    pattern, replacement = _split(line, 'dic', number)
    tokens, index = [], 0
    while index < len(pattern):
        char = pattern[index]
        if char in '*$=#' and pattern[index:index + 2] == char * 2:
            tokens.append((char, True))
            index += 2
        else:
            tokens.append((char, False))
            index += 1
    left = not (tokens and tokens[0] == ('*', False))
    if not left:
        tokens.pop(0)
    right = not (tokens and tokens[-1] == ('*', False))
    if not right:
        tokens.pop()
    if any(char in '*$=#' and not escaped for char, escaped in tokens):
        _error('dic', number, 'only edge * markers and doubled literal *, $, =, # are supported.')
    value = ''.join(char for char, _escaped in tokens)
    if not value:
        _error('dic', number, 'the pattern must contain literal text.')
    rule = _literal_rule(value, replacement, sensitive=sensitive, left=left, right=right)
    return rule, sensitive, len(value)


def _regex_pattern(pattern, kind, number):
    if not pattern or len(pattern) > 1024:
        _error(kind, number, 'use a nonempty regular expression of at most 1024 characters.')
    out, index, depth, in_class = [], 0, 0, False
    while index < len(pattern):
        char = pattern[index]
        if char == '\\':
            if index + 1 == len(pattern):
                _error(kind, number, 'incomplete regular expression escape.')
            following = pattern[index + 1]
            if following == 'x' and pattern[index + 2:index + 3] == '{':
                end = pattern.find('}', index + 3)
                code = pattern[index + 3:end] if end >= 0 else ''
                if not re.fullmatch(r'[0-9A-Fa-f]{4}', code) or 0xD800 <= int(code, 16) <= 0xDFFF:
                    _error(kind, number, 'use exactly four hexadecimal digits for a complete Unicode character.')
                out.append('\\u' + code)
                index = end + 1
                continue
            if following in '0123456789':
                _error(kind, number, 'numeric pattern escapes are not imported; use an explicit hexadecimal escape.')
            if following.isalpha() and following not in 'dDwWsSbBrtfnvx':
                _error(kind, number, 'this regular expression escape is not in the supported text subset.')
            if following == '=':
                out.append('=')
            else:
                out.append(pattern[index:index + 2])
            index += 2
            continue
        if char == '[' and not in_class:
            in_class = True
        elif char == ']' and in_class:
            in_class = False
        elif char == '(' and not in_class:
            if pattern[index:index + 2] == '(?':
                _error(kind, number, 'inline flags, special groups and lookarounds require explicit manual review.')
            depth += 1
            if depth > 32:
                _error(kind, number, 'regular expression nesting exceeds 32 groups.')
        elif char == ')' and not in_class:
            depth -= 1
        out.append(char)
        index += 1
    translated = ''.join(out)
    try:
        compiled = re.compile(translated)
    except (re.error, RecursionError, OverflowError) as exc:
        _error(kind, number, 'the regular expression is not valid in the reviewed substitution engine.')
    if compiled.groups > 99:
        _error(kind, number, 'regular expressions support at most 99 captured groups.')
    return translated, compiled.groups


def _regex_replacement(value, groups, kind, number):
    if len(value) > 4096:
        _error(kind, number, 'the replacement exceeds 4096 characters.')
    out, index = [], 0
    while index < len(value):
        char = value[index]
        if char == '$':
            if value[index:index + 2] == '$$':
                out.append('$')
                index += 2
                continue
            end = index + 1
            while end < len(value) and value[end] in '0123456789':
                end += 1
            if end == index + 1:
                _error(kind, number, 'use $N for a capture or $$ for a literal dollar.')
            group = int(value[index + 1:end])
            if group > groups:
                _error(kind, number, 'the replacement references a capture missing from its pattern.')
            out.append('\\g<' + str(group) + '>')
            index = end
            continue
        if char == '\\':
            if index + 1 == len(value):
                _error(kind, number, 'incomplete replacement escape.')
            following = value[index + 1]
            if following == '\\':
                out.append('\\\\')
            elif following == '$':
                out.append('$')
            elif following in 'nrtfv':
                out.append(dict(n='\n', r='\r', t='\t', f='\f', v='\v')[following])
            else:
                _error(kind, number, 'case operators and other replacement escapes require manual review.')
            index += 2
            continue
        out.append(char)
        index += 1
    return ''.join(out)


def _regex_rule(pattern, replacement, *, sensitive, kind, number):
    translated, groups = _regex_pattern(pattern, kind, number)
    return PronunciationRule(translated, _regex_replacement(replacement, groups, kind, number),
                             regex=True, ignore_case=not sensitive)


def _bxd(rows, cancelled):
    headers = []
    for _ in range(3):
        try:
            headers.append(next(rows))
        except StopIteration:
            _error('bxd', 1, 'Version=1, Delimiter=ETX and a three-field description are required.')
    if headers[0][1] != 'Version=1' or headers[1][1] != 'Delimiter=\x03':
        _error('bxd', 1, 'only the reviewed Version=1, ETX-delimited text dialect is supported.')
    if len(headers[2][1].split('\x03')) != 3:
        _error('bxd', 3, 'the description must contain exactly three fields.')
    result, seen_ids, seen_order, count, disabled = [], set(), set(), 0, 0
    for number, line in rows:
        check_cancelled(cancelled)
        if not line:
            continue
        count += 1
        if count > MAX_RULES:
            _error('bxd', number, 'use at most 1000 rows, including disabled rules.')
        fields = line.split('\x03')
        if len(fields) != 10:
            _error('bxd', number, 'each rule must contain exactly ten fields.')
        identity, ordinal, enabled, sensitive, regex, boundary, output, pattern, replacement, _note = fields
        if (not re.fullmatch('[A-Za-z0-9]{32}', identity) or identity in seen_ids
                or not re.fullmatch('[1-9][0-9]{0,8}', ordinal) or ordinal in seen_order):
            _error('bxd', number, 'rule identities and positive indices must be distinct.')
        seen_ids.add(identity)
        seen_order.add(ordinal)
        if enabled not in ('0', '1') or sensitive not in ('0', '1') or regex not in ('0', '1'):
            _error('bxd', number, 'unrecognized enable, case or pattern flags; export a reviewed text dialect.')
        if boundary not in ('0', '1', '2', '3'):
            _error('bxd', number, 'unrecognized BXD boundary mode; use whole, prefix, suffix or substring text rules.')
        if output in ('1', '2'):
            _error('bxd', number, 'phoneme output is not a plain-text pronunciation rule; explicitly export reviewed text replacements.')
        if output != '0':
            _error('bxd', number, 'unrecognized BXD output mode; explicitly export plain-text replacements.')
        # The author's regex mode ignores the literal boundary selector; all
        # four admitted values have the same observed regex matching behavior.
        if not pattern or len(pattern) > 1024 or len(replacement) > 4096:
            _error('bxd', number, 'a pattern or replacement exceeds the existing rule limit.')
        if enabled == '0':
            disabled += 1
            continue
        try:
            rule = (_regex_rule(pattern, replacement, sensitive=sensitive == '1', kind='bxd', number=number)
                    if regex == '1' else _literal_rule(pattern, replacement, sensitive=sensitive == '1',
                        left=boundary in ('0', '1'), right=boundary in ('0', '2')))
        except (ValueError, TypeError) as exc:
            if str(exc).startswith('BXD dictionary line '):
                raise
            _error('bxd', number, str(exc))
        result.append(rule)
    return DictionaryImport(tuple(result), 'bxd', disabled)


def parse_dictionary(raw, *, format, encoding='unicode', cancelled=lambda: False):
    """Convert captured bytes only; saving or applying is always a separate action."""
    if type(format) is not str or format not in ('dic', 'rex', 'bxd'):
        raise ValueError('Choose a supported DIC, REX or BXD pronunciation dictionary.')
    check_cancelled(cancelled)
    rows = iter(_lines(_decode(raw, encoding), format, cancelled))
    if format == 'bxd':
        result = _bxd(rows, cancelled)
    else:
        result = []
        for number, line in rows:
            if not line.strip() or (line.startswith('#')
                    and not (format == 'dic' and line.startswith('##'))):
                continue
            if len(result) >= MAX_RULES:
                _error(format, number, 'use at most 1000 active pronunciation rules.')
            try:
                if format == 'dic':
                    result.append(_dic(line, number))
                else:
                    sensitive = line.startswith('@')
                    pattern, replacement = _split(line[1:] if sensitive else line, format, number)
                    result.append(_regex_rule(pattern, replacement, sensitive=sensitive,
                                              kind=format, number=number))
            except (ValueError, TypeError) as exc:
                if str(exc).startswith(format.upper() + ' dictionary line '):
                    raise
                _error(format, number, str(exc))
        if format == 'dic':
            result = [rule for rule, _sensitive, _length in
                      sorted(result, key=lambda row: (not row[1], -row[2]))]
        result = DictionaryImport(tuple(result), format)
    check_cancelled(cancelled)
    return result
