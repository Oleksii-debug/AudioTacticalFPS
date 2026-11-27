"""Private isolated HTTPS worker, invoked only by the scoped parent transport.

Its parent owns the deadline and output directory. No document content is ever
interpreted as instructions. Source checkout and installed package use the same
trusted script path, avoiding cwd/module-search ambiguity under Python -I.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from carr_library.acquisition.transport import NetworkConsent, SafeTransport


def main():
    transport = None
    try:
        raw = sys.stdin.buffer.read(32769)
        if not raw or len(raw) > 32768:
            raise ValueError('Invalid request.')
        request = json.loads(raw)
        if set(request) != {'url', 'metadata', 'approved_hosts', 'max_bytes', 'timeout', 'output', 'spent_requests'}:
            raise ValueError('Invalid request.')
        if type(request['metadata']) is not bool or not isinstance(request['approved_hosts'], list):
            raise ValueError('Invalid request.')
        output = Path(request['output'])
        if not output.is_absolute() or output.name != 'response.bin' or not output.parent.is_dir():
            raise ValueError('Invalid output.')
        transport = SafeTransport(NetworkConsent(True,
            'metadata' if request['metadata'] else 'download', tuple(request['approved_hosts']),
            request['max_bytes'], request['timeout']), _in_worker=True)
        spent = request['spent_requests']
        if type(spent) is not int or not 0 <= spent < 12:
            raise ValueError('Invalid request budget.')
        transport.requests = spent
        with output.open('xb') as stream:
            info = transport._transfer(request['url'], stream, cancelled=lambda: False,
                                       metadata=request['metadata'])
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({'ok': True, 'info': asdict(info), 'requests': transport.requests}))
        return 0
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            code = 'timeout'
        elif isinstance(exc, InterruptedError):
            code = 'cancelled'
        else:
            # Map known safe local diagnostics without emitting request URLs,
            # arbitrary upstream messages, contact email, or interpreter traces.
            text = str(exc)
            code = 'not_found' if 'not found' in text else (
                'rate_limit' if 'rate limited' in text else (
                'redirect' if 'host was not approved' in text else (
                'size' if 'size limit' in text else (
                'format' if 'expected JSON or PDF' in text or 'not a PDF' in text else 'transfer'))))
        print(json.dumps({'ok': False, 'error': code, 'requests': transport.requests if transport else 0}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
