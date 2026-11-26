"""Verify the reviewed portable MSYS2 OCR runtime without executing it.

The approved manifest pins the original binary packages, the actual PE import
closure, all copied files, corresponding source archives and four model files.
This verifier neither installs packages nor builds or downloads OCR software.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat


APPROVED_MANIFEST = Path(__file__).with_name('windows-ocr.json')
MAX_MANIFEST = 2 * 1024 * 1024


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('OCR files must be regular, unlinked files.')
    return info


def _manifest(path):
    path = Path(path)
    if _regular(path).st_size > MAX_MANIFEST:
        raise ValueError('OCR manifest exceeds the supported limit.')
    result = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(result, dict) or result.get('schema') != 1 or not result.get('files'):
        raise ValueError('Invalid OCR runtime manifest.')
    return result


def verify_runtime(tree, manifest):
    """Reject changed provenance, missing/extra files, links and changed bytes."""
    approved = _manifest(APPROVED_MANIFEST)
    supplied = _manifest(manifest)
    if supplied != approved:
        raise ValueError('OCR manifest is not the reviewed pinned runtime.')
    root = Path(tree).absolute()
    for path in (root, *root.parents):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('OCR runtime directories must not be linked.')
    expected = approved['files']
    if not isinstance(expected, dict):
        raise ValueError('Invalid OCR file inventory.')
    actual = set()
    for path in root.rglob('*'):
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('OCR runtime paths must not be linked.')
        if stat.S_ISDIR(info.st_mode):
            continue
        _regular(path)
        actual.add(path.relative_to(root).as_posix())
    if actual != set(expected):
        raise ValueError('OCR runtime contains missing or unexpected files.')
    total = 0
    for name, record in expected.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or not relative.parts or relative.as_posix() != name
                or not isinstance(record, dict) or set(record) != {'sha256', 'size'}
                or type(record['size']) is not int or record['size'] < 1):
            raise ValueError('Invalid pinned OCR file identity.')
        path = root.joinpath(*relative.parts)
        if _regular(path).st_size != record['size']:
            raise ValueError('OCR runtime file size changed: ' + name)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != record['sha256']:
            raise ValueError('OCR runtime file checksum changed: ' + name)
        total += record['size']
    return {'files': len(actual), 'bytes': total, 'engine': approved['engine'],
            'windows_execution': 'NOT_RUN'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify_runtime(args.directory, args.manifest), sort_keys=True))


if __name__ == '__main__':
    main()
