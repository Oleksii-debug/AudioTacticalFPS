"""Checked sidecar adapters shared by library-backup format readers.

This is an explicit inventory of data formats, not a recursive settings copier.
Optional producers own their validators. Missing validators and unrecognized
state fail visibly, rather than dropping user data or copying arbitrary JSON.
Database-bound locations retain their exact historical source identities.
"""
from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .ingest.bundle import _plain, _unique_object
from .ingest.restore import RestoreError
from .reader.reading_state import MAX_BYTES as READING_BYTES, _decode

MAX_FILES = 100_000
MAX_BYTES = 512 * 1024 * 1024
_SIMPLE = ('reading-state.json', 'keybindings.json', 'keyboard-shortcuts.json',
           'evidence-drafts.json', 'progress-preferences.json', 'ocr-language-presets.json')
_DIRECTORIES = ('narration-projects', 'reading-copies', 'state')
_ID = r'[A-Za-z0-9_-]{1,80}'
_STUDIO = re.compile(r'state/studio/projects/(' + _ID + r')/(?:project\.json|revisions/(\d{8})-([0-9a-f]{64})\.json)\Z')
_ASSISTANT = re.compile(r'state/assistant/[0-9a-f]{32}(?:\.(?:control|restore-review))?\.json\Z')


def _check_cancelled(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Backup operation cancelled; no completed destination was published.')


def portable_name(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or '\\' in value or ':' in value or any(ord(c) < 32 for c in value)):
        raise RestoreError('invalid sidecar filename')
    path = PurePosixPath(value)
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                *(f'lpt{i}' for i in range(1, 10))}
    if (path.is_absolute() or str(path) != value or len(path.parts) > 8
            or any(p in ('.', '..') or p.endswith((' ', '.'))
                   or p.split('.')[0].casefold() in reserved for p in path.parts)):
        raise RestoreError('invalid portable sidecar filename')
    return path


def _model(module):
    try:
        model = import_module('.' + module, 'carr_library')
    except ImportError as exc:
        raise RestoreError('This backup needs the installed data validator for ' + module +
                           '; existing files were kept.') from exc
    required = {
        'extract.ocr_languages': ('MAX_PRESET_BYTES', 'validate_presets_bytes'),
        'assistant.backup_state': ('MAX_BYTES', 'validate_state_bytes', 'restore_review_bytes'),
    }.get(module, ())
    if any(not hasattr(model, name) for name in required):
        raise RestoreError('The installed data validator for ' + module +
                           ' has an incompatible version; existing files were kept.')
    return model


def sidecar_limit(name):
    """Resolve only explicit formats; also fail early if a producer is absent."""
    portable_name(name)
    if name.startswith('state/transcription/'):
        return _model('transcription_backup').limit(name)
    if name == 'reading-state.json':
        return READING_BYTES
    if name == 'keybindings.json':
        return _model('keybindings').MAX_BYTES
    if name == 'keyboard-shortcuts.json':
        return _model('keyboard_preferences').MAX_BYTES
    if name == 'progress-preferences.json':
        return _model('progress_preferences').MAX_BYTES
    if name == 'evidence-drafts.json':
        return _model('evidence_drafts').MAX_BYTES
    if name == 'ocr-language-presets.json':
        return _model('extract.ocr_languages').MAX_PRESET_BYTES
    if re.fullmatch(r'narration-projects/[0-9a-f]{32}(?:\.draft)?\.json', name):
        return _model('reader.narration').MAX_PROJECT_BYTES
    if re.fullmatch(r'reading-copies/[0-9a-f]{32}\.json', name):
        return _model('reader.reading_copy').MAX_PROJECT_BYTES
    if _STUDIO.fullmatch(name):
        _model('studio.project')
        return 32 * 1024 * 1024
    if _ASSISTANT.fullmatch(name):
        return _model('assistant.backup_state').MAX_BYTES
    raise RestoreError('No validated backup adapter is installed for ' + name +
                       '. Save and pause its producer; this backup was not completed.')


def validate_locations(db, locations):
    """Permit archived libraries and sealed historical revisions; never retarget."""
    for location in set(locations):
        row = db.execute('''SELECT 1 FROM revision r
            JOIN sealed s ON s.revision=r.id
            JOIN page p ON p.revision=r.id
            JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition AND e.asset=r.asset
            WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=? AND p.digest=?
              AND ((?='physical_page' AND p.physical=?) OR
                   (?='logical_segment' AND p.physical IS NULL AND p.ordinal=?))''',
            (location.workspace, location.edition, location.revision, location.asset_sha256,
             location.text_sha256, location.kind, location.index, location.kind, location.index)).fetchone()
        if row is None:
            raise RestoreError('sidecar does not match its exact saved source')


def _validate_original_text(db, location, text):
    if location is None:
        return
    validate_locations(db, (location,))
    row = db.execute('SELECT raw FROM page WHERE revision=? AND digest=? AND '
                     '((?=\'physical_page\' AND physical=?) OR '
                     '(?=\'logical_segment\' AND physical IS NULL AND ordinal=?))',
                     (location.revision, location.text_sha256, location.kind, location.index,
                      location.kind, location.index)).fetchone()
    if row is None or row[0] != text:
        raise RestoreError('reading-copy original text does not match its exact saved source')


def _json(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON number.')))


def validate_sidecar(db, name, raw):
    maximum = sidecar_limit(name)
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise RestoreError('sidecar is empty or exceeds its data-format budget')
    try:
        if name == 'reading-state.json':
            positions, bookmarks, _ = _decode(raw.decode('utf-8'))
            validate_locations(db, [value[0] for value in positions.values()] +
                               [bookmark.location for bookmark in bookmarks.values()])
        elif name == 'keybindings.json':
            _model('keybindings').decode_preferences(raw)
        elif name == 'keyboard-shortcuts.json':
            _model('keyboard_preferences').validate_keymap(raw)
        elif name == 'progress-preferences.json':
            _model('progress_preferences').validate_progress_preferences(raw)
        elif name == 'evidence-drafts.json':
            drafts = _model('evidence_drafts').validate_evidence_drafts(raw)
            for draft in drafts:
                row = db.execute('''SELECT 1 FROM research_evidence e
                    JOIN research_topic t ON t.workspace=e.workspace AND t.id=e.topic
                    JOIN workspace w ON w.id=e.workspace
                    WHERE e.workspace=? AND e.topic=? AND e.id=?''', draft.key).fetchone()
                if row is None:
                    raise ValueError('Recovery draft has no matching exact saved evidence.')
            # Archived scopes and changed saved-note/source baselines are kept.
            # A recovery copy is not an evidence edit: the editor authorizes
            # again after restore and offers conflicting copies read-only.
        elif name == 'ocr-language-presets.json':
            _model('extract.ocr_languages').validate_presets_bytes(raw)
        elif _ASSISTANT.fullmatch(name):
            workspaces = _model('assistant.backup_state').validate_state_bytes(name, raw)
            for workspace in workspaces:
                if db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (workspace,)).fetchone() is None:
                    raise ValueError('Assistant checkpoint belongs to a missing library.')
        elif name.startswith('narration-projects/'):
            project = _model('reader.narration').validate_narration_payload(name, raw)
            _validate_original_text(db, project.source_location, project.original_text)
        elif name.startswith('reading-copies/'):
            project = _model('reader.reading_copy').validate_project_bytes(raw)
            if name != 'reading-copies/' + project.id + '.json':
                raise ValueError('Reading-copy filename disagrees with its identity.')
            if db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (project.workspace,)).fetchone() is None:
                raise ValueError('Reading copy belongs to a missing library.')
            _validate_original_text(db, project.source, project.original_text)
        else:
            match = _STUDIO.fullmatch(name)
            project = _model('studio.project').Project.from_dict(_json(raw))
            if project.id != match[1] or (match[2] is not None and
                    (project.revision != int(match[2]) or project.fingerprint != match[3])):
                raise ValueError('Audio project disagrees with its stored identity or revision.')
    except (UnicodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise RestoreError('invalid backup sidecar ' + name + ': ' + str(exc)) from exc


@dataclass(frozen=True)
class Inventory:
    entries: tuple
    empty_roots: frozenset = frozenset()
    generated_audio_included: bool = False
    pending: dict | None = None


def _collection_budgets(entries):
    narration = [(name, size) for name, size in entries if name.startswith('narration-projects/')]
    if narration:
        model = _model('reader.narration')
        if (len(narration) > model.MAX_PROJECTS * 2
                or sum(size for _, size in narration) > model.MAX_SNAPSHOT_BYTES):
            raise RestoreError('narration producer snapshot budget exceeded')


def normalize_manifest(manifest):
    """Discriminate exact v3 shapes without changing the signed source manifest."""
    version = manifest.get('version')
    fields = {'version', 'kind', 'schema_version', 'database', 'assets'}
    if type(version) is not int or version not in (1, 2, 3, 4):
        raise RestoreError('unsupported library manifest')
    if version >= 2:
        fields.add('schema_fingerprint')
    extra = set(manifest) - fields
    entries, roots, generated, pending = [], frozenset(), False, None
    if version <= 2:
        if set(manifest) != fields:
            raise RestoreError('unsupported library manifest fields')
    elif version == 3 and extra == {'reading_state'}:
        value = manifest['reading_state']
        if value is not None:
            if type(value) is not dict or value.get('filename') != 'reading-state.json':
                raise RestoreError('invalid legacy reading-state manifest')
            entries = [value]
    elif version == 3 and extra == {'sidecars'}:
        entries = manifest['sidecars']
    elif (version == 3 and extra == {'state', 'generated_audio_included'}
            or version == 4 and extra == {'state', 'generated_audio_included', 'pending'}):
        generated = manifest['generated_audio_included']
        if type(manifest['state']) is not list or len(manifest['state']) > MAX_FILES:
            raise RestoreError('invalid legacy state manifest')
        entries = []
        for value in manifest['state']:
            if type(value) is not dict or not isinstance(value.get('filename'), str):
                raise RestoreError('invalid legacy state entry')
            portable_name(value['filename'])
            entries.append({**value, 'filename': 'state/' + value['filename']})
        roots = frozenset({'state'})
        if version == 4:
            pending = manifest['pending']
            if pending is None:
                raise RestoreError('missing pending import backup')
    elif version == 4 and extra in ({'sidecars', 'generated_audio_included'},
                                    {'sidecars', 'generated_audio_included', 'pending'}):
        entries, generated = manifest['sidecars'], manifest['generated_audio_included']
        if 'pending' in manifest:
            pending = manifest['pending']
            if pending is None:
                raise RestoreError('missing pending import backup')
    else:
        raise RestoreError('ambiguous or unsupported library manifest fields')
    if set(manifest) != fields | extra or not fields <= set(manifest) or type(generated) is not bool:
        raise RestoreError('invalid library manifest fields')
    if type(entries) is not list or len(entries) > MAX_FILES:
        raise RestoreError('invalid sidecar manifest')
    names, portable_paths, total, copied = set(), {}, 0, []
    for entry in entries:
        if (type(entry) is not dict or set(entry) != {'filename', 'size_bytes', 'sha256'}
                or type(entry['size_bytes']) is not int
                or not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
            raise RestoreError('invalid sidecar manifest entry')
        maximum = sidecar_limit(entry['filename'])
        minimum = 0 if entry['filename'].startswith('state/transcription/') else 1
        if not minimum <= entry['size_bytes'] <= maximum or entry['filename'].casefold() in names:
            raise RestoreError('invalid sidecar size or duplicate filename')
        names.add(entry['filename'].casefold())
        path = PurePosixPath(entry['filename'])
        for component in (path, *path.parents):
            name = str(component)
            if name == '.':
                continue
            previous = portable_paths.setdefault(name.casefold(), name)
            if previous != name:
                raise RestoreError('sidecar paths collide on a case-insensitive filesystem')
        total += entry['size_bytes']
        if total > MAX_BYTES:
            raise RestoreError('sidecar manifest byte limit')
        copied.append(dict(entry))
    _collection_budgets((entry['filename'], entry['size_bytes']) for entry in copied)
    if pending is not None:
        from .pending_backup import validate_pending
        validate_pending(pending)
    return Inventory(tuple(sorted(copied, key=lambda value: value['filename'])), roots, generated, pending)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular(path):
    info = _plain(path)
    if info.st_nlink != 1:
        raise RestoreError('sidecars must be private regular files without hard links')
    return info


def read_regular(path, maximum):
    before = _regular(path)
    if not 0 < before.st_size <= maximum:
        raise RestoreError('sidecar is empty or exceeds its data-format budget')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise RestoreError('sidecar changed while opening')
        raw = stream.read(maximum + 1)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise RestoreError('sidecar changed while reading')
    if len(raw) != before.st_size or _identity(_regular(path)) != _identity(before):
        raise RestoreError('sidecar changed while reading')
    return raw


def _ignored(name):
    if name.startswith('state/transcription/'):
        return _model('transcription_backup').ignored(name)
    path = PurePosixPath(name)
    if path.parent.as_posix() == 'state/assistant':
        return bool(re.fullmatch(r'[0-9a-f]{32}\.lock', path.name) or re.fullmatch(
            r'\.[0-9a-f]{32}(?:\.control)?\.json\.[0-9a-f]{32}\.tmp', path.name))
    if path.parent.as_posix() in ('narration-projects', 'reading-copies'):
        return path.name.startswith('.reading-copy-') and path.name.endswith('.tmp')
    return (name.startswith('state/studio/') and
            (path.name == '.state.lock' or path.name.startswith('.pending-')))


def _capture(root, *, cancelled=lambda: False):
    files, directories = {}, {}
    for name in _SIMPLE:
        if os.path.lexists(root / name):
            files[name] = _identity(_regular(root / name))
    pending = [name for name in _DIRECTORIES if os.path.lexists(root / name)]
    while pending:
        _check_cancelled(cancelled)
        name = pending.pop()
        portable_name(name)
        directory = root / name
        directories[name] = _identity(_plain(directory, directory=True))
        for path in directory.iterdir():
            _check_cancelled(cancelled)
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                _plain(path, directory=True)
                if relative != 'state/studio/cache' and not (relative.startswith('state/transcription/') and _ignored(relative)):
                    pending.append(relative)
            else:
                _regular(path)
                if not _ignored(relative):
                    sidecar_limit(relative)
                    files[relative] = _identity(info)
            if len(files) + len(directories) + len(pending) > MAX_FILES:
                raise RestoreError('sidecar inventory exceeds its file budget')
    return files, directories


def _write(stage, name, raw):
    path = stage / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def snapshot(db, root, stage, *, max_bytes=MAX_BYTES, cancelled=lambda: False):
    _check_cancelled(cancelled)
    before = _capture(root, cancelled=cancelled)
    _collection_budgets((name, identity[4]) for name, identity in before[0].items())
    entries, total = [], 0
    for name in sorted(before[0]):
        _check_cancelled(cancelled)
        total += before[0][name][4]
        if total > min(MAX_BYTES, max_bytes):
            raise RestoreError('sidecar snapshot byte limit')
        if name.startswith('state/transcription/'):
            entries.append(_model('transcription_backup').copy_entry(root, stage, name, cancelled=cancelled))
            continue
        raw = read_regular(root / name, sidecar_limit(name))
        validate_sidecar(db, name, raw)
        _write(stage, name, raw)
        entries.append(dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
    if _capture(root, cancelled=cancelled) != before:
        raise RestoreError('application state changed during backup; pause its producer and retry')
    _assistant_pairs(stage, entries)
    if any(entry['filename'].startswith('state/transcription/') for entry in entries):
        _model('transcription_backup').validate_snapshot(db, stage, entries, cancelled=cancelled)
    _check_cancelled(cancelled)
    return entries


def check_namespace(directory, inventory):
    names = {entry['filename'] for entry in inventory.entries}
    expected_dirs = set(inventory.empty_roots)
    if inventory.pending is not None:
        names.update('pending/' + entry['filename'] for entry in inventory.pending['files'])
        expected_dirs.add('pending')
    for name in names:
        expected_dirs.update(str(p) for p in PurePosixPath(name).parents if str(p) != '.')
    roots = {name.split('/')[0] for name in names} | set(inventory.empty_roots)
    if inventory.pending is not None:
        roots.add('pending')
    if set(path.name for path in directory.iterdir()) != {'library.sqlite', 'originals', 'manifest.json'} | roots:
        raise RestoreError('incomplete backup or unexpected files')
    for name in expected_dirs:
        _plain(directory / name, directory=True)
        actual = {path.relative_to(directory).as_posix() for path in (directory / name).iterdir()}
        expected = {path for path in names | expected_dirs if str(PurePosixPath(path).parent) == name}
        if actual != expected:
            raise RestoreError('incomplete sidecar backup or unexpected files')
    for name in names:
        _regular(directory / name)


def _assistant_pairs(root, entries):
    """Controls and restore gates must belong to the exact archived job."""
    by_name = {entry['filename']: entry for entry in entries}
    for name in by_name:
        match = re.fullmatch(r'(state/assistant/[0-9a-f]{32})\.(control|restore-review)\.json', name)
        if match is None:
            continue
        checkpoint = by_name.get(match[1] + '.json')
        if checkpoint is None:
            raise RestoreError('Assistant state has no matching job checkpoint.')
        if match[2] == 'restore-review':
            marker = _json(read_regular(root / name, sidecar_limit(name)))
            if marker['checkpoint_sha256'] != checkpoint['sha256']:
                raise RestoreError('Assistant restore review marker does not match its checkpoint.')


def restore(db, source, stage, inventory, *, cancelled=lambda: False):
    for entry in inventory.entries:
        _check_cancelled(cancelled)
        name = entry['filename']
        if name.startswith('state/transcription/'):
            _model('transcription_backup').copy_entry(source, stage, name, expected=entry, cancelled=cancelled)
            continue
        raw = read_regular(source / name, sidecar_limit(name))
        if len(raw) != entry['size_bytes'] or sha256(raw).hexdigest() != entry['sha256']:
            raise RestoreError('sidecar digest or size mismatch')
        validate_sidecar(db, name, raw)
        _write(stage, name, raw)
    _assistant_pairs(stage, inventory.entries)
    if any(entry['filename'].startswith('state/transcription/') for entry in inventory.entries):
        _model('transcription_backup').validate_snapshot(db, stage, inventory.entries, cancelled=cancelled)
    entries = list(inventory.entries)
    names = {entry['filename'] for entry in entries}
    total = sum(entry['size_bytes'] for entry in entries)
    for entry in inventory.entries:
        _check_cancelled(cancelled)
        name = entry['filename']
        if not re.fullmatch(r'state/assistant/[0-9a-f]{32}\.json', name):
            continue
        marker_name = name[:-5] + '.restore-review.json'
        if marker_name in names:
            continue
        raw = read_regular(stage / name, sidecar_limit(name))
        marker = _model('assistant.backup_state').restore_review_bytes(name, raw)
        if len(entries) >= MAX_FILES or total + len(marker) > MAX_BYTES:
            raise RestoreError('Restored assistant review markers exceed the sidecar budget.')
        validate_sidecar(db, marker_name, marker)
        _write(stage, marker_name, marker)
        entries.append(dict(filename=marker_name, size_bytes=len(marker), sha256=sha256(marker).hexdigest()))
        total += len(marker)
    _check_cancelled(cancelled)
    return tuple(entries)


def publish(stage, destination, entries, *, sync_directory):
    parents = set()
    for entry in entries:
        name = entry['filename']
        path = destination / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.link(stage / name, path)
        parents.update(p for p in path.parents if p == destination or p.is_relative_to(destination))
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        sync_directory(parent)
