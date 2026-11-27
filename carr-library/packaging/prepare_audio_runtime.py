"""Prepare the reviewed local FFmpeg archive and sources; never download or run it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
from carr_library import audio_runtime as runtime
from carr_library.native_runtime import _absolute, _directory, _entry, _plain


def _copy_file(source, target, record):
    info = _plain(source)
    if info.st_size != record['size']:
        raise ValueError('Audio source size differs: ' + source.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, total = hashlib.sha256(), 0
    with source.open('rb') as src, target.open('xb') as dst:
        while chunk := src.read(1024 * 1024):
            total += len(chunk)
            if total > record['size']:
                raise ValueError('Audio source grew during copy.')
            digest.update(chunk)
            dst.write(chunk)
    if total != record['size'] or digest.hexdigest() != record['sha256']:
        raise ValueError('Audio source changed or has the wrong checksum: ' + source.name)


def prepare(archive_path, sources_dir, output):
    """Create one fresh runtime directory from exact caller-supplied inputs."""
    archive_path, sources_dir, output = map(_absolute, (archive_path, sources_dir, output))
    _directory(sources_dir)
    _directory(output.parent)
    if os.path.lexists(output):
        raise ValueError('Audio runtime output must be a new directory.')
    lock_path = HERE / 'audio-runtime/source-lock.json'
    lock, lock_size = runtime._source_lock(lock_path)
    archive_info = _plain(archive_path)
    if archive_info.st_size != lock['archive']['size']:
        raise ValueError('The FFmpeg download differs from the pinned upstream archive.')
    prefix = lock['archive']['root'] + '/'
    with tempfile.TemporaryDirectory(prefix='carr-audio-prepare-', dir=output.parent) as scratch:
        staged = Path(scratch) / 'audio-runtime'
        staged.mkdir()
        snapshot = Path(scratch) / 'verified-upstream.zip'
        _copy_file(archive_path, snapshot, lock['archive'])
        with zipfile.ZipFile(snapshot) as archive:
            actual, folded = {}, set()
            for info in archive.infolist():
                if not info.filename.startswith(prefix) or info.orig_filename != info.filename or info.flag_bits & 1:
                    raise ValueError('Unexpected or encrypted archive entry.')
                relative = info.filename[len(prefix):].rstrip('/')
                if not relative:
                    if not info.is_dir():
                        raise ValueError('Invalid FFmpeg archive root.')
                    continue
                _entry(relative, '0' * 64)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if info.is_dir():
                    if mode not in (0, stat.S_IFDIR):
                        raise ValueError('Unsupported FFmpeg archive directory type.')
                    continue
                if mode not in (0, stat.S_IFREG) or relative.casefold() in folded:
                    raise ValueError('Linked or duplicate FFmpeg archive file.')
                folded.add(relative.casefold())
                actual[relative] = info
            if actual.keys() != lock['archive_files'].keys():
                raise ValueError('FFmpeg archive inventory differs from the reviewed build.')
            for name, record in lock['archive_files'].items():
                info = actual[name]
                if info.file_size != record['size']:
                    raise ValueError('FFmpeg archive file size differs.')
                target = staged / name
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, count = hashlib.sha256(), 0
                with archive.open(info) as src, target.open('xb') as dst:
                    while chunk := src.read(1024 * 1024):
                        count += len(chunk)
                        if count > record['size']:
                            raise ValueError('FFmpeg archive entry exceeds the reviewed size.')
                        digest.update(chunk)
                        dst.write(chunk)
                if count != record['size'] or digest.hexdigest() != record['sha256']:
                    raise ValueError('FFmpeg archive entry checksum failed: ' + name)
        for name, record in lock['extra_files'].items():
            source = (HERE / 'audio-runtime' / name if name.startswith(('licenses/', 'provenance/'))
                      or name == 'README.txt' else sources_dir / Path(name).name)
            _copy_file(source, staged / name, record)
        _copy_file(lock_path, staged / 'provenance/source-lock.json',
                   {'size': lock_size, 'sha256': runtime.SOURCE_LOCK_SHA256})
        inventory = {name: entry['sha256'] for section in ('archive_files', 'extra_files')
                     for name, entry in lock[section].items()}
        inventory['provenance/source-lock.json'] = runtime.SOURCE_LOCK_SHA256
        manifest = {'schema': 1, 'target': 'Windows x64', 'package_id': runtime.PACKAGE_ID,
                    'source_lock_sha256': runtime.SOURCE_LOCK_SHA256, 'files': inventory}
        (staged / runtime.MANIFEST).write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        report = runtime.verify_audio_runtime(staged)
        # Reserve the destination exclusively. Publish the acceptance manifest
        # last; an interruption can never expose a seemingly verified runtime.
        output.mkdir(exist_ok=False)
        try:
            for path in staged.iterdir():
                if path.name != runtime.MANIFEST:
                    shutil.move(str(path), output / path.name)
            shutil.move(str(staged / runtime.MANIFEST), output / runtime.MANIFEST)
        except BaseException:
            shutil.rmtree(output)
            raise
    report['tools'] = {name: str(output / value) for name, value in runtime.TOOLS.items()}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--sources-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.archive, args.sources_dir, args.output), sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
