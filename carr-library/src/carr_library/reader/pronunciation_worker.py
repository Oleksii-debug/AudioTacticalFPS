"""Isolated pronunciation matching; fixed package code and standard library only.

Based on the reviewed canonical speech/regex_worker.py contract. Matches always
refer to the input text; the parent merges literal and pattern matches without
cascading and owns the process deadline/cancellation.
"""
import json
import re
import sys

MAX_REQUEST = 64 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024
MAX_MATCHES = 10000
MAX_RESPONSE = 900 * 1024


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def reject_constant(_):
    raise ValueError



def _expansion_fits(match, template, maximum):
    """Bound character allocation from spans before Match.expand copies groups.

    The source template length is already an upper bound for its literal part.
    Add only the spans of referenced captures, never match.group() strings.
    Escaped backslashes and octal escapes do not name captures. The real
    Match.expand still validates and performs the replacement afterward.

    This is a conservative character bound. Exact UTF-8 JSON bytes are checked
    immediately after expansion; at most four bytes per admitted character can
    be allocated here, even when the platform has no address-space limit.
    """
    size, position, spans = len(template), 0, match.regs
    if size > maximum:
        return False
    while position < len(template):
        if template[position] != '\\':
            position += 1
            continue
        position += 1
        if position == len(template):
            raise re.error('Invalid replacement escape.')
        code = template[position]
        position += 1
        reference = None
        if code == 'g':
            if position == len(template) or template[position] != '<':
                raise re.error('Invalid replacement group.')
            end = template.find('>', position + 1)
            if end < 0:
                raise re.error('Invalid replacement group.')
            name = template[position + 1:end]
            position = end + 1
            if name.isidentifier():
                if name not in match.re.groupindex:
                    raise IndexError('Unknown replacement group.')
                reference = match.re.groupindex[name]
            else:
                try:
                    reference = int(name)
                except ValueError:
                    raise re.error('Invalid replacement group.') from None
                if reference < 0:
                    raise re.error('Invalid replacement group.')
        elif code == '0':
            for _ in range(2):
                if position < len(template) and template[position] in '01234567':
                    position += 1
                else:
                    break
        elif code in '123456789':
            digits = code
            if position < len(template) and template[position] in '0123456789':
                digits += template[position]
                position += 1
                if (all(char in '01234567' for char in digits)
                        and position < len(template) and template[position] in '01234567'):
                    position += 1  # Three octal digits denote a literal.
                    continue
            reference = int(digits)
        # Every other escaped character is literal or is rejected by expand.
        if reference is not None:
            if reference >= len(spans):
                raise re.error('Invalid replacement group.')
            start, end = spans[reference]
            size += max(0, end - start)
            if size > maximum:
                return False
    return True


def run():
    # The POSIX hard limit supplements the parent's deadline. Windows uses the
    # same bounded input/response and supervised lifetime; no memory claim is
    # made for an unmeasured native regex allocation.
    try:
        import resource
    except ImportError:
        pass
    else:
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        raise ValueError
    data = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=reject_constant)
    if (type(data) is not dict or set(data) != {'version', 'text', 'rules'}
            or type(data['version']) is not int or data['version'] != 1
            or type(data['text']) is not str or '\x00' in data['text']
            or len(data['text'].encode('utf-8')) > MAX_TEXT
            or type(data['rules']) is not list or not 1 <= len(data['rules']) <= 1000):
        raise ValueError
    results, size, seen = [], 0, set()
    for row in data['rules']:
        if (type(row) is not dict or set(row) != {'index', 'pattern', 'replacement', 'regex', 'ignore_case'}
                or type(row['index']) is not int or not 0 <= row['index'] < 1000
                or row['index'] in seen or type(row['pattern']) is not str
                or not 1 <= len(row['pattern']) <= 1000 or type(row['replacement']) is not str
                or len(row['replacement']) > 4000 or '\x00' in row['pattern'] + row['replacement']
                or type(row['regex']) is not bool or type(row['ignore_case']) is not bool):
            raise ValueError
        seen.add(row['index'])
        pattern = re.compile(row['pattern'] if row['regex'] else re.escape(row['pattern']),
                             re.IGNORECASE if row['ignore_case'] else 0)
        for match in pattern.finditer(data['text']):
            if row['regex'] and not _expansion_fits(match, row['replacement'], MAX_RESPONSE - 32 - size):
                return {'error': 'work-limit'}
            replacement = match.expand(row['replacement']) if row['regex'] else row['replacement']
            item = [match.start(), match.end(), replacement, row['index']]
            size += len(json.dumps(item, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + 1
            if len(results) >= MAX_MATCHES or size > MAX_RESPONSE - 32:
                return {'error': 'work-limit'}
            results.append(item)
    return {'matches': results}


def main():
    try:
        result = run()
    except (re.error, IndexError):
        result = {'error': 'invalid-pattern'}
    except (MemoryError, OverflowError, RecursionError):
        result = {'error': 'work-limit'}
    except (ValueError, TypeError, KeyError, UnicodeError, OSError):
        result = {'error': 'invalid-request'}
    raw = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_RESPONSE:
        raw = b'{"error":"work-limit"}'
    sys.stdout.buffer.write(raw)


if __name__ == '__main__':
    main()
