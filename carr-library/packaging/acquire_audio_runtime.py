"""Explicit setup-only acquisition of the three reviewed public FFmpeg inputs.

The desktop never calls this helper. Without --download it prints a plan only.
No model, voice, credential or user document is transferred.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
from carr_library import audio_runtime
from carr_library.native_runtime import _absolute, _directory, _plain, _sha


def inputs():
    path = HERE / 'audio-runtime/source-lock.json'
    value, _size = audio_runtime._source_lock(path)
    return [{key: item[key] for key in ('name', 'url', 'size', 'sha256')}
            for item in (value['archive'], *value['source_packages'])]


def download_one(record, directory):
    """Never overwrite an existing input, including a previously damaged one."""
    directory = _absolute(directory)
    _directory(directory)
    name = record['name']
    if not name or Path(name).name != name or '/' in name or '\\' in name or ':' in name:
        raise ValueError('Invalid locked download filename.')
    destination = directory / name
    if os.path.lexists(destination):
        info = _plain(destination)
        if info.st_size != record['size'] or _sha(destination) != record['sha256']:
            raise ValueError('Existing audio input differs; choose another directory: ' + name)
        return dict(record, local_path=str(destination), reused=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=name + '.', suffix='.partial', dir=directory, delete=False) as target:
            temporary = Path(target.name)
            digest, count = hashlib.sha256(), 0
            request = urllib.request.Request(record['url'], headers={'User-Agent': 'CARR-explicit-audio-setup/1'})
            with urllib.request.urlopen(request, timeout=45) as response:
                if not response.geturl().startswith('https://'):
                    raise ValueError('Audio download was redirected to an insecure transport.')
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > record['size']:
                        raise ValueError('Audio download exceeds its pinned size.')
                    digest.update(chunk)
                    target.write(chunk)
            if count != record['size'] or digest.hexdigest() != record['sha256']:
                raise ValueError('Audio download checksum or size differs: ' + name)
            target.flush()
            os.fsync(target.fileno())
        os.link(temporary, destination)
        return dict(record, local_path=str(destination), reused=False)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--download', action='store_true', help='Explicitly fetch the pinned public setup inputs.')
    args = parser.parse_args()
    records = inputs()
    if not args.download:
        print(json.dumps({'status': 'PLAN_ONLY', 'files': records, 'total_bytes': sum(r['size'] for r in records)}, indent=2))
        return 0
    output = _absolute(args.output)
    _directory(output.parent)
    output.mkdir(exist_ok=True)
    results = [download_one(record, output) for record in records]
    print(json.dumps({'status': 'PASS', 'source_lock_sha256': audio_runtime.SOURCE_LOCK_SHA256,
                      'files': results, 'native_execution': 'NOT_RUN'}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
