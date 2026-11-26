"""Prepare the reviewed C++ supplement from one local hash-locked download.

Does not download, install, execute or publish a binary. The original Microsoft
licenses are copied unchanged; the package is for the account holder's private
offline installation. Public redistribution requires the applicable grant.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
from zipfile import ZipFile

import zstandard


HERE = Path(__file__).resolve().parent


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare(package, output):
    package, output = Path(package).absolute(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Use a new output directory.')
    sys.path.insert(0, str(HERE.parent / 'src'))
    from carr_library.native_runtime import MANIFEST, SOURCE_LOCK_SHA256, verify_native_runtime
    lock_path = HERE / 'native-runtime' / 'source-lock.json'
    if digest(lock_path) != SOURCE_LOCK_SHA256:
        raise ValueError('The reviewed source lock changed.')
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    if (package.is_symlink() or not package.is_file()
            or package.stat().st_size != lock['package']['size']
            or digest(package) != lock['package']['sha256']):
        raise ValueError('The downloaded package does not match the reviewed publisher checksum.')
    wanted = lock['selected_original_files']
    contents = {}
    with ZipFile(package) as archive:
        for name in archive.namelist():
            if not name.endswith('.tar.zst'):
                continue
            with archive.open(name) as compressed:
                with zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
                    with tarfile.open(fileobj=stream, mode='r|') as tar:
                        count, total = 0, 0
                        for member in tar:
                            count += 1
                            total += member.size
                            if count > 10000 or total > 64 * 1024 * 1024:
                                raise ValueError('The runtime archive exceeds its extraction limit.')
                            if member.name not in wanted:
                                continue
                            item = wanted[member.name]
                            if (not member.isfile() or member.name in contents
                                    or member.size != item['size']):
                                raise ValueError('Invalid selected runtime archive entry.')
                            data = tar.extractfile(member).read(item['size'] + 1)
                            if len(data) != item['size'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                                raise ValueError('A selected runtime file differs from the reviewed bytes.')
                            contents[member.name] = data
    if set(contents) != set(wanted):
        raise ValueError('The runtime archive is incomplete.')
    output.mkdir(parents=True, exist_ok=False)
    for name, data in contents.items():
        target = output / wanted[name]['destination']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (output / 'provenance').mkdir()
    for name in ('source-lock.json', 'pe-compatibility.json'):
        shutil.copyfile(HERE / 'native-runtime' / name, output / 'provenance' / name)
    shutil.copyfile(HERE / 'native-runtime' / 'README.txt', output / 'README.txt')
    manifest = dict(schema=1, target='Windows x64', use_scope='private-local-installation',
                    source_lock_sha256=SOURCE_LOCK_SHA256,
                    files={p.relative_to(output).as_posix(): digest(p)
                           for p in output.rglob('*') if p.is_file()})
    (output / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return verify_native_runtime(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    arguments = parser.parse_args(argv)
    print(json.dumps(prepare(arguments.package, arguments.output), indent=2))


if __name__ == '__main__':
    main()
