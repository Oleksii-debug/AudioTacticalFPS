"""CI-only acquisition of the exact public selected-converter qualification inputs.

No app-side downloader. Archives, documents and expanded runtimes remain in
RUNNER_TEMP; the receipt contains identities and selected paths only.
"""
import argparse
from hashlib import sha1, sha256
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK_BLOB = 'c5822c7ff56d5139da911ea153a8fdcd626d70dd'
MAXIMUM_DOWNLOAD = 1024**3
MAXIMUM_EXTRACTED = 4 * 1024**3
MAXIMUM_MEMBERS = 50000


def require(value, message):
    if not value:
        raise ValueError(message)


def snapshot(path, maximum):
    before = path.stat()
    require(path.is_file() and not path.is_symlink() and before.st_nlink == 1,
            'Qualification input is not an ordinary owned file.')
    require(0 <= before.st_size <= maximum, 'Qualification input exceeds its declared bound.')
    digest = sha256()
    count = 0
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            count += len(block)
            require(count <= maximum, 'Qualification input grew beyond its declared bound.')
            digest.update(block)
    after = path.stat()
    stable = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_nlink,
                           item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    require(stable(after) == stable(before) and count == before.st_size,
            'Qualification input changed while it was read.')
    return {'bytes': count, 'sha256': digest.hexdigest()}


def download(item, target, remaining, deadline):
    started = time.monotonic()
    read_bytes, final_hostname = 0, None
    outcome, exception_type = 'FAIL', None
    try:
        limit = min(item.get('maximum_bytes', item.get('bytes', 0)), remaining)
        require(type(limit) is int and 0 < limit <= MAXIMUM_DOWNLOAD, 'Invalid download budget.')
        require(urllib.parse.urlsplit(item['url']).scheme == 'https', 'Only pinned public HTTPS is allowed.')
        partial = target.with_suffix(target.suffix + '.partial')
        digest = sha256()
        # No caller headers, account token or proxy credential is forwarded.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        count = 0
        with opener.open(urllib.request.Request(item['url'], headers={
                'User-Agent': 'CARR-public-converter-qualification/1'}), timeout=30) as response:
            final_hostname = urllib.parse.urlsplit(response.geturl()).hostname
            require(urllib.parse.urlsplit(response.geturl()).scheme == 'https', 'Insecure runtime redirect.')
            length = response.headers.get('Content-Length')
            if length is not None:
                require(length.isdecimal() and int(length) <= limit, 'Oversized runtime response.')
            with partial.open('xb') as output:
                while block := response.read(min(1024 * 1024, limit - count + 1)):
                    read_bytes += len(block)
                    require(time.monotonic() < deadline, 'Public acquisition exceeded its total deadline.')
                    count += len(block)
                    require(count <= limit, 'Public download exceeded its declared bound.')
                    digest.update(block)
                    output.write(block)
        require(count > 0 and ('bytes' not in item or count == item['bytes']),
                'Public input has a different exact byte count.')
        if 'sha256' in item:
            require(digest.hexdigest() == item['sha256'], 'Public runtime archive hash differs.')
        else:
            raw = partial.read_bytes()
            require(sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
                    == item['git_blob_sha1'], 'Public fixture/notice Git blob differs.')
        # Target does not exist in this fresh owned directory.
        require(not target.exists(), 'Public input target already exists.')
        partial.rename(target)
        outcome = 'PASS'
        return {'bytes': count, 'sha256': digest.hexdigest()}
    except BaseException as error:
        exception_type = type(error).__name__[:64]
        raise
    finally:
        # Diagnostic only: no URL path/query, headers, body or exception text.
        # Keep both normal stdout and the final acquisition receipt unchanged.
        try:
            input_id = item.get('filename', item.get('name'))
            value = {
                'schema': 'carr-public-acquisition-diagnostic/1',
                'input_id': input_id[:128] if type(input_id) is str else '<invalid>',
                'elapsed_seconds': round(max(0.0, time.monotonic() - started), 6),
                'read_bytes': read_bytes,
                'final_hostname': final_hostname[:253] if final_hostname else None,
                'outcome': outcome,
                'exception_type': exception_type,
            }
            print('CARR_PUBLIC_ACQUISITION ' + json.dumps(value, ensure_ascii=True, sort_keys=True),
                  file=sys.stderr, flush=True)
        except Exception:
            # A diagnostic write must not replace the original transfer outcome.
            pass


def inventory(root):
    entries, size, files = 0, 0, []
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in (*directories, *names):
            path = Path(parent) / name
            info = path.lstat()
            entries += 1
            require(entries <= MAXIMUM_MEMBERS, 'Expanded runtime inventory exceeds its bound.')
            require(not path.is_symlink() and not getattr(info, 'st_file_attributes', 0) & 0x400,
                    'Expanded runtime contains a link or reparse point.')
            require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode),
                    'Expanded runtime contains an unsupported filesystem entry.')
            if stat.S_ISREG(info.st_mode):
                require(info.st_nlink == 1, 'Expanded runtime file has multiple links.')
                size += info.st_size
                require(size <= MAXIMUM_EXTRACTED, 'Expanded runtime exceeds its byte bound.')
                files.append(path)
    return files, entries, size


def extract_zip(archive, destination):
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        require(len(members) <= MAXIMUM_MEMBERS, 'ZIP member inventory exceeds its bound.')
        used, total = set(), 0
        for item in members:
            name = item.filename
            relative = PurePosixPath(name)
            require(name and '\\' not in name and '\0' not in name
                    and not relative.is_absolute() and not name.startswith('/')
                    and all(part not in ('', '.', '..') and ':' not in part
                            and not part.endswith((' ', '.')) for part in relative.parts),
                    'Unsafe runtime ZIP member.')
            mode = stat.S_IFMT(item.external_attr >> 16)
            require(not item.flag_bits & 1 and mode in (0, stat.S_IFREG, stat.S_IFDIR),
                    'Encrypted or linked runtime ZIP member.')
            reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                        *(f'lpt{i}' for i in range(1, 10))}
            require(all(not any(ord(character) < 32 for character in part)
                        and part.split('.', 1)[0].casefold() not in reserved for part in relative.parts),
                    'A runtime ZIP member has a Windows device or control-character alias.')
            key = '/'.join(relative.parts).casefold()
            require(key not in used, 'Duplicate or case-aliased runtime ZIP member.')
            used.add(key)
            total += item.file_size
            require(total <= MAXIMUM_EXTRACTED and item.file_size >= 0,
                    'Expanded runtime ZIP exceeds its byte bound.')
        for item in members:
            target = destination.joinpath(*PurePosixPath(item.filename).parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with source.open(item) as input_stream, target.open('xb') as output:
                while block := input_stream.read(1024 * 1024):
                    count += len(block)
                    require(count <= item.file_size, 'Expanded ZIP member exceeds its declaration.')
                    output.write(block)
            require(count == item.file_size, 'Expanded ZIP member is truncated.')


def extract_msi(archive, destination, log, ci):
    executable = Path(os.environ['SystemRoot']) / 'System32/msiexec.exe'
    require(executable.is_file(), 'The existing Windows MSI interpreter is unavailable.')
    with log.open('xb') as output:
        process = subprocess.Popen([str(executable), '/a', str(archive), '/qn',
            'TARGETDIR=' + str(destination), '/lwe', str(log.with_suffix('.msi.txt'))],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            code = process.wait(timeout=600)
        except BaseException:
            ci._terminate_process_tree(process)
            raise
    require(log.stat().st_size <= 1024**2 and code == 0,
            'Verified MSI administrative extraction failed.')
    msi_log = log.with_suffix('.msi.txt')
    require(not msi_log.exists() or msi_log.stat().st_size <= 8 * 1024**2,
            'Administrative extraction diagnostics exceed their bound.')


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    require(os.name == 'nt' and __debug__, 'Actual Windows and assertions are required.')
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    root = args.root.resolve()
    require(root.parent == temporary and not root.exists(),
            'Qualification acquisition needs a fresh direct child of RUNNER_TEMP.')
    receipt = args.receipt.resolve()
    require(not receipt.exists() and receipt.parent.is_dir(), 'Acquisition receipt must be fresh.')
    path = ROOT / 'packaging/windows-document-converters.lock.json'
    raw = path.read_bytes()
    require(len(raw) <= 64 * 1024 and sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
            == LOCK_BLOB, 'The exact selected runtime lock changed.')
    lock = json.loads(raw)
    require(lock['schema'] == 'carr-selected-document-runtime-lock/1'
            and len(lock['runtimes']) == 3 and len(lock['fixtures']) == 4,
            'The fixed public runtime/fixture inventory changed.')
    ci_path = ROOT / 'scripts/ci_check.py'
    ci_raw = ci_path.read_bytes()
    require(sha1(b'blob ' + str(len(ci_raw)).encode('ascii') + b'\0' + ci_raw).hexdigest()
            == '6ad3055cfcd7c6a1b1af8544ae462c5d51adb473', 'Existing cleanup helper source differs.')
    spec = importlib.util.spec_from_file_location('_existing_ci_native_prepare', ci_path)
    ci = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ci
    spec.loader.exec_module(ci)
    root.mkdir()
    downloads = root / 'downloads'
    downloads.mkdir()
    expanded = root / 'expanded'
    expanded.mkdir()
    results, remaining = {}, MAXIMUM_DOWNLOAD
    deadline = time.monotonic() + 600
    for item in (*lock['runtimes'], *lock['fixtures']):
        name = item.get('filename', item.get('name'))
        require(type(name) is str and Path(name).name == name and name not in results,
                'Invalid fixed public input name.')
        row = download(item, downloads / name, remaining, deadline)
        remaining -= row['bytes']
        results[name] = row
        print('VERIFIED public qualification input ' + name, flush=True)
    paths = {}
    combined_entries = combined_bytes = 0
    for item in lock['runtimes']:
        folder = expanded / item['name']
        folder.mkdir()
        archive = downloads / item['filename']
        require(snapshot(archive, item['maximum_bytes']) == results[item['filename']],
                'Verified runtime archive changed before extraction.')
        if archive.suffix == '.msi':
            extract_msi(archive, folder, root / (item['name'] + '-extraction.txt'), ci)
        else:
            extract_zip(archive, folder)
        files, entries, size = inventory(folder)
        combined_entries += entries
        combined_bytes += size
        require(combined_entries <= MAXIMUM_MEMBERS and combined_bytes <= MAXIMUM_EXTRACTED,
                'Combined expanded runtime inventory exceeds the declared lock.')
        name = {'calibre': 'ebook-convert.exe', 'libreoffice': 'soffice.com',
                'pandoc': 'pandoc.exe'}[item['name']]
        matches = [path for path in files if path.name.casefold() == name]
        require(len(matches) == 1, 'A unique selected runtime executable is required: ' + name)
        paths[item['name']] = str(matches[0].resolve(strict=True))
        require(snapshot(archive, item['maximum_bytes']) == results[item['filename']],
                'Verified runtime archive changed during extraction.')
    value = {'status': 'PASS', 'scope': 'Explicit CI acquisition only; native execution remains NOT_RUN.',
        'lock_git_blob': LOCK_BLOB, 'lock_sha256': sha256(raw).hexdigest(),
        'root': str(root), 'root_identity': [root.stat().st_dev, root.stat().st_ino],
        'downloads': str(downloads), 'selected': paths, 'inputs': results,
        'expanded_entries': combined_entries, 'expanded_bytes': combined_bytes}
    with receipt.open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=True, indent=2)
        output.write('\n')
    print(json.dumps(value, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
