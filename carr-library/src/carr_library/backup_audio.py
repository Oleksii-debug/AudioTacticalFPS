"""Typed managed Audio Studio adapter for the shared library backup registry.

Project data is copied without rewriting saved or recovery bytes. Models,
credentials and arbitrary private neighbours are outside the producer inventory.
The existing Studio parser, source validator, input-asset resolver and project
locks remain authoritative; media is read and hashed in bounded chunks.
"""
from contextlib import contextmanager, ExitStack, nullcontext
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import unicodedata

from .ingest.bundle import _plain, _unique_object
from .ingest.restore import RestoreError
from .ingest.writer_guard import _regular_if_present
from .reader.reading_state import ReadingLocation

MAX_FILES = 100000
MAX_BYTES = 128 * 1024**3
MAX_FILE_BYTES = 32 * 1024**3
MAX_JSON_BYTES = 64 * 1024**2
MAX_PROJECTS = 1000
_PROJECT_ID = re.compile(r'[0-9a-f]{32}')
_HEX = r'[0-9a-f]{64}'
_MUSIC = re.compile(r'audio/music-jobs/' + _HEX +
    r'/(?:cue\.(?:wav|mid|music\.json)|cue\.music-work/(?:state\.json|part-[0-9]{8}\.wav|score\.partial\.mid|provenance\.partial\.json))\Z')


def audio_name(name):
    return isinstance(name, str) and name.startswith('audio-projects/')


def is_generated(name):
    if not audio_name(name):
        return False
    parts = _portable_relative(name)
    return len(parts) >= 4 and parts[2] == 'audio' and parts[3] != 'assets'


def allowed(name):
    parts = _portable_relative(name)
    if len(parts) < 3 or parts[0] != 'audio-projects' or not _PROJECT_ID.fullmatch(parts[1]):
        return False
    tail = '/'.join(parts[2:])
    from .audio.backup import _allowed
    return tail == 'restore-manifest.json' or bool(_MUSIC.fullmatch(tail)) or bool(_allowed(tail))


def minimum_size(name):
    return 0 if name.lower().endswith(('.srt', '.vtt', '.ffmeta')) else 1


def sidecar_limit(name):
    if not allowed(name):
        raise RestoreError('unsupported managed audio artifact')
    return MAX_JSON_BYTES if name.lower().endswith('.json') else MAX_FILE_BYTES


def validate_inventory(entries):
    names = {entry['filename'] for entry in entries if audio_name(entry['filename'])}
    for name in names:
        owner = '/'.join(_portable_relative(name)[:2])
        if owner + '/studio/project.json' not in names:
            raise RestoreError('managed audio artifacts require their same-project saved Studio document')


def _portable_relative(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value:
        raise RestoreError('invalid audio-project artifact path')
    path = PurePosixPath(value)
    if (path.is_absolute() or len(path.parts) > 16
            or any(part in ('', '.', '..') for part in value.split('/'))
            or any(part.rstrip(' .') != part or PureWindowsPath(part).is_reserved()
                   or any(c in '<>:"|?*' or ord(c) < 32 or 127 <= ord(c) < 160 for c in part)
                   for part in path.parts)):
        raise RestoreError('invalid audio-project artifact path')
    return path.parts


def _claim_path(relative, names, *, allow_existing=False):
    """Reject aliases of either a file or one of its parent directories."""
    parts = _portable_relative(relative)
    for count in range(1, len(parts) + 1):
        name = '/'.join(parts[:count])
        key = unicodedata.normalize('NFC', name).casefold()
        item = (name, count == len(parts))
        previous = names.get(key)
        if previous is not None and (previous != item or (item[1] and not allow_existing)):
            raise RestoreError('duplicate or conflicting reading/audio paths on Windows')
        names[key] = item


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _check_internal_links(inventory):
    """Canonical production links cache chunks into completed renders.

    Permit those links only when every name for that inode belongs to this
    bounded snapshot and the same project. Copies in the archive are independent.
    """
    groups = {}
    for name, identity in inventory.items():
        if identity[3] > 1:
            groups.setdefault(identity[:2], []).append((name, identity))
    for group in groups.values():
        names = [name for name, _ in group]
        identities = {identity for _, identity in group}
        if (len(identities) != 1 or len(group) != group[0][1][3]
                or any(not _production_link_path(name) for name in names)
                or len({PurePosixPath(name).parts[1] for name in names}) != 1):
            raise RestoreError('audio hard links extend outside the verified project inventory')


def _children(path, limit):
    _plain(path, directory=True)
    children = []
    for child in path.iterdir():
        if len(children) >= limit:
            raise RestoreError('audio-project directory exceeds entry limit')
        children.append(child)
    return tuple(sorted(children))


def _projects(root):
    folder = root / 'audio-projects'
    if not os.path.lexists(folder):
        return ()
    result = []
    for child in _children(folder, MAX_PROJECTS):
        if _PROJECT_ID.fullmatch(child.name):
            _plain(child, directory=True)
            result.append(child)
    return tuple(result)


def _audio_document(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise RestoreError('audio-project metadata exceeds its size limit')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON number.')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RestoreError('invalid audio-project metadata') from exc
    if type(value) is not dict:
        raise RestoreError('invalid audio-project metadata')
    return value


@contextmanager
def _locked_sidecars(root):
    from filelock import FileLock, Timeout
    from .audio.jobs import project_lock
    projects = _projects(root)
    with ExitStack() as stack:
        for project in projects:
            lock_path = project / '.draft.lock'
            _regular_if_present(lock_path, empty=True)
            try:
                stack.enter_context(FileLock(str(lock_path), timeout=0))
            except Timeout as exc:
                raise RestoreError('An audio project is being edited. Retry the backup after it stops.') from exc
            if os.path.lexists(project / 'studio' / 'project.json'):
                try:
                    # A saved draft can have no render files yet. Establish
                    # its lock now, before any renderer can create those files.
                    stack.enter_context(project_lock(project / 'audio'))
                except RuntimeError as exc:
                    raise RestoreError('An audio project is rendering. Retry the backup after it stops.') from exc
        def verify():
            if _projects(root) != projects:
                raise RestoreError('Audio projects changed during backup. Retry with the projects idle.')
        yield verify


def _validate_studio_source(db, payload):
    from .audio.backup import _studio
    from .reader.narration_text import NarrationDraft
    _studio(payload)
    reference = payload['source_reference']
    if reference is None or reference['kind'] != 'library-source-reading-copy':
        return
    original = NarrationDraft.from_dict(payload['draft']).source_text
    pages = reference.get('pages')
    if type(pages) is not list or not 1 <= len(pages) <= 10000:
        raise RestoreError('invalid audio source-page inventory')
    offset, locations = 0, []
    for index, page in enumerate(pages):
        if (type(page) is not dict or set(page) != {'location', 'source_start', 'source_end', 'verified'}
                or type(page['location']) is not dict or type(page['source_start']) is not int
                or type(page['source_end']) is not int or type(page['verified']) is not bool
                or page['source_start'] != offset or not offset <= page['source_end'] <= len(original)):
            raise RestoreError('invalid audio source-page binding')
        try:
            location = ReadingLocation(**page['location'])
        except (TypeError, ValueError) as exc:
            raise RestoreError('invalid audio source location') from exc
        if (location.workspace != reference.get('workspace') or location.edition != reference.get('edition')
                or location.revision != reference.get('revision')
                or sha256(original[offset:page['source_end']].encode('utf-8')).hexdigest() != location.text_sha256):
            raise RestoreError('audio reading copy does not match its exact saved source')
        locations.append(location)
        offset = page['source_end']
        if index + 1 < len(pages):
            if original[offset:offset + 1] != '\f':
                raise RestoreError('invalid audio source-page separator')
            offset += 1
    if offset != len(original):
        raise RestoreError('audio source-page inventory is incomplete')
    from .backup_sidecars import validate_locations
    validate_locations(db, locations)


def _validate_studios(db, root):
    for project in _projects(root):
        saved = project / 'studio' / 'project.json'
        if not os.path.lexists(saved):
            continue
        payload = _audio_document(saved)
        _validate_studio_source(db, payload)
        if payload['project_id'] != project.name:
            raise RestoreError('saved audio project identity differs from its managed folder')
        recovery = saved.with_name(saved.name + '.recovery.json')
        if os.path.lexists(recovery):
            value = _audio_document(recovery)
            if (set(value) != {'kind', 'version', 'project_id', 'base_sha256', 'project'}
                    or value['kind'] != 'audio-studio-recovery' or type(value['version']) is not int
                    or value['version'] != 1 or value['project_id'] != payload['project_id']
                    or not isinstance(value['base_sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', value['base_sha256'])):
                raise RestoreError('invalid audio recovery draft')
            _validate_studio_source(db, value['project'])
            if value['project']['project_id'] != payload['project_id']:
                raise RestoreError('audio recovery draft belongs to another project')


def _managed_output_directories(entries):
    # Empty output roots contain no file to archive. Recreate only the reserved
    # audio sibling of a verified managed studio document, never a JSON path.
    return tuple(sorted({str(PurePosixPath(entry['filename']).parents[1] / 'audio')
                         for entry in entries if entry['filename'].endswith('/studio/project.json')}))

def _production_link_path(relative):
    parts = _portable_relative(relative)
    if len(parts) < 5 or parts[0] != 'audio-projects' or parts[2] != 'audio':
        return False
    return parts[3] in ('cache', 'renders', 'music-jobs') and allowed(relative)


def _sidecar_info(root, relative):
    maximum = sidecar_limit(relative)
    parts = _portable_relative(relative)
    parent = root
    _plain(parent, directory=True)
    for part in parts[:-1]:
        parent = parent / part
        _plain(parent, directory=True)
    info = _plain(parent / parts[-1])
    if info.st_nlink < 1 or (info.st_nlink != 1 and not _production_link_path(relative)):
        raise RestoreError('audio sidecars cannot have unaccounted hard links')
    if not minimum_size(relative) <= info.st_size <= maximum:
        raise RestoreError('audio sidecar exceeds its file budget')
    return info


def _cancel(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Library backup or verification cancelled.')


def _studio_files(project, max_files, include_generated, cancelled):
    from .audio.backup import _allowed, input_assets
    from .audio.jobs import sha256_file
    saved = project / 'studio' / 'project.json'
    if not os.path.lexists(saved):
        # An interrupted first save has no committed document to back up.
        # Existing committed audio files must never disappear silently.
        if (project / 'audio').exists():
            meaningful = [p for p in _children(project / 'audio', max_files)
                          if p.name not in ('.render.lock',)]
            if meaningful:
                raise RestoreError('managed audio artifacts have no saved Studio document')
        return
    yield saved
    recovery = saved.with_name(saved.name + '.recovery.json')
    if os.path.lexists(recovery):
        yield recovery
    for path in input_assets(saved, project / 'audio', file_budget=max_files, cancelled=cancelled).values():
        yield path
    if not include_generated:
        return
    audio = project / 'audio'
    if not os.path.lexists(audio):
        return
    for child in _children(audio, max_files):
        _cancel(cancelled)
        if child.name == 'assets':
            continue  # The producer resolver above owns this namespace.
        if _allowed('audio/' + child.name):
            yield child
        elif child.name == 'cache':
            for item in _children(child, max_files):
                if _allowed('audio/cache/' + item.name):
                    yield item
        elif child.name == 'renders':
            for render in _children(child, max_files):
                if not re.fullmatch(_HEX, render.name):
                    continue
                for item in _children(render, max_files):
                    if _allowed('audio/renders/' + render.name + '/' + item.name):
                        yield item
        elif child.name == 'music-jobs':
            for job in _children(child, max_files):
                if not re.fullmatch(_HEX, job.name):
                    continue
                for item in _children(job, max_files):
                    relative = item.relative_to(project).as_posix()
                    if _MUSIC.fullmatch(relative):
                        yield item
                    elif item.name == 'cue.music-work':
                        for checkpoint in _children(item, max_files):
                            relative = checkpoint.relative_to(project).as_posix()
                            if not _MUSIC.fullmatch(relative):
                                continue
                            if checkpoint.name in ('score.partial.mid', 'provenance.partial.json'):
                                final = job / ('cue.mid' if checkpoint.name == 'score.partial.mid' else 'cue.music.json')
                                if not os.path.lexists(final):
                                    continue
                                _plain(checkpoint)
                                _plain(final)
                                if (_plain(checkpoint).st_size > sidecar_limit(checkpoint.relative_to(project.parent.parent).as_posix())
                                        or sha256_file(checkpoint, cancelled=cancelled) != sha256_file(final, cancelled=cancelled)):
                                    continue  # Uncommitted staging bytes are not a saved checkpoint.
                            yield checkpoint


def capture(root, *, include_generated=True, max_files=MAX_FILES,
            max_bytes=MAX_BYTES, cancelled=lambda: False):
    _cancel(cancelled)
    files, names, total = {}, {}, 0
    for project in _projects(root):
        for path in _studio_files(project, max_files, include_generated, cancelled):
            _cancel(cancelled)
            name = path.relative_to(root).as_posix()
            if name in files:
                continue
            _claim_path(name, names)
            info = _sidecar_info(root, name)
            total += info.st_size
            if len(files) >= max_files or total > max_bytes:
                raise RestoreError('managed audio exceeds the snapshot file or byte budget')
            files[name] = _identity(info)
        provenance = project / 'restore-manifest.json'
        if os.path.lexists(provenance):
            name = provenance.relative_to(root).as_posix()
            _claim_path(name, names)
            info = _sidecar_info(root, name)
            total += info.st_size
            if len(files) >= max_files or total > max_bytes:
                raise RestoreError('managed audio exceeds the snapshot file or byte budget')
            files[name] = _identity(info)
    _check_internal_links(files)
    validate_inventory([{'filename': name} for name in files])
    return dict(sorted(files.items()))


def validate(root, db, *, max_files=MAX_FILES, cancelled=lambda: False):
    from .audio.backup import input_assets
    _validate_studios(db, root)
    for project in _projects(root):
        saved = project / 'studio' / 'project.json'
        if not os.path.lexists(saved):
            raise RestoreError('managed audio has no saved Studio document')
        input_assets(saved, project / 'audio', strict=True, file_budget=max_files, cancelled=cancelled)
        music = project / 'audio' / 'music-jobs'
        if music.exists():
            for job in _children(music, MAX_FILES):
                if not re.fullmatch(_HEX, job.name):
                    raise RestoreError('invalid music checkpoint identity')
                for path in (job / 'cue.music.json', job / 'cue.music-work' / 'state.json'):
                    if path.exists():
                        value = _audio_document(path)
                        if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
                            raise RestoreError('unsupported music checkpoint metadata')
                        if path.name == 'state.json':
                            if (not isinstance(value.get('settings_sha256'), str)
                                    or not re.fullmatch(_HEX, value['settings_sha256'])
                                    or not isinstance(value.get('settings'), dict)
                                    or not isinstance(value.get('chunks'), dict)):
                                raise RestoreError('invalid accepted music checkpoint inventory')


def copy_record(root, relative, target, identity, cancelled):
    before = _sidecar_info(root, relative)
    if _identity(before) != identity:
        raise RestoreError('Audio state changed before copying or verification.')
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(target.parent).free < before.st_size:
            raise OSError('Insufficient free disk for the verified audio snapshot.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    digest, count = sha256(), 0
    output_context = target.open('xb') if target is not None else nullcontext(None)
    with os.fdopen(os.open(root / relative, flags), 'rb') as incoming, output_context as output:
        if _identity(os.fstat(incoming.fileno())) != identity:
            raise RestoreError('Audio state changed while opening.')
        if relative.lower().endswith('.wav') or '/audio/assets/' in relative:
            header = incoming.read(12)
            if len(header) != 12 or header[:4] not in (b'RIFF', b'RF64') or header[8:] != b'WAVE':
                raise RestoreError('project input/audio artifact is not WAV or RF64')
            incoming.seek(0)
        while block := incoming.read(min(1024 * 1024, before.st_size + 1 - count)):
            _cancel(cancelled)
            count += len(block)
            if count > before.st_size:
                raise RestoreError('audio sidecar grew during copying')
            if output is not None:
                output.write(block)
            digest.update(block)
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
        if _identity(os.fstat(incoming.fileno())) != identity:
            raise RestoreError('audio sidecar changed during copying')
    if count != before.st_size or _identity(_sidecar_info(root, relative)) != identity:
        raise RestoreError('audio sidecar changed during copying')
    return dict(filename=relative, size_bytes=count, sha256=digest.hexdigest())


locked = _locked_sidecars
output_directories = _managed_output_directories
