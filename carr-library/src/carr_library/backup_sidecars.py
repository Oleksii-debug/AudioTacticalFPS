"""Shared allowlist for source-bound reader state and optional private sidecars.

This is the existing sidecar format, shared by full-library backup versions 3,
4 and 5. Optional features provide their own byte validators; an unavailable
validator fails explicitly instead of dropping or trusting an existing file.
No sidecar grants quotation verification or reader authorization on restore.
"""
from hashlib import sha256
from importlib import import_module
import os
from pathlib import Path
import re

from .ingest.bundle import _plain
from .ingest.restore import RestoreError
from .reader.reading_state import FILENAME as READING_STATE, MAX_BYTES as MAX_READING_BYTES, _decode
from .state_backup import read_state_bytes


NARRATION_DIRECTORY = 'narration-projects'
MAX_SIDECARS = 2005
MAX_SIDECAR_BYTES = 96 * 1024**2
_NARRATION = re.compile(r'narration-projects/[0-9a-f]{32}(?:\.draft)?\.json')
# Reuse the owning feature's public validator; do not interpret or migrate its
# private schema here. Optional imports happen only for a present/declared file.
_OPTIONAL = {
    'keybindings.json': (128 * 1024, 'carr_library.keybindings', 'decode_preferences'),
    'ocr-language-presets.json': (64 * 1024, 'carr_library.extract.ocr_languages', 'validate_presets_bytes'),
    'keyboard-shortcuts.json': (64 * 1024, 'carr_library.keyboard_preferences', 'validate_keymap'),
    'evidence-drafts.json': (8 * 1024**2, 'carr_library.evidence_drafts', 'validate_evidence_drafts'),
}


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Backup or restore cancelled before completion.')


def sidecar_limit(name):
    if name == READING_STATE:
        return MAX_READING_BYTES
    if type(name) is str and name in _OPTIONAL:
        return _OPTIONAL[name][0]
    if type(name) is str and _NARRATION.fullmatch(name):
        return 32 * 1024**2
    raise RestoreError('unsupported backup sidecar')


def _validator(module, name):
    try:
        return getattr(import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise RestoreError('the installed feature cannot validate this backup sidecar: ' + module) from exc


def sidecar_manifest(sidecars):
    return [dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest())
            for name, raw in sorted(sidecars.items())]


def validate_sidecar_manifest(entries):
    if type(entries) is not list or len(entries) > MAX_SIDECARS:
        raise RestoreError('invalid sidecar manifest')
    names, total = set(), 0
    for entry in entries:
        if type(entry) is not dict or set(entry) != {'filename', 'size_bytes', 'sha256'}:
            raise RestoreError('invalid sidecar manifest')
        limit = sidecar_limit(entry['filename'])
        if (entry['filename'] in names or type(entry['size_bytes']) is not int
                or not 0 < entry['size_bytes'] <= limit or type(entry['sha256']) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
            raise RestoreError('invalid or duplicate sidecar manifest')
        names.add(entry['filename'])
        total += entry['size_bytes']
    if total > MAX_SIDECAR_BYTES:
        raise RestoreError('backup sidecar byte budget exceeded')


def snapshot_sidecars(root, *, cancelled=lambda: False):
    root = Path(root)
    result, total = {}, 0
    names = [READING_STATE, *_OPTIONAL]
    narration = root / NARRATION_DIRECTORY
    if os.path.lexists(narration):
        _plain(narration, directory=True)
        with os.scandir(narration) as entries:
            for entry in entries:
                _check(cancelled)
                name = NARRATION_DIRECTORY + '/' + entry.name
                sidecar_limit(name)
                names.append(name)
                if len(names) > MAX_SIDECARS:
                    raise RestoreError('backup sidecar file budget exceeded')
    for name in sorted(names):
        _check(cancelled)
        path = root / name
        if not os.path.lexists(path):
            continue
        raw = read_state_bytes(path, sidecar_limit(name), cancelled=cancelled)
        total += len(raw)
        if total > MAX_SIDECAR_BYTES:
            raise RestoreError('backup sidecar byte budget exceeded')
        result[name] = raw
    return result


def validate_locations(db, locations):
    """Preserve exact archived/historical identities, never the active pointer."""
    for location in locations:
        predicate = ('p.physical=?' if location.kind == 'physical_page'
                     else 'p.physical IS NULL AND p.ordinal=?')
        row = db.execute('''SELECT p.digest FROM revision r
            JOIN sealed s ON s.revision=r.id
            JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition AND e.asset=r.asset
            JOIN workspace_meta w ON w.workspace=r.workspace
            JOIN page p ON p.revision=r.id
            WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=? AND ''' + predicate,
            (location.workspace, location.edition, location.revision,
             location.asset_sha256, location.index)).fetchone()
        if row is None or row[0] != location.text_sha256:
            raise RestoreError('reading state does not match its exact saved library source')


def validate_sidecars(db, sidecars, *, cancelled=lambda: False):
    for name, raw in sidecars.items():
        _check(cancelled)
        sidecar_limit(name)
        try:
            if name == READING_STATE:
                positions, bookmarks, _ = _decode(raw.decode('utf-8'))
                locations = {row[0] for row in positions.values()} | {row.location for row in bookmarks.values()}
            elif name in _OPTIONAL:
                _, module, validator = _OPTIONAL[name]
                _validator(module, validator)(raw)
                locations = ()
            else:
                project = _validator('carr_library.reader.narration', 'validate_narration_payload')(name, raw)
                locations = (project.source_location,) if project.source_location is not None else ()
        except RestoreError:
            raise
        except (ValueError, UnicodeError) as exc:
            raise RestoreError('invalid source-bound reading state or optional backup sidecar') from exc
        validate_locations(db, locations)


def stage_sidecars(stage, sidecars, sync_file):
    for name, raw in sorted(sidecars.items()):
        (stage / name).parent.mkdir(mode=0o700, exist_ok=True)
        sync_file(stage / name, raw)


def publish_sidecars(stage, destination, sidecars, sync_directory):
    for name in sorted(sidecars):
        (destination / name).parent.mkdir(mode=0o700, exist_ok=True)
        os.link(stage / name, destination / name)
    for parent in {(destination / name).parent for name in sidecars}:
        sync_directory(parent)


def check_namespace(directory, entries, has_state, *, has_pending=False):
    expected = {'library.sqlite', 'originals', 'manifest.json'} | {entry['filename'].split('/')[0] for entry in entries}
    if has_state:
        expected.add('state')
    if has_pending:
        expected.add('pending')
    actual = set()
    with os.scandir(directory) as children:
        for child in children:
            actual.add(child.name)
            if len(actual) > len(expected):
                break
    if actual != expected:
        raise RestoreError('incomplete backup or unexpected files')
    narration = {entry['filename'].split('/')[1] for entry in entries if '/' in entry['filename']}
    if narration:
        folder = directory / NARRATION_DIRECTORY
        _plain(folder, directory=True)
        actual = set()
        with os.scandir(folder) as children:
            for child in children:
                actual.add(child.name)
                if len(actual) > len(narration):
                    break
        if actual != narration:
            raise RestoreError('incomplete narration sidecar backup or unexpected files')
