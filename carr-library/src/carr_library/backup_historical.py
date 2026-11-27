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

from .backup_files import local_path, regular, sync_directory, optional_present
from .backup_legacy_state import validate_state, verify_state, _inventory, _stamp
from .ingest.bundle import _unique_object

DIRECTORY = 'state/historical-backups'
MAX_ARCHIVES = 1000
MAX_MANIFEST = 8 * 1024 * 1024
_HASH = re.compile('[0-9a-f]{64}')


def is_state_path(value):
    return isinstance(value, str) and bool(re.fullmatch(
        r'state/historical-backups/[0-9a-f]{64}/(?:manifest\.json|files/[0-9a-f]{64})', value))


def _file_key(name):
    return sha256(name.encode('utf-8')).hexdigest()


def _payload_entries(manifest):
    return [dict(filename=_file_key(item['filename']), size_bytes=item['size_bytes'],
                 sha256=item['sha256']) for item in manifest['state']]


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
    fields = {'version', 'kind', 'schema_version', 'schema_fingerprint',
              'database', 'assets', 'state', 'generated_audio_included'}
    if isinstance(value, dict) and value.get('version') == 4:
        fields.add('pending')
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['version']) is not int or value['version'] not in (3, 4)
            or value['kind'] != 'carr-library'
            or type(value['schema_version']) is not int or value['schema_version'] not in (3, 4, 5)
            or type(value['generated_audio_included']) is not bool):
        raise ValueError('Unsupported historical state manifest.')
    validate_state(value['state'])
    # This is inert provenance, not an executable replay. The enclosing original
    # restore already validated the catalog/pending components before retention.
    return raw, value


def state_paths(root, *, cancelled=lambda: False):
    base = Path(root) / DIRECTORY
    if not optional_present(base):
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
    verify_state(source / 'state', archive / 'files', manifest['state'],
                 cancelled=cancelled, destination_name=_file_key)
    sync_directory(archive)
    prefix = (Path(DIRECTORY) / expected_manifest_sha256).as_posix()
    return [dict(path=prefix + '/manifest.json', size_bytes=len(raw), sha256=expected_manifest_sha256),
            *[dict(path=prefix + '/files/' + entry['filename'],
                   size_bytes=entry['size_bytes'], sha256=entry['sha256'])
              for entry in _payload_entries(manifest)]]
