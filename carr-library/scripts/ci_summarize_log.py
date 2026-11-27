"""Print a bounded view of retained CI output without deciding its verdict.

The workflow preserves the original process exit status. Full output remains
in the existing exact-commit artifact, even when this view omits text.
"""
import argparse
from pathlib import Path
import re

MAX_INPUT = 64 * 1024 * 1024
MAX_OUTPUT = 100_000
VERDICT = re.compile(r'^(?:PASS:|FAIL:|Required suites complete:|FAILED |ERROR |XFAIL |XPASS |INSTALLED_CASE_FAILURE )')
FAILURE = re.compile(r'^(?:E\s{2,}|Traceback \(most recent call last\):|[A-Za-z_.]*(?:Error|Exception):)')


def summarize(path):
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as stream:
        if size <= MAX_INPUT:
            raw = stream.read(MAX_INPUT + 1)
        else:
            raw = stream.read(MAX_INPUT // 2)
            stream.seek(max(0, size - MAX_INPUT // 2))
            raw += b'\n[Middle of oversized diagnostic log omitted from console view.]\n' + stream.read(MAX_INPUT // 2)
    lines = raw.decode('utf-8', errors='replace').splitlines()
    remaining = MAX_OUTPUT

    def emit(text):
        nonlocal remaining
        if remaining <= 0:
            return
        bounded = text[:remaining]
        print(bounded, flush=True)
        remaining -= len(bounded) + 1

    emit('RETAINED_CI_LOG ' + str(path) + ' bytes=' + str(size))
    emit('VERDICTS (original process exit determines workflow acceptance)')
    verdicts = [line for line in lines if VERDICT.match(line)]
    for line in verdicts:
        emit(line[:2000])
    emit('FAILURE_CONTEXTS (bounded; full text retained in the existing artifact)')
    emitted, contexts = set(), 0
    for index, line in enumerate(lines):
        if not FAILURE.match(line) or index in emitted:
            continue
        first, last = max(0, index - 12), min(len(lines), index + 24)
        context = '\n'.join(lines[first:last])
        emit(context[:8000])
        emitted.update(range(first, last))
        contexts += 1
        if contexts >= 10 or remaining < 5000:
            break
    emit('LOG_TAIL')
    emit('\n'.join(lines[-35:])[-8000:])
    if remaining <= 0:
        print('[Console summary reached its bound; full output is retained.]', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    args = parser.parse_args()
    summarize(args.log)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
