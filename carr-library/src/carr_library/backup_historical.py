"""Typed, inert retention of the alternate canonical state-folder backups.

No runtime reads these files as active assistant jobs, providers or projects.
The complete original manifest remains hash-bound to the archive directory.
Original filenames live in that manifest; stored payload names are their hashes,
so historical path depth, suffixes and settings cannot activate another service.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from .backup_files import local_path, regular, sync_directory
from .backup_legacy_state import (
    MAX_STATE_FILES, MAX_STATE_BYTES, validate_state, verify_state, _inventory, _stamp,
)
from .ingest.bundle import _unique_object

DIRECTORY = 'state/historical-backups'
MAX_ARCHIVES = 1000
MAX_MANIFEST = 8 * 1024 * 1024
_HASH = re.compile('[0-9a-f]{64}')
_BASE = {'version', 'kind', 'schema_version', 'schema_fingerprint', 'database', 'assets'}
_CORE = frozenset({'library.sqlite', 'originals', 'manifest.json', 'pending'})
_ROOT_FILES = frozenset({'reading-state.json', 'keybindings.json', 'keyboard-shortcuts.json',
                         'evidence-drafts.json', 'ocr-language-presets.json', 'progress-preferences.json'})
_ROOT_DIRECTORIES = frozenset({'state', 'narration-projects', 'reading-copies'})
_GENERATED_MEDIA = re.compile(r'state/studio/cache/[0-9a-f]{64}(?:-[0-9a-f]{10})?'
                              r'\.(?:wav|flac|mp3|ogg|opus|m4a|m4b|aiff|wma)\Z')


def manifest_layout(value):
    """Identify only disjoint, exact canonical historical field envelopes."""
    if not isinstance(value, dict) or type(value.get('version')) is not int:
        return None
    extra = set(value) - _BASE
    if not _BASE <= set(value):
        return None
    version = value['version']
    if (version == 3 and extra == {'state', 'generated_audio_included'}
            or version == 4 and extra == {'state', 'generated_audio_included', 'pending'}):
        return 'state'
    if version == 5 and extra == {'state', 'generated_audio_included', 'pending', 'reading_state'}:
        return 'state-reading'
    if version == 4 and extra in ({'sidecars', 'generated_audio_included'},
                                  {'sidecars', 'generated_audio_included', 'pending'}):
        return 'root'
    return None


def root_entries(manifest):
    """Return source-relative names without rewriting the authenticated manifest."""
    layout = manifest_layout(manifest)
    if layout == 'root':
        return manifest['sidecars']
    if layout in ('state', 'state-reading'):
        entries = [dict(item, filename='state/' + item['filename']) for item in manifest['state']]
        if layout == 'state-reading':
            entries.append(manifest['reading_state'])
        return entries
    raise ValueError('Unsupported historical state manifest.')


def source_inventory(manifest):
    # Existing v3/v4 archives keep their original state-relative hashed names.
    # Newly supported root/v5 archives use full root-relative names, avoiding a
    # collision between state/reading-state.json and root reading-state.json.
    if manifest_layout(manifest) == 'state':
        return 'state', manifest['state'], ()
    return '', root_entries(manifest), _CORE


def validate_manifest(manifest, *, max_files=MAX_STATE_FILES, max_bytes=MAX_STATE_BYTES):
    layout = manifest_layout(manifest)
    if (layout is None or manifest['kind'] != 'carr-library'
            or type(manifest['schema_version']) is not int or manifest['schema_version'] not in (3, 4, 5)
            or type(manifest['generated_audio_included']) is not bool):
        raise ValueError('Unsupported historical state manifest.')
    if layout in ('state', 'state-reading'):
        validate_state(manifest['state'])
    if layout == 'state-reading':
        reading = manifest['reading_state']
        if (not isinstance(reading, dict) or reading.get('filename') != 'reading-state.json'
                or type(reading.get('size_bytes')) is not int or reading['size_bytes'] <= 0):
            raise ValueError('Invalid historical reading-state manifest.')
    entries = root_entries(manifest)
    validate_state(entries, max_files=max_files, max_bytes=max_bytes)
    if layout == 'root':
        metadata_bytes = media_bytes = 0
        for item in entries:
            name = item['filename']
            parts = name.split('/')
            if (len(name) > 1024 or len(parts) > 8 or item['size_bytes'] == 0
                    or not (name in _ROOT_FILES or len(parts) > 1 and parts[0] in _ROOT_DIRECTORIES)):
                raise ValueError('Unsupported historical root-sidecar namespace.')
            if name.startswith('state/studio/cache/') and not manifest['generated_audio_included']:
                raise ValueError('Historical generated audio requires explicit manifest inclusion.')
            if _GENERATED_MEDIA.fullmatch(name):
                media_bytes += item['size_bytes']
            else:
                metadata_bytes += item['size_bytes']
        if metadata_bytes > 512 * 1024**2 or media_bytes > 32 * 1024**3:
            raise ValueError('Historical common-format sidecars exceed their byte budgets.')
    # None was a documented empty pending component only in legacy v5.
    if 'pending' in manifest and manifest['pending'] is None and layout != 'state-reading':
        raise ValueError('Missing historical pending-import component.')
    return layout


def is_state_path(value):
    return isinstance(value, str) and bool(re.fullmatch(
        r'state/historical-backups/[0-9a-f]{64}/(?:manifest\.json|files/[0-9a-f]{64})', value))


def _file_key(name):
    return sha256(name.encode('utf-8')).hexdigest()


def _payload_entries(manifest):
    _, entries, _ = source_inventory(manifest)
    return [dict(filename=_file_key(item['filename']), size_bytes=item['size_bytes'],
                 sha256=item['sha256']) for item in entries]


def _manifest(path, digest):
    before = regular(path)
    if not 0 < before.st_size <= MAX_MANIFEST:
        raise ValueError('Historical state manifest exceeds its size budget.')
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Historical state manifest changed during read.')
        raw = stream.read(MAX_MANIFEST + 1)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Historical state manifest changed during read.')
    if (len(raw) != before.st_size or _stamp(regular(path)) != _stamp(before)
            or sha256(raw).hexdigest() != digest):
        raise ValueError('Historical state manifest checksum mismatch.')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite state.')))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid historical state manifest.') from exc
    validate_manifest(value)
    # This is inert provenance, not an executable replay. The enclosing original
    # restore already validated the catalog/pending components before retention.
    return raw, value


def state_paths(root, *, cancelled=lambda: False):
    base = Path(root) / DIRECTORY
    if not os.path.lexists(base):
        return ()
    local_path(base, exists=True)
    regular(base, directory=True)
    archives, result, selected = 0, [], []
    with os.scandir(base) as children:
        for entry in children:
            if len(selected) >= MAX_ARCHIVES:
                raise ValueError('Too many historical recovery archives.')
            selected.append(Path(entry.path))
    for archive in sorted(selected):
        if cancelled():
            raise InterruptedError('Historical backup verification cancelled.')
        archives += 1
        if archives > MAX_ARCHIVES or not _HASH.fullmatch(archive.name):
            raise ValueError('Invalid historical backup namespace or archive count.')
        regular(archive, directory=True)
        names = set()
        with os.scandir(archive) as children:
            for entry in children:
                names.add(entry.name)
                if len(names) > 2:
                    raise ValueError('Historical backup has unexpected files.')
        if names != {'manifest.json', 'files'}:
            raise ValueError('Historical backup has unexpected files.')
        regular(archive / 'files', directory=True)
        _, manifest = _manifest(archive / 'manifest.json', archive.name)
        entries = _payload_entries(manifest)
        _inventory(archive / 'files', entries, cancelled)
        result.append(archive / 'manifest.json')
        result.extend(archive / 'files' / entry['filename'] for entry in entries)
    return tuple(sorted(result))


def validate_state_tree(root, *, cancelled=lambda: False):
    paths = state_paths(root, cancelled=cancelled)
    for path in paths:
        if path.name == 'manifest.json':
            _, manifest = _manifest(path, path.parent.name)
            verify_state(path.parent / 'files', None, _payload_entries(manifest), cancelled=cancelled)


def retain_state(source, stage, manifest, expected_manifest_sha256, *, cancelled=lambda: False):
    """Retain original bytes as registered recovery data in a private stage."""
    source = local_path(source, exists=True)
    raw, stored = _manifest(source / 'manifest.json', expected_manifest_sha256)
    if stored != manifest:
        raise ValueError('Historical state manifest changed during restoration.')
    archive = Path(stage) / DIRECTORY / expected_manifest_sha256
    archive.mkdir(parents=True, mode=0o700)
    with (archive / 'manifest.json').open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    subdirectory, entries, ignored = source_inventory(manifest)
    verify_state(source / subdirectory, archive / 'files', entries,
                 cancelled=cancelled, destination_name=_file_key, ignored=ignored)
    sync_directory(archive)
    prefix = (Path(DIRECTORY) / expected_manifest_sha256).as_posix()
    return [dict(path=prefix + '/manifest.json', size_bytes=len(raw), sha256=expected_manifest_sha256),
            *[dict(path=prefix + '/files/' + entry['filename'],
                   size_bytes=entry['size_bytes'], sha256=entry['sha256'])
              for entry in _payload_entries(manifest)]]
