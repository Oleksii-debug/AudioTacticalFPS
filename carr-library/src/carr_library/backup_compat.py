"""Read-only compatibility for the canonical root-relative sidecar envelopes.

This is an explicit inventory of data formats, not a recursive settings copier.
Optional producers own their validators. Missing validators and unrecognized
state fail visibly, rather than dropping user data or copying arbitrary JSON.
Database-bound locations retain their exact historical source identities.
Selected pure adapters are reused from canonical 966e5ccf. The active writer
remains library_backup format 5; this module creates no second producer/store.
"""
from contextlib import ExitStack
from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat

from .ingest.bundle import _plain, _unique_object
from .ingest.restore import RestoreError
from .reader.reading_state import MAX_BYTES as READING_BYTES, _decode
from . import backup_producers, backup_generated, recording_backup, backup_transcription
from .studio import backup_inputs, book_backup

MAX_FILES = 100_000
MAX_BYTES = 512 * 1024 * 1024
ENGINE_SETTINGS_BYTES = 32 * 1024 * 1024
_SIMPLE = ('reading-state.json', 'keybindings.json', 'keyboard-shortcuts.json',
           'evidence-drafts.json', 'ocr-language-presets.json', 'progress-preferences.json',
           'desktop-preferences.json', 'portable-sync-state.json')
_DIRECTORIES = ('narration-projects', 'reading-copies', 'state')
_ID = r'[A-Za-z0-9_-]{1,80}'
_STUDIO = re.compile(r'state/studio/projects/(' + _ID + r')/(?:project\.json|revisions/(\d{8})-([0-9a-f]{64})\.json)\Z')


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
    depth = 12 if backup_transcription.namespace(value) and backup_transcription.limit(value) is not None else 8
    if (path.is_absolute() or str(path) != value or len(path.parts) > depth
            or any(p in ('.', '..') or p.endswith((' ', '.'))
                   or p.split('.')[0].casefold() in reserved for p in path.parts)):
        raise RestoreError('invalid portable sidecar filename')
    return path


def _model(module):
    # Preserve legacy preferences as inert data; do not install another live
    # command registry or change this host's active progress configuration.
    if module in ('keyboard_preferences', 'progress_preferences'):
        from . import backup_legacy_preferences
        return (backup_legacy_preferences.KEYBOARD if module == 'keyboard_preferences'
                else backup_legacy_preferences.PROGRESS)
    try:
        return import_module('.' + module, 'carr_library')
    except ImportError as exc:
        raise RestoreError('This backup needs the installed data validator for ' + module +
                           '; existing files were kept.') from exc


def sidecar_limit(name):
    """Resolve only explicit formats; also fail early if a producer is absent."""
    portable_name(name)
    if name == 'portable-sync-state.json':
        return _model('sync.state').MAX_BYTES
    if name == 'desktop-preferences.json':
        return _model('desktop_preferences').MAX_BYTES
    if name == 'reading-state.json':
        return READING_BYTES
    if name == 'keybindings.json':
        return _model('keybindings').MAX_BYTES
    if name == 'keyboard-shortcuts.json':
        return _model('keyboard_preferences').MAX_BYTES
    if name == 'evidence-drafts.json':
        return _model('evidence_drafts').MAX_BYTES
    if name == 'ocr-language-presets.json':
        return _model('extract.ocr_languages').MAX_PRESET_BYTES
    if name == 'progress-preferences.json':
        return _model('progress_preferences').MAX_BYTES
    if name.casefold() == 'state/audio-metadata/choices.json':
        return _model('audio.metadata_review').MAX_BYTES
    if name.casefold() == 'state/speech/playback-queue.json':
        return _model('speech.playlist').MAX_STATE_BYTES
    if name.casefold() == 'state/speech/reading-tabs.json':
        return _model('speech.tabs').MAX_BYTES
    if name == 'state/audio/engines.json':
        return ENGINE_SETTINGS_BYTES
    if name == 'state/operations/resources.json':
        return 32768
    if book_backup.namespace(name):
        maximum = book_backup.limit(name)
        if maximum is None:
            raise RestoreError('Unsupported Book recovery path.')
        return maximum
    if backup_inputs.namespace(name):
        return backup_inputs.limit(name)
    if backup_transcription.namespace(name):
        maximum = backup_transcription.limit(name)
        if name != name.casefold() or maximum is None:
            raise RestoreError('Unsupported local-file transcription sidecar path.')
        return maximum
    if recording_backup.state_path(name):
        return recording_backup.state_limit(name)
    if maximum := backup_generated.limit(name):
        return maximum
    if maximum := backup_producers.limit(name):
        return maximum
    if re.fullmatch(r'narration-projects/[0-9a-f]{32}(?:\.draft)?\.json', name):
        return _model('reader.narration').MAX_PROJECT_BYTES
    if re.fullmatch(r'reading-copies/[0-9a-f]{32}\.json', name):
        return _model('reader.reading_copy').MAX_PROJECT_BYTES
    if _STUDIO.fullmatch(name):
        _model('studio.project')
        return 32 * 1024 * 1024
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


def _validate_engine_settings(value):
    """Passive schema-1 declarations, never installed engines or their assets.

    Mirror runtime_settings, speech.runtime's Piper/LocalHelper constructors,
    speech.dordiai, and audio.models.LocalModelConfig without importing them.
    External files can be absent after relocation; normal engine activation
    remains responsible for checking their availability, trust and hashes.
    """
    def fields(record, required, optional=()):
        if (type(record) is not dict or not set(required) <= record.keys()
                or not record.keys() <= set(required) | set(optional)):
            raise ValueError('Unknown or missing local engine setting fields.')

    def text(value, maximum):
        if (type(value) is not str or not value.strip() or len(value) > maximum
                or '\x00' in value):
            raise ValueError('Invalid local engine text setting.')
        value.encode('utf-8', errors='strict')

    def path(value, *, absolute=False):
        # These are inert external references, not archive member names. Keep
        # both Windows and POSIX spellings, including valid legacy relatives.
        text(value, 32767)
        normalized = value.replace('\\', '/')
        if (normalized.startswith('//') or '://' in value
                or any(ord(character) < 32 for character in value)):
            raise ValueError('Engine settings require local filesystem references.')
        without_drive = value[2:] if re.match(r'^[A-Za-z]:', value) else value
        if ':' in without_drive:
            raise ValueError('Engine references cannot contain URLs or alternate streams.')
        if absolute and not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
            raise ValueError('A local music model needs absolute installation references.')

    def checksum(value):
        if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('An engine asset needs a lowercase SHA-256 fingerprint.')

    def boolean(value):
        if type(value) is not bool:
            raise ValueError('Engine enablement and use profiles must be booleans.')

    def entries(value, maximum=100):
        if type(value) is not list or len(value) > maximum:
            raise ValueError('Invalid or excessive local engine registrations.')
        return value

    fields(value, {'schema', 'ffmpeg', 'ffprobe', 'piper', 'voice_helpers', 'music_models', 'fluidsynth'},
           {'dordiai', 'voice_preferences'})
    if type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Unsupported local engine settings schema.')
    if 'voice_preferences' in value:
        _model('runtime_settings').validate_voice_preferences(value['voice_preferences'])
    # The primary speech provider has its own optional top-level declaration.
    # Resolve only its pure settings validator, never a provider or installation.
    dordiai = value.get('dordiai')
    if dordiai is not None:
        validator = getattr(_model('runtime_settings'), 'validate_dordiai_configuration', None)
        if not callable(validator):
            raise ValueError('The installed settings validator does not support dordiai-voices.')
        dordiai = validator(dordiai)
        for name in ('runtime_directory', 'python_executable'):
            if dordiai.get(name) is not None:
                path(dordiai[name])
    for name in ('ffmpeg', 'ffprobe'):
        if value[name] is not None:
            path(value[name])
    for registration in entries(value['piper']):
        fields(registration, {'executable', 'models'})
        path(registration['executable'])
        seen = set()
        for model in entries(registration['models'], maximum=10000):
            fields(model, {'path', 'sha256', 'config_sha256', 'license', 'name', 'language'}, {'config'})
            path(model['path'])
            if 'config' in model:
                path(model['config'])
            for name in ('sha256', 'config_sha256'):
                checksum(model[name])
            if model['sha256'] in seen:
                raise ValueError('Duplicate Piper model identity.')
            seen.add(model['sha256'])
            for name in ('license', 'name', 'language'):
                text(model[name], 2048)
    for helper in entries(value['voice_helpers']):
        if type(helper) is not dict:
            raise ValueError('Invalid local voice registration.')
        if 'adapter' in helper:
            fields(helper, {'adapter', 'runtime_directory', 'rights_notice', 'enabled'},
                   {'python_executable', 'expected_worker_sha256'})
            if helper['adapter'] != 'dordiai-voices':
                raise ValueError('Unsupported local voice adapter.')
            text(helper['runtime_directory'], 4096)
            path(helper['runtime_directory'])
            text(helper['rights_notice'], 4096)
            boolean(helper['enabled'])
            if helper.get('python_executable') is not None:
                path(helper['python_executable'])
            if 'expected_worker_sha256' in helper:
                checksum(helper['expected_worker_sha256'])
        else:
            fields(helper, {'executable', 'sha256', 'engine_name'},
                   {'enabled', 'max_voices', 'script', 'script_sha256'})
            path(helper['executable'])
            checksum(helper['sha256'])
            _model('runtime_settings').validate_helper_script_configuration(helper)
            text(helper['engine_name'], 128)
            if 'enabled' in helper:
                boolean(helper['enabled'])
            if 'max_voices' in helper and (type(helper['max_voices']) is not int
                                         or not 1 <= helper['max_voices'] <= 10000):
                raise ValueError('Invalid local helper voice limit.')
    for model in entries(value['music_models']):
        fields(model, {'engine', 'python', 'directory', 'manifest'},
               {'device', 'private_noncommercial', 'max_chunk_seconds'})
        if model['engine'] not in ('musicgen', 'ace-step') or model.get('device', 'cpu') not in ('cpu', 'cuda', 'mps'):
            raise ValueError('Unsupported local music engine or device.')
        for name in ('python', 'directory', 'manifest'):
            path(model[name], absolute=True)
        if 'private_noncommercial' in model:
            boolean(model['private_noncommercial'])
        chunk = model.get('max_chunk_seconds', 30)
        if type(chunk) not in (int, float) or not 1 <= chunk <= 600 or not math.isfinite(chunk):
            raise ValueError('Invalid local music chunk allowance.')
    font = value['fluidsynth']
    if font is not None:
        fields(font, {'binary', 'soundfont', 'sha256', 'license_notice'})
        path(font['binary'])
        path(font['soundfont'])
        if PureWindowsPath(font['soundfont']).suffix.lower() not in ('.sf2', '.sf3'):
            raise ValueError('Unsupported local SoundFont filename.')
        checksum(font['sha256'])
        text(font['license_notice'], 4000)


def validate_sidecar(db, name, raw):
    maximum = sidecar_limit(name)
    if (type(raw) is not bytes or len(raw) > maximum
            or not raw and not backup_transcription.allows_empty(name)):
        raise RestoreError('sidecar is empty or exceeds its data-format budget')
    try:
        if name == 'portable-sync-state.json':
            _model('sync.state').validate_portable_state(raw)
        elif name == 'desktop-preferences.json':
            _model('desktop_preferences').validate_desktop_preferences(raw)
        elif name == 'reading-state.json':
            positions, bookmarks, _ = _decode(raw.decode('utf-8'))
            validate_locations(db, [value[0] for value in positions.values()] +
                               [bookmark.location for bookmark in bookmarks.values()])
        elif name == 'keybindings.json':
            _model('keybindings').decode_preferences(raw)
        elif name == 'keyboard-shortcuts.json':
            _model('keyboard_preferences').validate_keymap(raw)
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
        elif name == 'progress-preferences.json':
            _model('progress_preferences').validate_progress_preferences(raw)
        elif name == 'state/audio/engines.json':
            _validate_engine_settings(_json(raw))
        elif name.casefold() == 'state/audio-metadata/choices.json':
            _model('audio.metadata_review').validate_choices(_json(raw))
        elif name.casefold() == 'state/speech/playback-queue.json':
            _model('speech.playlist').SavedPlaylist.from_dict(_json(raw))
        elif name.casefold() == 'state/speech/reading-tabs.json':
            _model('speech.tabs').ReadingTabs.from_dict(_json(raw))
        elif name == 'state/operations/resources.json':
            from .state_recovery import validate_resource_policy
            validate_resource_policy(_json(raw))
        elif book_backup.namespace(name):
            book_backup.validate_database_document(db, name, _json(raw))
        elif backup_inputs.namespace(name):
            backup_inputs.validate_metadata(name, _json(raw))
        elif backup_transcription.namespace(name):
            backup_transcription.validate(name, raw)
        elif recording_backup.state_path(name):
            recording_backup.validate_receipt(name, _json(raw))
        elif name.startswith(backup_generated.PREFIX):
            backup_generated.validate_receipt(name, _json(raw))
        elif backup_producers.SUBTITLE.fullmatch(name):
            # Generated text transcripts are retained as inert UTF-8, never
            # interpreted as commands or authenticated source quotations.
            value = raw.decode('utf-8')
            if '\x00' in value or (name.endswith('.vtt') and not value.startswith('WEBVTT\n')):
                raise ValueError('Invalid generated subtitle text.')
        elif backup_producers.limit(name) is not None:
            backup_producers.validate(db, name, _json(raw))
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
    entries = tuple(entries)
    presets = [(name, size) for name, size in entries
               if name.startswith('state/studio/effect-presets/')]
    if len(presets) > backup_inputs.MAX_PRESETS:
        raise RestoreError('effect preset snapshot budget exceeded')
    narration = [(name, size) for name, size in entries if name.startswith('narration-projects/')]
    if narration:
        model = _model('reader.narration')
        if (len(narration) > model.MAX_PROJECTS * 2
                or sum(size for _, size in narration) > model.MAX_SNAPSHOT_BYTES):
            raise RestoreError('narration producer snapshot budget exceeded')


def normalize_manifest(manifest, *, backup_budget=None):
    """Discriminate exact v3 shapes without changing the signed source manifest."""
    from .backup_budget import arguments as resource_arguments
    resource_arguments(backup_budget)
    version = manifest.get('version')
    fields = {'version', 'kind', 'schema_version', 'database', 'assets'}
    if type(version) is not int or version not in (1, 2, 3, 4, 5):
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
            or version == 4 and extra == {'state', 'generated_audio_included', 'pending'}
            or version == 5 and extra == {'state', 'generated_audio_included', 'pending', 'reading_state'}):
        generated = manifest['generated_audio_included']
        if type(manifest['state']) is not list or len(manifest['state']) > MAX_FILES:
            raise RestoreError('invalid legacy state manifest')
        entries = []
        for value in manifest['state']:
            if type(value) is not dict or not isinstance(value.get('filename'), str):
                raise RestoreError('invalid legacy state entry')
            portable_name('state/' + value['filename'])
            entries.append({**value, 'filename': 'state/' + value['filename']})
        roots = frozenset({'state'})
        if version >= 4:
            pending = manifest['pending']
            if pending is None and version != 5:
                raise RestoreError('missing pending import backup')
        if version == 5:
            reading = manifest['reading_state']
            if type(reading) is not dict or reading.get('filename') != 'reading-state.json':
                raise RestoreError('invalid legacy reading-state manifest')
            entries.append(reading)
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
    names, portable_paths, total, generated_total, copied = set(), {}, 0, 0, []
    for entry in entries:
        if (type(entry) is not dict or set(entry) != {'filename', 'size_bytes', 'sha256'}
                or type(entry['filename']) is not str or type(entry['size_bytes']) is not int
                or not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
            raise RestoreError('invalid sidecar manifest entry')
        if entry['filename'].startswith(backup_generated.PREFIX) and not generated:
            raise RestoreError('Generated audio requires explicit manifest inclusion.')
        maximum = sidecar_limit(entry['filename'])
        if backup_transcription.is_cache_file(entry['filename']) and not generated:
            raise RestoreError('Transcription cache requires explicit manifest inclusion.')
        if (entry['size_bytes'] < 0 or entry['size_bytes'] > maximum
                or entry['size_bytes'] == 0 and not backup_transcription.allows_empty(entry['filename'])
                or entry['filename'].casefold() in names):
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
        if backup_generated.is_media(entry['filename']):
            generated_total += entry['size_bytes']
        else:
            total += entry['size_bytes']
        if total > MAX_BYTES or generated_total > backup_generated.MAX_BYTES:
            raise RestoreError('sidecar manifest byte limit')
        copied.append(dict(entry))
    _collection_budgets((entry['filename'], entry['size_bytes']) for entry in copied)
    if pending is not None:
        from .pending_backup import validate_pending
        validate_pending(pending, **resource_arguments(backup_budget))
    return Inventory(tuple(sorted(copied, key=lambda value: value['filename'])), roots, generated, pending)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular(path):
    info = _plain(path)
    if info.st_nlink != 1:
        raise RestoreError('sidecars must be private regular files without hard links')
    return info


def read_regular(path, maximum, *, allow_empty=False):
    before = _regular(path)
    if before.st_size > maximum or before.st_size == 0 and not allow_empty:
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


def _write(stage, name, raw):
    path = stage / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _stream_file(source, *, destination=None, expected=None, maximum, cancelled, allow_empty=False):
    """Copy or recheck actual bytes without allocating a complete media file."""
    _check_cancelled(cancelled)
    before = _regular(source)
    if before.st_size > maximum or before.st_size == 0 and not allow_empty:
        raise RestoreError('sidecar is empty or exceeds its data-format budget')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    if expected is not None and before.st_size != expected['size_bytes']:
        raise RestoreError('sidecar size disagrees with the backup manifest')
    digest, size, header = sha256(), 0, b''
    with ExitStack() as stack:
        reader = stack.enter_context(os.fdopen(os.open(source, flags), 'rb'))
        if _identity(os.fstat(reader.fileno())) != _identity(before):
            raise RestoreError('sidecar changed while opening')
        writer = None
        if destination is not None:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            writer = stack.enter_context(destination.open('xb'))
        while block := reader.read(1024 * 1024):
            _check_cancelled(cancelled)
            size += len(block)
            if size > before.st_size:
                raise RestoreError('sidecar changed while copying')
            if not header:
                header = block[:64]
            digest.update(block)
            if writer is not None:
                writer.write(block)
        if _identity(os.fstat(reader.fileno())) != _identity(before):
            raise RestoreError('sidecar changed while copying')
        if writer is not None:
            writer.flush()
            os.fsync(writer.fileno())
    if size != before.st_size or _identity(_regular(source)) != _identity(before):
        raise RestoreError('sidecar changed while copying')
    checksum = digest.hexdigest()
    if expected is not None and (size != expected['size_bytes'] or checksum != expected['sha256']):
        raise RestoreError('sidecar digest changed or disagrees with the backup manifest')
    _check_cancelled(cancelled)
    return size, checksum, header


def is_flat_manifest(manifest):
    """Distinguish accepted foreign envelopes before interpreting sidecar fields."""
    version = manifest.get('version')
    return ((version == 3 and ('reading_state' in manifest
                              or type(manifest.get('sidecars')) is list))
            or (version == 4 and 'sidecars' in manifest)
            or (version == 5 and 'reading_state' in manifest))


def check_namespace(directory, inventory, *, cancelled=lambda: False):
    """Inspect a manifest-bounded namespace without materializing unknown trees.

    Registered originals retain their existing catalog verifier. Every other
    file and directory must be declared, including the separate pending bundle.
    lstat errors other than genuine absence propagate; no links are traversed.
    """
    names = {entry['filename'] for entry in inventory.entries}
    directories = set(inventory.empty_roots) | {'originals'}
    if inventory.pending is not None:
        names.update('pending/' + entry['filename'] for entry in inventory.pending['files'])
        directories.add('pending')
    for name in tuple(names):
        directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != '.')
    if names & directories:
        raise RestoreError('sidecar file and directory identities collide')
    files = names | {'library.sqlite', 'manifest.json'}
    expected = files | directories
    if len(expected) > (MAX_FILES * 9) + 10:
        raise RestoreError('sidecar namespace exceeds its bounded manifest')
    optional = backup_transcription.optional_cache_directories(inventory.entries) - expected
    seen, optional_seen, pending = set(), set(), [directory]
    while pending:
        _check_cancelled(cancelled)
        parent = pending.pop()
        _plain(parent, directory=True)
        # Stream the actual entries on every supported Python version.
        # Reject the first extra name before stat or consuming another name.
        with os.scandir(parent) as children:
            for entry in children:
                _check_cancelled(cancelled)
                child = parent / entry.name
                name = child.relative_to(directory).as_posix()
                if name in optional:
                    if name in optional_seen:
                        raise RestoreError('Duplicate optional transcription cache directory.')
                    _plain(child, directory=True)
                    optional_seen.add(name)
                    pending.append(child)
                    continue
                if name not in expected or name in seen:
                    raise RestoreError('incomplete backup or unexpected sidecar entry: ' + name)
                seen.add(name)
                if name in directories:
                    _plain(child, directory=True)
                    if name != 'originals':
                        pending.append(child)
                elif name in names:
                    _regular(child)
                else:
                    _plain(child)
        _check_cancelled(cancelled)
    _check_cancelled(cancelled)
    if seen != expected:
        missing = ', '.join(sorted(expected - seen)[:10])
        raise RestoreError('incomplete sidecar backup: ' + missing)


def restore(db, source, stage, inventory, *, cancelled=lambda: False, destination=None):
    """Verify exact canonical producers, then revoke authority in private state."""
    receipts = {}
    # Capture the tiny passive index first. Its referenced histories are decoded
    # only by the complete copied-collection verifier, after aggregate preflight.
    tab_name = 'state/speech/reading-tabs.json'
    entries = sorted(inventory.entries, key=lambda row: row['filename'].casefold() != tab_name)
    deferred_drafts = set()
    for name in inventory.empty_roots:
        (stage / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in entries:
        _check_cancelled(cancelled)
        name = entry['filename']
        if (backup_transcription.is_input(name) or recording_backup.is_audio(name)
                or backup_inputs.is_input(name)):
            # Retained originals use the existing streaming manifest proof.
            # PCM/retake verification runs against the complete private state;
            # an orphan is preserved without parsing binary bytes as JSON.
            _stream_file(source / name, destination=stage / name, expected=entry,
                         maximum=sidecar_limit(name), cancelled=cancelled)
        elif backup_generated.is_media(name):
            size, _, header = _stream_file(source / name, destination=stage / name,
                expected=entry, maximum=sidecar_limit(name), cancelled=cancelled)
            backup_generated.validate_container(name, header, size)
        else:
            # Use the same bounded streaming/checksum path for metadata too;
            # structural validation reads only the verified private copy.
            empty = backup_transcription.allows_empty(name)
            _stream_file(source / name, destination=stage / name, expected=entry,
                         maximum=sidecar_limit(name), cancelled=cancelled, allow_empty=empty)
            if name.casefold() in deferred_drafts:
                continue
            raw = read_regular(stage / name, sidecar_limit(name), allow_empty=empty)
            validate_sidecar(db, name, raw)
            if name.casefold() == tab_name:
                from .state_recovery import _reading_tab_rows
                index = _model('speech.tabs').ReadingTabs.from_dict(_json(raw))
                state_inventory = {row['filename'][len('state/'):].casefold(): row
                    for row in inventory.entries if row['filename'].startswith('state/')}
                deferred_drafts = {'state/' + relative for relative, _ in
                    _reading_tab_rows(index, state_inventory, cancelled=cancelled)}
            if name.startswith(backup_generated.PREFIX):
                receipts[name] = _json(raw)
    backup_generated.validate_collection(inventory.entries, receipts)
    if any(entry['filename'].casefold() == 'state/speech/playback-queue.json'
           for entry in inventory.entries):
        _model('speech.playlist').validate_collection(stage, inventory.entries)
    state = [{**row, 'filename': row['filename'][len('state/'):]}
             for row in inventory.entries if row['filename'].startswith('state/')]
    if state:
        # This host's stronger restore revocation is mandatory even when the
        # donor format did not record restore markers. Preserve input paths,
        # source hashes and historical receipts, never their old authority.
        from .state_recovery import prepare_restored_state
        state = prepare_restored_state(stage / 'state', state, cancelled=cancelled,
            destination_state_root=Path(destination) / 'state' if destination is not None else None)
        book_backup.validate_database_bindings(db, stage / 'state', state, cancelled=cancelled)
        inventory = Inventory(tuple(
            [row for row in inventory.entries if not row['filename'].startswith('state/')]
            + [{**row, 'filename': 'state/' + row['filename']} for row in state]),
            inventory.empty_roots, inventory.generated_audio_included, inventory.pending)
    # Projection adds checked revision/notice files, but does not increase
    # this foreign envelope's existing metadata/original-byte allowances.
    if (len(inventory.entries) > MAX_FILES or sum(row['size_bytes']
            for row in inventory.entries if not backup_generated.is_media(row['filename'])) > MAX_BYTES):
        raise RestoreError('restored sidecar projection exceeds the existing snapshot budget')
    _collection_budgets((row['filename'], row['size_bytes']) for row in inventory.entries)
    _check_cancelled(cancelled)
    return inventory


def publish(stage, destination, inventory, *, sync_directory, cancelled=lambda: False):
    """Link only the verified private stage, with an explicit cancellation gate."""
    parents = {destination}
    for name in inventory.empty_roots:
        _check_cancelled(cancelled)
        (destination / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        parents.add(destination / name)
    for entry in inventory.entries:
        _check_cancelled(cancelled)
        name = entry['filename']
        path = destination / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.link(stage / name, path)
        parents.update(p for p in path.parents if p == destination or p.is_relative_to(destination))
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        _check_cancelled(cancelled)
        sync_directory(parent)
