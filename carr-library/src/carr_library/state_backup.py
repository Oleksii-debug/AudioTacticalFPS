"""Manifest-bound private application sidecars for complete library backups.

Snapshot committed JSON/project files and optionally generated audio. Atomic
writers may continue: a changing file causes a clear retryable failure, never
a successful partial snapshot. Rebuildable audio cache is optional; a restored
render checks every receipt and regenerates missing/corrupt chunks.
"""
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import unicodedata

from .durable_state import local_path, regular, sync_directory
from . import backup_effect_presets, backup_playback_queue, backup_book_batches

MAX_STATE_FILES = 100_000
MAX_STATE_BYTES = 32 * 1024**3


def _local_studio_origin(path):
    parts = tuple(unicodedata.normalize('NFC', part).casefold() for part in path.parts)
    return parts[:2] == ('studio', '.local-origin.json')


def _local_transcription_origin(path):
    parts = tuple(unicodedata.normalize('NFC', part).casefold() for part in path.parts)
    return parts[:2] == ('transcription', '.local-origin.json')


def relative_name(value):
    if (not isinstance(value, str) or not value
            or any(c in '<>:"\\|?*' or ord(c) < 32 for c in value)):
        raise ValueError('Invalid state manifest path.')
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or str(path) != value
            or any(x in ('.', '..') or x.endswith((' ', '.')) for x in path.parts)):
        raise ValueError('Invalid state manifest path.')
    # Win32 also reserves superscript 1, 2 and 3 for COM/LPT devices.
    # A name valid on a POSIX source must not create an unrestorable backup.
    digits = '123456789\u00b9\u00b2\u00b3'
    reserved = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$',
                *(f'{prefix}{i}' for prefix in ('com', 'lpt') for i in digits)}
    if any(x.split('.')[0].rstrip(' ').casefold() in reserved for x in path.parts):
        raise ValueError('Invalid portable state filename.')
    if _local_studio_origin(path):
        raise ValueError('Local Studio origin authority must not be restored from a backup.')
    if _local_transcription_origin(path):
        raise ValueError('Local transcription origin authority must not be restored from a backup.')
    return path


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _include(path, generated):
    name = 'state/' + path.as_posix()
    if backup_book_batches.reserved(name):
        return not backup_book_batches.ignored(name)
    if backup_effect_presets.reserved(name):
        return not backup_effect_presets.ignored(name)
    return (not _local_studio_origin(path) and not _local_transcription_origin(path)
            and not any(part.startswith('.pending-') or part.endswith(('.lock', '.tmp')) for part in path.parts)
            and (generated or 'cache' not in path.parts))


def _walk_error(error):
    # os.walk silently suppresses directory enumeration errors by default.
    # An unreadable subtree is not evidence that it contains no user state.
    raise error


def snapshot_state(root, target, *, include_generated=False, max_bytes=MAX_STATE_BYTES,
                   cancelled=lambda: False):
    root = Path(root)
    if cancelled():
        raise InterruptedError('Backup cancelled.')
    if not os.path.lexists(root):
        return []
    root = local_path(root, exists=True)
    regular(root, directory=True)
    def inventory():
        found = {}
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=_walk_error):
            if cancelled():
                raise InterruptedError('Backup cancelled.')
            for name in dirs:
                regular(Path(directory) / name, directory=True)
                backup_effect_presets.validate_directory(
                    'state/' + (Path(directory) / name).relative_to(root).as_posix())
                backup_playback_queue.validate_directory(
                    'state/' + (Path(directory) / name).relative_to(root).as_posix())
                backup_book_batches.validate_directory(
                    'state/' + (Path(directory) / name).relative_to(root).as_posix())
            dirs[:] = [name for name in dirs if _include((Path(directory) / name).relative_to(root), include_generated)]
            for name in sorted(files):
                source = Path(directory) / name
                relative = source.relative_to(root)
                if _include(relative, include_generated):
                    found[relative] = _stamp(regular(source))
                    if len(found) > MAX_STATE_FILES:
                        raise ValueError('Application state exceeds the file budget.')
        return found
    before_inventory = inventory()
    backup_book_batches.collection_budgets(
        ({'filename': 'state/' + name.as_posix(), 'size_bytes': stamp[2]}
         for name, stamp in before_inventory.items()), cancelled=cancelled)
    backup_playback_queue.collection_budgets(
        ({'filename': 'state/' + name.as_posix(), 'size_bytes': stamp[2]}
         for name, stamp in before_inventory.items()), cancelled=cancelled)
    backup_effect_presets.collection_budgets(
        ({'filename': 'state/' + name.as_posix(), 'size_bytes': stamp[2]}
         for name, stamp in before_inventory.items()), cancelled=cancelled)
    entries, total = [], 0
    for relative in sorted(before_inventory):
            source = root / relative
            relative_name(relative.as_posix())
            if source.suffix.casefold() in ('.sqlite', '.db') or source.name.endswith(('-wal', '-shm', '-journal')):
                raise ValueError('An active state database needs its own consistent snapshot adapter.')
            before = regular(source)
            if _stamp(before) != before_inventory[relative]:
                raise ValueError('Application state changed during backup; retry after saving.')
            total += before.st_size
            if len(entries) >= MAX_STATE_FILES or total > max_bytes:
                raise ValueError('Application state exceeds the backup budget.')
            destination = Path(target) / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest, count = sha256(), 0
            with source.open('rb') as read, destination.open('xb') as write:
                if _stamp(os.fstat(read.fileno())) != _stamp(before):
                    raise ValueError('Application state changed during backup; retry after saving.')
                while block := read.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError('Backup cancelled.')
                    count += len(block)
                    if count > before.st_size:
                        raise ValueError('Application state grew during backup; retry after saving.')
                    digest.update(block)
                    write.write(block)
                write.flush()
                os.fsync(write.fileno())
                if _stamp(os.fstat(read.fileno())) != _stamp(before):
                    raise ValueError('Application state changed during backup; retry after saving.')
            if count != before.st_size or _stamp(regular(source)) != _stamp(before):
                raise ValueError('Application state changed during backup; retry after saving.')
            entries.append({'filename': relative.as_posix(), 'size_bytes': count, 'sha256': digest.hexdigest()})
    if inventory() != before_inventory:
        raise ValueError('Application state changed during backup; retry after saving.')
    # A Windows creation timestamp is not a write counter, and last-write
    # timestamps may be coarse or restored. Check copied content again before
    # accepting the complete snapshot, without buffering whole state files.
    for entry in entries:
        if cancelled():
            raise InterruptedError('Backup cancelled.')
        relative = Path(entry['filename'])
        source = local_path(root / relative, root=root, exists=True)
        expected = before_inventory[relative]
        digest, count = sha256(), 0
        if _stamp(regular(source)) != expected:
            raise ValueError('Application state changed during backup; retry after saving.')
        with source.open('rb') as stream:
            if _stamp(os.fstat(stream.fileno())) != expected:
                raise ValueError('Application state changed during backup; retry after saving.')
            while block := stream.read(min(1024 * 1024, entry['size_bytes'] + 1 - count)):
                if cancelled():
                    raise InterruptedError('Backup cancelled.')
                count += len(block)
                if count > entry['size_bytes']:
                    raise ValueError('Application state changed during backup; retry after saving.')
                digest.update(block)
            if _stamp(os.fstat(stream.fileno())) != expected:
                raise ValueError('Application state changed during backup; retry after saving.')
        if (count != entry['size_bytes'] or digest.hexdigest() != entry['sha256']
                or _stamp(regular(source)) != expected):
            raise ValueError('Application state changed during backup; retry after saving.')
    if inventory() != before_inventory:
        raise ValueError('Application state changed during backup; retry after saving.')
    # A snapshot must pass the same portable manifest checks as a restore.
    # Keep this after content revalidation and before publication.
    validate_state(entries)
    _validate_managed_clip_inputs(target, entries, cancelled=cancelled)
    from .assistant.templates import validate_copied_templates
    validate_copied_templates(target, entries, cancelled=cancelled)
    from .backup_book_audio import validate_collection
    validate_collection(None, target, entries, cancelled=cancelled)
    backup_effect_presets.validate_collection(target, entries, cancelled=cancelled)
    backup_playback_queue.validate_collection(target, entries, cancelled=cancelled)
    backup_book_batches.validate_collection(target, entries, cancelled=cancelled)
    _validate_reader_preferences(target, entries, cancelled=cancelled)
    from .audio.recording import verify_retained_recordings
    verify_retained_recordings(target, entries, cancelled=cancelled)
    return sorted(entries, key=lambda entry: entry['filename'])


def _validate_reader_preferences(copied_root, entries, *, cancelled=lambda: False):
    """Validate new passive reader records in the already-copied state only."""
    from .durable_state import read_json_snapshot
    from .speech.tabs import validate_collection
    from .speech.presentation import ReadingPresentation, MAX_PROFILE_BYTES
    from .runtime_settings import validate_voice_preferences

    names = {item['filename'] for item in entries}
    validate_collection(copied_root, entries, state_only=True, cancelled=cancelled)
    if cancelled():
        raise InterruptedError('Reader preference backup validation cancelled.')
    appearance = 'speech/reading-presentation.json'
    if appearance in names:
        ReadingPresentation.from_dict(read_json_snapshot(
            copied_root / appearance, max_bytes=MAX_PROFILE_BYTES)[0])
    engines = 'audio/engines.json'
    if engines in names:
        value = read_json_snapshot(copied_root / engines)[0]
        if isinstance(value, dict) and 'voice_preferences' in value:
            validate_voice_preferences(value['voice_preferences'])


def validate_state(entries):
    if not isinstance(entries, list) or len(entries) > MAX_STATE_FILES:
        raise ValueError('Invalid application state manifest.')
    names, directories, portable_paths, total = set(), set(), {}, 0
    for item in entries:
        if (not isinstance(item, dict) or set(item) != {'filename', 'size_bytes', 'sha256'}
                or type(item['size_bytes']) is not int or item['size_bytes'] < 0
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('Invalid application state manifest.')
        name = relative_name(item['filename']).as_posix()
        key = unicodedata.normalize('NFC', name).casefold()
        if key in names:
            raise ValueError('Duplicate application state filename.')
        names.add(key)
        # Reuse the typed sidecar portable-path policy, including ancestors.
        # A/one and a/two may coexist on POSIX but alias on Windows.
        path = PurePosixPath(name)
        for component in (path, *path.parents):
            current = str(component)
            if current == '.':
                continue
            portable = unicodedata.normalize('NFC', current).casefold()
            previous = portable_paths.setdefault(portable, current)
            if previous != current:
                raise ValueError('Application state paths collide on a portable filesystem.')
            if component != path:
                directories.add(portable)
        total += item['size_bytes']
    if names & directories:
        raise ValueError('Application state file and directory paths collide.')
    if total > MAX_STATE_BYTES:
        raise ValueError('Application state manifest exceeds its byte budget.')
    backup_effect_presets.collection_budgets(
        ({**entry, 'filename': 'state/' + entry['filename']} for entry in entries))
    backup_playback_queue.collection_budgets(
        ({**entry, 'filename': 'state/' + entry['filename']} for entry in entries))
    backup_book_batches.collection_budgets(
        ({**entry, 'filename': 'state/' + entry['filename']} for entry in entries))


def _validate_managed_clip_inputs(copied_root, entries, *, cancelled=lambda: False):
    """Check known portable Studio records against this already-copied state.

    Opaque historical sidecars and schema1 declarations remain byte archives;
    their external paths are never opened. A Studio local-origin nonce is
    excluded separately and can never supply restored output authority.
    """
    from .durable_state import MAX_JSON_BYTES, _unique
    from .studio.managed_inputs import (
        managed_input_sha256, project_managed_inputs, resolve_managed_input,
    )
    from .studio.project import Project

    copied_root = Path(copied_root)
    by_name = {entry['filename']: entry for entry in entries}
    checked, processed_jobs = set(), []

    def copied_record(entry):
        # Keep only one bounded record at a time, including the second pass.
        if entry['size_bytes'] > MAX_JSON_BYTES:
            return None
        name = entry['filename']
        path = local_path(copied_root / name, root=copied_root, exists=True)
        before = regular(path)
        if before.st_size != entry['size_bytes']:
            raise ValueError('Copied Studio state differs from its manifest.')
        raw = bytearray()
        with path.open('rb') as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise ValueError('Copied Studio state changed before validation.')
            while block := stream.read(min(1024 * 1024, before.st_size + 1 - len(raw))):
                if cancelled():
                    raise InterruptedError('Studio input backup validation cancelled.')
                raw.extend(block)
                if len(raw) > before.st_size:
                    raise ValueError('Copied Studio state grew during validation.')
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise ValueError('Copied Studio state changed during validation.')
        if (len(raw) != entry['size_bytes'] or sha256(raw).hexdigest() != entry['sha256']
                or _stamp(regular(path)) != _stamp(before)):
            raise ValueError('Copied Studio state differs from its manifest.')
        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite state number.')))
        except (UnicodeError, ValueError, RecursionError):
            # Preserve opaque historical sidecars, including non-JSON bytes.
            return None
        return value

    identifier = r'[a-zA-Z0-9_-]{1,80}'
    for entry in entries:
        if cancelled():
            raise InterruptedError('Studio input backup validation cancelled.')
        name = entry['filename']
        current = re.fullmatch(rf'studio/projects/({identifier})/project\.json', name, re.IGNORECASE)
        revision = re.fullmatch(rf'studio/projects/({identifier})/revisions/([0-9]{{8,}})-([0-9a-f]{{64}})\.json', name, re.IGNORECASE)
        job = re.fullmatch(rf'studio/jobs/({identifier})/job\.json', name, re.IGNORECASE)
        exported = re.fullmatch(rf'studio/jobs/({identifier})/project\.json', name, re.IGNORECASE)
        if not any((current, revision, job, exported)) or entry['size_bytes'] > MAX_JSON_BYTES:
            # A larger opaque sidecar cannot be a readable ProjectStore record.
            continue
        value = copied_record(entry)
        if not isinstance(value, dict):
            continue
        declaration = value.get('project') if job else value
        if (not isinstance(declaration, dict)
                or type(declaration.get('schema')) is not int or declaration['schema'] not in (2, 3)):
            continue
        project = Project.from_dict(declaration)
        if current and project.id != current[1]:
            raise ValueError('Copied Studio project directory differs from its identity.')
        if revision and (project.id != revision[1]
                or revision[2] != f'{project.revision:08d}' or revision[3] != project.fingerprint):
            raise ValueError('Copied Studio revision differs from its identity.')
        if job and (type(value.get('schema')) is not int or value['schema'] != 1
                or value.get('id') != job[1] or value.get('fingerprint') != project.fingerprint):
            raise ValueError('Copied Studio job differs from its project identity.')
        if job and project.schema == 3:
            processed_jobs.append((name, job[1]))
        for declared in project_managed_inputs(project):
            expected = managed_input_sha256(declared)
            incoming = by_name.get('studio/' + declared)
            if (incoming is None or incoming['sha256'] != expected
                    or incoming['size_bytes'] <= 0):
                raise ValueError('A managed Studio input is missing or differs from its content identity.')
            if declared not in checked:
                resolve_managed_input(copied_root / 'studio', declared, cancelled=cancelled)
                checked.add(declared)
    # Schema3 processing metadata is passive, but must agree with its saved
    # job. Inspect copied records only; output destinations stay inert.
    from .durable_state import encode
    from .studio.project import validate_project_effects
    from .backup_producers import _studio_job, _timeline
    for name, identity in processed_jobs:
        if cancelled():
            raise InterruptedError('Studio effect backup validation cancelled.')
        value = copied_record(by_name[name])
        _studio_job(value, identity)
        project = Project.from_dict(value['project'])
        parent = name.rsplit('/', 1)[0]
        exported_name, timeline_name = parent + '/project.json', parent + '/timeline.json'
        if exported_name in by_name:
            exported = Project.from_dict(copied_record(by_name[exported_name]))
            if encode(exported.to_dict()) != encode(project.to_dict()):
                raise ValueError('Copied exported Studio project differs from its saved job.')
        if timeline_name in by_name:
            result = copied_record(by_name[timeline_name])
            _timeline(result)
            validate_project_effects(project, result)
            if (result['project'] != project.id or result['revision'] != project.revision
                    or result['source_sha256'] != sha256(project.original_script.encode()).hexdigest()):
                raise ValueError('Copied timeline belongs to another Studio project.')
            if 'result' in value and encode(result) != encode(value['result']):
                raise ValueError('Copied timeline differs from the saved Studio result.')


def restore_state(source, target, entries):
    validate_state(entries)
    if os.path.lexists(Path(target) / 'studio' / '.local-origin.json'):
        raise ValueError('Choose a restore target without existing local Studio origin authority.')
    if os.path.lexists(Path(target) / 'transcription' / '.local-origin.json'):
        raise ValueError('Choose a restore target without existing local transcription origin authority.')
    source = local_path(source, exists=True)
    regular(source, directory=True)
    missing = {item['filename'] for item in entries}
    for directory, dirs, files in os.walk(source, followlinks=False, onerror=_walk_error):
        for name in dirs:
            regular(Path(directory) / name, directory=True)
            backup_effect_presets.validate_directory(
                'state/' + (Path(directory) / name).relative_to(source).as_posix())
            backup_playback_queue.validate_directory(
                'state/' + (Path(directory) / name).relative_to(source).as_posix())
            backup_book_batches.validate_directory(
                'state/' + (Path(directory) / name).relative_to(source).as_posix())
        for name in files:
            path = Path(directory) / name
            regular(path)
            # Reject the first unexpected file rather than accumulating an
            # attacker-controlled inventory beyond the manifest's file budget.
            relative = path.relative_to(source).as_posix()
            if relative not in missing:
                raise ValueError('Backup has missing or unlisted application state.')
            missing.remove(relative)
    if missing:
        raise ValueError('Backup has missing or unlisted application state.')
    backup_effect_presets.validate_collection(source, entries)
    backup_playback_queue.validate_collection(source, entries)
    backup_book_batches.validate_collection(source, entries)
    from .audio.recording import verify_retained_recordings
    verify_retained_recordings(source, entries)
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for item in entries:
        incoming = local_path(source / item['filename'], root=source, exists=True)
        if regular(incoming).st_size != item['size_bytes']:
            raise ValueError('Application state size mismatch.')
        destination = target / item['filename']
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, size = sha256(), 0
        with incoming.open('rb') as read, destination.open('xb') as write:
            while block := read.read(1024 * 1024):
                size += len(block)
                if size > item['size_bytes']:
                    raise ValueError('Application state changed during restore.')
                digest.update(block)
                write.write(block)
            write.flush()
            os.fsync(write.fileno())
        if size != item['size_bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('Application state checksum mismatch.')
    _validate_managed_clip_inputs(target, entries)
    from .assistant.templates import validate_copied_templates
    validate_copied_templates(target, entries)
    from .backup_book_audio import validate_collection
    validate_collection(None, target, entries)
    backup_effect_presets.validate_collection(target, entries)
    backup_playback_queue.validate_collection(target, entries)
    backup_book_batches.validate_collection(target, entries)
    _validate_reader_preferences(target, entries)
    verify_retained_recordings(target, entries)
    if (target / 'transcription').exists():
        from .integrations.transcription_protocol import prepare_restored_state
        prepare_restored_state(target)
    sync_directory(target)


def publish_state(source, target):
    target = Path(target)
    target.mkdir(mode=0o700)
    for path in sorted(Path(source).rglob('*')):
        relative = path.relative_to(source)
        if path.is_dir():
            (target / relative).mkdir(exist_ok=True)
        else:
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            os.link(path, target / relative)
    sync_directory(target)
