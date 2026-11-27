"""Fetch exact optional CI tools only; publisher mismatch is a hard failure.

The application never imports this helper. An explicit --download is required.
LibreOffice's authoring-time digest is a candidate: the publisher's fixed-release
Metalink must independently agree, including exact byte size, before use.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import urllib.parse
import urllib.request
from zipfile import ZipFile

from defusedxml import ElementTree

MAX_METADATA = 4 * 1024**2


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise ValueError('An optional-tool download attempted a non-HTTPS redirect.')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def read_https(url, maximum):
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise ValueError('A fixed HTTPS publisher URL is required.')
    opener = urllib.request.build_opener(HttpsRedirects())
    request = urllib.request.Request(url, headers={'User-Agent': 'CARR-explicit-qualification/1'})
    with opener.open(request, timeout=120) as response:
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('Publisher metadata exceeds its byte budget.')
    return raw


def publisher_identity(name, pin):
    raw = read_https(pin['publisher_metadata'], MAX_METADATA)
    if name == 'pandoc':
        release = json.loads(raw)
        assets = [row for row in release['assets'] if row.get('name') == pin['asset']]
        if (release.get('tag_name') != pin['version'] or release.get('draft')
                or release.get('prerelease') or len(assets) != 1):
            raise ValueError('The official Pandoc release differs from its exact pin.')
        asset = assets[0]
        if (asset.get('browser_download_url') != pin['url']
                or asset.get('digest') != 'sha256:' + pin['sha256']
                or asset.get('size') != pin['bytes']):
            raise ValueError('The official Pandoc asset identity differs from its pin.')
        size = pin['bytes']
    elif name == 'libreoffice':
        document = ElementTree.fromstring(raw)
        files = [node for node in document.iter() if node.tag.rsplit('}', 1)[-1] == 'file']
        if len(files) != 1 or files[0].get('name') != pin['asset']:
            raise ValueError('The official LibreOffice Metalink names a different artifact.')
        fields = {}
        for node in files[0]:
            tag = node.tag.rsplit('}', 1)[-1]
            if tag == 'size' or tag == 'hash' and node.get('type', '').lower() == 'sha-256':
                if tag in fields:
                    raise ValueError('Ambiguous publisher artifact identity.')
                fields[tag] = (node.text or '').strip()
        if fields.get('hash') != pin['sha256']:
            raise ValueError('The fixed LibreOffice candidate checksum is not confirmed by its publisher.')
        if not re.fullmatch('[1-9][0-9]{0,9}', fields.get('size', '')):
            raise ValueError('The publisher must provide an exact bounded artifact size.')
        size = int(fields['size'])
        if size > pin['maximum_bytes']:
            raise ValueError('The publisher artifact exceeds its fixed download budget.')
    else:
        raise ValueError('Undeclared optional runtime.')
    return size, sha256(raw).hexdigest()


def download(pin, path, size):
    if path.exists() or path.is_symlink():
        raise FileExistsError('Use a fresh optional-tool cache path.')
    opener = urllib.request.build_opener(HttpsRedirects())
    request = urllib.request.Request(pin['url'], headers={'User-Agent': 'CARR-explicit-qualification/1'})
    digest, count = sha256(), 0
    with opener.open(request, timeout=120) as response, path.open('xb') as output:
        while block := response.read(1024**2):
            count += len(block)
            if count > size:
                raise ValueError('Optional download exceeds its exact publisher byte size.')
            digest.update(block)
            output.write(block)
    if count != size or digest.hexdigest() != pin['sha256']:
        raise ValueError('Downloaded optional runtime differs from its publisher and fixed pin.')


def pandoc_payload(archive_path, destination, expected):
    destination.mkdir()
    names = set()
    count, total = 0, 0
    with ZipFile(archive_path) as archive:
        for item in archive.infolist():
            count += 1
            path = PurePosixPath(item.filename.rstrip('/'))
            if (count > 4096 or not item.filename or '\\' in item.filename
                    or path.is_absolute() or any(part in ('', '.', '..') for part in path.parts)
                    or any(PureWindowsPath(part).is_reserved() or part.rstrip(' .') != part
                           or ':' in part for part in path.parts)
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError('Unsafe or excessive Pandoc archive inventory.')
            key = path.as_posix().casefold()
            if key in names:
                raise ValueError('Ambiguous Pandoc archive inventory.')
            names.add(key)
            if item.is_dir():
                continue
            if not 0 <= item.file_size <= 256 * 1024**2:
                raise ValueError('Pandoc archive member exceeds its byte budget.')
            total += item.file_size
            if total > 512 * 1024**2:
                raise ValueError('Pandoc archive exceeds its expanded byte budget.')
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open('xb') as output:
                written = 0
                while block := source.read(1024**2):
                    written += len(block)
                    if written > item.file_size:
                        raise ValueError('Pandoc member exceeds its declared size.')
                    output.write(block)
                if written != item.file_size:
                    raise ValueError('Truncated Pandoc archive member.')
    executable = destination / expected
    info = executable.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('The exact selected Pandoc executable is absent.')
    return executable.resolve(strict=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--family', choices=('selected', 'office'), required=True)
    args = parser.parse_args()
    if os.name != 'nt' or not args.download:
        raise ValueError('This Windows CI preparer requires an explicit --download.')
    root = args.destination.absolute()
    root.mkdir(parents=True, exist_ok=False)
    lock_path = Path(__file__).with_name('conversion-runtimes.json')
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    if lock['schema'] != 1:
        raise ValueError('Unsupported optional-runtime lock.')
    report = {'schema': 1, 'status': 'PREPARING', 'lock_sha256': sha256(lock_path.read_bytes()).hexdigest(),
              'artifacts': {}, 'native_execution': 'NOT_RUN'}
    report_path = root / 'preparation.json'
    try:
        for name in (('pandoc',) if args.family == 'selected' else ('libreoffice',)):
            pin = lock[name]
            size, metadata = publisher_identity(name, pin)
            artifact = root / pin['asset']
            download(pin, artifact, size)
            report['artifacts'][name] = dict(path=str(artifact), bytes=size, sha256=pin['sha256'],
                publisher_url=pin['publisher_metadata'], publisher_metadata_sha256=metadata,
                publisher_identity='PASS', version=pin['version'])
        if args.family == 'selected':
            executable = pandoc_payload(Path(report['artifacts']['pandoc']['path']),
                                        root / 'pandoc', lock['pandoc']['executable'])
            report['pandoc'] = str(executable)
        else:
            report['libreoffice_msi'] = report['artifacts']['libreoffice']['path']
        report['status'] = 'PASS'
    except BaseException as error:
        report.update(status='FAIL', error_type=type(error).__name__, error=str(error)[:1000])
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
