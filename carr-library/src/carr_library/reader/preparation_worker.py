"""Fixed isolated worker for explicit Hunspell and num2words/Babel previews.

Started with -I -B -S. Only an explicitly verified optional package directory is
added for normalization; host site-packages and PATH tools are never fallback.
The parent owns deadlines, cancellation, immutable runtime readers and results.
"""
from decimal import Decimal, InvalidOperation
import ctypes
from datetime import date
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

MAX_BYTES = 256 * 1024


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def reject_constant(_):
    raise ValueError


def audit(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
        raise PermissionError('Network and nested processes are disabled in reading preparation.')


def normalizer(value):
    if (set(value) != {'version', 'operation', 'selected', 'context', 'packages'}
            or type(value['selected']) is not str or len(value['selected'].encode('utf-8')) > 512
            or '\x00' in value['selected'] or type(value['context']) is not dict
            or type(value['packages']) is not str or not Path(value['packages']).is_absolute()):
        raise ValueError
    context = value['context']
    required = {'language', 'kind', 'number_mode', 'date_order', 'unit_id', 'unit_symbol',
                'gender', 'grammatical_case', 'voice_id'}
    if set(context) != required or any(type(item) is not str for item in context.values()):
        raise ValueError
    language, kind, mode = context['language'], context['kind'], context['number_mode']
    if (language not in ('en', 'uk', 'sk') or kind not in ('number', 'date', 'unit', 'symbol')
            or mode not in ('cardinal', 'ordinal', 'digits', 'year')
            or context['date_order'] not in ('ymd', 'dmy', 'mdy')
            or context['gender'] not in ('masculine', 'feminine')
            or context['grammatical_case'] not in
                ('nominative', 'genitive', 'dative', 'accusative', 'instrumental', 'locative')):
        raise ValueError
    # No .pth processing, site initialization, user directories or package lookup
    # outside this reviewed directory is introduced by the optional adapter.
    sys.path.insert(0, value['packages'])
    try:
        if (importlib.metadata.version('num2words') != '0.5.14' or
                importlib.metadata.version('Babel') != '2.18.0'):
            return {'error': 'unavailable'}
        from num2words import num2words
        from babel import Locale
        from babel.dates import format_date
        from babel.numbers import parse_decimal, format_decimal
        from babel.units import format_unit
    except (ImportError, importlib.metadata.PackageNotFoundError):
        return {'error': 'unavailable'}
    locale = Locale.parse(language)
    selected = value['selected']
    core = selected.strip()
    if not core or '\n' in core or '\r' in core:
        return {'error': 'invalid-selection'}
    prefix = selected[:len(selected) - len(selected.lstrip())]
    suffix = selected[len(selected.rstrip()):]

    def words(number, selected_mode='cardinal'):
        kwargs = {}
        if language == 'uk' and selected_mode == 'cardinal':
            kwargs = dict(gender=context['gender'], case=context['grammatical_case'])
        return num2words(number, lang=language, to=selected_mode, **kwargs)

    def number(text):
        if (len(text) > 80 or not re.fullmatch(r'[+-]?[0-9][0-9., \u00a0\u202f]*', text)
                or sum(char.isdigit() for char in text) > 18):
            raise ValueError
        parsed = parse_decimal(text, locale=locale, strict=True)
        if not parsed.is_finite() or abs(parsed) >= Decimal('1000000000000000'):
            raise ValueError
        # Explicit digit mode handles identifiers such as 0012. Cardinal mode
        # cannot silently discard their significant leading zeroes.
        digits = re.sub(r'[^0-9]', '', text.lstrip('+-'))
        integer = re.split(r'[.,]', text.lstrip('+-'))[0].replace(' ', '').replace('\u00a0', '').replace('\u202f', '')
        if len(integer) > 1 and integer.startswith('0'):
            raise ValueError
        if parsed.as_tuple().exponent < -6 or not digits:
            raise ValueError
        return parsed

    try:
        if kind == 'symbol':
            if language != 'en' or len(core) != 1 or unicodedata.category(core)[0] not in ('P', 'S'):
                raise ValueError
            replacement = unicodedata.name(core).lower()
        elif kind == 'date':
            if mode != 'cardinal':
                raise ValueError
            match = re.fullmatch(r'([0-9]{1,4})([-/.])([0-9]{1,2})\2([0-9]{1,4})', core)
            if match is None:
                raise ValueError
            fields = dict(zip(context['date_order'], (match[1], match[3], match[4])))
            if len(fields['y']) != 4 or len(fields['m']) > 2 or len(fields['d']) > 2:
                raise ValueError
            exact = date(int(fields['y']), int(fields['m']), int(fields['d']))
            localized = format_date(exact, format='long', locale=locale)
            replacement = re.sub(r'[0-9]+', lambda item: words(int(item[0])), localized)
        elif kind == 'unit':
            if (mode != 'cardinal' or not re.fullmatch('[a-z]+-[a-z0-9-]+', context['unit_id'])
                    or not context['unit_symbol'] or not core.endswith(context['unit_symbol'])):
                raise ValueError
            amount = core[:-len(context['unit_symbol'])].strip()
            parsed = number(amount)
            pattern = '#,##0.##################'
            localized_number = format_decimal(parsed, format=pattern, locale=locale)
            localized_unit = format_unit(parsed, context['unit_id'], length='long', format=pattern, locale=locale)
            if localized_unit.count(localized_number) != 1:
                raise ValueError
            replacement = localized_unit.replace(localized_number, words(parsed), 1)
        elif mode == 'digits':
            if not re.fullmatch('[0-9]{1,18}', core):
                raise ValueError
            replacement = ' '.join(words(int(char)) for char in core)
        else:
            parsed = number(core)
            if mode in ('ordinal', 'year'):
                if parsed != parsed.to_integral_value() or parsed < 0:
                    raise ValueError
                parsed = int(parsed)
                if mode == 'year' and not 1 <= parsed <= 9999:
                    raise ValueError
            replacement = words(parsed, mode)
        replacement = prefix + replacement + suffix
        if not replacement or '\x00' in replacement or len(replacement.encode('utf-8')) > 16 * 1024:
            return {'error': 'work-limit'}
        return {'replacement': replacement, 'engine_version': 'num2words 0.5.14; Babel 2.18.0; Unicode ' + unicodedata.unidata_version}
    except (ValueError, TypeError, OverflowError, InvalidOperation, NotImplementedError, IndexError):
        return {'error': 'invalid-selection'}


def spelling(value):
    if (set(value) != {'version', 'operation', 'words', 'dll', 'aff', 'dic', 'library_version'}
            or value['library_version'] != '1.7.6' or type(value['words']) is not list
            or len(value['words']) > 512 or len(set(value['words'])) != len(value['words'])):
        raise ValueError
    for word in value['words']:
        if type(word) is not str or not word or '\x00' in word or len(word.encode('utf-8')) > 128:
            raise ValueError
    for key in ('dll', 'aff', 'dic'):
        if type(value[key]) is not str or not Path(value[key]).is_absolute():
            raise ValueError
    # Windows lookup is limited to the explicitly selected DLL directory and
    # operating-system libraries. Neither PATH nor CWD participates.
    library = ctypes.CDLL(value['dll'], **({'winmode': 0x00000900} if os.name == 'nt' else {}))
    handle_type = ctypes.c_void_p
    array_type = ctypes.POINTER(ctypes.c_char_p)
    library.Hunspell_create.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    library.Hunspell_create.restype = handle_type
    library.Hunspell_destroy.argtypes = [handle_type]
    library.Hunspell_destroy.restype = None
    library.Hunspell_get_library_version.argtypes = []
    library.Hunspell_get_library_version.restype = ctypes.c_char_p
    library.Hunspell_get_dic_encoding.argtypes = [handle_type]
    library.Hunspell_get_dic_encoding.restype = ctypes.c_char_p
    library.Hunspell_spell.argtypes = [handle_type, ctypes.c_char_p]
    library.Hunspell_spell.restype = ctypes.c_int
    library.Hunspell_suggest.argtypes = [handle_type, ctypes.POINTER(array_type), ctypes.c_char_p]
    library.Hunspell_suggest.restype = ctypes.c_int
    library.Hunspell_free_list.argtypes = [handle_type, ctypes.POINTER(array_type), ctypes.c_int]
    library.Hunspell_free_list.restype = None
    version = library.Hunspell_get_library_version()
    if version != b'1.7.6':
        return {'error': 'unavailable'}
    def dictionary_path(key):
        path = value[key]
        # Hunspell's reviewed MSVC myopen() uses UTF-16 only for this prefix.
        # Inputs were already restricted to absolute, non-network local files.
        if os.name == 'nt' and not path.startswith('\\\\?\\'):
            path = '\\\\?\\' + path
        return path.encode('utf-8')
    handle = library.Hunspell_create(dictionary_path('aff'), dictionary_path('dic'))
    if not handle:
        return {'error': 'unavailable'}
    try:
        encoding = library.Hunspell_get_dic_encoding(handle)
        if encoding is None or encoding.upper().replace(b'-', b'') != b'UTF8':
            return {'error': 'unavailable'}
        results = []
        for word in value['words']:
            raw = word.encode('utf-8')
            correct = bool(library.Hunspell_spell(handle, raw))
            suggestions = []
            if not correct:
                array = array_type()
                count = library.Hunspell_suggest(handle, ctypes.byref(array), raw)
                try:
                    if count < 0 or count > 4096 or (count and not array):
                        return {'error': 'work-limit'}
                    for index in range(min(count, 16)):
                        item = array[index]
                        if item is None or len(item) > 512:
                            return {'error': 'work-limit'}
                        suggestion = item.decode('utf-8')
                        if (suggestion and len(suggestion) <= 128 and
                                all(ord(c) >= 32 and not 127 <= ord(c) < 160 for c in suggestion)
                                and suggestion not in suggestions and suggestion != word):
                            suggestions.append(suggestion)
                finally:
                    if array:
                        library.Hunspell_free_list(handle, ctypes.byref(array), count)
            results.append(dict(word=word, correct=correct, suggestions=suggestions))
        return {'words': results, 'engine_version': version.decode('ascii')}
    finally:
        library.Hunspell_destroy(handle)


def main():
    try:
        import resource
    except ImportError:
        pass
    else:
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    sys.addaudithook(audit)
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=reject_constant)
        if type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1:
            raise ValueError
        if value.get('operation') == 'normalize':
            result = normalizer(value)
        elif value.get('operation') == 'spell':
            result = spelling(value)
        else:
            raise ValueError
    except (MemoryError, OverflowError, RecursionError):
        result = {'error': 'work-limit'}
    except (ImportError, AttributeError, OSError):
        result = {'error': 'unavailable'}
    except (ValueError, TypeError, KeyError, UnicodeError):
        result = {'error': 'invalid-request'}
    raw = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_BYTES:
        raw = b'{"error":"work-limit"}'
    sys.stdout.buffer.write(raw)


if __name__ == '__main__':
    main()
