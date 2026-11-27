"""Isolated bounded narration substitution worker; standard library only."""
import json
import re
import sys


def main():
    raw = sys.stdin.buffer.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError('Substitution input is too large.')
    data = json.loads(raw)
    pattern = re.compile(data['pattern'], re.IGNORECASE if data.get('ignore_case') else 0)
    matches = []
    output_size = 0
    for match in pattern.finditer(data['text']):
        replacement = match.expand(data['replacement'])
        output_size += len(replacement.encode('utf-8')) + 64
        if len(matches) >= 10000 or output_size > 1_000_000:
            raise ValueError('Substitution output is too large.')
        matches.append([match.start(), match.end(), replacement])
    # This protocol is UTF-8 even when redirected Windows stdout uses a legacy
    # code page. The parent decodes bytes, not terminal display text.
    sys.stdout.buffer.write(json.dumps({'matches': matches}, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()
