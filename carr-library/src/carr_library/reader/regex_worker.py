"""Isolated bounded narration substitution worker; standard library only.

Reused from canonical speech/regex_worker.py, blob
630f6d9a6b5a90355125b932cc469fe4d628f883. The adapter additionally checks
expansion before allocation and bounds the actual UTF-8 reply before writing.
"""
import json
import re
import sys


MAX_INPUT_BYTES = 8_000_000
MAX_OUTPUT_BYTES = 1_000_000
MAX_MATCHES = 10_000
MAX_PATTERN_CHARACTERS = 1024
MAX_REPLACEMENT_CHARACTERS = 4096


def main():
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('Substitution input is too large.')
    data = json.loads(raw)
    if (type(data) is not dict
            or set(data) != {'text', 'pattern', 'replacement', 'ignore_case'}
            or not isinstance(data['text'], str)
            or not isinstance(data['pattern'], str)
            or not 1 <= len(data['pattern']) <= MAX_PATTERN_CHARACTERS
            or not isinstance(data['replacement'], str)
            or len(data['replacement']) > MAX_REPLACEMENT_CHARACTERS
            or type(data['ignore_case']) is not bool):
        raise ValueError('Invalid substitution request.')
    pattern = re.compile(data['pattern'], re.IGNORECASE if data['ignore_case'] else 0)
    matches = []
    output_size = 0
    template = data['replacement']
    template_bytes = len(template.encode('utf-8'))
    references = sum(1 for index, char in enumerate(template[:-1])
                     if char == '\\' and (template[index + 1] in '123456789'
                                           or template[index + 1:index + 3] == 'g<'))
    for match in pattern.finditer(data['text']):
        # Each possible backreference expands at most one captured group.
        # Counting escaped backslashes too is conservative. Match spans avoid
        # copying large captures merely to check their size. Four UTF-8 bytes
        # per character is a conservative bound, including lookaround captures.
        largest_capture = max((end - start for start, end in match.regs if start >= 0), default=0)
        if (len(matches) >= MAX_MATCHES
                or output_size + template_bytes + references * largest_capture * 4 + 64 > MAX_OUTPUT_BYTES):
            raise ValueError('Substitution output is too large.')
        replacement = match.expand(template)
        output_size += len(replacement.encode('utf-8')) + 64
        if output_size > MAX_OUTPUT_BYTES:
            raise ValueError('Substitution output is too large.')
        matches.append([match.start(), match.end(), replacement])
    reply = json.dumps({'matches': matches}, ensure_ascii=False).encode('utf-8')
    if len(reply) > MAX_OUTPUT_BYTES:
        raise ValueError('Substitution reply is too large.')
    sys.stdout.buffer.write(reply)


if __name__ == '__main__':
    main()
