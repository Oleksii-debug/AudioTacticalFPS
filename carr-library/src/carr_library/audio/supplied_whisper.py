"""Prepare only the exact reviewed private Whisper V4.3 source.

The source bytes stay outside the public repository. The strict recognition
settings transformation reuses PR135; the remaining transformations correct
subtitle timestamp carry and make text output newlines deterministic. The supplied model and inference implementation stay
in the supplied worker and execute in its separately selected interpreter.
"""
from __future__ import annotations

import ast
import hashlib

SOURCE_SHA256 = '1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9'
PREPARATION_VERSION = 'supplied-whisper-v43-strict-3'


def prepare(raw: bytes) -> bytes:
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError('The supplied Whisper worker differs from the reviewed source.')
    source = raw.decode('utf-8')
    tree = ast.parse(source)
    retry = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == '_v43_settings_attempts')
    timestamp = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == 'srt_time')
    worker = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                  and n.name == 'Worker')
    transcribe = next(n for n in worker.body if isinstance(n, ast.FunctionDef)
                      and n.name == 'transcribe_one')
    fallback = next(n for n in transcribe.body if isinstance(n, ast.Try))
    if (len(fallback.body) != 1 or len(fallback.handlers) != 1
            or not isinstance(fallback.handlers[0].type, ast.Name)
            or fallback.handlers[0].type.id != 'TypeError'):
        raise ValueError('The reviewed recognition call shape does not match.')
    call = ast.unparse(fallback.body[0])
    edits = [
        (retry.body[0].lineno, retry.end_lineno,
         '    return [("original", dict(base))]\n'),
        (fallback.lineno, fallback.end_lineno, '        ' + call + '\n'),
        (timestamp.body[0].lineno, timestamp.end_lineno,
         '    milliseconds = round(float(seconds or 0) * 1000)\n'
         '    hours, milliseconds = divmod(milliseconds, 3600000)\n'
         '    minutes, milliseconds = divmod(milliseconds, 60000)\n'
         '    seconds, milliseconds = divmod(milliseconds, 1000)\n'
         '    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"\n'),
    ]
    # The host verifies the actual exported bytes on Windows and POSIX alike.
    # Path.write_text otherwise translates LF into CRLF on Windows, and would
    # double a pre-existing CRLF from an engine segment. Preserve exact text.
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call_node = statement.value
        if (isinstance(call_node.func, ast.Attribute) and call_node.func.attr == 'write_text'
                and not any(keyword.arg == 'newline' for keyword in call_node.keywords)):
            call_node.keywords.append(ast.keyword(arg='newline', value=ast.Constant('\n')))
            edits.append((statement.lineno, statement.end_lineno,
                          ' ' * statement.col_offset + ast.unparse(statement) + '\n'))
    lines = source.splitlines(keepends=True)
    for first, last, replacement in sorted(edits, reverse=True):
        lines[first - 1:last] = [replacement]
    result = ''.join(lines)
    compile(result, '<prepared-supplied-worker>', 'exec')
    return result.encode('utf-8')

