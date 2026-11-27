"""One bounded regex preview for the existing versioned reading-copy model."""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
from time import monotonic

from .narration import MAX_TEXT_BYTES, _body, text_digest
from .reading_state import _unique
from .regex_worker import (MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, MAX_MATCHES,
                           MAX_PATTERN_CHARACTERS, MAX_REPLACEMENT_CHARACTERS)


@dataclass(frozen=True)
class RegexPreview:
    source_sha256: str
    text: str
    match_count: int
    pattern: str
    replacement: str
    ignore_case: bool


def preview(text, pattern, replacement, *, ignore_case=False, timeout=2.0,
            cancelled=lambda: False):
    """Return a derivative proposal; never compile user patterns in this process.

    Input is one UTF-8 JSON request of at most 8,000,000 bytes. The child has a
    10,000-match/1,000,000-byte reply bound; the final reading text retains the
    existing 8 MiB section budget. Deadline and cancellation cover validation,
    the owned child and result assembly. No project or source is modified.
    """
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not .05 <= timeout <= 10):
        raise ValueError('Regex preview time budget must be between 0.05 and 10 seconds.')
    deadline = monotonic() + timeout

    def check():
        if cancelled():
            raise InterruptedError('Regular-expression preview cancelled.')
        if monotonic() >= deadline:
            raise TimeoutError('Regular-expression preview exceeded its time budget. Use a simpler pattern or smaller section.')

    check()
    _body(text)
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= MAX_PATTERN_CHARACTERS:
        raise ValueError('Enter a regular expression of 1 to 1024 characters.')
    if not isinstance(replacement, str) or len(replacement) > MAX_REPLACEMENT_CHARACTERS:
        raise ValueError('The replacement must be at most 4096 characters.')
    _body(pattern)
    _body(replacement)
    if type(ignore_case) is not bool:
        raise ValueError('The case-insensitive option must be a boolean.')
    payload = json.dumps(dict(text=text, pattern=pattern, replacement=replacement,
                              ignore_case=ignore_case), ensure_ascii=False).encode('utf-8')
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError('Regex preview request exceeds its 8,000,000-byte budget. Open a smaller section.')
    check()
    from ..audio.engines import EngineUnavailable, _run
    from ..audio.model_manifest import offline_environment
    environment = offline_environment()
    for name in tuple(environment):
        if name.upper().startswith('PYTHON'):
            environment.pop(name)
    try:
        reply = _run([sys.executable, '-I', '-B', '-S', '-X', 'utf8',
                      str(Path(__file__).with_name('regex_worker.py'))],
                     data=payload, timeout=max(.001, deadline - monotonic()),
                     cancelled=cancelled, env=environment)
    except EngineUnavailable as exc:
        raise ValueError('Regex preview was rejected. Check the expression and replacement, '
                         'or reduce the number and size of substitutions.') from exc
    except TimeoutError as exc:
        raise TimeoutError('Regular-expression preview exceeded its time budget. '
                           'Use a simpler pattern or smaller section.') from exc
    check()
    if not isinstance(reply, bytes) or not 0 < len(reply) <= MAX_OUTPUT_BYTES:
        raise ValueError('Invalid or oversized regular-expression reply.')
    try:
        value = json.loads(reply.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number.')))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid regular-expression reply.') from exc
    if type(value) is not dict or set(value) != {'matches'}:
        raise ValueError('Invalid regular-expression reply.')
    matches = value['matches']
    if type(matches) is not list or len(matches) > MAX_MATCHES:
        raise ValueError('Invalid regular-expression match inventory.')
    pieces, position, size, previous = [], 0, 0, None
    for number, row in enumerate(matches):
        if number % 64 == 0:
            check()
        if (type(row) is not list or len(row) != 3
                or type(row[0]) is not int or type(row[1]) is not int
                or not position <= row[0] <= row[1] <= len(text)
                or not isinstance(row[2], str)
                or row[0] == row[1] and previous == (row[0], row[1])):
            raise ValueError('Regex reply contains invalid or overlapping source offsets.')
        start, end, inserted = row
        _body(inserted)
        unchanged = text[position:start]
        size += len(unchanged.encode('utf-8')) + len(inserted.encode('utf-8'))
        if size > MAX_TEXT_BYTES:
            raise ValueError('Regex expansion exceeds the 8 MiB reading-section budget.')
        pieces.extend((unchanged, inserted))
        position, previous = end, (start, end)
    tail = text[position:]
    if size + len(tail.encode('utf-8')) > MAX_TEXT_BYTES:
        raise ValueError('Regex expansion exceeds the 8 MiB reading-section budget.')
    pieces.append(tail)
    result = ''.join(pieces) if matches else text
    check()
    proposal = RegexPreview(text_digest(text), result, len(matches), pattern, replacement, ignore_case)
    check()
    return proposal
