"""Prepare the pinned private FFmpeg supplement from a selected local archive.

This program never downloads or executes binaries. It parses only a byte snapshot
whose size and SHA256 match the publisher release; original notices are retained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
from carr_library.audio.runtime import ARCHIVE_SHA256, FILES, MANIFEST, manifest, verify_runtime
from carr_library.audio.ffmpeg import _local_path


def prepare(archive, output):
    archive, output = _local_path(archive).absolute(), _local_path(output).absolute()
    lock = json.loads((HERE / 'windows-audio.lock.json').read_text(encoding='utf-8'))
    if lock['archive']['sha256'] != ARCHIVE_SHA256 or lock['selected_files'] != FILES:
        raise ValueError('The reviewed audio source lock changed.')
    if output.exists() or output.is_symlink():
        raise FileExistsError('Choose a new output directory for the private audio runtime.')
    named = archive.lstat()
    if not stat.S_ISREG(named.st_mode) or named.st_size != lock['archive']['size']:
        raise ValueError('The selected FFmpeg archive has an unexpected type or size.')
    with TemporaryDirectory(prefix='carr-audio-prepare-') as temporary:
        temporary = Path(temporary)
        snapshot = temporary / 'verified.zip'
        descriptor = os.open(archive, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, 'rb') as source, snapshot.open('xb') as target:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError('The selected FFmpeg archive changed before it was opened.')
            while data := source.read(1024 * 1024):
                size += len(data)
                if size > lock['archive']['size']:
                    raise ValueError('The FFmpeg archive exceeds its pinned size.')
                digest.update(data)
                target.write(data)
        if size != lock['archive']['size'] or digest.hexdigest() != ARCHIVE_SHA256:
            raise ValueError('The FFmpeg archive differs from the reviewed publisher bytes.')
        stage = temporary / 'payload'
        stage.mkdir()
        wanted = {lock['archive']['root'] + '/' + name: (name, entry) for name, entry in FILES.items()}
        with ZipFile(snapshot) as package:
            entries = package.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or not set(wanted).issubset(names):
                raise ValueError('The reviewed archive has duplicate or missing selected entries.')
            for entry in entries:
                if entry.filename not in wanted:
                    continue
                name, expected = wanted[entry.filename]
                mode = entry.external_attr >> 16
                if entry.is_dir() or stat.S_ISLNK(mode) or entry.flag_bits & 1 or entry.file_size != expected['size']:
                    raise ValueError('Invalid selected audio archive entry.')
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                check, count = hashlib.sha256(), 0
                with package.open(entry) as source, target.open('xb') as destination:
                    while data := source.read(1024 * 1024):
                        count += len(data)
                        if count > expected['size']:
                            raise ValueError('An audio archive entry exceeds its pinned size.')
                        check.update(data)
                        destination.write(data)
                if count != expected['size'] or check.hexdigest() != expected['sha256']:
                    raise ValueError('A selected audio executable or notice changed.')
        (stage / MANIFEST).write_text(json.dumps(manifest(), indent=2, sort_keys=True) + '\n', encoding='utf-8')
        verify_runtime(stage)
        output.parent.mkdir(parents=True, exist_ok=True)
        # copytree claims the destination with exclusive mkdir; it never replaces
        # an existing directory. A failed copy is visibly incomplete, not accepted.
        shutil.copytree(stage, output)
    return verify_runtime(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.archive, args.output), indent=2))


if __name__ == '__main__':
    main()
