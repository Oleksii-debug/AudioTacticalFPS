"""Acquire pinned publication validators for CI; never execute or install them.

Default operation is offline. --download explicitly permits only reviewed HTTPS
publisher hosts. Expected byte counts and SHA-256 are checked before create-only
cache publication. Java, lxml and the production application are not imported.
"""
import argparse
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import urllib.parse
import urllib.request
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('publication_packaging_artifacts',
    ROOT / 'packaging' / 'prepare_windows_audio.py')
artifacts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(artifacts)
HOSTS = frozenset(('github.com', 'release-assets.githubusercontent.com',
                   'raw.githubusercontent.com', 'www.w3.org', 'w3.org'))
SCHEMAS = frozenset(('SMIL10.dtd','xhtml1-transitional.dtd','xhtml-lat1.ent',
                     'xhtml-symbol.ent','xhtml-special.ent'))
MAX_DOWNLOAD = 128 * 1024**2


def approved_url(value):
    if not isinstance(value, str):
        raise ValueError('Expected the pinned HTTPS publication-tool URL.')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in HOSTS
            or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None, 443)):
        raise ValueError('Publication-tool downloads require an approved HTTPS publisher host.')
    return value


class ApprovedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, newurl):
        approved_url(newurl)
        return super().redirect_request(request, stream, code, message, headers, newurl)


def read_pin(path):
    path = artifacts.local(path, exists=True)
    if artifacts.regular(path).st_size > artifacts.MAX_LOCK:
        raise ValueError('Publication-tool manifest is too large.')
    raw = path.read_bytes()
    return json.loads(raw, object_pairs_hook=artifacts._unique), sha256(raw).hexdigest()


def cached(destination, record, *, download=False, opener=None):
    artifacts._record(record, MAX_DOWNLOAD)
    approved_url(record['url'])
    destination = artifacts.local(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    artifacts.local(destination.parent, exists=True)
    if os.path.lexists(destination):
        artifacts.validate_artifact(destination, record)
        return destination
    if not download:
        raise FileNotFoundError('Missing offline publication tool: ' + str(destination))
    opener = opener or urllib.request.build_opener(ApprovedRedirects())
    with TemporaryDirectory(prefix='.publication-download-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'artifact'
        count = 0
        request = urllib.request.Request(approved_url(record['url']),
            headers={'User-Agent': 'Resonance-publication-conformance-setup/1'})
        with opener.open(request, timeout=30) as response, staged.open('xb') as stream:
            approved_url(response.url)
            while block := response.read(min(1024**2, record['size'] - count + 1)):
                count += len(block)
                if count > record['size']:
                    raise ValueError('Publication-tool response exceeds its pinned byte count.')
                stream.write(block)
            stream.flush(); os.fsync(stream.fileno())
        artifacts.validate_artifact(staged, record)
        try:
            os.link(staged, destination)
        except FileExistsError:
            # A concurrent writer keeps ownership. Only its exact pinned bytes
            # may satisfy this cache request; nothing is replaced.
            artifacts.validate_artifact(destination, record)
    artifacts.validate_artifact(destination, record)
    return destination


def notice_receipts(archive, pin):
    """Read only the bounded license notices from the verified archive."""
    receipts = {}
    with ZipFile(archive) as zipped:
        names = [entry.filename for entry in zipped.infolist()]
        if len(names) != len(set(names)) or len(names) > 4096:
            raise ValueError('Unexpected or duplicate validator archive entries.')
        for name in pin['notices']:
            artifacts.relative(name)
            entry = zipped.getinfo(name)
            if entry.flag_bits & 1 or not 0 < entry.file_size <= 1024**2:
                raise ValueError('The validator license notice exceeds its bounded size.')
            with zipped.open(entry) as stream:
                raw = stream.read(1024**2 + 1)
            if len(raw) != entry.file_size:
                raise ValueError('The validator license notice changed while being read.')
            receipts[name] = {'size': len(raw), 'sha256': sha256(raw).hexdigest()}
    return receipts


def prepare(output, *, download=False):
    epub, epub_pin = read_pin(ROOT / 'packaging' / 'epubcheck.json')
    schemas, schema_pin = read_pin(ROOT / 'packaging' / 'daisy-validation-schemas.json')
    if (epub.get('schema') != 1 or epub.get('name') != 'EPUBCheck' or epub.get('version') != '5.3.0'
            or epub.get('license') != 'BSD-3-Clause'
            or epub.get('notices') != ['epubcheck-5.3.0/LICENSE.txt','epubcheck-5.3.0/THIRD-PARTY.txt']
            or epub.get('url') != 'https://github.com/w3c/epubcheck/releases/download/v5.3.0/epubcheck-5.3.0.zip'
            or schemas.get('schema') != 1 or set(schemas.get('files', ())) != SCHEMAS):
        raise ValueError('Unsupported publication validator pin manifests.')
    output = artifacts.local(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    artifacts.local(output, exists=True)
    archive = cached(output / 'epubcheck-5.3.0.zip', epub, download=download)
    notices = notice_receipts(archive, epub)
    artifacts.validate_artifact(archive, epub)
    schema_root = output / 'daisy-schemas'
    files = {'epubcheck-5.3.0.zip': {'size': epub['size'], 'sha256': epub['sha256']}}
    for name, record in schemas['files'].items():
        # Names and the full source URLs are reviewed in the committed manifest.
        cached(schema_root / name, record, download=download)
        files['daisy-schemas/' + name] = {'size': record['size'], 'sha256': record['sha256']}
    return {'schema': 1, 'status': 'PASS', 'epubcheck_archive': str(archive),
            'daisy_schemas': str(schema_root), 'files': files, 'license_notices': notices,
            'manifest_sha256': {'epubcheck.json': epub_pin, 'daisy-validation-schemas.json': schema_pin},
            'execution': 'NONE', 'installation': 'NONE'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--download', action='store_true', help='Explicitly acquire the exact pinned public inputs')
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.output, download=args.download), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
