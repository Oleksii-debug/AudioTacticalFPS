"""Finite, explicit speech preparation of a separate reading-copy passage.

This is a deterministic three-locale proposal, not linguistic inference, a
translator or a speech engine. Every accepted edit carries its original span.
"""
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import math
import re
from time import perf_counter_ns

MAX_TEXT_CHARACTERS = 200_000
MAX_OUTPUT_CHARACTERS = 1_000_000
MAX_EDITS = 5_000
MAX_TOKENS = 50_000
LOCALES = ('uk-UA', 'en-US', 'sk-SK')
ACTIONS = ('keep', 'read', 'skip')


@dataclass(frozen=True)
class SpeechOptions:
    mode: str = 'verbatim'
    locale: str = ''
    numbers: bool = False
    dates: bool = False
    units: bool = False
    abbreviations: bool = False
    math: bool = False
    symbols: bool = False
    date_format: str = 'iso'
    decimal_mark: str = 'locale'
    number_style: str = 'cardinal'
    headings: str = 'keep'
    tables: str = 'keep'
    footnotes: str = 'keep'
    code: str = 'keep'
    capitalize_paragraphs: bool = False
    join_soft_hyphens: bool = False
    join_wrapped_lines: bool = False

    def __post_init__(self):
        if any(type(getattr(self, name)) is not str for name in ('mode', 'locale', 'date_format',
                'decimal_mark', 'number_style', 'headings', 'tables', 'footnotes', 'code')):
            raise ValueError('Speech profile names and choices must be plain text.')
        if (self.mode not in ('verbatim', 'speech') or self.locale not in ('', *LOCALES)
                or self.mode == 'speech' and self.locale not in LOCALES
                or self.date_format not in ('iso', 'locale')
                or self.decimal_mark not in ('locale', '.', ',')
                or self.number_style not in ('cardinal', 'digits')):
            raise ValueError('Choose verbatim or an explicit supported speech locale and format.')
        for name in ('numbers', 'dates', 'units', 'abbreviations', 'math', 'symbols',
                     'capitalize_paragraphs', 'join_soft_hyphens', 'join_wrapped_lines'):
            if type(getattr(self, name)) is not bool:
                raise ValueError('Speech expansion choices must be booleans.')
        if any(getattr(self, name) not in ACTIONS for name in ('headings', 'tables', 'footnotes', 'code')):
            raise ValueError('Choose keep, read or skip for each supported structure.')
        if self.mode == 'verbatim' and (any(getattr(self, name) for name in
                ('numbers', 'dates', 'units', 'abbreviations', 'math', 'symbols',
                 'capitalize_paragraphs', 'join_soft_hyphens', 'join_wrapped_lines'))
                or any(getattr(self, name) != 'keep' for name in ('headings', 'tables', 'footnotes', 'code'))):
            raise ValueError('Verbatim mode keeps all source text and structure exactly.')


@dataclass(frozen=True)
class SpeechEdit:
    start: int
    end: int
    before: str
    after: str
    kind: str


@dataclass(frozen=True)
class SpeechPreview:
    source_sha256: str
    output_sha256: str
    source: str
    text: str
    options: SpeechOptions
    edits: tuple[SpeechEdit, ...]
    notices: tuple[str, ...]

    @property
    def changed(self):
        return self.text != self.source


def _text(value, maximum):
    if (type(value) is not str or len(value) > maximum or '\x00' in value
            or any(0xD800 <= ord(character) <= 0xDFFF for character in value)):
        raise ValueError('Choose a bounded Unicode reading passage without NUL or unpaired surrogates.')
    return value


def validate_preview(preview):
    """Rebuild the exact source-bound proposal before the controller accepts it."""
    if not isinstance(preview, SpeechPreview) or not isinstance(preview.options, SpeechOptions):
        raise ValueError('Preview the current speech preparation before applying it.')
    _text(preview.source, MAX_TEXT_CHARACTERS)
    _text(preview.text, MAX_OUTPUT_CHARACTERS)
    if (type(preview.edits) is not tuple or len(preview.edits) > MAX_EDITS
            or type(preview.notices) is not tuple or len(preview.notices) > 32
            or any(type(value) is not str or len(value) > 300 for value in preview.notices)):
        raise ValueError('The speech proposal exceeds its review limits.')
    pieces, offset, reconstructed_length = [], 0, 0
    for edit in preview.edits:
        if (not isinstance(edit, SpeechEdit) or type(edit.start) is not int or type(edit.end) is not int
                or not offset <= edit.start < edit.end <= len(preview.source)
                or edit.kind not in ('number', 'date', 'unit', 'abbreviation', 'math', 'symbol',
                                     'heading', 'table', 'footnote', 'code', 'format')
                or preview.source[edit.start:edit.end] != edit.before
                or type(edit.after) is not str or edit.before == edit.after):
            raise ValueError('The reviewed speech spans no longer match their source.')
        reconstructed_length += edit.start - offset + len(edit.after)
        if reconstructed_length > MAX_OUTPUT_CHARACTERS:
            raise ValueError('The reviewed speech exceeds its output allowance.')
        _text(edit.after, MAX_OUTPUT_CHARACTERS)
        pieces.extend((preview.source[offset:edit.start], edit.after))
        offset = edit.end
    if reconstructed_length + len(preview.source) - offset > MAX_OUTPUT_CHARACTERS:
        raise ValueError('The reviewed speech exceeds its output allowance.')
    pieces.append(preview.source[offset:])
    if (''.join(pieces) != preview.text
            or sha256(preview.source.encode('utf-8')).hexdigest() != preview.source_sha256
            or sha256(preview.text.encode('utf-8')).hexdigest() != preview.output_sha256
            or preview.options.mode == 'verbatim' and (preview.edits or preview.changed)):
        raise ValueError('The speech proposal is not bound to these exact source and output bytes.')
    return preview


_EN_SMALL = ('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen '
             'fifteen sixteen seventeen eighteen nineteen').split()
_EN_TENS = ('', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety')
_UK_SMALL = ("нуль один два три чотири п'ять шість сім вісім дев'ять десять одинадцять дванадцять "
             "тринадцять чотирнадцять п'ятнадцять шістнадцять сімнадцять вісімнадцять дев'ятнадцять").split()
_UK_TENS = ('', '', 'двадцять', 'тридцять', 'сорок', "п'ятдесят", 'шістдесят', 'сімдесят', 'вісімдесят', "дев'яносто")
_UK_HUNDREDS = ('', 'сто', 'двісті', 'триста', 'чотириста', "п'ятсот", 'шістсот', 'сімсот', 'вісімсот', "дев'ятсот")


def _plural(value):
    if value % 10 == 1 and value % 100 != 11:
        return 0
    if 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        return 1
    return 2


def _cardinal(value, locale, feminine=False):
    if locale == 'sk-SK':
        from ._vendor_num2words.lang_SK import Num2Word_SK
        # The upstream cardinal converter is used only for bounded integers.
        # No unsupported ordinal or contextual grammatical inference is claimed.
        if feminine and value in (1, 2):
            return 'jedna' if value == 1 else 'dve'
        return Num2Word_SK().to_cardinal(value)
    if locale == 'en-US':
        if value < 20:
            return _EN_SMALL[value]
        if value < 100:
            return _EN_TENS[value // 10] + ('-' + _EN_SMALL[value % 10] if value % 10 else '')
        for scale, label in ((1_000_000, 'million'), (1000, 'thousand'), (100, 'hundred')):
            if value >= scale:
                result = _cardinal(value // scale, locale) + ' ' + label
                return result + (' ' + _cardinal(value % scale, locale) if value % scale else '')
    if value < 20:
        return ('одна' if value == 1 else 'дві') if feminine and value in (1, 2) else _UK_SMALL[value]
    if value < 100:
        return _UK_TENS[value // 10] + (' ' + _cardinal(value % 10, locale, feminine) if value % 10 else '')
    if value < 1000:
        return _UK_HUNDREDS[value // 100] + (' ' + _cardinal(value % 100, locale, feminine) if value % 100 else '')
    for scale, nouns, female in ((1_000_000, ('мільйон', 'мільйони', 'мільйонів'), False),
                                 (1000, ('тисяча', 'тисячі', 'тисяч'), True)):
        if value >= scale:
            count, rest = divmod(value, scale)
            result = _cardinal(count, locale, female) + ' ' + nouns[_plural(count)]
            return result + (' ' + _cardinal(rest, locale, feminine) if rest else '')
    raise ValueError('This number is outside the finite cardinal profile.')


_FRACTIONS = (('десята', 'десятих'), ('сота', 'сотих'), ('тисячна', 'тисячних'),
              ('десятитисячна', 'десятитисячних'), ('стотисячна', 'стотисячних'),
              ('мільйонна', 'мільйонних'))


def _number(token, options, *, feminine=False):
    sign = ''
    if token[:1] in ('+', '-'):
        sign, token = token[0], token[1:]
    marker = options.decimal_mark
    if marker == 'locale':
        marker = ',' if options.locale in ('uk-UA', 'sk-SK') else '.'
    if not re.fullmatch(r'[0-9]+(?:[.,][0-9]+)?', token):
        return None
    if len(token) > 25 or (',' in token or '.' in token) and marker not in token:
        return None
    parts = token.split(marker)
    integer, fraction = parts[0], parts[1] if len(parts) == 2 else None
    if len(integer) > (18 if options.number_style == 'digits' else 9):
        return None
    if options.number_style == 'cardinal' and len(integer) > 1 and integer.startswith('0'):
        return None
    if fraction is not None and (len(fraction) > 6 or options.number_style == 'digits' and len(integer + fraction) > 18):
        return None
    locale = options.locale
    digits = (_UK_SMALL if locale == 'uk-UA' else
              _SK_DIGITS if locale == 'sk-SK' else _EN_SMALL)
    if options.number_style == 'digits':
        spoken = ' '.join(digits[int(character)] for character in integer)
        if fraction is not None:
            spoken += ((' кома ' if marker == ',' else ' крапка ') if locale == 'uk-UA' else
                       (' čiarka ' if marker == ',' else ' bodka ') if locale == 'sk-SK' else ' point ')
            spoken += ' '.join(digits[int(character)] for character in fraction)
    elif fraction is None:
        spoken = _cardinal(int(integer), locale, feminine)
    elif locale == 'sk-SK':
        spoken = _cardinal(int(integer), locale) + (' čiarka ' if marker == ',' else ' bodka ')
        spoken += ' '.join(digits[int(character)] for character in fraction)
    elif locale == 'en-US':
        spoken = _cardinal(int(integer), locale) + ' point ' + ' '.join(digits[int(character)] for character in fraction)
    else:
        whole, part = int(integer), int(fraction)
        spoken = (_cardinal(whole, locale, True) + (' ціла ' if _plural(whole) == 0 else ' цілих ')
                  + _cardinal(part, locale, True) + ' ' + _FRACTIONS[len(fraction) - 1][0 if _plural(part) == 0 else 1])
    if sign:
        spoken = (('плюс ' if sign == '+' else 'мінус ') if locale == 'uk-UA'
                  else ('plus ' if sign == '+' else 'mínus ') if locale == 'sk-SK'
                  else ('plus ' if sign == '+' else 'minus ')) + spoken
    return spoken


# Explicit case-sensitive codes and nominative quantity labels; no magnitude,
# currency, locale or grammatical sentence-case inference.
_UNITS = {
    'm': ('meter', 'meters', 'метр', 'метри', 'метрів', 'метра', False),
    'cm': ('centimeter', 'centimeters', 'сантиметр', 'сантиметри', 'сантиметрів', 'сантиметра', False),
    'mm': ('millimeter', 'millimeters', 'міліметр', 'міліметри', 'міліметрів', 'міліметра', False),
    'km': ('kilometer', 'kilometers', 'кілометр', 'кілометри', 'кілометрів', 'кілометра', False),
    'kg': ('kilogram', 'kilograms', 'кілограм', 'кілограми', 'кілограмів', 'кілограма', False),
    'g': ('gram', 'grams', 'грам', 'грами', 'грамів', 'грама', False),
    'mg': ('milligram', 'milligrams', 'міліграм', 'міліграми', 'міліграмів', 'міліграма', False),
    's': ('second', 'seconds', 'секунда', 'секунди', 'секунд', 'секунди', True),
    'ms': ('millisecond', 'milliseconds', 'мілісекунда', 'мілісекунди', 'мілісекунд', 'мілісекунди', True),
    'min': ('minute', 'minutes', 'хвилина', 'хвилини', 'хвилин', 'хвилини', True),
    'h': ('hour', 'hours', 'година', 'години', 'годин', 'години', True),
    'Hz': ('hertz', 'hertz', 'герц', 'герци', 'герців', 'герца', False),
    'kHz': ('kilohertz', 'kilohertz', 'кілогерц', 'кілогерци', 'кілогерців', 'кілогерца', False),
    'MB': ('megabyte', 'megabytes', 'мегабайт', 'мегабайти', 'мегабайтів', 'мегабайта', False),
    'GB': ('gigabyte', 'gigabytes', 'гігабайт', 'гігабайти', 'гігабайтів', 'гігабайта', False),
    'l': ('liter', 'liters', 'літр', 'літри', 'літрів', 'літра', False),
    'ml': ('milliliter', 'milliliters', 'мілілітр', 'мілілітри', 'мілілітрів', 'мілілітра', False),
    '%': ('percent', 'percent', 'відсоток', 'відсотки', 'відсотків', 'відсотка', False),
    '°C': ('degree Celsius', 'degrees Celsius', 'градус Цельсія', 'градуси Цельсія', 'градусів Цельсія', 'градуса Цельсія', False),
    '°F': ('degree Fahrenheit', 'degrees Fahrenheit', 'градус Фаренгейта', 'градуси Фаренгейта', 'градусів Фаренгейта', 'градуса Фаренгейта', False),
    'km/h': ('kilometer per hour', 'kilometers per hour', 'кілометр за годину', 'кілометри за годину', 'кілометрів за годину', 'кілометра за годину', False),
    'm/s': ('meter per second', 'meters per second', 'метр за секунду', 'метри за секунду', 'метрів за секунду', 'метра за секунду', False),
    'm²': ('square meter', 'square meters', 'квадратний метр', 'квадратні метри', 'квадратних метрів', 'квадратного метра', False),
    'm³': ('cubic meter', 'cubic meters', 'кубічний метр', 'кубічні метри', 'кубічних метрів', 'кубічного метра', False),
}
_UK_UNIT_ALIASES = dict(zip(
    ('м', 'см', 'мм', 'км', 'кг', 'г', 'мг', 'с', 'мс', 'хв', 'год', 'Гц', 'кГц', 'МБ', 'ГБ', 'л', 'мл', 'км/год', 'м/с', 'м²', 'м³'),
    ('m', 'cm', 'mm', 'km', 'kg', 'g', 'mg', 's', 'ms', 'min', 'h', 'Hz', 'kHz', 'MB', 'GB', 'l', 'ml', 'km/h', 'm/s', 'm²', 'm³')))
_UNITS.update({alias: _UNITS[key] for alias, key in _UK_UNIT_ALIASES.items()})
_SK_DIGITS = 'nula jeden dva tri štyri päť šesť sedem osem deväť'.split()
# Fixed quantity labels, not contextual case agreement or pronunciation quality.
_SK_UNITS = {
    'm': ('meter', 'metre', 'metrov', False), 'cm': ('centimeter', 'centimetre', 'centimetrov', False),
    'mm': ('milimeter', 'milimetre', 'milimetrov', False), 'km': ('kilometer', 'kilometre', 'kilometrov', False),
    'kg': ('kilogram', 'kilogramy', 'kilogramov', False), 'g': ('gram', 'gramy', 'gramov', False),
    'mg': ('miligram', 'miligramy', 'miligramov', False),
    's': ('sekunda', 'sekundy', 'sekúnd', True), 'ms': ('milisekunda', 'milisekundy', 'milisekúnd', True),
    'min': ('minúta', 'minúty', 'minút', True), 'h': ('hodina', 'hodiny', 'hodín', True),
    'Hz': ('hertz', 'hertze', 'hertzov', False), 'kHz': ('kilohertz', 'kilohertze', 'kilohertzov', False),
    'MB': ('megabajt', 'megabajty', 'megabajtov', False), 'GB': ('gigabajt', 'gigabajty', 'gigabajtov', False),
    'l': ('liter', 'litre', 'litrov', False), 'ml': ('mililiter', 'mililitre', 'mililitrov', False),
    '%': ('percento', 'percentá', 'percent', False),
    '°C': ('stupeň Celzia', 'stupne Celzia', 'stupňov Celzia', False),
    '°F': ('stupeň Fahrenheita', 'stupne Fahrenheita', 'stupňov Fahrenheita', False),
    'km/h': ('kilometer za hodinu', 'kilometre za hodinu', 'kilometrov za hodinu', False),
    'm/s': ('meter za sekundu', 'metre za sekundu', 'metrov za sekundu', False),
    'm²': ('štvorcový meter', 'štvorcové metre', 'štvorcových metrov', False),
    'm³': ('kubický meter', 'kubické metre', 'kubických metrov', False),
}
_UNIT_PATTERN = '|'.join(re.escape(key) for key in sorted(_UNITS, key=len, reverse=True))
_NUM = r'[+-]?[0-9]+(?:[.,][0-9]+)?'
_QUANTITY = re.compile(r'(' + _NUM + r')([ \t]*)(' + _UNIT_PATTERN + r')')
_EN_ABBREVIATIONS = {'e.g.': 'for example', 'i.e.': 'that is', 'etc.': 'et cetera', 'approx.': 'approximately'}
_UK_ABBREVIATIONS = {'напр.': 'наприклад', 'прибл.': 'приблизно', 'і т. д.': 'і так далі', 'і т. ін.': 'і таке інше'}
_SK_ABBREVIATIONS = {'napr.': 'napríklad', 't. j.': 'to jest', 'atď.': 'a tak ďalej', 'pribl.': 'približne'}
_ABBREVIATIONS = '|'.join(re.escape(key) for key in sorted(
    {*_EN_ABBREVIATIONS, *_UK_ABBREVIATIONS, *_SK_ABBREVIATIONS}, key=len, reverse=True))
_OPERATORS = {
    '+': ('plus', 'плюс'), '-': ('minus', 'мінус'), '−': ('minus', 'мінус'),
    '*': ('times', 'помножити на'), '×': ('times', 'помножити на'),
    '/': ('divided by', 'поділити на'), '÷': ('divided by', 'поділити на'),
    '=': ('equals', 'дорівнює'), '<': ('less than', 'менше ніж'), '>': ('greater than', 'більше ніж'),
    '≤': ('less than or equal to', 'менше або дорівнює'), '≥': ('greater than or equal to', 'більше або дорівнює'),
    '^': ('to the power of', 'у степені'),
}
_SYMBOLS = {
    '&': ('ampersand', 'амперсанд'), '%': ('percent sign', 'знак відсотка'),
    '@': ('at sign', 'знак ет'), '#': ('hash sign', 'знак решітки'), '$': ('dollar sign', 'знак долара'),
    '€': ('euro sign', 'знак євро'), '£': ('pound sign', 'знак фунта'),
    '±': ('plus or minus', 'плюс або мінус'), '∞': ('infinity', 'нескінченність'),
    '√': ('square root sign', 'знак квадратного кореня'), '∑': ('summation sign', 'знак суми'),
    '∫': ('integral sign', 'знак інтеграла'),
}
_SK_OPERATORS = {
    '+': 'plus', '-': 'mínus', '−': 'mínus', '*': 'krát', '×': 'krát',
    '/': 'delené', '÷': 'delené', '=': 'rovná sa', '<': 'menšie ako', '>': 'väčšie ako',
    '≤': 'menšie alebo rovné', '≥': 'väčšie alebo rovné', '^': 'na mocninu',
}
_SK_SYMBOLS = {
    '&': 'ampersand', '%': 'znak percenta', '@': 'zavináč', '#': 'mriežka',
    '$': 'znak dolára', '€': 'znak eura', '£': 'znak libry', '±': 'plus alebo mínus',
    '∞': 'nekonečno', '√': 'znak odmocniny', '∑': 'znak súčtu', '∫': 'znak integrálu',
}
# Whole nonselected/invalid dates are never partially reinterpreted as math.
_TOKEN = re.compile(
    r'(?P<protected>(?:https?://|www\.)[^\s<>]+|(?<![\w.+-])[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
    r'|(?<![\w])[+-]?[0-9]+(?:[.,][0-9]+)?[eE][+-]?[0-9]+(?!\w)'
    r'|`+[^`\r\n]*`+)'
    r'|(?P<footnote>\[\^[A-Za-z0-9_-]{1,64}\])'
    r'|(?P<date>(?<!\w)[0-9]+(?:[-/.][0-9]+){2,}(?!\w))'
    r'|(?P<unit>(?<![\w.,])' + _NUM + r'[ \t]*(?:' + _UNIT_PATTERN + r')(?!\w))'
    r'|(?P<identifier>(?<![\w.-])(?=[\w.-]*[0-9])(?:[^\W\d_][\w.-]*|[0-9][\w.-]*[^\W\d_][\w.-]*)(?!\w)'
    r'|(?<!\w)[0-9]+(?:,[0-9]+){2,}(?!\w))'
    r'|(?P<math>(?<![\w.,])' + _NUM + r'(?:[ \t]*[+\-−×÷*/=<>≤≥^][ \t]*' + _NUM + r')+(?![\w]|[.,][0-9]))'
    r'|(?P<abbreviation>(?<!\w)(?i:' + _ABBREVIATIONS + r')(?!\w))'
    r'|(?P<number>(?<![\w.,])' + _NUM + r'(?![\w]|[.,][0-9]))'
    r'|(?P<symbol>[' + re.escape(''.join(_SYMBOLS)) + r'])'
)
_EN_MONTHS = 'January February March April May June July August September October November December'.split()
_UK_MONTHS = 'січня лютого березня квітня травня червня липня серпня вересня жовтня листопада грудня'.split()
_EN_ORDINALS = ('first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth '
    'thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth '
    'twenty-first twenty-second twenty-third twenty-fourth twenty-fifth twenty-sixth twenty-seventh '
    'twenty-eighth twenty-ninth thirtieth thirty-first').split()
_UK_ORDINALS = ('першого другого третього четвертого п’ятого шостого сьомого восьмого дев’ятого десятого '
    'одинадцятого дванадцятого тринадцятого чотирнадцятого п’ятнадцятого шістнадцятого сімнадцятого '
    'вісімнадцятого дев’ятнадцятого двадцятого').split() + [
    'двадцять ' + word for word in ('першого', 'другого', 'третього', 'четвертого', 'п’ятого',
        'шостого', 'сьомого', 'восьмого', 'дев’ятого')] + ['тридцятого', 'тридцять першого']


def _date(token, options):
    try:
        if options.date_format == 'iso':
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', token):
                return None
            year, month, day = map(int, token.split('-'))
        elif options.locale in ('uk-UA', 'sk-SK'):
            if not re.fullmatch(r'[0-9]{2}\.[0-9]{2}\.[0-9]{4}', token):
                return None
            day, month, year = map(int, token.split('.'))
        else:
            if not re.fullmatch(r'[0-9]{2}/[0-9]{2}/[0-9]{4}', token):
                return None
            month, day, year = map(int, token.split('/'))
        date(year, month, day)
        if options.locale == 'sk-SK':
            return (f'deň {_cardinal(day, options.locale)}, mesiac {_cardinal(month, options.locale)}, '
                    f'rok {_cardinal(year, options.locale)}')
        if options.locale == 'uk-UA':
            return f'{_UK_ORDINALS[day - 1]} {_UK_MONTHS[month - 1]}, рік {_cardinal(year, options.locale)}'
        return f'{_EN_MONTHS[month - 1]} {_EN_ORDINALS[day - 1]}, year {_cardinal(year, options.locale)}'
    except ValueError:
        return None


def _ending(text):
    return '\r\n' if text.endswith('\r\n') else text[-1:] if text.endswith(('\r', '\n')) else ''


def _table_cells(line):
    value = line.rstrip('\r\n').strip()
    if '\\|' in value or '`' in value:
        raise ValueError('Escaped pipes or inline code in a table need manual reading-copy review.')
    if value.startswith('|'):
        value = value[1:]
    if value.endswith('|'):
        value = value[:-1]
    return [cell.strip() for cell in value.split('|')]


def prepare_speech(text, options=SpeechOptions(), *, cancelled=lambda: False, timeout=2.0):
    """Produce one complete bounded proposal, or raise without a partial edit."""
    _text(text, MAX_TEXT_CHARACTERS)
    if not isinstance(options, SpeechOptions):
        raise TypeError('Use explicit SpeechOptions.')
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not .001 <= timeout <= 10):
        raise ValueError('Choose a speech-preview deadline between 0.001 and 10 seconds.')
    numerator, denominator = float(timeout).as_integer_ratio()
    deadline = perf_counter_ns() + numerator * 1_000_000_000 // denominator
    edits, notices, tokens = [], set(), 0
    plain_rows, protected = set(), []

    def check():
        if cancelled():
            raise InterruptedError('Speech preparation cancelled; no reading text was applied.')
        if perf_counter_ns() >= deadline:
            raise TimeoutError('Speech preparation reached its deadline. Select a smaller passage.')

    def add(start, end, replacement, kind):
        check()
        if replacement == text[start:end]:
            return
        if len(edits) >= MAX_EDITS:
            raise ValueError('Speech preparation exceeds 5,000 edits. Select a smaller passage.')
        if len(replacement) > MAX_OUTPUT_CHARACTERS:
            raise ValueError('The prepared speech exceeds its output allowance.')
        edits.append(SpeechEdit(start, end, text[start:end], replacement, kind))

    uk, sk = options.locale == 'uk-UA', options.locale == 'sk-SK'
    locale_index = int(uk)
    sk_labels = {'Footnote ': 'Poznámka ', 'Code.': 'Kód.', 'End code.': 'Koniec kódu.',
                 'Column ': 'Stĺpec ', 'Row ': 'Riadok ', 'Table.': 'Tabuľka.',
                 'Heading level ': 'Nadpis úrovne '}
    def label(english, ukrainian):
        return sk_labels[english] if sk else ukrainian if uk else english

    def convert(match, following):
        kind, token = match.lastgroup, match.group()
        if kind in ('protected', 'identifier'):
            return token, kind
        if kind == 'footnote':
            if options.footnotes == 'skip':
                return '', kind
            if options.footnotes == 'read':
                before = match.string[:match.start()]
                prefix = ' ' if before and not before[-1].isspace() else ''
                suffix = ' ' if following and following[0].isalnum() else ''
                return prefix + label('Footnote ', 'Примітка ') + token[2:-1] + suffix, kind
            return token, kind
        if kind == 'date':
            if not options.dates:
                return token, kind
            value = _date(token, options)
            if value is None:
                notices.add('A date-like token was kept because it is invalid or does not match the selected exact date format.')
            return value if value is not None else token, kind
        if kind == 'unit':
            number, space, unit = _QUANTITY.fullmatch(token).groups()
            if unit in _UK_UNIT_ALIASES and not uk:
                return token, kind
            forms = _SK_UNITS[unit] if sk else _UNITS[unit]
            if not (options.numbers or options.units or options.symbols and unit == '%'):
                return token, kind
            checked = _number(number, options, feminine=forms[-1])
            if checked is None:
                notices.add('An ambiguous, leading-zero or out-of-range quantity was kept literally.')
                return token, kind
            spoken = checked if options.numbers else number
            if options.units:
                unsigned = number.lstrip('+-')
                decimal = '.' in unsigned or ',' in unsigned
                integer = int(re.split('[.,]', unsigned)[0])
                if sk:
                    if decimal:
                        # Avoid inventing fractional case agreement. A labelled
                        # unit remains explicit and stable for any digit fraction.
                        noun = 'jednotka ' + forms[0]
                    else:
                        noun = forms[0 if integer == 1 else 1 if 2 <= integer <= 4 else 2]
                else:
                    noun = forms[5 if decimal else 2 + _plural(integer)] if uk else forms[0 if not decimal and integer == 1 else 1]
                return spoken + ' ' + noun, kind
            if options.symbols and unit == '%':
                return spoken + ' ' + (_SK_SYMBOLS['%'] if sk else _SYMBOLS['%'][locale_index]), kind
            return spoken + space + unit, kind
        if kind == 'math':
            if not options.math:
                return token, kind
            result, cursor, expecting_number = [], 0, True
            operand = re.compile(_NUM)
            while cursor < len(token):
                check()
                if token[cursor] in ' \t':
                    cursor += 1
                    continue
                if expecting_number:
                    found = operand.match(token, cursor)
                    if found is None:
                        return token, kind
                    value = _number(found.group(), options) if options.numbers else found.group()
                    if value is None:
                        notices.add('An ambiguous numeric expression was kept literally.')
                        return token, kind
                    result.append(value)
                    cursor = found.end()
                else:
                    operator = token[cursor]
                    if operator not in _OPERATORS:
                        return token, kind
                    result.append(_SK_OPERATORS[operator] if sk else _OPERATORS[operator][locale_index])
                    cursor += 1
                expecting_number = not expecting_number
                if len(result) > 33:
                    notices.add('An expression with more than 16 operators was kept literally.')
                    return token, kind
            return (' '.join(result), kind) if not expecting_number else (token, kind)
        if kind == 'abbreviation':
            mapping = _SK_ABBREVIATIONS if sk else _UK_ABBREVIATIONS if uk else _EN_ABBREVIATIONS
            replacement = mapping.get(token.casefold()) if options.abbreviations else None
            if replacement is None:
                return token, kind
            if token[0].isupper():
                replacement = replacement[0].upper() + replacement[1:]
            if not following.strip() or following.lstrip(' \t').startswith(('\r', '\n')):
                replacement += '.'
            return replacement, kind
        if kind == 'number':
            replacement = _number(token, options) if options.numbers else token
            if replacement is None:
                notices.add('An ambiguous, leading-zero or out-of-range number was kept literally.')
            return token if replacement is None else replacement, kind
        if not options.symbols:
            return token, kind
        before = match.string[:match.start()]
        left = ' ' if before and not before[-1].isspace() else ''
        right = ' ' if following and not following[0].isspace() and following[0] not in ',.;:!?)]}' else ''
        return left + (_SK_SYMBOLS[token] if sk else _SYMBOLS[token][locale_index]) + right, kind

    def plain(value, offset=None):
        nonlocal tokens
        parts, end = [], 0
        for match in _TOKEN.finditer(value):
            check()
            tokens += 1
            if tokens > MAX_TOKENS:
                raise ValueError('The speech token allowance is exhausted. Select a smaller passage.')
            replacement, kind = convert(match, value[match.end():])
            if offset is not None and kind in ('protected', 'identifier'):
                protected.append((offset + match.start(), offset + match.end()))
            parts.extend((value[end:match.start()], replacement))
            if offset is not None and replacement != match.group():
                add(offset + match.start(), offset + match.end(), replacement, kind)
            end = match.end()
        parts.append(value[end:])
        result = ''.join(parts)
        if len(result) > MAX_OUTPUT_CHARACTERS:
            raise ValueError('The prepared speech exceeds its output allowance.')
        return result

    check()
    if options.mode != 'verbatim':
        lines, offsets, position = text.splitlines(keepends=True), [], 0
        for line in lines:
            offsets.append(position)
            position += len(line)
        index = 0
        while index < len(lines):
            check()
            line, first = lines[index], index
            fence = re.match(r'^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)', line)
            if fence:
                marker = fence.group(1)
                index += 1
                while index < len(lines):
                    check()
                    if re.fullmatch(r'[ \t]{0,3}' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}[ \t]*(?:\r\n|\r|\n)?', lines[index]):
                        break
                    index += 1
                closed = index < len(lines)
                if not closed and options.code != 'keep':
                    raise ValueError('An unclosed code fence must be reviewed manually before reading or skipping it.')
                last = index + 1 if closed else index
                raw = ''.join(lines[first:last])
                if options.code != 'keep':
                    replacement = _ending(raw)
                    if options.code == 'read':
                        ending = _ending(lines[first]) or '\n'
                        body = ''.join(lines[first + 1:index])
                        replacement = label('Code.', 'Код.') + ending + body + (
                            '' if not body or _ending(body) else ending) + label('End code.', 'Кінець коду.') + _ending(raw)
                    add(offsets[first], offsets[first] + len(raw), replacement, 'code')
                index = last
                continue
            is_table = False
            if '|' in line and index + 1 < len(lines) and '|' in lines[index + 1]:
                separator = lines[index + 1].rstrip('\r\n').strip().strip('|')
                is_table = len(separator.split('|')) >= 2 and all(re.fullmatch(r'[ \t]*:?-{3,}:?[ \t]*', cell)
                                                                    for cell in separator.split('|'))
            if is_table:
                index += 2
                while index < len(lines) and '|' in lines[index] and lines[index].strip():
                    check()
                    index += 1
                raw = ''.join(lines[first:index])
                if options.tables != 'keep':
                    cells = [_table_cells(row) for row_index, row in enumerate(lines[first:index]) if row_index != 1]
                    width = len(_table_cells(line))
                    if width > 32 or len(cells) > 2000 or any(len(row) != width for row in cells):
                        raise ValueError('Only simple consistent pipe tables of at most 32 columns and 2,000 rows can be prepared.')
                    replacement = _ending(raw)
                    if options.tables == 'read':
                        ending = _ending(line) or '\n'
                        rows = []
                        for row_index, row in enumerate(cells, 1):
                            check()
                            columns = [label('Column ', 'Стовпець ') + _cardinal(column, options.locale) + ': ' + plain(cell)
                                       for column, cell in enumerate(row, 1)]
                            rows.append(label('Row ', 'Рядок ') + _cardinal(row_index, options.locale) + '. ' + '; '.join(columns))
                        replacement = label('Table.', 'Таблиця.') + ending + ending.join(rows) + _ending(raw)
                    add(offsets[first], offsets[first] + len(raw), replacement, 'table')
                continue
            footnote = re.match(r'^[ \t]{0,3}\[\^([A-Za-z0-9_-]{1,64})\]:[ \t]*(.*?)(?:\r\n|\r|\n)?$', line)
            if footnote:
                index += 1
                while index < len(lines) and (lines[index].startswith('    ') or lines[index].startswith('\t')):
                    check()
                    index += 1
                raw = ''.join(lines[first:index])
                if options.footnotes != 'keep':
                    replacement = _ending(raw)
                    if options.footnotes == 'read':
                        body = footnote.group(2) + _ending(line) + ''.join(
                            row[4:] if row.startswith('    ') else row[1:] for row in lines[first + 1:index])
                        replacement = label('Footnote ', 'Примітка ') + footnote.group(1) + ': ' + plain(body)
                    add(offsets[first], offsets[first] + len(raw), replacement, 'footnote')
                continue
            heading = re.match(r'^[ \t]{0,3}(#{1,6})[ \t]+(.*?)(?:\r\n|\r|\n)?$', line)
            if heading:
                if options.headings != 'keep':
                    replacement = _ending(line)
                    if options.headings == 'read':
                        replacement = label('Heading level ', 'Заголовок рівня ') + _cardinal(
                            len(heading.group(1)), options.locale) + ': ' + plain(heading.group(2)) + _ending(line)
                    add(offsets[index], offsets[index] + len(line), replacement, 'heading')
            else:
                plain_rows.add(index)
                plain(line, offsets[index])
            index += 1

        # These are explicit layout choices only. No comma, question mark,
        # sentence-final punctuation, proper name or ambiguous hyphen is guessed.
        formatting = []
        for row in sorted(plain_rows):
            check()
            line = lines[row]
            body = line.rstrip('\r\n')
            first = len(body) - len(body.lstrip(' \t'))
            if first == len(body):
                continue
            start = offsets[row] + first
            paragraph_start = row == 0 or row - 1 not in plain_rows or not lines[row - 1].strip()
            if options.capitalize_paragraphs and paragraph_start and body[first].islower():
                upper = body[first].upper()
                existing = next((index for index, edit in enumerate(edits)
                                 if edit.start <= start < edit.end), None)
                if existing is not None and edits[existing].start == start:
                    edit = edits[existing]
                    if edit.after and edit.after[0].islower():
                        edits[existing] = SpeechEdit(edit.start, edit.end, edit.before,
                                                    edit.after[0].upper() + edit.after[1:], edit.kind)
                elif existing is None:
                    formatting.append((start, start + 1, upper))
            if row + 1 not in plain_rows or not _ending(line):
                continue
            following = lines[row + 1].rstrip('\r\n')
            indent = len(following) - len(following.lstrip(' \t'))
            if not following or indent == len(following):
                continue
            end = offsets[row + 1] + indent
            if (options.join_soft_hyphens and body.endswith('\u00ad') and len(body) >= 2
                    and body[-2].isalpha() and following[indent].isalpha()):
                formatting.append((offsets[row] + len(body) - 1, end, ''))
            elif (options.join_wrapped_lines and body and body[-1].isalpha()
                    and following[indent].islower()):
                formatting.append((offsets[row] + len(body), end, ' '))
        for start, end, after in formatting:
            check()
            if any(start < finish and begin < end for begin, finish in protected):
                notices.add('Protected identifiers, URLs and inline code keep their exact layout.')
                continue
            if any(start < edit.end and edit.start < end for edit in edits):
                notices.add('An overlapping layout proposal was kept for manual review.')
                continue
            add(start, end, after, 'format')
        if options.capitalize_paragraphs or options.join_soft_hyphens or options.join_wrapped_lines:
            notices.add('Layout is explicit: blank paragraphs and recognized structures stay separate; ambiguous hyphens and sentence punctuation are not inferred.')
        if sk:
            notices.add('Slovak uses bounded upstream cardinal words and explicit date/unit labels; contextual case agreement and speech quality are not inferred.')
        edits.sort(key=lambda edit: edit.start)
    check()
    pieces, offset = [], 0
    output_length = len(text)
    for edit in edits:
        output_length += len(edit.after) - len(edit.before)
        if output_length > MAX_OUTPUT_CHARACTERS:
            raise ValueError('The prepared speech exceeds its output allowance.')
        pieces.extend((text[offset:edit.start], edit.after))
        offset = edit.end
    pieces.append(text[offset:])
    result = ''.join(pieces)
    check()
    preview = SpeechPreview(sha256(text.encode('utf-8')).hexdigest(), sha256(result.encode('utf-8')).hexdigest(),
                            text, result, options, tuple(edits), tuple(sorted(notices)))
    validated = validate_preview(preview)
    check()
    return validated
