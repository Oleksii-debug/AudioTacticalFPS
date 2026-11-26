"""Reproduce the pinned offline Windows OCR payload without executing it.

Requires Python 3.11+ and zstandard. With --download, reads only the exact HTTPS
URLs in the reviewed lock. It never installs packages or executes their contents.
The default is offline: every locked artifact must already be in --cache.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
from tempfile import TemporaryDirectory
import urllib.parse
import urllib.request

import zstandard

MAX_LOCK = 4 * 1024 * 1024
MAX_ARTIFACT = 400 * 1024 * 1024
MAX_UNPACKED = 700 * 1024 * 1024


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Invalid relative path in OCR lock.')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')):
        raise ValueError('Invalid relative path in OCR lock.')
    return path


def regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('OCR input must be a regular local file: ' + str(path))
    return info


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def validate(path, record):
    if type(record.get('size')) is not int or not 0 < record['size'] <= MAX_ARTIFACT:
        raise ValueError('Invalid artifact size in OCR lock.')
    if regular(path).st_size != record['size'] or digest(path) != record['sha256']:
        raise ValueError('OCR artifact differs from reviewed lock: ' + path.name)


def cached_artifact(cache, record, download):
    name = safe_relative(record['filename'])
    path = cache.joinpath(*name.parts)
    if not path.exists():
        if not download:
            raise FileNotFoundError('Missing offline OCR input: ' + str(path))
        url = urllib.parse.urlsplit(record['url'])
        if (url.scheme != 'https' or url.username or url.password
                or url.hostname not in ('repo.msys2.org', 'raw.githubusercontent.com')):
            raise ValueError('OCR artifact URL must be an approved HTTPS primary source.')
        path.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='ocr-download-', dir=path.parent) as scratch:
            temporary = Path(scratch) / 'artifact'
            count = 0
            with urllib.request.urlopen(record['url'], timeout=60) as response, temporary.open('xb') as stream:
                while block := response.read(1024 * 1024):
                    count += len(block)
                    if count > record['size'] or count > MAX_ARTIFACT:
                        raise ValueError('OCR download exceeds locked size.')
                    stream.write(block)
            validate(temporary, record)
            if path.exists():
                validate(path, record)
            else:
                # Destination is a build cache, never a library/data directory.
                temporary.rename(path)
    validate(path, record)
    return path


class BoundedReader:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.count = stream, limit, 0

    def read(self, size=-1):
        if size < 0:
            size = self.limit - self.count + 1
        data = self.stream.read(min(size, self.limit - self.count + 1))
        self.count += len(data)
        if self.count > self.limit:
            raise ValueError('OCR package decompression exceeds its work limit.')
        return data


def selected_archive_members(path, names):
    """Read named regular entries only; no extraction paths or hooks are used."""
    wanted = set(names)
    result = {}
    with path.open('rb') as raw, zstandard.ZstdDecompressor().stream_reader(raw) as reader:
        with tarfile.open(fileobj=BoundedReader(reader, MAX_UNPACKED), mode='r|') as archive:
            for member in archive:
                if member.name not in wanted:
                    continue
                safe_relative(member.name)
                if not member.isfile() or member.size > MAX_ARTIFACT or member.name in result:
                    raise ValueError('Invalid or duplicate selected OCR archive entry.')
                result[member.name] = archive.extractfile(member).read(member.size + 1)
                if len(result[member.name]) != member.size:
                    raise ValueError('Truncated OCR archive member.')
    if set(result) != wanted:
        raise ValueError('Locked OCR archive entry is missing.')
    return result


def nested_member(data, name):
    safe_relative(name)
    with tarfile.open(fileobj=BytesIO(data), mode='r:*') as archive:
        matches = [m for m in archive.getmembers() if m.name == name]
        if len(matches) != 1 or not matches[0].isfile() or matches[0].size > MAX_ARTIFACT:
            raise ValueError('Invalid selected nested source notice.')
        return archive.extractfile(matches[0]).read()


def _write_source_archive(target, specification, artifacts):
    """Fixed order, USTAR, mtime=0, uid/gid=0, empty owners; no recompression."""
    with tarfile.open(target, 'w', format=tarfile.USTAR_FORMAT) as archive:
        seen = set()
        for entry in specification['entries']:
            name = str(safe_relative(entry['name']))
            if name in seen:
                raise ValueError('Duplicate corresponding-source entry.')
            seen.add(name)
            info = tarfile.TarInfo(name)
            info.mode, info.mtime = 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            if 'artifact' in entry:
                path = artifacts[entry['artifact']]
                info.size = path.stat().st_size
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
            else:
                data = entry['text'].encode('utf-8')
                info.size = len(data)
                archive.addfile(info, BytesIO(data))
    validate(target, specification)


def prepare(lock_path, cache, output, source_archive, *, download=False):
    """Build exact normalized runtime and corresponding source; never overwrite."""
    if regular(lock_path).st_size > MAX_LOCK:
        raise ValueError('OCR lock is too large.')
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    if lock.get('schema') != 1 or lock.get('target') != 'win_amd64':
        raise ValueError('Unsupported OCR build lock.')
    source_specification = lock.get('source_archive')
    if (not isinstance(source_specification, dict)
            or source_specification.get('filename') != 'ocr-corresponding-source.tar'):
        raise ValueError('Invalid corresponding-source archive filename in OCR lock.')
    if os.path.lexists(output) or os.path.lexists(source_archive):
        raise FileExistsError('OCR output and source archive must be new paths.')
    cache.mkdir(parents=True, exist_ok=True)
    artifacts = {name: cached_artifact(cache, record, download)
                 for name, record in lock['artifacts'].items()}
    selected = defaultdict(set)
    for record in lock['output_files'].values():
        if 'member' in record:
            selected[record['artifact']].add(record['member'])
    members = {name: selected_archive_members(artifacts[name], wanted)
               for name, wanted in selected.items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    source_archive.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='ocr-runtime-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'ocr'
        staged.mkdir()
        for name, record in lock['output_files'].items():
            relative = safe_relative(name)
            destination = staged.joinpath(*relative.parts)
            if 'text' in record:
                data = record['text'].encode('utf-8')
            elif 'member' in record:
                data = members[record['artifact']][record['member']]
                if 'nested_member' in record:
                    data = nested_member(data, record['nested_member'])
            else:
                data = artifacts[record['artifact']].read_bytes()
            if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
                raise ValueError('Normalized OCR output differs from reviewed bytes: ' + name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        manifest = lock['manifest']
        expected_files = {name: {key: record[key] for key in ('size', 'sha256')}
                          for name, record in lock['output_files'].items()}
        if manifest['files'] != expected_files:
            raise ValueError('OCR runtime manifest does not match normalized output.')
        (staged / 'ocr-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        with TemporaryDirectory(prefix='ocr-source-', dir=source_archive.parent) as source_tmp:
            staged_source = Path(source_tmp) / source_specification['filename']
            _write_source_archive(staged_source, source_specification, artifacts)
            if os.path.lexists(output) or os.path.lexists(source_archive):
                raise FileExistsError('OCR destination appeared during preparation.')
            staged_source.rename(source_archive)
            staged.rename(output)
    return {'engine_version': lock['engine_version'], 'runtime_files': len(expected_files),
            'runtime_bytes': sum(r['size'] for r in expected_files.values()),
            'source_archive_sha256': digest(source_archive), 'windows_execution': 'NOT_RUN'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=Path(__file__).with_name('windows-ocr.lock.json'))
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-archive', type=Path, required=True)
    parser.add_argument('--download', action='store_true', help='Fetch exact missing locked artifacts over HTTPS.')
    args = parser.parse_args()
    result = prepare(args.lock, args.cache, args.output, args.source_archive, download=args.download)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
