"""Inert preservation of canonical V3/V4 application state.

Only file identity and inventory are accepted here. Saved commands, provider
permissions, engine paths, reading provenance and job receipts are never
interpreted or activated. The visible review receipt survives subsequent typed
library backups. This is preservation, not semantic migration or resumability.
"""
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import unicodedata

from .durable_state import atomic_bytes, local_path, regular, sync_directory

DIRECTORY = 'preserved-state'
REVIEW = 'review.json'
SCHEMA = 'carr-preserved-state/1'
STATUS = 'PRESERVED_REVIEW_REQUIRED'
MAX_FILES = 100001
MAX_BYTES = 32 * 1024**3
MAX_METADATA = 8 * 1024**2
MAX_PATH_BYTES = 2048
MAX_DEPTH = 32
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_RESERVED = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$',
             *(prefix + digit for prefix in ('com', 'lpt') for digit in '123456789¹²³')}
_LIVE_MARKERS = ('studio', 'speech', 'providers', 'audio', 'progress', 'ui',
                 'translation/jobs', 'translation/provider')
_TYPED_ROOTS = frozenset(('transcription', 'operations'))


def _check(cancelled):
    if cancelled():
        raise InterruptedError('State preservation cancelled.')


def _limits(max_bytes, max_files):
    if (type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BYTES
            or type(max_files) is not int or not 0 <= max_files <= MAX_FILES):
        raise ValueError('Invalid preserved-state resource budget.')
    return max_bytes, max_files


def relative_name(value):
    if (not isinstance(value, str) or not value
            or len(value.encode('utf-8', 'strict')) > MAX_PATH_BYTES
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
            or any(char in value for char in '\\:<>\"|?*')):
        raise ValueError('Invalid portable preserved-state path.')
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or len(path.parts) > MAX_DEPTH
            or any(part in ('.', '..') or part.endswith((' ', '.'))
                   or part.split('.')[0].rstrip(' ').casefold() in _RESERVED for part in path.parts)):
        raise ValueError('Invalid portable preserved-state path.')
    return path


def _portable_key(value):
    return unicodedata.normalize('NFC', value).casefold()


def _names(entries, *, max_bytes=MAX_BYTES, max_files=MAX_FILES):
    _limits(max_bytes, max_files)
    if type(entries) is not list or len(entries) > max_files:
        raise ValueError('Preserved state exceeds its file budget.')
    names, spelling, total, metadata, path_bytes = set(), {}, 0, 0, 0
    for item in entries:
        if (type(item) is not dict or set(item) != {'filename', 'size_bytes', 'sha256'}
                or type(item['size_bytes']) is not int or item['size_bytes'] < 0
                or not isinstance(item['sha256'], str) or not _HASH.fullmatch(item['sha256'])):
            raise ValueError('Invalid preserved-state file identity.')
        path = relative_name(item['filename'])
        folded = _portable_key(item['filename'])
        if folded in names:
            raise ValueError('Duplicate preserved-state filename.')
        names.add(folded)
        for part in (path, *path.parents):
            if str(part) == '.':
                continue
            text = str(part)
            if _portable_key(text) not in spelling:
                path_bytes += len(text.encode('utf-8')) + 64
                if path_bytes > MAX_METADATA:
                    raise ValueError('Preserved-state path inventory exceeds its metadata budget.')
            previous = spelling.setdefault(_portable_key(text), text)
            if previous != text:
                raise ValueError('Ambiguous preserved-state path casing.')
        total += item['size_bytes']
        metadata += len(item['filename'].encode('utf-8')) + 160
        if total > max_bytes or metadata > MAX_METADATA:
            raise ValueError('Preserved state exceeds its byte or metadata budget.')
    for item in entries:
        if any(_portable_key(str(parent)) in names for parent in PurePosixPath(item['filename']).parents
               if str(parent) != '.'):
            raise ValueError('A preserved-state file is also used as a directory.')
    return {item['filename']: item for item in entries}


def validate_canonical_state(entries):
    """The canonical manifest's raw list is data, not an application allowlist."""
    return _names(entries, max_files=MAX_FILES - 1)


def _identity(info):
    """Full change stamp; compare only results from the same stat API."""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, getattr(info, 'st_file_attributes', 0))


def _file_binding(info):
    """Bind a checked path to its open handle without comparing API clocks."""
    return info.st_dev, info.st_ino, info.st_size, info.st_nlink


def _volatile(name):
    return name.startswith('.pending-') or name.endswith(('.lock', '.tmp'))


def _walk(root, *, cancelled, live=False, max_bytes=MAX_BYTES, max_files=MAX_FILES):
    """Bound directory visits and path allocation before retaining an inventory."""
    root = local_path(root, exists=True)
    regular(root, directory=True)
    files, directories, seen = {}, {}, {}
    count = metadata = total = 0

    def visit(folder, relative):
        nonlocal count, metadata, total
        _check(cancelled)
        directories[relative] = _identity(regular(folder, directory=True))
        with os.scandir(folder) as children:
            for child in children:
                _check(cancelled)
                count += 1
                if count > 2 * MAX_FILES + MAX_DEPTH:
                    raise ValueError('Preserved-state directory inventory exceeds its entry budget.')
                name = child.name if not relative else relative + '/' + child.name
                relative_name(name)
                metadata += len(name.encode('utf-8')) + 160
                if metadata > MAX_METADATA:
                    raise ValueError('Preserved-state directory inventory exceeds its metadata budget.')
                previous = seen.setdefault(_portable_key(name), name)
                if previous != name:
                    raise ValueError('Ambiguous preserved-state path casing.')
                path = Path(child.path)
                is_dir = child.is_dir(follow_symlinks=False)
                # Windows DirEntry.stat can omit st_nlink. Use the same checked
                # Path.lstat metadata as every later read and copy comparison.
                info = regular(path, directory=is_dir)
                # These two current typed components retain their own snapshot
                # guards, private-runtime exclusions and restore review gates.
                if live and not relative and child.name.casefold() in _TYPED_ROOTS:
                    if not is_dir:
                        raise ValueError('A typed application state root must be a directory.')
                    continue
                if live and _volatile(child.name):
                    continue
                if is_dir:
                    visit(path, name)
                else:
                    if live and (path.suffix.casefold() in ('.sqlite', '.db')
                                 or path.name.endswith(('-wal', '-shm', '-journal'))):
                        raise ValueError('A live legacy state database needs a consistent snapshot adapter; '
                                         'use its verified canonical backup before preserving it.')
                    total += info.st_size
                    if len(files) >= max_files or total > max_bytes:
                        raise ValueError('Preserved state exceeds the remaining library backup budget.')
                    files[name] = _identity(info)
    visit(root, '')
    return files, directories


def _transfer(source, destination, *, expected=None, identity=None, cancelled=lambda: False):
    source = local_path(source, exists=True)
    before = regular(source)
    if identity is not None and _identity(before) != identity:
        raise ValueError('Application state changed before preservation; close its producer and retry.')
    if expected is not None and before.st_size != expected['size_bytes']:
        raise ValueError('Preserved-state size mismatch.')
    size, digest = 0, sha256()
    with ExitStack() as stack:
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        read = stack.enter_context(os.fdopen(os.open(source, flags), 'rb'))
        opened = os.fstat(read.fileno())
        if _file_binding(opened) != _file_binding(before):
            raise ValueError('Application state changed while opening.')
        write = None
        if destination is not None:
            destination = local_path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            local_path(destination.parent, exists=True)
            write = stack.enter_context(destination.open('xb'))
        while block := read.read(min(1024 * 1024, before.st_size + 1 - size)):
            _check(cancelled)
            size += len(block)
            if size > before.st_size:
                raise ValueError('Application state grew during preservation.')
            digest.update(block)
            if write is not None:
                write.write(block)
        if write is not None:
            write.flush()
            os.fsync(write.fileno())
        if _identity(os.fstat(read.fileno())) != _identity(opened):
            raise ValueError('Application state changed during preservation.')
    if size != before.st_size or _identity(regular(source)) != _identity(before):
        raise ValueError('Application state changed during preservation.')
    value = {'size_bytes': size, 'sha256': digest.hexdigest()}
    if expected is not None and any(value[key] != expected[key] for key in value):
        raise ValueError('Preserved-state checksum mismatch.')
    return value


def _copy_state(source, target, entries=None, *, cancelled, live=False,
                max_bytes=MAX_BYTES, max_files=MAX_FILES):
    before = _walk(source, cancelled=cancelled, live=live, max_bytes=max_bytes, max_files=max_files)
    expected = None if entries is None else _names(entries, max_bytes=max_bytes, max_files=max_files)
    if expected is not None and set(before[0]) != set(expected):
        raise ValueError('Canonical state has missing or unlisted files.')
    if expected is not None:
        _canonical_directories(before, expected)
    target.mkdir(mode=0o700, parents=True)
    copied = []
    for name in sorted(before[0]):
        _check(cancelled)
        copied.append({'filename': name, **_transfer(Path(source) / name, target / name,
            expected=None if expected is None else expected[name],
            identity=before[0][name], cancelled=cancelled)})
    # A stable timestamp is not a content identity. Reattest every source
    # against the already copied digest before the completion marker can publish.
    for item in copied:
        _check(cancelled)
        _transfer(Path(source) / item['filename'], None, expected=item,
                  identity=before[0][item['filename']], cancelled=cancelled)
    if _walk(source, cancelled=cancelled, live=live,
             max_bytes=max_bytes, max_files=max_files) != before:
        raise ValueError('Application state changed during preservation; close its producer and retry.')
    sync_directory(target)
    return copied


def _canonical_directories(inventory, expected):
    directories = {''}
    for name in expected:
        directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != '.')
    if set(inventory[1]) != directories:
        raise ValueError('Canonical state has unlisted directories.')


def validate_canonical_namespace(source, entries, *, cancelled=lambda: False):
    expected = validate_canonical_state(entries)
    inventory = _walk(source, cancelled=cancelled, max_files=MAX_FILES - 1)
    if set(inventory[0]) != set(expected):
        raise ValueError('Canonical state has missing or unlisted files.')
    _canonical_directories(inventory, expected)


def _encode(value):
    pieces, size = [], 0
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))
    for text in encoder.iterencode(value):
        raw = text.encode('utf-8', 'strict')
        size += len(raw)
        if size + 1 > MAX_METADATA:
            raise ValueError('Preserved-state review receipt exceeds its byte budget.')
        pieces.append(raw)
    return b''.join(pieces) + b'\n'


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate preservation receipt field.')
            result[key] = value
        return result
    def finite(value):
        raise ValueError('Non-finite preservation receipt value.')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=finite)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid preservation review receipt.') from exc


def _receipt(value):
    fields = {'schema', 'status', 'origin', 'files', 'execution_authorized',
              'provider_authorized', 'quotation_authority', 'migration_completed'}
    if (type(value) is not dict or set(value) != fields or value['schema'] != SCHEMA
            or value['status'] != STATUS or any(value[key] is not False for key in
                ('execution_authorized', 'provider_authorized', 'quotation_authority', 'migration_completed'))):
        raise ValueError('Preserved state requires an inert review receipt.')
    origin = value['origin']
    if type(origin) is not dict:
        raise ValueError('Invalid preserved-state origin.')
    if origin.get('kind') == 'canonical-library-backup':
        if (set(origin) != {'kind', 'manifest_version', 'manifest_sha256', 'generated_audio_included'}
                or type(origin['manifest_version']) is not int or origin['manifest_version'] not in (3, 4)
                or not isinstance(origin['manifest_sha256'], str) or not _HASH.fullmatch(origin['manifest_sha256'])
                or type(origin['generated_audio_included']) is not bool):
            raise ValueError('Invalid canonical backup preservation origin.')
    elif (set(origin) != {'kind', 'generated_audio_included'}
            or origin.get('kind') != 'recognized-canonical-live-layout'
            or origin.get('generated_audio_included') is not True):
        raise ValueError('Invalid live-state preservation origin.')
    validate_canonical_state(value['files'])
    return value


def _review_bytes(folder):
    path = local_path(Path(folder) / REVIEW, exists=True)
    before = regular(path)
    if not 0 < before.st_size <= MAX_METADATA:
        raise ValueError('Invalid preservation review receipt size.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if _file_binding(opened) != _file_binding(before):
            raise ValueError('Preservation review receipt changed while opening.')
        raw = stream.read(MAX_METADATA + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) != before.st_size or _identity(after) != _identity(opened)
            or _identity(regular(path)) != _identity(before)):
        raise ValueError('Preservation review receipt changed while reading.')
    return raw, _receipt(_decode(raw))


def _component(receipt, raw):
    return {'version': 1, 'status': STATUS, 'files': [
        {'filename': REVIEW, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()},
        *[dict(item, filename='files/' + item['filename']) for item in receipt['files']],
    ]}


def validate_manifest(value):
    if (type(value) is not dict or set(value) != {'version', 'status', 'files'}
            or type(value['version']) is not int or value['version'] != 1 or value['status'] != STATUS):
        raise ValueError('Invalid preserved-state component manifest.')
    files = _names(value['files'])
    if REVIEW not in files or not 0 < files[REVIEW]['size_bytes'] <= MAX_METADATA:
        raise ValueError('The preserved-state review receipt is required.')
    for name in files:
        if name != REVIEW:
            if not name.startswith('files/'):
                raise ValueError('Unknown preserved-state component file.')
            relative_name(name[len('files/'):])
    return files


def _finish(target, entries, origin, *, max_bytes, max_files):
    receipt = _receipt(dict(schema=SCHEMA, status=STATUS, origin=origin, files=entries,
        execution_authorized=False, provider_authorized=False,
        quotation_authority=False, migration_completed=False))
    raw = _encode(receipt)
    component = _component(receipt, raw)
    validate_manifest(component)
    _names(component['files'], max_bytes=max_bytes, max_files=max_files)
    atomic_bytes(target / REVIEW, raw)
    sync_directory(target)
    return component


def preserve_canonical(source, target, manifest, manifest_sha256, *, cancelled=lambda: False,
                       max_bytes=MAX_BYTES, max_files=MAX_FILES):
    """Preserve an already authenticated canonical archive's state, never activate it."""
    _limits(max_bytes, max_files)
    if not max_files:
        raise ValueError('No remaining preserved-state file budget.')
    target = Path(target)
    entries = _copy_state(Path(source) / 'state', target / 'files', manifest['state'],
        cancelled=cancelled, max_bytes=max_bytes, max_files=max_files - 1)
    return _finish(target, entries, {
        'kind': 'canonical-library-backup', 'manifest_version': manifest['version'],
        'manifest_sha256': manifest_sha256,
        'generated_audio_included': manifest['generated_audio_included'],
    }, max_bytes=max_bytes, max_files=max_files)


def live_layout(root):
    """Recognize actual canonical namespaces, without accepting arbitrary neighbors."""
    state = Path(root) / 'state'
    if not os.path.lexists(state):
        return False
    local_path(state, exists=True)
    regular(state, directory=True)
    found = any(os.path.lexists(state / name) for name in _LIVE_MARKERS)
    if found and os.path.lexists(state / 'translation'):
        folder = local_path(state / 'translation', exists=True)
        regular(folder, directory=True)
        with os.scandir(folder) as items:
            for count, item in enumerate(items, 1):
                if count > 5000:
                    raise ValueError('Translation state exceeds its entry budget.')
                if re.fullmatch(r'[0-9a-f]{32}\.json', item.name):
                    raise ValueError('Canonical and current translation checkpoints coexist. '
                                     'Review them before backup; their independent cost ledgers cannot be merged implicitly.')
    return found


def snapshot(root, target, *, preserve_live=False, cancelled=lambda: False,
             max_bytes=MAX_BYTES, max_files=MAX_FILES):
    _limits(max_bytes, max_files)
    source, target = Path(root) / DIRECTORY, Path(target)
    if os.path.lexists(source):
        if preserve_live:
            raise ValueError('Preserved and active canonical state coexist. Review them before creating another backup; no state was omitted.')
        raw, receipt = _review_bytes(source)
        component = _component(receipt, raw)
        return restore(source, target, component, cancelled=cancelled,
                       max_bytes=max_bytes, max_files=max_files)
    if not preserve_live:
        return None
    if not max_files:
        raise ValueError('No remaining preserved-state file budget.')
    entries = _copy_state(Path(root) / 'state', target / 'files', cancelled=cancelled,
        live=True, max_bytes=max_bytes, max_files=max_files - 1)
    return _finish(target, entries, {
        'kind': 'recognized-canonical-live-layout', 'generated_audio_included': True,
    }, max_bytes=max_bytes, max_files=max_files)


def restore(source, target, component, *, cancelled=lambda: False,
            max_bytes=MAX_BYTES, max_files=MAX_FILES):
    """Copy only checked inert files into a new component directory."""
    validate_manifest(component)
    _names(component['files'], max_bytes=max_bytes, max_files=max_files)
    source, target = local_path(source, exists=True), Path(target)
    raw, receipt = _review_bytes(source)
    if _component(receipt, raw) != component:
        raise ValueError('Preservation receipt disagrees with its sealed file inventory.')
    before = _walk(source, cancelled=cancelled, max_bytes=max_bytes, max_files=max_files)
    if set(before[0]) != {item['filename'] for item in component['files']}:
        raise ValueError('Preserved state has missing or unlisted files.')
    expected_dirs = {'', 'files'}
    for item in component['files']:
        expected_dirs.update(str(p) for p in PurePosixPath(item['filename']).parents if str(p) != '.')
    if set(before[1]) != expected_dirs:
        raise ValueError('Preserved state has unlisted directories.')
    target.mkdir(mode=0o700)
    (target / 'files').mkdir(mode=0o700)
    for entry in component['files']:
        _check(cancelled)
        _transfer(source / entry['filename'], target / entry['filename'],
                  expected=entry, identity=before[0][entry['filename']], cancelled=cancelled)
    if _walk(source, cancelled=cancelled, max_bytes=max_bytes, max_files=max_files) != before:
        raise ValueError('Preserved state changed while copying.')
    sync_directory(target)
    return component


def review_notice(root):
    """Small read-only UI notice; no engine, job or provider is reconstructed."""
    folder = Path(root) / DIRECTORY
    if not os.path.lexists(folder):
        return ''
    _, receipt = _review_bytes(folder)
    return (f"Preserved application state requires review: {len(receipt['files'])} recorded files in "
            f"{folder}. These bytes have not been migrated. Saved jobs cannot resume from this folder; "
            'engine paths, provider approvals and quotation authority are not activated. '
            'See preserved-state/review.json for the exact inventory.')
