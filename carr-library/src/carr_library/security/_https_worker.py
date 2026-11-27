"""Fixed stdlib HTTPS GET child for the reviewed TUF client.

The existing parent process owner bounds DNS, TLS, headers and the complete body.
No proxy, redirect, cookie, credential, publisher key or installation code runs here.
"""
from __future__ import annotations

from hashlib import sha256
import http.client
import json
import math
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.parse import urlsplit


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('Duplicate request field.')
        value[key] = item
    return value


def transfer(request, output):
    if (type(request) is not dict or set(request) != {'url', 'maximum', 'timeout', 'ca_pem'}
            or type(request['maximum']) is not int or not 1 <= request['maximum'] <= 16 * 1024**3
            or type(request['timeout']) not in (int, float)
            or not math.isfinite(request['timeout']) or not 0 < request['timeout'] <= 1800):
        raise ValueError('Invalid reviewed transfer limits.')
    url = request['url']
    if (not isinstance(url, str) or not 0 < len(url) <= 8192
            or any(ord(c) < 33 or ord(c) > 126 or c == '\\' for c in url)):
        raise ValueError('Invalid HTTPS request.')
    address = urlsplit(url)
    if (address.scheme != 'https' or not address.hostname or address.username is not None
            or address.password is not None or address.query or address.fragment
            or not 1 <= (443 if address.port is None else address.port) <= 65535):
        raise ValueError('Only the exact reviewed HTTPS address is allowed.')
    ca = request['ca_pem']
    if ca is not None and (not isinstance(ca, str) or not 0 < len(ca.encode('ascii')) <= 128 * 1024):
        raise ValueError('Invalid reviewed TLS trust input.')
    context = ssl.create_default_context(cadata=ca)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    deadline = time.monotonic() + request['timeout']
    connection = http.client.HTTPSConnection(address.hostname, 443 if address.port is None else address.port,
        context=context, timeout=min(10.0, request['timeout']))
    try:
        connection.request('GET', address.path or '/', headers={
            'Accept': 'application/octet-stream, application/json',
            'Accept-Encoding': 'identity', 'Connection': 'close',
            'User-Agent': 'Resonance-Reviewed-TUF/1',
        })
        response = connection.getresponse()
        try:
            # Even a redirect to the same hostname requires a new reviewed source.
            if response.status != 200:
                return {'status': 'http', 'code': response.status}
            if response.getheader('Content-Encoding', 'identity').strip().lower() != 'identity':
                raise ValueError('Encoded update transport is not accepted.')
            lengths = response.headers.get_all('Content-Length', [])
            if len(lengths) > 1 or (lengths and not re.fullmatch(r'[0-9]{1,20}', lengths[0])):
                raise ValueError('Ambiguous update response length.')
            expected = int(lengths[0]) if lengths else None
            if expected is not None and not 0 <= expected <= request['maximum']:
                raise ValueError('Update response exceeds its byte limit.')
            if response.getheader('Transfer-Encoding') and lengths:
                raise ValueError('Ambiguous update response framing.')
            used, digest = 0, sha256()
            with output.open('xb') as stream:
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Update transfer exceeded its deadline.')
                    if connection.sock is not None:
                        connection.sock.settimeout(min(5.0, max(.001, deadline - time.monotonic())))
                    block = response.read1(min(65536, request['maximum'] + 1 - used))
                    if not block:
                        break
                    used += len(block)
                    if used > request['maximum']:
                        raise ValueError('Update response exceeds its byte limit.')
                    digest.update(block)
                    stream.write(block)
                if expected is not None and used != expected:
                    raise ValueError('Update response was truncated.')
                stream.flush()
                os.fsync(stream.fileno())
            return {'status': 'complete', 'bytes': used, 'sha256': digest.hexdigest()}
        finally:
            response.close()
    finally:
        connection.close()


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 2
    root = Path(arguments[0])
    try:
        # The parent creates this private directory and both fixed filenames.
        if not root.is_absolute() or not root.is_dir():
            raise ValueError('Invalid owned transfer directory.')
        with (root / 'request.json').open('rb') as stream:
            raw = stream.read(192 * 1024 + 1)
        if len(raw) > 192 * 1024:
            raise ValueError('Oversized transfer request.')
        request = json.loads(raw.decode('ascii'), object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite request.')))
        result = transfer(request, root / 'response.bin')
    except Exception:
        result = {'status': 'failed'}
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0 if result['status'] in ('complete', 'http') else 2


if __name__ == '__main__':
    raise SystemExit(main())
