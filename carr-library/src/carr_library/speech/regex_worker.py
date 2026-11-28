"""Isolated bounded narration substitution worker; standard library only."""
import io
import json
import re
import sys


# Each atom contains at most one backreference. Match.expand remains Python's
# authority for escapes/groups, but cannot allocate thousands of source-sized
# group copies before the output budget is checked.
_TEMPLATE_ATOM = re.compile(r'\\g<[^>]*>|\\[0-9]{1,3}|\\[\s\S]|[^\\]+')


def _expand_bounded(match, template, *, characters, encoded_bytes):
    position = size = byte_size = 0
    with io.StringIO() as output:
        while position < len(template):
            atom = _TEMPLATE_ATOM.match(template, position)
            if atom is None:
                raise ValueError('Invalid pronunciation replacement escape.')
            piece = match.expand(atom.group())
            size += len(piece)
            if size > characters:
                raise ValueError('Substitution output exceeds its character budget.')
            byte_size += len(piece.encode('utf-8'))
            if byte_size > encoded_bytes:
                raise ValueError('Substitution output is too large.')
            output.write(piece)
            position = atom.end()
        return output.getvalue(), byte_size


def main():
    raw = sys.stdin.buffer.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError('Substitution input is too large.')
    data = json.loads(raw)
    value = data['text']
    if not isinstance(value, str) or len(value) > 2_000_000:
        raise ValueError('Substitution input exceeds its character budget.')
    pattern = re.compile(data['pattern'], re.IGNORECASE if data.get('ignore_case') else 0)
    matches = []
    output_size = characters = position = 0
    for match in pattern.finditer(value):
        if len(matches) >= 10000:
            raise ValueError('Substitution output is too large.')
        characters += match.start() - position
        if characters > 2_000_000:
            raise ValueError('Substitution output exceeds its character budget.')
        replacement, encoded = _expand_bounded(match, data['replacement'],
            characters=2_000_000 - characters, encoded_bytes=1_000_000 - output_size - 64)
        characters += len(replacement)
        output_size += encoded + 64
        if output_size > 1_000_000:
            raise ValueError('Substitution output is too large.')
        matches.append([match.start(), match.end(), replacement])
        position = match.end()
    if characters + len(value) - position > 2_000_000:
        raise ValueError('Substitution output exceeds its character budget.')
    sys.stdout.write(json.dumps({'matches': matches}, ensure_ascii=False))


if __name__ == '__main__':
    main()
