"""Explicit bounded component identity for optional legacy reading tools.

No PATH lookup, executable discovery, installation or download is performed.
This binds the selected installation, not operating-system libraries or an OS
sandbox. The outer existing conversion worker owns every native descendant.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat
import time

from ..audio.jobs import checked_directory, file_identity, regular_file, verified_reader
from .models import ConversionError, ConversionUnavailable

MAX_RUNTIME_FILES = 8192
MAX_RUNTIME_BYTES = 2 * 1024**3
MAX_COMPONENT_BYTES = 512 * 1024**2
MAX_INVENTORY_BYTES = 2 * 1024**2
INVENTORY_NAME = 'legacy-runtime.json'
EXECUTABLES = {
    'calibre': ('ebook-convert.exe', 'ebook-convert'),
    'djvu': ('djvutxt.exe', 'djvutxt'),
    'chm': ('7z.exe', '7zz', '7z'),
}


def _check(cancelled, deadline=None):
    if cancelled():
        raise InterruptedError('Selected legacy conversion was cancelled.')
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('Selected legacy conversion exceeded its time budget.')


def _relative(name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or PurePosixPath(name).is_absolute()
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ConversionError('Invalid selected runtime component path.')
    return name


def _inventory(root, cancelled, deadline=None):
    root = checked_directory(root)
    pending, result, aliases, total, count = [(root, 0)], {}, set(), 0, 0
    while pending:
        directory, depth = pending.pop()
        _check(cancelled, deadline)
        with os.scandir(directory) as items:
            for entry in items:
                _check(cancelled, deadline)
                count += 1
                if count > MAX_RUNTIME_FILES or depth > 16:
                    raise ConversionUnavailable('Selected runtime exceeds its entry/depth budget.')
                path = Path(entry.path)
                # Windows DirEntry.stat caches st_ino/st_dev/st_nlink as zero.
                # Use a fresh non-following filesystem result for the actual
                # independent-file identity and retain every reparse/link guard.
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ConversionUnavailable('Selected runtime contains a linked or redirected component.')
                name = _relative(path.relative_to(root).as_posix())
                key = name.casefold()
                if key in aliases:
                    raise ConversionUnavailable('Selected runtime has ambiguous component names.')
                aliases.add(key)
                if stat.S_ISDIR(info.st_mode):
                    checked_directory(path)
                    pending.append((path, depth + 1))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    if not 0 <= info.st_size <= MAX_COMPONENT_BYTES:
                        raise ConversionUnavailable('Selected runtime component exceeds its byte budget.')
                    total += info.st_size
                    if total > MAX_RUNTIME_BYTES:
                        raise ConversionUnavailable('Selected runtime exceeds its total byte budget.')
                    result[name] = file_identity(regular_file(path))
                else:
                    raise ConversionUnavailable('Selected runtime contains an unsupported component.')
    if not result:
        raise ConversionUnavailable('Selected runtime is empty.')
    return result


def _required(root, executable, kind, names):
    if kind not in EXECUTABLES or executable not in EXECUTABLES[kind] or executable not in names:
        raise ConversionUnavailable('Choose the actual executable of the requested legacy tool.')
    if names[executable][2] <= 0:
        raise ConversionUnavailable('The selected executable must not be empty.')
    if kind == 'djvu':
        companion = 'djvused.exe' if executable.endswith('.exe') else 'djvused'
        if companion not in names:
            raise ConversionUnavailable('The selected DjVuLibre installation lacks its page-inventory companion.')
    if kind == 'calibre':
        debug = 'calibre-debug.exe' if executable.endswith('.exe') else 'calibre-debug'
        if debug not in names:
            raise ConversionUnavailable('The selected calibre installation lacks its companion interpreter.')
    if kind == 'chm' and executable == '7z.exe' and '7z.dll' not in names:
        raise ConversionUnavailable('The selected 7-Zip installation lacks its bound archive component.')


@dataclass(frozen=True)
class ToolSelection:
    path: Path
    kind: str
    root_identity: tuple
    files: tuple

    @classmethod
    def capture(cls, path, kind):
        path = Path(path)
        if not path.is_absolute():
            raise ConversionUnavailable('Select an absolute local legacy-tool executable explicitly.')
        root = checked_directory(path.parent)
        names = _inventory(root, lambda: False)
        _required(root, path.name, kind, names)
        info = root.stat()
        return cls(path, kind, (info.st_dev, info.st_ino), tuple(sorted(names.items())))

    def snapshot(self, stage, *, cancelled=lambda: False):
        root = checked_directory(self.path.parent)
        info = root.stat()
        expected = dict(self.files)
        if (info.st_dev, info.st_ino) != self.root_identity or _inventory(root, cancelled) != expected:
            raise ConversionError('The selected legacy runtime changed after selection.')
        rows = []
        for name, identity in self.files:
            _check(cancelled)
            digest = sha256()
            with verified_reader(root / name, expected_identity=identity) as stream:
                while block := stream.read(1024**2):
                    _check(cancelled)
                    digest.update(block)
            rows.append(dict(name=name, bytes=identity[2], sha256=digest.hexdigest(),
                             identity=list(identity)))
        if _inventory(root, cancelled) != expected:
            raise ConversionError('The selected legacy runtime changed during its snapshot.')
        value = dict(schema='carr-selected-legacy-runtime/1', kind=self.kind,
                     root=str(root), root_identity=list(self.root_identity),
                     executable=self.path.name, files=rows)
        raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw) > MAX_INVENTORY_BYTES:
            raise ConversionError('The selected runtime identity exceeds its manifest budget.')
        path = checked_directory(stage) / INVENTORY_NAME
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return dict(path=str(path), bytes=len(raw), sha256=sha256(raw).hexdigest())


def _load(selection, stage, kind, deadline):
    if (type(selection) is not dict or set(selection) != {'path', 'bytes', 'sha256'}
            or selection['path'] != str(stage / INVENTORY_NAME)
            or type(selection['bytes']) is not int or not 1 <= selection['bytes'] <= MAX_INVENTORY_BYTES
            or type(selection['sha256']) is not str):
        raise ConversionError('Invalid private legacy-runtime selection.')
    _check(lambda: False, deadline)
    path = stage / INVENTORY_NAME
    info = regular_file(path)
    if info.st_nlink != 1 or info.st_size != selection['bytes']:
        raise ConversionError('Private legacy-runtime inventory changed.')
    with verified_reader(path, expected_identity=file_identity(info)) as stream:
        raw = stream.read(MAX_INVENTORY_BYTES + 1)
    if len(raw) != selection['bytes'] or sha256(raw).hexdigest() != selection['sha256']:
        raise ConversionError('Private legacy-runtime inventory checksum changed.')
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {'schema', 'kind', 'root', 'root_identity', 'executable', 'files'}
            or value['schema'] != 'carr-selected-legacy-runtime/1' or value['kind'] != kind
            or type(value['root']) is not str or not Path(value['root']).is_absolute()
            or type(value['root_identity']) is not list or len(value['root_identity']) != 2
            or any(type(item) is not int or item < 0 for item in value['root_identity'])
            or type(value['files']) is not list or not 1 <= len(value['files']) <= MAX_RUNTIME_FILES):
        raise ConversionError('Invalid selected legacy-runtime manifest.')
    expected = {}
    for row in value['files']:
        if (type(row) is not dict or set(row) != {'name', 'bytes', 'sha256', 'identity'}
                or type(row['bytes']) is not int or not 0 <= row['bytes'] <= MAX_COMPONENT_BYTES
                or type(row['sha256']) is not str or len(row['sha256']) != 64
                or any(ch not in '0123456789abcdef' for ch in row['sha256'])
                or type(row['identity']) is not list or len(row['identity']) != 5
                or any(type(item) is not int for item in row['identity'])
                or row['identity'][2] != row['bytes']):
            raise ConversionError('Invalid selected runtime component identity.')
        name = _relative(row['name'])
        if name in expected:
            raise ConversionError('Duplicate selected runtime component.')
        expected[name] = tuple(row['identity'])
    root = checked_directory(value['root'])
    _required(root, value['executable'], kind, expected)
    info = root.stat()
    if [info.st_dev, info.st_ino] != value['root_identity']:
        raise ConversionError('Selected legacy-runtime directory changed.')
    if _inventory(root, lambda: False, deadline) != expected:
        raise ConversionError('Selected legacy-runtime component inventory changed.')
    for row in value['files']:
        digest = sha256()
        with verified_reader(root / row['name'], expected_identity=tuple(row['identity'])) as stream:
            while block := stream.read(1024**2):
                _check(lambda: False, deadline)
                digest.update(block)
        if digest.hexdigest() != row['sha256']:
            raise ConversionError('Selected legacy-runtime bytes changed.')
    return value


@contextmanager
def admitted_runtime(selection, stage, kind, deadline):
    value = _load(selection, stage, kind, deadline)
    root = Path(value['root'])
    # Hold executable, extension and packed-code files with the existing
    # immutable reader through native execution. Other selected resources are
    # rehashed afterwards; this is not a filesystem sandbox.
    code = [row for row in value['files']
            if Path(row['name']).suffix.casefold() in ('.exe', '.dll', '.pyd', '.zip', '.so')
            or row['name'] in (value['executable'], 'calibre-debug')]
    if len(code) > 384:
        raise ConversionUnavailable('Selected native code inventory exceeds its handle budget.')
    with ExitStack() as stack:
        for row in code:
            stack.enter_context(verified_reader(root / row['name'], expected_identity=tuple(row['identity'])))
        yield root / value['executable'], value, selection['sha256']
        _load(selection, stage, kind, deadline)
