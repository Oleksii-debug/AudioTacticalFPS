"""Typed V5 additions to the canonical state and pending-import backup.

The original V1--V4 formats and their producers remain authoritative. This
module binds exact source positions, typed reading sessions, and inert legacy
archives to that snapshot. Selected Studio inputs are copied under an exact
receipt; engine and publication paths remain declarations. Restore reads only
verified staged media and never probes or executes recorded external inputs.
"""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3

from .durable_state import regular
from .ingest.bundle import _unique_object
from .ingest.restore import RestoreError
from .reader.reading_state import FILENAME as READING, MAX_BYTES as MAX_READING, _decode
from .reader.live_state import FILENAME as LIVE, MAX_BYTES as MAX_LIVE, validate_live_state
from .speech.dordiai_settings import (FILENAME as DORDIAI, MAX_BYTES as MAX_DORDIAI,
    validate_settings_bytes, restored_settings_bytes)
from .audio.transcription_settings import (FILENAME as ASR_SETTINGS, MAX_BYTES as MAX_ASR_SETTINGS,
    validate_settings_bytes as validate_asr_settings, restored_settings_bytes as restored_asr_settings)
from .state_backup import relative_name, validate_portable_paths

DIRECTORY = 'extensions'
LEGACY = 'legacy-backups'
MAX_FILES = 100_000
MAX_BYTES = 80 * 1024**3
MAX_JSON = 64 * 1024**2
SIDECARS = {READING: MAX_READING, LIVE: MAX_LIVE, DORDIAI: MAX_DORDIAI, ASR_SETTINGS: MAX_ASR_SETTINGS}
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_READER = re.compile(r'speech/[0-9a-f]{64}\.reader\.json\Z')
_PROJECT = re.compile(r'studio/projects/[a-zA-Z0-9_-]{1,80}/(?:project\.json|revisions/[0-9]{8,}-[0-9a-f]{64}\.json)\Z')
_TRANSCRIPTION = re.compile(r'transcription/[0-9a-f]{64}/job\.json\Z')
_PUBLICATION = re.compile(r'studio/publications/[0-9a-f]{32}\.json\Z')
_RECORDING = re.compile(r'studio/recordings/[0-9a-f]{32}\.json\Z')
_RECORDING_WAV = re.compile(r'studio/recordings/[0-9a-f]{32}\.wav\Z')


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Backup cancelled.')


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def _json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON number.')))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RestoreError('Invalid typed backup JSON.') from exc


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_bytes(path, maximum, *, optional=False):
    try:
        before = regular(path)
    except FileNotFoundError:
        if optional:
            return None
        raise
    if not 0 < before.st_size <= maximum:
        raise RestoreError('Typed backup file exceeds its byte budget.')
    with Path(path).open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise RestoreError('Typed backup file changed while opening.')
        raw = stream.read(maximum + 1)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise RestoreError('Typed backup file changed while reading.')
    if len(raw) != before.st_size or _stamp(regular(path)) != _stamp(before):
        raise RestoreError('Typed backup file changed during snapshot.')
    return raw


def write_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def workspaces(database):
    with closing(sqlite3.connect(Path(database).as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        return sorted(row[0] for row in db.execute('SELECT id FROM workspace'))


def _source_text(db, location):
    row = db.execute('''SELECT p.raw FROM revision r
        JOIN sealed s ON s.revision=r.id JOIN page p ON p.revision=r.id
        JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition AND e.asset=r.asset
        WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=? AND p.digest=?
        AND ((?='physical_page' AND p.physical=?) OR
             (?='logical_segment' AND p.physical IS NULL AND p.ordinal=?))''',
        (location.workspace, location.edition, location.revision, location.asset_sha256,
         location.text_sha256, location.kind, location.index, location.kind, location.index)).fetchone()
    if row is None or sha256(row[0].encode('utf-8')).hexdigest() != location.text_sha256:
        raise RestoreError('Reading recovery does not match its exact authenticated source.')
    return row[0]


def validate_sidecars(database, values, allowed):
    with closing(sqlite3.connect(Path(database).as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        for name, raw in values.items():
            if name == READING:
                positions, bookmarks, _ = _decode(raw.decode('utf-8'))
                locations = [row[0] for row in positions.values()] + [b.location for b in bookmarks.values()]
                for location in locations:
                    if location.workspace not in allowed:
                        raise RestoreError('Reading state is outside the authorized backup scope.')
                    _source_text(db, location)
            elif name == LIVE:
                for item in validate_live_state(raw):
                    if item.location.workspace not in allowed or len(_source_text(db, item.location)) != item.text_length:
                        raise RestoreError('Live-reading position does not match its exact source length or scope.')
            elif name == DORDIAI:
                validate_settings_bytes(raw)
            elif name == ASR_SETTINGS:
                validate_asr_settings(raw)
            else:
                raise RestoreError('Unsupported typed backup sidecar.')


def _required(component, kind, reference):
    if (not isinstance(reference, str) or not reference or len(reference) > 32768
            or any(ord(c) < 32 for c in reference)):
        raise RestoreError('Invalid declared external backup input.')
    return dict(component=component, kind=kind, reference=reference, included=False)


def _ordered_inputs(values):
    unique = {_encoded(item): item for item in values}
    return [unique[key] for key in sorted(unique)]


def inspect_media_state(root, entries, *, independent, cancelled=lambda: False):
    """Validate copied media state without following recorded external paths."""
    from .studio.backup_inputs import inspect_snapshot_inputs, _record

    root = Path(root)
    by_name = {entry['filename']: entry for entry in entries}
    receipt = inspect_snapshot_inputs(root, entries, cancelled=cancelled)
    media = dict(studio_input_receipt=receipt is not None, studio_publications=[],
                 studio_recordings=[], studio_unverified_recordings=[])
    required = []
    if receipt is not None:
        # Completed or queued jobs can outlive the current editor project.
        # Keep their voice/music dependencies explicit as well.
        for binding in receipt['projects']:
            _check(cancelled)
            found = _record(root, binding['filename'])
            if found is None:
                raise RestoreError('A verified Studio project binding disappeared.')
            project = found[2]
            component = 'state/' + binding['filename']
            for role in project.roles:
                if role.voice_id:
                    required.append(_required(component, 'local-voice', role.voice_id))
            for scene in project.scenes:
                if scene.music is not None and scene.music.engine != 'procedural':
                    required.append(_required(component, 'music-engine', scene.music.engine))
    for name in sorted(by_name):
        _check(cancelled)
        if name.casefold().startswith('studio/publications/'):
            if not _PUBLICATION.fullmatch(name):
                raise RestoreError('Invalid typed Studio publication filename.')
            from .studio.publishing import validate_publication_receipt
            record = validate_publication_receipt(
                _json(read_bytes(root / name, MAX_JSON)),
                publication_id=Path(name).stem)
            media['studio_publications'].append(name)
            required.append(_required('state/' + name, 'publication-output', record['output']))
        elif name.casefold().startswith('studio/recordings/'):
            if _RECORDING.fullmatch(name):
                from .audio.recording import validate_recording_receipt
                record = validate_recording_receipt(
                    _json(read_bytes(root / name, MAX_JSON)),
                    recording_id=Path(name).stem)
                audio = by_name.get(name.removesuffix('.json') + '.wav')
                if (audio is None or audio['size_bytes'] != record['audio']['size_bytes']
                        or audio['sha256'] != record['audio']['sha256']):
                    raise RestoreError('A recording receipt differs from its retained original.')
                from .speech.runtime import _wave_info
                expected_pcm = tuple(record['audio'][key] for key in
                    ('sample_rate', 'channels', 'frames', 'sample_width'))
                if _wave_info(root / audio['filename'], cancelled=cancelled,
                        max_output_bytes=record['limits']['max_bytes']) != expected_pcm:
                    raise RestoreError('A recording receipt differs from its retained PCM header.')
                media['studio_recordings'].append(name)
            elif _RECORDING_WAV.fullmatch(name):
                if name.removesuffix('.wav') + '.json' not in by_name:
                    media['studio_unverified_recordings'].append(name)
            else:
                raise RestoreError('Invalid typed Studio recording filename.')
    if not independent and any(media.values()):
        raise RestoreError('Independent Studio media is outside the workspace backup scope.')
    retained = frozenset(receipt['inputs']) if receipt is not None else frozenset()
    return media, required, retained


def _has_media(media):
    return any(media.values())


def inspect_state(root, entries, database, allowed, *, independent, cancelled=lambda: False,
                  retained_inputs=frozenset()):
    """Authenticate typed producers without interpreting unrelated V3 state."""
    from .speech.session import decode_reader_resume
    from .studio.project import Project
    sessions, projects, transcriptions, required = [], [], [], []
    saved_names = {entry['filename'] for entry in entries}
    with closing(sqlite3.connect(Path(database).as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        for entry in entries:
            _check(cancelled)
            name = entry['filename']
            if name.startswith('transcription/'):
                parts = name.split('/')
                if (len(parts) < 3 or not _HASH.fullmatch(parts[1])
                        or '/'.join(parts[:2]) + '/job.json' not in saved_names):
                    raise RestoreError('A transcription state file has no typed owning job.')
            if _READER.fullmatch(name):
                raw = read_bytes(Path(root) / name, MAX_JSON)
                record = decode_reader_resume(_json(raw))
                if not independent and not record.source_locations:
                    raise RestoreError('An unbound reading draft cannot enter a workspace-scoped backup.')
                if any(location.workspace not in allowed for location in record.source_locations):
                    raise RestoreError('Reading draft is outside the authorized backup scope.')
                if record.source_locations:
                    text = '\n\n'.join(_source_text(db, location) for location in record.source_locations)
                    if text != record.document.source_text:
                        raise RestoreError('Reading draft source text differs from its authenticated pages.')
                sessions.append(name)
                if record.voice_id:
                    required.append(_required('state/' + name, 'local-voice', record.voice_id))
            elif _PROJECT.fullmatch(name):
                if not independent:
                    raise RestoreError('Independent audio projects cannot enter a workspace-scoped backup.')
                project = Project.from_dict(_json(read_bytes(Path(root) / name, MAX_JSON)))
                if project.id != name.split('/')[2]:
                    raise RestoreError('Audio project identity differs from its saved directory.')
                projects.append(name)
                for role in project.roles:
                    if role.voice_id:
                        required.append(_required('state/' + name, 'local-voice', role.voice_id))
                for scene in project.scenes:
                    for cue in scene.cues:
                        if cue.path not in retained_inputs:
                            required.append(_required('state/' + name, 'audio-file', cue.path))
                    if scene.music is not None and scene.music.engine != 'procedural':
                        required.append(_required('state/' + name, 'music-engine', scene.music.engine))
            elif _TRANSCRIPTION.fullmatch(name):
                if not independent:
                    raise RestoreError('Independent transcription jobs cannot enter a workspace-scoped backup.')
                from .audio.transcription import validate_saved_snapshot
                record = _json(read_bytes(Path(root) / name, MAX_JSON))
                validate_saved_snapshot((Path(root) / name).parent, record, cancelled=cancelled)
                transcriptions.append(name)
                required.append(_required('state/' + name, 'local-asr-runtime', record['request']['runtime']))
            elif not independent:
                raise RestoreError('Only explicitly source-bound reading drafts are supported in a scoped state snapshot.')
    return sorted(sessions), sorted(projects), sorted(transcriptions), _ordered_inputs(required)


def scoped_state_selection(root, database, allowed):
    """Return exact bound draft bytes; slot hashes never grant workspace scope."""
    from .speech.session import decode_reader_resume
    root = Path(root)
    if not os.path.lexists(root):
        return {}
    regular(root, directory=True)
    directory = root / 'speech'
    if not os.path.lexists(directory):
        return {}
    regular(directory, directory=True)
    selected = {}
    for path in sorted(directory.iterdir()):
        name = path.relative_to(root).as_posix()
        if not _READER.fullmatch(name):
            continue
        raw = read_bytes(path, MAX_JSON)
        record = decode_reader_resume(_json(raw))
        if record.source_locations and all(location.workspace in allowed for location in record.source_locations):
            selected[name] = sha256(raw).hexdigest()
    entries = [dict(filename=name) for name in selected]
    inspect_state(root, entries, database, allowed, independent=False)
    return selected


def snapshot(root, target, database, state_entries, *, independent=True, cancelled=lambda: False):
    root, target = Path(root), Path(target)
    allowed = workspaces(database)
    sidecars = {}
    for name, maximum in SIDECARS.items():
        _check(cancelled)
        if name in (DORDIAI, ASR_SETTINGS) and not independent:
            continue
        raw = read_bytes(root / name, maximum, optional=True)
        if raw is not None:
            sidecars[name] = raw
    validate_sidecars(database, sidecars, allowed)
    media, media_inputs, retained = inspect_media_state(target.parent / 'state', state_entries,
        independent=independent, cancelled=cancelled)
    sessions, projects, transcriptions, required = inspect_state(target.parent / 'state', state_entries,
        database, allowed, independent=independent, cancelled=cancelled, retained_inputs=retained)
    required.extend(media_inputs)
    archives = []
    if independent and os.path.lexists(root / LEGACY):
        from .backup_compat.archive import snapshot_archives
        archives, archive_inputs = snapshot_archives(root / LEGACY, target / LEGACY, cancelled=cancelled)
        required.extend(archive_inputs)
    if (not sidecars and not archives and not sessions and not projects and not transcriptions
            and not _has_media(media) and independent):
        return None
    target.mkdir(exist_ok=True, mode=0o700)
    for name, raw in sidecars.items():
        write_bytes(target / name, raw)
    if DORDIAI in sidecars:
        settings = validate_settings_bytes(sidecars[DORDIAI])
        for field in ('runtime_directory', 'python_executable'):
            required.append(_required(DORDIAI, 'dordiai-runtime', settings[field]))
    if ASR_SETTINGS in sidecars:
        settings = validate_asr_settings(sidecars[ASR_SETTINGS])
        if settings['manifest_path']:
            required.append(_required(ASR_SETTINGS, 'local-asr-manifest', settings['manifest_path']))
    for item in validate_live_state(sidecars[LIVE]) if LIVE in sidecars else ():
        required.append(_required(LIVE, 'local-voice', item.voice_id))
    files = inventory(target, cancelled=cancelled)
    manifest = dict(version=1, workspaces=allowed, independent_state=independent,
        files=files, reader_sessions=sessions, studio_projects=projects,
        legacy_archives=archives, required_inputs=_ordered_inputs(required))
    if transcriptions or ASR_SETTINGS in sidecars:
        manifest.update(version=2, transcription_jobs=transcriptions)
    if _has_media(media):
        manifest.update(version=3, transcription_jobs=transcriptions, **media)
    validate_manifest(manifest)
    return manifest


def _tree(root, *, cancelled=lambda: False):
    regular(root, directory=True)
    files, directories, folded = {}, set(), set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        _check(cancelled)
        if len(Path(directory).relative_to(root).parts) > 16:
            raise RestoreError('Typed backup directory nesting exceeds its budget.')
        for name in dirs:
            path = Path(directory) / name
            regular(path, directory=True)
            relative = relative_name(path.relative_to(root).as_posix()).as_posix()
            if relative.casefold() in folded:
                raise RestoreError('Duplicate typed backup path.')
            folded.add(relative.casefold())
            directories.add(relative)
        for name in names:
            path = Path(directory) / name
            info = regular(path)
            relative = relative_name(path.relative_to(root).as_posix()).as_posix()
            if relative.casefold() in folded:
                raise RestoreError('Duplicate typed backup path.')
            folded.add(relative.casefold())
            files[relative] = info
        if len(files) > MAX_FILES or len(folded) > MAX_FILES * 4:
            raise RestoreError('Typed backup exceeds its file budget.')
    return files, directories


def _ancestors(names):
    return {parent.as_posix() for name in names for parent in Path(name).parents if parent != Path('.')}


def _legacy_original_directories(names):
    # Empty catalogs still carry the exact required originals directory. It is
    # the sole empty-directory exception in the typed extension inventory.
    return {name.rsplit('/', 1)[0] + '/originals' for name in names
            if re.fullmatch(r'legacy-backups/[0-9a-f]{64}/manifest\.json', name)}


def inventory(root, *, cancelled=lambda: False):
    before, directories = _tree(root, cancelled=cancelled)
    if directories != _ancestors(before) | _legacy_original_directories(before):
        raise RestoreError('Typed backup contains an unlisted empty directory.')
    result, total = [], 0
    for name, info in sorted(before.items()):
        total += info.st_size
        if info.st_size < 1 or total > MAX_BYTES:
            raise RestoreError('Typed backup exceeds its byte budget.')
        digest, size = sha256(), 0
        with (Path(root) / name).open('rb') as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(info):
                raise RestoreError('Typed backup file changed.')
            while block := stream.read(1024 * 1024):
                _check(cancelled)
                size += len(block)
                if size > info.st_size:
                    raise RestoreError('Typed backup file grew.')
                digest.update(block)
        if size != info.st_size:
            raise RestoreError('Typed backup file changed.')
        result.append(dict(filename=name, size_bytes=size, sha256=digest.hexdigest()))
    after, later_directories = _tree(root, cancelled=cancelled)
    if ({name: _stamp(info) for name, info in before.items()} !=
            {name: _stamp(info) for name, info in after.items()} or directories != later_directories):
        raise RestoreError('Typed backup inventory changed.')
    return result


def validate_manifest(value):
    fields = {'version', 'workspaces', 'independent_state', 'files', 'reader_sessions',
              'studio_projects', 'legacy_archives', 'required_inputs'}
    if type(value) is dict and value.get('version') in (2, 3):
        fields.add('transcription_jobs')
    if type(value) is dict and value.get('version') == 3:
        fields.update(('studio_input_receipt', 'studio_publications', 'studio_recordings',
                       'studio_unverified_recordings'))
    if (type(value) is not dict or set(value) != fields or type(value['version']) is not int
or value['version'] not in (1, 2, 3) or type(value['independent_state']) is not bool
            or type(value['workspaces']) is not list or len(value['workspaces']) > 10000
            or any(not isinstance(item, str) or not item or len(item) > 512 for item in value['workspaces'])
            or value['workspaces'] != sorted(set(value['workspaces']))):
        raise RestoreError('Unsupported typed backup manifest or scope.')
    files = value['files']
    if type(files) is not list or len(files) > MAX_FILES:
        raise RestoreError('Invalid typed backup file inventory.')
    total, names = 0, set()
    for item in files:
        if (type(item) is not dict or set(item) != {'filename', 'size_bytes', 'sha256'}
                or type(item['size_bytes']) is not int or item['size_bytes'] < 1
                or not isinstance(item['sha256'], str) or not _HASH.fullmatch(item['sha256'])):
            raise RestoreError('Invalid typed backup file record.')
        name = relative_name(item['filename']).as_posix()
        if (name not in SIDECARS and not re.fullmatch(r'legacy-backups/[0-9a-f]{64}(?:\.json|/.+)', name)):
            raise RestoreError('Unsupported typed backup filename.')
        if name.casefold() in names:
            raise RestoreError('Duplicate typed backup filename.')
        names.add(name.casefold())
        total += item['size_bytes']
        if total > MAX_BYTES or (name in SIDECARS and item['size_bytes'] > SIDECARS[name]):
            raise RestoreError('Typed backup exceeds its byte budget.')
    if [item['filename'] for item in files] != sorted(item['filename'] for item in files):
        raise RestoreError('Typed backup files must be ordered.')
    validate_portable_paths(item['filename'] for item in files)
    if value['version'] == 3 and type(value['studio_input_receipt']) is not bool:
        raise RestoreError('Invalid typed Studio input receipt declaration.')
    for field, pattern in (('reader_sessions', _READER), ('studio_projects', _PROJECT),
            ('transcription_jobs', _TRANSCRIPTION), ('studio_publications', _PUBLICATION),
            ('studio_recordings', _RECORDING), ('studio_unverified_recordings', _RECORDING_WAV)):
        values = value.get(field, [])
        if (type(values) is not list or len(values) > MAX_FILES
                or any(not isinstance(name, str) or not pattern.fullmatch(name) for name in values)
                or values != sorted(set(values))):
            raise RestoreError('Invalid typed state producer inventory.')
    archives = value['legacy_archives']
    if type(archives) is not list or len(archives) > 1000:
        raise RestoreError('Invalid legacy archive inventory.')
    keys = []
    for item in archives:
        if (type(item) is not dict or set(item) != {'manifest_sha256', 'format', 'status'}
                or not isinstance(item['manifest_sha256'], str) or not _HASH.fullmatch(item['manifest_sha256'])
                or item['format'] not in ('legacy-sidecars-v3', 'legacy-recovery-v4') or item['status'] != 'inert'):
            raise RestoreError('Invalid legacy archive declaration.')
        keys.append(item['manifest_sha256'])
    if keys != sorted(set(keys)) or {name.split('/')[1].removesuffix('.json') for name in names if name.startswith(LEGACY + '/')} != set(keys):
        raise RestoreError('Legacy archive inventory differs from its declarations.')
    inputs = value['required_inputs']
    if type(inputs) is not list or len(inputs) > MAX_FILES:
        raise RestoreError('Invalid required-input inventory.')
    for item in inputs:
        if (type(item) is not dict or set(item) != {'component', 'kind', 'reference', 'included'}
                or item['included'] is not False or item['kind'] not in
('local-voice', 'audio-file', 'music-engine', 'dordiai-runtime', 'source-file',
                 'local-asr-runtime', 'local-asr-manifest', 'publication-output')
                or not isinstance(item['component'], str)):
            raise RestoreError('Invalid required-input declaration.')
        relative_name(item['component'])
        _required(item['component'], item['kind'], item['reference'])
    if inputs != _ordered_inputs(inputs):
        raise RestoreError('Required inputs must be unique and ordered.')
    if ASR_SETTINGS in names and value['version'] not in (2, 3):
        raise RestoreError('Transcription settings require the typed ASR extension version.')
    if not value['independent_state'] and (archives or DORDIAI in names or ASR_SETTINGS in names
            or value['studio_projects'] or value.get('transcription_jobs')
            or value.get('studio_input_receipt') or value.get('studio_publications')
            or value.get('studio_recordings') or value.get('studio_unverified_recordings')):
        raise RestoreError('Independent private state is outside the workspace backup scope.')
    return value


def restore(source, stage, manifest, state_entries, *, cancelled=lambda: False):
    value = validate_manifest(manifest)
    if workspaces(Path(stage) / 'library.sqlite') != value['workspaces']:
        raise RestoreError('Typed backup workspace scope differs from the catalog.')
    if inventory(source, cancelled=cancelled) != value['files']:
        raise RestoreError('Typed backup digest or inventory mismatch.')
    temporary = Path(stage) / '.verified-extensions'
    # Canonical state copier enforces the same exact bytes. Its 32-GiB state
    # budget is intentionally not reused for larger inert archive components.
    copy_files(source, temporary, value['files'], cancelled=cancelled)
    for name in _legacy_original_directories(item['filename'] for item in value['files']):
        (temporary / name).mkdir(exist_ok=True, mode=0o700)
    sidecars = {name: read_bytes(temporary / name, maximum) for name, maximum in SIDECARS.items()
                if (temporary / name).exists()}
    validate_sidecars(Path(stage) / 'library.sqlite', sidecars, value['workspaces'])
    media, media_inputs, retained = inspect_media_state(Path(stage) / 'state', state_entries,
        independent=value['independent_state'], cancelled=cancelled)
    expected_media = dict(studio_input_receipt=value.get('studio_input_receipt', False),
        studio_publications=value.get('studio_publications', []),
        studio_recordings=value.get('studio_recordings', []),
        studio_unverified_recordings=value.get('studio_unverified_recordings', []))
    if media != expected_media:
        raise RestoreError('Typed Studio media inventory differs from the snapshot.')
    sessions, projects, transcriptions, required = inspect_state(Path(stage) / 'state', state_entries,
        Path(stage) / 'library.sqlite', value['workspaces'], independent=value['independent_state'],
        cancelled=cancelled, retained_inputs=retained)
    required.extend(media_inputs)
    if (sessions != value['reader_sessions'] or projects != value['studio_projects']
            or transcriptions != value.get('transcription_jobs', [])):
        raise RestoreError('Typed state producer inventory differs from the snapshot.')
    paths = []
    if value['legacy_archives']:
        from .backup_compat.archive import verify_archives
        archives, legacy_inputs = verify_archives(temporary / LEGACY, cancelled=cancelled)
        if archives != value['legacy_archives']:
            raise RestoreError('Legacy archive declarations differ from their verified contents.')
        required.extend(legacy_inputs)
        os.rename(temporary / LEGACY, Path(stage) / LEGACY)
        paths.append(LEGACY)
    if DORDIAI in sidecars:
        settings = validate_settings_bytes(sidecars[DORDIAI])
        for field in ('runtime_directory', 'python_executable'):
            required.append(_required(DORDIAI, 'dordiai-runtime', settings[field]))
    if ASR_SETTINGS in sidecars:
        settings = validate_asr_settings(sidecars[ASR_SETTINGS])
        if settings['manifest_path']:
            required.append(_required(ASR_SETTINGS, 'local-asr-manifest', settings['manifest_path']))
    for item in validate_live_state(sidecars[LIVE]) if LIVE in sidecars else ():
        required.append(_required(LIVE, 'local-voice', item.voice_id))
    if _ordered_inputs(required) != value['required_inputs']:
        raise RestoreError('Required external inputs differ from the typed saved state.')
    if transcriptions:
        from .audio.transcription import restored_job
        from .durable_state import atomic_json
        for name in transcriptions:
            path = Path(stage) / 'state' / name
            record = _json(read_bytes(path, MAX_JSON))
            atomic_json(path, restored_job(record), replace=True)
            atomic_json(path.parent / 'control.json', {'command': 'pause'}, replace=True)
    for name, raw in sidecars.items():
        if name == DORDIAI:
            raw = restored_settings_bytes(raw)
        elif name == ASR_SETTINGS:
            raw = restored_asr_settings(raw)
        write_bytes(Path(stage) / name, raw)
        paths.append(name)
    return paths


def copy_files(source, target, entries, *, cancelled=lambda: False):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for item in entries:
        _check(cancelled)
        incoming = Path(source) / relative_name(item['filename'])
        before = regular(incoming)
        if before.st_size != item['size_bytes']:
            raise RestoreError('Backup file size changed.')
        outgoing = target / item['filename']
        outgoing.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest, count = sha256(), 0
        with incoming.open('rb') as read, outgoing.open('xb') as write:
            if _stamp(os.fstat(read.fileno())) != _stamp(before):
                raise RestoreError('Backup file changed while opening.')
            while block := read.read(1024 * 1024):
                _check(cancelled)
                count += len(block)
                if count > item['size_bytes']:
                    raise RestoreError('Backup file grew during copy.')
                digest.update(block)
                write.write(block)
            write.flush()
            os.fsync(write.fileno())
        if (count != item['size_bytes'] or digest.hexdigest() != item['sha256']
                or _stamp(regular(incoming)) != _stamp(before)):
            raise RestoreError('Backup file changed or its digest is invalid.')
