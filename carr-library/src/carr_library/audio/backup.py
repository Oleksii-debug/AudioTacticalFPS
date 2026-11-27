"""Streaming, verified local Audio Studio snapshots and separate-folder restore.

This covers a saved studio project, its recovery draft and known production
assets/checkpoints/results. It is not a library database or installed-model
backup. API profiles, credential references, executable helpers, quarantine
and arbitrary neighboring files are never collected.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
import zipfile

from ..reader.narration_text import NarrationDraft, decode_json
from ..research.export_file import _filename
from .jobs import check_cancelled, project_lock


DEFAULT_BYTE_BUDGET = 1024 ** 4
DEFAULT_FILE_BUDGET = 1000000
MANIFEST_BUDGET = 64 * 1024 ** 2
CENTRAL_DIRECTORY_BUDGET = 64 * 1024 ** 2
MUSIC_STATE_BUDGET = 16 * 1024 ** 2
BLOCK = 1024 * 1024
_HEX = r'[0-9a-f]{64}'
_STUDIO_KEYS = {'kind', 'version', 'mode', 'project_id', 'title', 'voice', 'language',
                'output_dir', 'source_reference', 'draft'}


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _budget(byte_budget, file_budget):
    if type(byte_budget) is not int or byte_budget <= 0 or type(file_budget) is not int or file_budget <= 0:
        raise ValueError('Snapshot byte and file budgets must be positive integers.')


def _local(path):
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or raw.startswith(('\\\\', '//')) or '://' in raw:
        raise ValueError('Choose a local snapshot path.')
    result = Path(raw).absolute()
    for component in result.parts[1:]:
        _filename(component)
    return result


def _safe(path, *, directory=False):
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Snapshot paths cannot contain links or Windows reparse points.')
        if parent != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('Snapshot parents must be regular directories.')
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('Choose a regular snapshot file or directory.')
    return info


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _allowed(name):
    if name in ('studio/project.json', 'studio/project.json.recovery.json'):
        return True
    if name == 'audio/render-journal.json' or re.fullmatch('audio/plan-' + _HEX + r'\.json', name):
        return True
    # The typed input resolver owns suffix-independent PCM references.
    if re.fullmatch(r'audio/assets/[^/]+', name):
        return True
    if re.fullmatch('audio/cache/' + _HEX + r'\.(wav|json|mid|music\.json)', name):
        return True
    if re.fullmatch('audio/music-jobs/' + _HEX
            + r'/(?:cue\.(?:wav|mid|music\.json)|cue\.music-work/(?:state\.json|part-[0-9]{8}\.wav|score\.partial\.mid|provenance\.partial\.json))', name):
        return True
    if re.fullmatch('audio/renders/' + _HEX + r'/[^/]+\.(wav|mp3|flac|ogg|opus|aac|m4a|m4b|aiff|wma|srt|vtt|json|ffmeta|mid)', name):
        return True
    return False


def _member(name):
    if (not isinstance(name, str) or not name or '\\' in name or name.startswith('/')
            or str(PurePosixPath(name)) != name or not _allowed(name)):
        raise ValueError('Unexpected or unsafe snapshot member.')
    for part in name.split('/'):
        _filename(part)
    return name


def _studio(payload):
    keys = _STUDIO_KEYS | ({'input_kind'} if isinstance(payload, dict) and payload.get('version') == 2 else set())
    if (type(payload) is not dict or set(payload) != keys
            or payload['kind'] != 'research-audio-studio' or type(payload['version']) is not int
            or payload['version'] not in (1, 2) or payload['mode'] != 'LOCAL ONLY'
            or not isinstance(payload['project_id'], str)
            or not re.fullmatch('[0-9a-f]{32}', payload['project_id'])):
        raise ValueError('A saved local Audio Studio project is required for this snapshot.')
    if payload['version'] == 2 and payload['input_kind'] not in ('narration', 'script'):
        raise ValueError('The saved narration or script mode is invalid.')
    for name in ('title', 'voice', 'language'):
        if not isinstance(payload[name], str) or not payload[name].strip():
            raise ValueError('The saved Audio Studio settings are invalid.')
    draft = NarrationDraft.from_dict(payload['draft'])
    reference = payload['source_reference']
    if reference is not None:
        if type(reference) is dict and reference.get('kind') == 'saved-reading-copy-snapshot':
            from .narration_bridge import validate_source_reference
            validate_source_reference(reference, draft)
            return payload
        if type(reference) is not dict or reference.get('kind') not in ('local-text', 'library-source-reading-copy'):
            raise ValueError('Unknown source-reference settings are not exported.')
        allowed = ({'kind', 'filename', 'original_path', 'bytes_sha256'} if reference['kind'] == 'local-text'
                   else {'kind', 'workspace', 'edition', 'revision', 'pages', 'separator'})
        if set(reference) - allowed:
            raise ValueError('Unknown source-reference fields are not exported.')
    return payload


def _read_json(path, *, budget=MANIFEST_BUDGET):
    before = _safe(path)
    if before.st_size > budget:
        raise ValueError('Project metadata exceeds the configured metadata budget.')
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError('Project metadata changed before reading.')
        raw = stream.read(budget + 1)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError('Project metadata changed during reading.')
    if len(raw) > budget or len(raw) != before.st_size or _identity(_safe(path)) != _identity(before):
        raise ValueError('Project metadata changed or exceeds the byte budget.')
    return decode_json(raw.decode('utf-8'))


def _checked_hash(path, *, maximum, cancelled):
    before = _safe(path)
    if before.st_size > maximum:
        raise ValueError('Music checkpoint exceeds the snapshot byte budget.')
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError('Music checkpoint changed before reading.')
        while block := stream.read(min(BLOCK, before.st_size + 1 - size)):
            check_cancelled(cancelled)
            size += len(block)
            if size > before.st_size:
                raise ValueError('Music checkpoint grew while reading.')
            digest.update(block)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError('Music checkpoint changed while reading.')
    if size != before.st_size or _identity(_safe(path)) != _identity(before):
        raise ValueError('Music checkpoint changed while reading.')
    return digest.hexdigest()


def _music_inventory(project_dir, include, *, file_budget, byte_budget, cancelled):
    """Collect accepted music chunks and checked resume state, never scratch."""
    from .ffmpeg import wav_info
    from .music import COMPOSER_VERSION, _settings
    from .models import SAMPLE_RATES

    root = project_dir / 'music-jobs'
    if not root.exists() and not root.is_symlink():
        return
    _safe(root, directory=True)
    with os.scandir(root) as entries:
        for entry in entries:
            check_cancelled(cancelled)
            if not re.fullmatch(_HEX, entry.name):
                continue
            job = root / entry.name
            _safe(job, directory=True)
            state_path, output = job / 'cue.music-work' / 'state.json', job / 'cue.wav'
            if not state_path.exists() and not state_path.is_symlink():
                if output.exists() or output.is_symlink():
                    raise ValueError('Completed music is missing its validated resume state.')
                continue
            prefix = 'audio/music-jobs/' + entry.name + '/'
            include(prefix + 'cue.music-work/state.json', state_path)
            state = _read_json(state_path, budget=MUSIC_STATE_BUDGET)
            if (type(state) is not dict or set(state) != {'schema_version', 'settings_sha256', 'settings', 'chunks'}
                    or type(state['schema_version']) is not int or state['schema_version'] != 1
                    or type(state['chunks']) is not dict or len(state['chunks']) > file_budget
                    or type(state['settings']) is not dict):
                raise ValueError('Invalid music checkpoint state.')
            settings = state['settings']
            known = {'version', 'engine', 'frames', 'sample_rate', 'mood', 'tempo', 'seed',
                     'gain', 'ffmpeg_sha256', 'build_sha256', 'model'}
            if (set(settings) != known or settings['version'] != COMPOSER_VERSION
                    or settings['engine'] not in ('procedural', 'musicgen')
                    or type(settings['frames']) is not int or settings['frames'] < 1
                    or type(settings['sample_rate']) is not int or settings['sample_rate'] not in SAMPLE_RATES
                    or settings['frames'] > 10**12 * settings['sample_rate']
                    or any(not isinstance(settings[key], str) or not re.fullmatch(_HEX, settings[key])
                           for key in ('ffmpeg_sha256', 'build_sha256'))):
                raise ValueError('Invalid music checkpoint settings.')
            frames, rate = settings['frames'], settings['sample_rate']
            _, tempo = _settings(frames / rate, settings['mood'], settings['tempo'],
                                settings['seed'], settings['gain'], rate)
            model = settings['model']
            if settings['engine'] == 'procedural':
                if model is not None:
                    raise ValueError('Procedural music cannot claim a neural model.')
            elif (type(model) is not dict
                    or set(model) != {'engine', 'model_id', 'revision', 'license', 'manifest_sha256'}
                    or any(not isinstance(value, str) or not value or len(value) > 4096 for value in model.values())
                    or model['engine'] != 'musicgen' or not re.fullmatch(_HEX, model['manifest_sha256'])
                    or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', model['revision'])):
                raise ValueError('Invalid music checkpoint model identity.')
            fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(',', ':'),
                                                      allow_nan=False).encode('utf-8')).hexdigest()
            if state['settings_sha256'] != fingerprint:
                raise ValueError('Music checkpoint settings checksum mismatch.')
            chunk_size = rate * 16 * 60 / tempo if settings['engine'] == 'procedural' else rate * 25
            planned = math.ceil(frames / chunk_size)
            if len(state['chunks']) > planned:
                raise ValueError('Music checkpoint contains too many accepted chunks.')
            parent = None
            for number in range(len(state['chunks'])):
                check_cancelled(cancelled)
                record = state['chunks'].get(str(number))
                wanted = min(frames, round((number + 1) * chunk_size)) - round(number * chunk_size)
                if (type(record) is not dict or set(record) != {'frames', 'sha256', 'parent_sha256'}
                        or type(record['frames']) is not int or record['frames'] != wanted
                        or not isinstance(record['sha256'], str) or not re.fullmatch(_HEX, record['sha256'])
                        or record['parent_sha256'] != (parent if settings['engine'] == 'musicgen' else None)):
                    raise ValueError('Music checkpoint has invalid or discontinuous accepted chunks.')
                name = f'cue.music-work/part-{number:08d}.wav'
                chunk = job / name
                include(prefix + name, chunk)
                info = wav_info(chunk)
                if ((info.frames, info.rate, info.channels) != (wanted, rate, 1)
                        or _checked_hash(chunk, maximum=byte_budget, cancelled=cancelled) != record['sha256']):
                    raise ValueError('Music checkpoint audio does not match its accepted checksum and frames.')
                parent = record['sha256']
            if not output.exists() and not output.is_symlink():
                continue
            if len(state['chunks']) != planned:
                raise ValueError('Completed music lacks all its accepted chunks.')
            include(prefix + 'cue.wav', output)
            provenance_path = job / 'cue.music.json'
            include(prefix + 'cue.music.json', provenance_path)
            provenance = _read_json(provenance_path, budget=MUSIC_STATE_BUDGET)
            info = wav_info(output)
            provenance_fields = {'schema_version', 'engine', 'settings_sha256', 'frames', 'sample_rate',
                'channels', 'sha256', 'chunks', 'reused_chunks', 'mood', 'tempo', 'seed', 'kind',
                'assembly', 'model', 'listening_acceptance', 'rights'}
            if (type(provenance) is not dict or set(provenance) != provenance_fields
                    or type(provenance.get('schema_version')) is not int or provenance['schema_version'] != 1
                    or any(type(provenance.get(key)) is not int for key in
                           ('frames', 'sample_rate', 'channels', 'chunks', 'reused_chunks', 'seed'))
                    or any(not isinstance(provenance.get(key), str) or not provenance[key]
                           or len(provenance[key]) > 8192 or '\x00' in provenance[key]
                           for key in ('kind', 'assembly', 'listening_acceptance', 'rights'))
                    or provenance.get('settings_sha256') != fingerprint
                    or any(provenance.get(key) != settings[key] for key in
                           ('engine', 'frames', 'sample_rate', 'mood', 'tempo', 'seed', 'model'))
                    or provenance.get('channels') != 1 or provenance.get('chunks') != planned
                    or type(provenance.get('reused_chunks')) is not int
                    or not 0 <= provenance['reused_chunks'] <= planned
                    or (info.frames, info.rate, info.channels) != (frames, rate, 1)
                    or provenance.get('sha256') != _checked_hash(output, maximum=byte_budget, cancelled=cancelled)):
                raise ValueError('Completed music does not match its accepted provenance.')
            if settings['engine'] == 'procedural':
                include(prefix + 'cue.mid', job / 'cue.mid')
            # The composer retains these exact accepted hard-link twins. They
            # are portable only after their corresponding final artifact exists
            # and independently matches; arbitrary partial work stays excluded.
            for name, final in (('score.partial.mid', job / 'cue.mid'),
                                ('provenance.partial.json', provenance_path)):
                twin = job / 'cue.music-work' / name
                if os.path.lexists(twin) and os.path.lexists(final):
                    if (_safe(twin).st_size == _safe(final).st_size
                            and _checked_hash(twin, maximum=byte_budget, cancelled=cancelled)
                            == _checked_hash(final, maximum=byte_budget, cancelled=cancelled)):
                        include(prefix + 'cue.music-work/' + name, twin)


def _asset_references(project, *, file_budget, cancelled):
    """Read only sound directives, keeping unrelated unfinished edits portable."""
    legacy_kind = 'narration' if project['source_reference'] is not None else 'script'
    if project.get('input_kind', legacy_kind) != 'script':
        return set()
    from .models import Sound
    from .production import parse_script
    names = set()
    draft = NarrationDraft.from_dict(project['draft'])
    historical = False
    scanned = 0
    while True:
        check_cancelled(cancelled)
        scanned += len(draft.text)
        if scanned > 256 * 1024 ** 2:
            raise ValueError('Sound input history exceeds its bounded scan budget.')
        for line in draft.text.splitlines():
            check_cancelled(cancelled)
            line = line.strip()
            if not re.match(r'\[sound(?:\s|\])', line):
                continue
            # The production parser and Sound model own quoting, names and
            # controls. shlex does not expand variables or execute anything.
            try:
                plan = parse_script(line, voice=project['voice'], language=project['language'])
            except ValueError:
                if historical:
                    # Incomplete older edits remain undoable text, not invented
                    # asset references. The current script still fails closed.
                    continue
                raise
            names.update(event.asset for scene in plan.scenes for event in scene.events
                         if isinstance(event, Sound))
            if len(names) > file_budget:
                raise ValueError('Sound inputs exceed the snapshot file budget.')
        # Work only on our fully validated private reconstruction. Public undo
        # reaches every accepted saved edit and the immutable source text.
        if not draft.undo():
            break
        historical = True
    return names


def input_assets(project_file, project_dir, *, strict=False,
                 file_budget=DEFAULT_FILE_BUDGET, cancelled=lambda: False):
    """Return checked input WAVs regardless of generated-output inclusion.

    Referenced names come from saved/recovery scripts and their validated undo
    history, including immutable source text. Existing WAV-named inputs remain
    included; other neighboring files are not collected.
    Strict mode validates an archive's asset directory without allowing extras.
    """
    _budget(DEFAULT_BYTE_BUDGET, file_budget)
    project_file = Path(project_file)
    project = _studio(_read_json(project_file))
    projects = [project]
    recovery = project_file.with_name(project_file.name + '.recovery.json')
    if recovery.exists() or recovery.is_symlink():
        value = _read_json(recovery)
        if (type(value) is not dict or set(value) != {'kind', 'version', 'project_id', 'base_sha256', 'project'}
                or value['kind'] != 'audio-studio-recovery' or type(value['version']) is not int
                or value['version'] != 1 or value['project_id'] != project['project_id']):
            raise ValueError('The recovery draft is invalid or belongs to another project.')
        recovered = _studio(value['project'])
        if recovered['project_id'] != project['project_id']:
            raise ValueError('The recovery draft belongs to another project.')
        projects.append(recovered)
    required = set()
    for payload in projects:
        required.update(_asset_references(payload, file_budget=file_budget, cancelled=cancelled))
    if len(required) > file_budget:
        raise ValueError('Sound inputs exceed the snapshot file budget.')
    if project_dir is None:
        if required:
            raise ValueError('Choose the audio project folder to include its referenced sound inputs.')
        return {}
    directory = Path(project_dir) / 'assets'
    if not directory.exists() and not directory.is_symlink():
        if required:
            raise ValueError('A referenced sound input is missing from the project assets folder.')
        return {}
    _safe(directory, directory=True)
    names = set(required)
    for count, path in enumerate(directory.iterdir(), 1):
        check_cancelled(cancelled)
        if count > file_budget:
            raise ValueError('Sound inputs exceed the snapshot file budget.')
        if path.name.lower().endswith('.wav'):
            names.add(path.name)
        elif strict and path.name not in required:
            raise ValueError('An archived asset is not a referenced sound input.')
    if len(names) > file_budget:
        raise ValueError('Sound inputs exceed the snapshot file budget.')
    from .ffmpeg import wav_info
    files, folded = {}, set()
    for name in sorted(names):
        check_cancelled(cancelled)
        member = _member('audio/assets/' + name)
        if name.casefold() in folded:
            raise ValueError('Sound input filenames collide on a case-insensitive filesystem.')
        folded.add(name.casefold())
        path = directory / name
        if not path.exists() and not path.is_symlink():
            raise ValueError('A referenced sound input is missing from the project assets folder.')
        _safe(path)
        wav_info(path)  # Same bounded PCM16 RIFF/RF64 validation as the renderer.
        files[member] = path
    return files


def _inventory(project_file, project_dir, *, include_cache, include_audio,
               file_budget=DEFAULT_FILE_BUDGET, byte_budget=DEFAULT_BYTE_BUDGET,
               cancelled=lambda: False):
    files, total, path_bytes = {}, 0, 0

    def include(name, path):
        nonlocal total, path_bytes
        check_cancelled(cancelled)
        _member(name)
        info = _safe(path)
        total += info.st_size
        path_bytes += len(name.encode('utf-8')) + 128
        if len(files) + 1 > file_budget or path_bytes > MANIFEST_BUDGET:
            raise ValueError('The project exceeds the configured snapshot file or metadata budget.')
        if total > byte_budget:
            raise ValueError('The project exceeds the configured snapshot byte budget.')
        files[name] = path

    include('studio/project.json', project_file)
    recovery = project_file.with_name(project_file.name + '.recovery.json')
    if recovery.exists() or recovery.is_symlink():
        data = _read_json(recovery)
        if (type(data) is not dict or set(data) != {'kind', 'version', 'project_id', 'base_sha256', 'project'}
                or data['kind'] != 'audio-studio-recovery' or type(data['version']) is not int
                or data['version'] != 1 or data['project_id'] != _read_json(project_file)['project_id']):
            raise ValueError('The recovery draft is invalid or belongs to another project.')
        _studio(data['project'])
        include('studio/project.json.recovery.json', recovery)
    for name, path in input_assets(project_file, project_dir,
            file_budget=file_budget, cancelled=cancelled).items():
        include(name, path)
    if project_dir is None:
        return files
    def visit(directory, relative=()):
        # Scandir enforces budgets during enumeration rather than first making
        # unbounded per-directory name lists. Only this fixed tree is traversed.
        with os.scandir(directory) as entries:
            for entry in entries:
                check_cancelled(cancelled)
                path = directory / entry.name
                permitted = ({'cache'} if include_cache else set()) | ({'renders'} if include_audio else set())
                is_directory = (not relative and entry.name in permitted) or (
                    relative == ('renders',) and re.fullmatch(_HEX, entry.name))
                if is_directory:
                    _safe(path, directory=True)
                    visit(path, (*relative, entry.name))
                else:
                    member = 'audio/' + '/'.join((*relative, entry.name))
                    if _allowed(member):
                        include(member, path)

    visit(project_dir)
    if include_cache:
        _music_inventory(project_dir, include, file_budget=file_budget,
                         byte_budget=byte_budget, cancelled=cancelled)
    return files


@contextmanager
def _snapshot_lock(project_dir):
    if project_dir is None:
        yield
    else:
        with project_lock(project_dir):
            yield


def backup_project(project_file, destination, *, project_dir=None, include_cache=True,
                   include_audio=True, byte_budget=DEFAULT_BYTE_BUDGET,
                   file_budget=DEFAULT_FILE_BUDGET, cancelled=lambda: False,
                   on_status=lambda message: None):
    """Publish a new ZIP64 snapshot of an idle project; never overwrite a file."""
    _budget(byte_budget, file_budget)
    if type(include_cache) is not bool or type(include_audio) is not bool:
        raise ValueError('Choose whether to include cached chunks and rendered outputs.')
    source, target = _local(project_file), _local(destination)
    _safe(source)
    _safe(target.parent, directory=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError('Choose a new snapshot filename.')
    root = _local(project_dir) if project_dir is not None else None
    if root is not None:
        _safe(root, directory=True)
        if target.is_relative_to(root):
            raise ValueError('Save the snapshot outside its audio source folder.')
    if target == source:
        raise ValueError('The snapshot cannot replace its project source.')
    with _snapshot_lock(root):
        check_cancelled(cancelled)
        project = _studio(_read_json(source))
        paths = _inventory(source, root, include_cache=include_cache, include_audio=include_audio,
                           file_budget=file_budget, byte_budget=byte_budget, cancelled=cancelled)
        if len(paths) > file_budget:
            raise ValueError('The project exceeds the configured snapshot file budget.')
        before = {name: _identity(_safe(path)) for name, path in paths.items()}
        total = sum(info[2] for info in before.values())
        if total > byte_budget:
            raise ValueError('The project exceeds the configured snapshot byte budget.')
        # ZIP_STORED keeps memory bounded and avoids spending hours compressing
        # already compressed audio. Its space requirement is predictable.
        if shutil.disk_usage(target.parent).free < total + len(paths) * 1024 + 1024 * 1024:
            raise OSError('There is not enough free space for this project snapshot.')
        parent_identity = _identity(_safe(target.parent, directory=True))[:2]
        fd, temporary = tempfile.mkstemp(prefix='.audio-snapshot-', dir=target.parent)
        os.close(fd)
        stage = Path(temporary)
        records = []
        try:
            with zipfile.ZipFile(stage, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for number, (name, path) in enumerate(sorted(paths.items()), 1):
                    check_cancelled(cancelled)
                    checksum, size = hashlib.sha256(), 0
                    with path.open('rb') as original, archive.open(name, 'w', force_zip64=True) as output:
                        if _identity(os.fstat(original.fileno())) != before[name]:
                            raise ValueError('A source file changed before the snapshot was read.')
                        while block := original.read(BLOCK):
                            check_cancelled(cancelled)
                            size += len(block)
                            if size > before[name][2]:
                                raise ValueError('A source file grew while the snapshot was being made.')
                            checksum.update(block)
                            output.write(block)
                    if size != before[name][2] or _identity(_safe(path)) != before[name]:
                        raise ValueError('A source file changed while the snapshot was being made.')
                    records.append({'path': name, 'size': size, 'sha256': checksum.hexdigest()})
                    on_status(f'Snapshot: saved {number} of {len(paths)} files.')
                current = _inventory(source, root, include_cache=include_cache, include_audio=include_audio,
                                     file_budget=file_budget, byte_budget=byte_budget, cancelled=cancelled)
                if set(current) != set(paths) or any(_identity(_safe(current[name])) != before[name] for name in paths):
                    raise ValueError('The project changed during its snapshot. Try again while it is idle.')
                manifest = {'kind': 'audio-studio-snapshot', 'version': 1, 'project_id': project['project_id'],
                            'files': records, 'total_bytes': total, 'include_cache': include_cache,
                            'include_audio': include_audio,
                            'limitations': ['Audio Studio project only; not a full research-library backup.',
                                'API profiles, credentials, executable helpers and installed voice/music models are excluded.',
                                'Completed audio, accepted cached/music chunks and their checked resume state are copied when selected. Partial working scratch files are excluded.']}
                raw_manifest = _json(manifest)
                if len(raw_manifest) > MANIFEST_BUDGET:
                    raise ValueError('Snapshot manifest exceeds its metadata budget.')
                archive.writestr('manifest.json', raw_manifest)
            check_cancelled(cancelled)
            if _identity(_safe(target.parent, directory=True))[:2] != parent_identity:
                raise ValueError('The snapshot destination folder changed.')
            # Windows needs a writable descriptor for fsync; r+b preserves the
            # completed ZIP bytes and still flushes before atomic publication.
            with stage.open('r+b') as stream:
                os.fsync(stream.fileno())
            os.link(stage, target)
            return {'snapshot': str(target), 'files': len(records), 'bytes': total,
                    'project_id': project['project_id'], 'limitations': manifest['limitations']}
        finally:
            stage.unlink(missing_ok=True)


def _preflight_archive(stream, size, *, file_budget, cancelled):
    """Count bounded central records before ZipFile allocates its inventory.

    Studio writes comment-free single-disk ZIP/ZIP64. End-record entry counts
    alone are insufficient because ZipFile scans to the directory's end.
    """
    if size < 22:
        raise ValueError('Truncated audio snapshot archive.')
    end = size - 22
    stream.seek(end)
    try:
        signature, disk, directory_disk, disk_count, count, length, offset, comment = struct.unpack(
            '<4s4H2LH', stream.read(22))
        if signature != b'PK\x05\x06' or disk or directory_disk or disk_count != count or comment:
            raise ValueError('Unsupported audio snapshot end record.')
        legacy, locator = (count, length, offset), b''
        if end >= 20:
            stream.seek(end - 20)
            locator = stream.read(20)
        if locator.startswith(b'PK\x06\x07') or count == 0xffff or length == 0xffffffff or offset == 0xffffffff:
            if end < 76:
                raise ValueError('Truncated audio ZIP64 end record.')
            signature, disk, end64, disks = struct.unpack('<4sLQL', locator)
            if signature != b'PK\x06\x07' or disk or disks != 1 or end64 + 56 != end - 20:
                raise ValueError('Unsupported audio ZIP64 locator.')
            stream.seek(end64)
            values = struct.unpack('<4sQ2H2L4Q', stream.read(56))
            signature, record_size, _, _, disk, directory_disk, disk_count, count, length, offset = values
            if signature != b'PK\x06\x06' or record_size != 44 or disk or directory_disk or disk_count != count:
                raise ValueError('Unsupported audio ZIP64 end record.')
            if any(old != sentinel and old != actual for old, actual, sentinel in
                   zip(legacy, (count, length, offset), (0xffff, 0xffffffff, 0xffffffff))):
                raise ValueError('Conflicting audio snapshot directory lengths.')
            end = end64
    except struct.error as exc:
        raise ValueError('Truncated audio snapshot end record.') from exc
    if (not 0 < count <= file_budget + 1 or not 0 < length <= CENTRAL_DIRECTORY_BUDGET
            or offset < 30 or offset + length != end or length < 46 * count):
        raise ValueError('Audio snapshot directory exceeds its budget or is inconsistent.')
    stream.seek(offset)
    for _ in range(count):
        check_cancelled(cancelled)
        header = stream.read(46)
        if len(header) != 46 or header[:4] != b'PK\x01\x02':
            raise ValueError('Invalid audio snapshot central-directory record.')
        name_size, extra_size, comment_size = struct.unpack_from('<3H', header, 28)
        if not 0 < name_size <= 4096 or extra_size > 4096 or comment_size:
            raise ValueError('Unsupported audio snapshot directory metadata.')
        if stream.tell() + name_size + extra_size > offset + length:
            raise ValueError('Truncated audio snapshot central directory.')
        stream.seek(name_size + extra_size, os.SEEK_CUR)
    if stream.tell() != offset + length:
        raise ValueError('Audio snapshot directory count disagrees with its actual records.')
    stream.seek(0)
    return count


def _archive_manifest(archive, *, byte_budget, file_budget):
    infos = archive.infolist()
    if len(infos) > file_budget + 1:
        raise ValueError('The snapshot exceeds the configured file budget.')
    names = [info.filename for info in infos]
    if len(names) != len(set(name.casefold() for name in names)) or names.count('manifest.json') != 1:
        raise ValueError('The snapshot contains duplicate or ambiguous paths.')
    for info in infos:
        mode = (info.external_attr >> 16) & 0xFFFF
        if (info.is_dir() or info.flag_bits & 1 or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            raise ValueError('Linked, encrypted or unsupported snapshot entries are not accepted.')
    manifest_info = archive.getinfo('manifest.json')
    if manifest_info.file_size > MANIFEST_BUDGET:
        raise ValueError('Snapshot manifest exceeds its metadata budget.')
    raw = archive.read(manifest_info)
    value = decode_json(raw.decode('utf-8'))
    keys = {'kind', 'version', 'project_id', 'files', 'total_bytes', 'include_cache', 'include_audio', 'limitations'}
    if (type(value) is not dict or set(value) != keys or value['kind'] != 'audio-studio-snapshot'
            or type(value['version']) is not int or value['version'] != 1 or type(value['files']) is not list
            or type(value['total_bytes']) is not int or value['total_bytes'] < 0
            or type(value['include_cache']) is not bool or type(value['include_audio']) is not bool
            or len(value['files']) > file_budget):
        raise ValueError('Unsupported Audio Studio snapshot manifest.')
    expected, folded, total = {}, set(), 0
    for record in value['files']:
        if (type(record) is not dict or set(record) != {'path', 'size', 'sha256'}
                or type(record['size']) is not int or record['size'] < 0
                or not isinstance(record['sha256'], str) or not re.fullmatch(_HEX, record['sha256'])):
            raise ValueError('Invalid snapshot file record.')
        name = _member(record['path'])
        if ((not value['include_cache'] and name.startswith(('audio/cache/', 'audio/music-jobs/')))
                or (not value['include_audio'] and name.startswith('audio/renders/'))):
            raise ValueError('Snapshot files disagree with the selected content scope.')
        if name.casefold() in folded:
            raise ValueError('Duplicate snapshot file record.')
        folded.add(name.casefold())
        expected[name] = record
        total += record['size']
    if (set(names) != set(expected) | {'manifest.json'} or 'studio/project.json' not in expected
            or total != value['total_bytes'] or total > byte_budget):
        raise ValueError('Snapshot contents, byte budget or manifest do not agree.')
    for name, record in expected.items():
        if archive.getinfo(name).file_size != record['size']:
            raise ValueError('Snapshot member size does not match its manifest.')
    return value, expected


@contextmanager
def _checked_archive(source, *, file_budget, cancelled):
    before = _safe(source)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(source, flags), 'rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError('Audio snapshot changed before it was opened.')
        count = _preflight_archive(stream, before.st_size, file_budget=file_budget, cancelled=cancelled)
        try:
            with zipfile.ZipFile(stream, 'r') as archive:
                if len(archive.infolist()) != count:
                    raise ValueError('Audio snapshot inventory count is inconsistent.')
                yield archive
        except zipfile.BadZipFile as exc:
            raise ValueError('Invalid or damaged audio snapshot archive.') from exc
        if (_identity(os.fstat(stream.fileno())) != _identity(before)
                or _identity(_safe(source)) != _identity(before)):
            raise ValueError('Audio snapshot changed during restore.')


def _check_restored_music(stage, records, *, file_budget, byte_budget, cancelled):
    expected = {name for name in records if name.startswith('audio/music-jobs/')}
    seen = set()

    def include(name, path):
        if name not in expected or name in seen:
            raise ValueError('Music checkpoint contents disagree with the snapshot inventory.')
        seen.add(name)

    _music_inventory(stage / 'audio', include, file_budget=file_budget,
                     byte_budget=byte_budget, cancelled=cancelled)
    if seen != expected:
        raise ValueError('Snapshot contains music files that were not accepted checkpoints.')


def restore_project(snapshot, destination, *, byte_budget=DEFAULT_BYTE_BUDGET,
                    file_budget=DEFAULT_FILE_BUDGET, cancelled=lambda: False,
                    on_status=lambda message: None):
    """Verify every byte, then restore into a newly claimed destination folder.

    The destination contains one ``project`` directory published after complete
    verification. The saved output path and compatible recovery baseline are
    relocated; original hashes remain in ``restore-manifest.json``.
    """
    _budget(byte_budget, file_budget)
    source, target = _local(snapshot), _local(destination)
    _safe(source)
    _safe(target.parent, directory=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError('Restore requires a new destination folder.')
    if source.is_relative_to(target):
        raise ValueError('The restore destination cannot contain its source snapshot.')
    parent_id = _identity(_safe(target.parent, directory=True))[:2]
    check_cancelled(cancelled)
    with tempfile.TemporaryDirectory(prefix='.audio-restore-', dir=target.parent) as temporary:
        stage = Path(temporary) / 'project'
        stage.mkdir(mode=0o700)
        with _checked_archive(source, file_budget=file_budget, cancelled=cancelled) as archive:
            manifest, records = _archive_manifest(archive, byte_budget=byte_budget, file_budget=file_budget)
            if shutil.disk_usage(target.parent).free < manifest['total_bytes'] + 1024 * 1024:
                raise OSError('There is not enough free space to restore the project.')
            for number, (name, record) in enumerate(sorted(records.items()), 1):
                check_cancelled(cancelled)
                path = stage.joinpath(*name.split('/'))
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                checksum, size = hashlib.sha256(), 0
                with archive.open(name) as incoming, path.open('xb') as output:
                    while block := incoming.read(BLOCK):
                        check_cancelled(cancelled)
                        size += len(block)
                        if size > record['size']:
                            raise ValueError('A snapshot member exceeded its declared size.')
                        checksum.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if size != record['size'] or checksum.hexdigest() != record['sha256']:
                    raise ValueError('A snapshot file failed its SHA256 integrity check.')
                on_status(f'Restore: verified {number} of {len(records)} files.')
        _check_restored_music(stage, records, file_budget=file_budget,
                              byte_budget=byte_budget, cancelled=cancelled)
        project_path = stage / 'studio' / 'project.json'
        project = _studio(_read_json(project_path))
        if project['project_id'] != manifest['project_id']:
            raise ValueError('The snapshot project identity does not match its manifest.')
        assets = input_assets(project_path, stage / 'audio', strict=True,
                              file_budget=file_budget, cancelled=cancelled)
        if set(assets) != {name for name in records if name.startswith('audio/assets/')}:
            raise ValueError('The snapshot sound inputs do not match its saved and recovery scripts.')
        output_root = target / 'project' / 'audio'
        (stage / 'audio').mkdir(exist_ok=True)
        old_hash = records['studio/project.json']['sha256']
        project['output_dir'] = str(output_root)
        relocated = _json(project)
        project_path.write_bytes(relocated)
        recovery = stage / 'studio' / 'project.json.recovery.json'
        recovery_relocated = False
        if recovery.exists():
            value = _read_json(recovery)
            if (type(value) is not dict or set(value) != {'kind', 'version', 'project_id', 'base_sha256', 'project'}
                    or value['kind'] != 'audio-studio-recovery' or type(value['version']) is not int
                    or value['version'] != 1 or value['project_id'] != project['project_id']):
                raise ValueError('The snapshot recovery draft is invalid.')
            _studio(value['project'])
            if value['project']['project_id'] != project['project_id']:
                raise ValueError('The recovery draft belongs to another project.')
            if value['base_sha256'] == old_hash:
                value['base_sha256'] = hashlib.sha256(relocated).hexdigest()
                value['project']['output_dir'] = str(output_root)
                recovery.write_bytes(_json(value))
                recovery_relocated = True
        restored_manifest = {'kind': 'audio-studio-restored-snapshot', 'version': 1,
                             'source_manifest': manifest, 'output_folder_relocated': True,
                             'recovery_relocated': recovery_relocated,
                             'project_sha256': hashlib.sha256(relocated).hexdigest()}
        (stage / 'restore-manifest.json').write_bytes(_json(restored_manifest))
        for changed in (project_path, recovery if recovery.exists() else project_path, stage / 'restore-manifest.json'):
            # These are our newly written staging files, not original inputs.
            # Use update mode so Windows can durably flush the relocated state.
            with changed.open('r+b') as stream:
                os.fsync(stream.fileno())
        check_cancelled(cancelled)
        if _identity(_safe(target.parent, directory=True))[:2] != parent_id:
            raise ValueError('The restore destination parent changed.')
        # Claim the new parent without overwriting even an empty pre-existing
        # directory, then atomically move the verified private staging tree.
        target.mkdir(mode=0o700)
        try:
            stage.rename(target / 'project')
        except BaseException:
            target.rmdir()
            raise
    return {'directory': str(target / 'project'), 'project_file': str(target / 'project' / 'studio' / 'project.json'),
            'project_dir': str(output_root), 'files': len(records), 'bytes': manifest['total_bytes'],
            'recovery_relocated': recovery_relocated, 'limitations': manifest['limitations']}
