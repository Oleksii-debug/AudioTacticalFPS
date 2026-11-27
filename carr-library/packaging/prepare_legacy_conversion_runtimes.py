"""Explicit pinned legacy converter provisioning for synthetic Windows CI only."""
import argparse
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
from time import monotonic

import zstandard

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('_carr_optional_preparer', HERE / 'prepare_conversion_runtimes.py')
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


def run(args, output):
    command = [str(value) for value in args]
    receipt = output.with_name(output.stem + '-command.json')
    details = dict(schema=1, status='RUNNING', argv=command, timeout_seconds=600)
    started = monotonic()
    receipt.write_text(json.dumps(details, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(details, sort_keys=True), flush=True)
    try:
        with output.open('xb') as stream:
            result = subprocess.run(command, stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT, timeout=600, check=False)
        details['returncode'] = result.returncode
        if result.returncode:
            raise ValueError('The pinned runtime could not be prepared: ' + str(result.returncode))
        details['status'] = 'PASS'
    except BaseException as error:
        details.update(status='FAIL', error_type=type(error).__name__, error=str(error)[:2000])
        raise
    finally:
        details['elapsed_seconds'] = round(monotonic() - started, 3)
        receipt.write_text(json.dumps(details, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        print(json.dumps(details, sort_keys=True), flush=True)
        if details['status'] != 'PASS' and output.is_file():
            with output.open('rb') as stream:
                stream.seek(max(0, output.stat().st_size - 16000))
                print(stream.read(16000).decode('utf-8', errors='replace'), flush=True)


def artifact(name, pin, destination):
    if name in ('calibre', 'seven_zip'):
        raw = common.read_https(pin['publisher'], common.MAX_METADATA)
        metadata = json.loads(raw)
        expected_tag = 'v' + pin['version'] if name == 'calibre' else pin['version']
        matches = [row for row in metadata['assets'] if row.get('browser_download_url') == pin['url']]
        if (metadata.get('tag_name') != expected_tag or metadata.get('draft') or metadata.get('prerelease')
                or len(matches) != 1 or matches[0].get('digest') != 'sha256:' + pin['sha256']
                or matches[0].get('size') != pin['bytes']):
            raise ValueError('Official legacy-tool asset metadata differs from the fixed release pin.')
        common.download(pin, destination, pin['bytes'])
        size = pin['bytes']
    else:
        # The MSYS2 publisher page supplied the exact source pin when authored.
        # Keep a fixed version URL and checksum even after its latest page moves.
        raw = common.read_https(pin['url'], pin['maximum_bytes'])
        if not raw or sha256(raw).hexdigest() != pin['sha256']:
            raise ValueError('MSYS2 DjVuLibre differs from its publisher checksum.')
        destination.write_bytes(raw)
        size = len(raw)
    return dict(url=pin['url'], bytes=size, sha256=pin['sha256'], version=pin['version'],
                provider=pin.get('provider', 'upstream official release'),
                status='PINNED_BYTES_VERIFIED')


def djvu_payload(package, output, ocr):
    from carr_library.extract.ocr_runtime import verify_ocr_bundle
    runtime = verify_ocr_bundle(ocr)
    output.mkdir()
    required = {'ucrt64/bin/' + name for name in (
        'djvutxt.exe', 'djvused.exe', 'cjb2.exe', 'djvm.exe', 'libdjvulibre-21.dll')}
    found, count, total = set(), 0, 0
    with package.open('rb') as source, zstandard.ZstdDecompressor().stream_reader(source) as decoded:
        with tarfile.open(fileobj=decoded, mode='r|') as archive:
            for member in archive:
                count += 1
                total += member.size
                if count > 4096 or total > 128 * 1024**2:
                    raise ValueError('Pinned DjVuLibre archive exceeds its expansion budget.')
                selected = member.name in required or member.name.startswith('ucrt64/share/djvu/osi/')
                if not selected or member.isdir():
                    continue
                path = PurePosixPath(member.name)
                if (not member.isfile() or member.name in found or not 0 <= member.size <= 64 * 1024**2
                        or any(part in ('', '.', '..') for part in path.parts) or '\\' in member.name):
                    raise ValueError('Unsafe selected DjVuLibre archive component.')
                found.add(member.name)
                if member.name.startswith('ucrt64/bin/'):
                    relative = Path(path.name)
                else:
                    relative = Path(*path.parts[1:])
                target = output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as read, target.open('xb') as write:
                    raw = read.read(member.size + 1)
                    if len(raw) != member.size:
                        raise ValueError('Truncated selected DjVuLibre component.')
                    write.write(raw)
    if not required <= found:
        raise ValueError('The pinned DjVuLibre package lacks required tools or library.')
    copied = {}
    for path in sorted(ocr.glob('*.dll')):
        raw = path.read_bytes()
        target = output / path.name
        if target.exists():
            raise ValueError('Ambiguous native dependency component.')
        target.write_bytes(raw)
        copied[path.name] = sha256(raw).hexdigest()
    if not copied:
        raise ValueError('The reviewed native dependency closure is absent.')
    return dict(ocr_manifest_sha256=runtime.manifest_sha256,
                dependency_files=copied, package_components=sorted(found))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', choices=('calibre', 'djvu', 'chm'), required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--ocr-root', type=Path)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt' or not args.download:
        raise ValueError('Explicit Windows CI provisioning requires --download.')
    root = args.destination.absolute()
    root.mkdir(parents=True, exist_ok=False)
    lock_path = HERE / 'legacy-conversion-runtimes.json'
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    report = dict(schema=1, status='PREPARING', family=args.family,
                  lock_sha256=sha256(lock_path.read_bytes()).hexdigest(), native_fixtures='NOT_RUN')
    def checkpoint(phase):
        report['phase'] = phase
        (root / 'preparation.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        print(json.dumps({'phase': phase, 'status': report['status'], 'family': args.family}, sort_keys=True), flush=True)
    checkpoint('preflight')
    try:
        payload = root / 'payload'
        if args.family == 'calibre':
            # Upstream v9.19.0 portable-installer.cpp opens a modal error and
            # folder picker above 58 UTF-16 units, even with its argv[1] path.
            # Refuse before downloading or starting that GUI. CI supplies a
            # short, fresh owned directory under the runner's temporary root.
            portable_path = str(payload / 'Calibre Portable')
            units = len(portable_path.encode('utf-16-le')) // 2
            report['portable_destination'] = portable_path
            report['portable_destination_utf16_units'] = units
            if units > 58:
                raise ValueError('The fixed calibre portable installer needs a destination of at most 58 UTF-16 units; use a shorter fresh CI work directory.')
        key = {'calibre': 'calibre', 'djvu': 'djvulibre', 'chm': 'seven_zip'}[args.family]
        pin = lock[key]
        package = root / pin['url'].rsplit('/', 1)[1]
        checkpoint('verify-publisher-artifact')
        report['artifact'] = artifact(key, pin, package)
        checkpoint('prepare-verified-runtime')
        if args.family == 'calibre':
            run([package, str(payload)], root / 'prepare-native.txt')
            matches = list(payload.rglob('ebook-convert.exe'))
            if len(matches) != 1:
                raise ValueError('The pinned portable package must produce one actual calibre runtime.')
            executable = matches[0]
        elif args.family == 'chm':
            payload.mkdir()
            installer = Path(os.environ['SYSTEMROOT']) / 'System32/msiexec.exe'
            run([installer, '/a', package, '/qn', '/norestart', 'TARGETDIR=' + str(payload),
                 '/l*v', root / 'prepare-msi.log'], root / 'prepare-native.txt')
            matches = list(payload.rglob('7z.exe'))
            if len(matches) != 1:
                raise ValueError('The pinned MSI must produce one actual 7-Zip runtime.')
            executable = matches[0]
            help_file = executable.with_name('7-zip.chm')
            if not help_file.is_file():
                raise ValueError('The publisher help fixture is missing from its verified release.')
            report['help_fixture'] = str(help_file)
        else:
            if args.ocr_root is None:
                raise ValueError('DjVuLibre needs the explicitly prepared reviewed dependency closure.')
            report['dependencies'] = djvu_payload(package, payload, args.ocr_root.resolve(strict=True))
            executable = payload / 'djvutxt.exe'
        info = executable.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_size:
            raise ValueError('The selected executable is not a regular nonempty prepared file.')
        report.update(status='PASS', executable=str(executable.resolve(strict=True)),
                      runtime_root=str(executable.parent.resolve(strict=True)))
    except BaseException as error:
        report.update(status='FAIL', error_type=type(error).__name__, error=str(error)[:1000])
        raise
    finally:
        (root / 'preparation.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
