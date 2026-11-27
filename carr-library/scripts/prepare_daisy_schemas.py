"""Explicit CI acquisition of the five official, exact-pinned DAISY XML schemas.

This tool is never called by application startup or publication. It does not run
downloaded code, install a dependency, or change the application's runtime.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PIN_FILE = Path(__file__).resolve().parents[1] / 'packaging' / 'daisy-validation-schemas.json'
MAX_PIN_BYTES = 16384
MAX_FILE_BYTES = 65536
MAX_TOTAL_BYTES = 128 * 1024
TIMEOUT = 120
URLS = {
    'SMIL10.dtd': 'https://raw.githubusercontent.com/daisy/pipeline-modules/'
        '88d39392e9a3e10c1f55f1a82e5e7ea12b0b28b6/'
        'smil-utils/src/main/resources/xml/dtd/SMIL10.dtd',
    **{name: 'https://www.w3.org/TR/xhtml1/DTD/' + name for name in
       ('xhtml-lat1.ent', 'xhtml-special.ent', 'xhtml-symbol.ent', 'xhtml1-transitional.dtd')},
}


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Duplicate schema manifest field.')
        result[name] = value
    return result


def _regular(path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or (not stat.S_ISDIR(info.st_mode) if directory else
                not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
        raise ValueError('Use ordinary local schema files and directories.')
    return info


def _read(path, maximum):
    before = _regular(path)
    if before.st_size > maximum:
        raise ValueError('Schema input exceeds its byte limit.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev, before.st_ino, before.st_size):
            raise ValueError('Schema input changed while opening.')
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) != before.st_size or len(raw) > maximum
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) !=
               (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
        raise ValueError('Schema input changed while reading.')
    return raw


def load_pins(path=None):
    raw = _read(Path(path) if path is not None else PIN_FILE, MAX_PIN_BYTES)
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    if (type(value) is not dict or set(value) != {'schema', 'purpose', 'files'}
            or type(value['schema']) is not int or value['schema'] != 1
            or not isinstance(value['purpose'], str) or not 1 <= len(value['purpose']) <= 512
            or type(value['files']) is not dict or set(value['files']) != set(URLS)):
        raise ValueError('Exactly the five reviewed schema pins are required.')
    for name, row in value['files'].items():
        if (type(row) is not dict or set(row) != {'url', 'size', 'sha256'}
                or row['url'] != URLS[name]
                or type(row['size']) is not int or not 1 <= row['size'] <= MAX_FILE_BYTES
                or not isinstance(row['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['sha256'])):
            raise ValueError('Invalid official schema pin: ' + name)
    if sum(row['size'] for row in value['files'].values()) > MAX_TOTAL_BYTES:
        raise ValueError('Schema pins exceed the aggregate byte limit.')
    return value['files'], sha256(raw).hexdigest()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError('Schema downloads do not follow redirects.')


def _check_deadline(clock, deadline):
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError('Pinned schema preparation exceeded its deadline.')
    return remaining


def _fetch(opener, row, *, clock, deadline):
    request = Request(row['url'], headers={
        'User-Agent': 'Resonance-CI-schema-preparer/1', 'Accept-Encoding': 'identity'})
    with opener.open(request, timeout=min(10, _check_deadline(clock, deadline))) as response:
        if response.status != 200 or response.geturl() != row['url']:
            raise ValueError('The official schema request did not return its exact HTTPS resource.')
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise ValueError('Encoded schema downloads are not accepted.')
        declared = response.headers.get('Content-Length')
        if declared is not None and (
                not re.fullmatch(r'[0-9]{1,10}', declared) or int(declared) != row['size']):
            raise ValueError('Schema Content-Length differs from its exact pin.')
        result = bytearray()
        while len(result) <= row['size']:
            _check_deadline(clock, deadline)
            # One underlying read per iteration: slow streams still revisit the
            # monotonic deadline. The socket has a separate bounded timeout.
            block = response.read1(min(16384, row['size'] + 1 - len(result)))
            _check_deadline(clock, deadline)
            if not block:
                break
            result.extend(block)
        raw = bytes(result)
    if len(raw) != row['size'] or sha256(raw).hexdigest() != row['sha256']:
        raise ValueError('Downloaded schema bytes differ from their exact size/SHA256 pin.')
    return raw


def prepare(destination, *, opener=None, clock=time.monotonic):
    """Create a new validation directory only after all five files verify."""
    rows, manifest_sha = load_pins()
    text = os.fspath(destination)
    if not isinstance(text, str) or not text or '\x00' in text or text.startswith(('//', '\\\\')):
        raise ValueError('Choose an explicit local schema destination.')
    target = Path(os.path.abspath(text))
    for parent in reversed((target.parent, *target.parent.parents)):
        _regular(parent, directory=True)
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError('The schema destination already exists; it will not be overwritten.')
    deadline = clock() + TIMEOUT
    if opener is None:
        # Preserve default HTTPS certificate/hostname validation. No ambient
        # proxy, credentials, redirect handler, or alternate source is adopted.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
    verified = {}
    for name, row in rows.items():
        verified[name] = _fetch(opener, row, clock=clock, deadline=deadline)
    _check_deadline(clock, deadline)
    target.mkdir()  # Exclusive directory creation; a competing target fails.
    for name, raw in verified.items():
        _regular(target, directory=True)
        with (target / name).open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    for name, row in rows.items():
        raw = _read(target / name, row['size'])
        if len(raw) != row['size'] or sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('Prepared schema bytes changed before their receipt.')
    _check_deadline(clock, deadline)
    return {'schema': 1, 'status': 'PASS', 'manifest_sha256': manifest_sha,
            'file_count': len(rows), 'total_bytes': sum(row['size'] for row in rows.values()),
            'files': [{'name': name, **row} for name, row in rows.items()],
            'purpose': 'Explicit CI schema preparation; no application runtime installation.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.destination)
    except Exception as exc:
        result = {'schema': 1, 'status': 'FAIL',
                  'error': type(exc).__name__ + ': ' + str(exc)[:2000]}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
