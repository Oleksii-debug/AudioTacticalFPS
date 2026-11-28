"""One fixed HTTPS GET in an owned process; no redirects, proxies or uploads.

The parent supplies a private, existing response file and enforces the whole
process deadline, including DNS/TLS. Only the standard verified TLS stack is
used. This worker supplies untrusted bytes; python-tuf authenticates them.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import http.client
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.parse import quote, urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from carr_library.security._files import TrustError, directory, file_info, json_value, read_bytes


def _endpoint(value):
    if (type(value) is not str or not 1 <= len(value) <= 2048
            or any(ord(c) < 33 or ord(c) == 127 or c in '\\%?#' for c in value)):
        raise TrustError('Invalid configured HTTPS repository.')
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port or 443
        if (parsed.scheme != 'https' or not host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or not 1 <= port <= 65535 or not parsed.path.endswith('/')):
            raise ValueError()
        host = host.encode('idna').decode('ascii').lower()
        if (len(host) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)
                or any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-')
                       for part in host.split('.'))):
            raise ValueError()
        from carr_library.security.updates import relative_path
        if parsed.path != '/':
            relative_path(parsed.path[1:-1])
        parsed.path.encode('ascii')
    except (ValueError, UnicodeError):
        raise TrustError('Invalid configured HTTPS repository.') from None
    return host, port, parsed.path


def _directories(path):
    path = directory(path)
    return [[str(p), p.stat().st_dev, p.stat().st_ino]
            for p in (*reversed(path.parents), path)]


def _file_identity(info):
    return [info.st_dev, info.st_ino, info.st_nlink]


def transfer(request):
    fields = {'schema', 'endpoint', 'url', 'maximum', 'total', 'offset',
              'timeout', 'directories', 'file_identity', 'ca_pem'}
    if (type(request) is not dict or set(request) != fields
            or type(request['schema']) is not int or request['schema'] != 1):
        raise TrustError('Invalid update transfer request.')
    host, port, base = _endpoint(request['endpoint'])
    url = request['url']
    if (type(url) is not str or not url.startswith(request['endpoint'])
            or '%' in url or len(url) > 4096):
        raise TrustError('The request left its configured repository.')
    parsed = urlsplit(url)
    if (parsed.netloc != urlsplit(request['endpoint']).netloc or parsed.query
            or parsed.fragment or not parsed.path.startswith(base)):
        raise TrustError('The request left its configured repository.')
    from carr_library.security.updates import relative_path
    parts = relative_path(parsed.path[len(base):])
    if not parts or parts[0] not in ('metadata', 'targets'):
        raise TrustError('Unsupported update object.')
    for name, maximum in (('maximum', 16 * 1024**3), ('total', 16 * 1024**3)):
        value = request[name]
        if type(value) is not int or not 0 <= value <= maximum:
            raise TrustError('Invalid update transfer budget.')
    maximum, total, offset = request['maximum'], request['total'], request['offset']
    if (maximum == 0 or type(offset) is not int or not 0 <= offset <= maximum
            or total and (maximum != total or offset >= total)
            or not total and offset != 0):
        raise TrustError('Invalid update transfer range.')
    timeout = request['timeout']
    if type(timeout) not in (int, float) or not .05 <= timeout <= 3600:
        raise TrustError('Invalid transfer deadline.')
    rows = request['directories']
    if (type(rows) is not list or not 1 <= len(rows) <= 256
            or any(type(row) is not list or len(row) != 3 or type(row[0]) is not str
                   or any(type(v) is not int for v in row[1:]) for row in rows)):
        raise TrustError('Invalid response directory binding.')
    parent = Path(rows[-1][0])
    path = parent / 'response.part'
    expected = request['file_identity']
    if type(expected) is not list or len(expected) != 3 or any(type(v) is not int for v in expected):
        raise TrustError('Invalid response file binding.')
    deadline = time.monotonic() + timeout

    def current():
        if time.monotonic() >= deadline:
            raise TimeoutError('The HTTPS transfer reached its deadline.')
        if _directories(parent) != rows or _file_identity(file_info(path)) != expected:
            raise TrustError('The private response destination changed.')

    current()
    before = file_info(path)
    if before.st_size != offset:
        raise TrustError('The resumable prefix changed before its request.')
    ca = request['ca_pem']
    if ca is not None and (type(ca) is not str or not 1 <= len(ca) <= 16384):
        raise TrustError('Invalid explicitly configured TLS trust.')
    context = ssl.create_default_context(cadata=ca)
    context.set_alpn_protocols(['http/1.1'])
    connection = http.client.HTTPSConnection(host, port, timeout=min(10, timeout), context=context)
    response = None
    try:
        headers = {'Accept-Encoding': 'identity', 'Connection': 'close',
                   'User-Agent': 'Resonance-SignedUpdate/1'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        connection.request('GET', quote(parsed.path, safe='/'), headers=headers)
        response = connection.getresponse()
        current()
        if response.status == 404:
            return {'schema': 1, 'status': 404, 'bytes': offset, 'start': offset}
        if response.status not in (200, 206):
            raise TrustError('The configured repository refused the request or redirected it.')
        lengths = response.headers.get_all('Content-Length', [])
        if (len(lengths) != 1 or re.fullmatch(r'0|[1-9][0-9]{0,11}', lengths[0]) is None
                or response.headers.get('Transfer-Encoding')
                or response.headers.get('Content-Encoding', 'identity').casefold() != 'identity'):
            raise TrustError('The update response has unsupported framing.')
        length = int(lengths[0])
        if response.status == 206:
            if not offset or not total or response.headers.get_all('Content-Range', []) != [
                    f'bytes {offset}-{total - 1}/{total}'] or length != total - offset:
                raise TrustError('The update response changed its reviewed range.')
            start = offset
        else:
            if response.headers.get('Content-Range'):
                raise TrustError('Unexpected update range header.')
            start = 0
        if not 0 < length <= maximum - start or total and length != total - start:
            raise TrustError('The update response differs from its approved byte budget.')
        fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'r+b', buffering=0) as stream:
            if _file_identity(os.fstat(fd)) != expected or os.fstat(fd).st_size != offset:
                raise TrustError('The private response file changed while opening.')
            current()
            if start == 0 and offset:
                stream.truncate(0)  # Only our unverified prefix; signed target is unchanged.
            stream.seek(start)
            used = start
            while used < start + length:
                current()
                block = response.read(min(65536, start + length - used))
                current()
                if not block:
                    raise TrustError('The update response ended before its declared size.')
                stream.write(block)
                used += len(block)
                if os.fstat(fd).st_size != used:
                    raise TrustError('The private update prefix changed during transfer.')
            # A fixed Content-Length bounds the HTTP body; TUF authenticates all bytes.
            stream.flush()
            os.fsync(fd)
            current()
        return {'schema': 1, 'status': response.status, 'bytes': used, 'start': start}
    finally:
        if response is not None:
            response.close()
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args(argv)
    try:
        raw = read_bytes(args.request, 32768)
        if sha256(raw).hexdigest() != args.sha256:
            raise TrustError('The private transfer request changed.')
        result = transfer(json_value(raw, maximum=32768))
        import json
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        # Do not print endpoint, headers, TLS internals, paths or upstream messages.
        print('{"schema":1,"status":"failed"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
