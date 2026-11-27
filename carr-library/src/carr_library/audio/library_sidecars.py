"""Streaming adapters for the existing managed Audio Studio producer.

Only ``audio-projects/<id>/studio`` and that project's fixed ``audio`` child
are authoritative. Saved output paths are never traversed. Metadata is bounded
JSON; accepted media is copied in bounded blocks under the producer's native
draft/render locks. Model installations, credentials, active process journals,
unaccepted work and arbitrary neighboring files are outside this inventory.
"""
from contextlib import ExitStack, nullcontext
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import shutil

from ..ingest.restore import RestoreError
from ..reader.narration_text import NarrationDraft
from . import backup as producer
from .managed import ManagedProjects, MAX_PROJECTS, MAX_DOCUMENT_BYTES


DIRECTORY = 'audio-projects'
MAX_FILES = 100_000
MAX_BYTES = 128 * 1024 ** 3
MAX_FILE_BYTES = 32 * 1024 ** 3
BLOCK = 1024 * 1024
_ID = r'[0-9a-f]{32}'
_HEX = r'[0-9a-f]{64}'
_ROOT = re.compile(DIRECTORY + '/(' + _ID + ')/(.+)\\Z')
_RENDER = re.compile(r'(?:complete\.json|timeline\.json|subtitles\.srt|chapters\.ffmeta|'
                     r'master\.(?:wav|flac|mp3|opus|m4a|m4b|aiff)|stem-[0-9]{3,6}\.wav|'
                     r'chapter-[0-9]{6}\.wav|music-[0-9]{6}\.(?:mid|music\.json))\Z')


def check_cancelled(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Audio library backup cancelled; no completed destination was published.')


def _guard(cancelled):
    def guarded():
        check_cancelled(cancelled)
        return False
    return guarded


def _parts(name):
    match = _ROOT.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        raise RestoreError('unsupported managed audio sidecar')
    tail = producer._member(match[2])
    if tail == 'audio/render-journal.json':
        raise RestoreError('live audio process journals are not portable checkpoints')
    if tail.startswith('audio/renders/') and not _RENDER.fullmatch(PurePosixPath(tail).name):
        raise RestoreError('unsupported completed audio artifact')
    return match[1], tail


def accepts(name):
    if not isinstance(name, str) or not name.startswith(DIRECTORY + '/'):
        return False
    _parts(name)
    return True


def generated(name):
    tail = _parts(name)[1]
    return tail.startswith('audio/') and not tail.startswith('audio/assets/')


def streamed(name):
    return _parts(name)[1].startswith('audio/')


def limit(name):
    _, tail = _parts(name)
    if tail.startswith('audio/assets/'):
        return MAX_FILE_BYTES
    if re.fullmatch('audio/cache/' + _HEX + r'\.json', tail):
        return 65536
    return MAX_DOCUMENT_BYTES if tail.endswith('.json') else MAX_FILE_BYTES


def minimum(name):
    _parts(name)
    return 0 if name.endswith(('.srt', '.vtt', '.ffmeta')) else 1


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _projects(root):
    base = root / DIRECTORY
    if not os.path.lexists(base):
        return ()
    producer._safe(base, directory=True)
    result, count = [], 0
    with os.scandir(base) as children:
        for child in children:
            count += 1
            if count > MAX_PROJECTS:
                raise RestoreError('managed audio project inventory exceeds its limit')
            if not re.fullmatch(_ID, child.name):
                continue
            path = Path(child.path)
            producer._safe(path, directory=True)
            saved = path / 'studio' / 'project.json'
            if os.path.lexists(saved):
                result.append(child.name)
            elif os.path.lexists(saved.with_name(saved.name + '.recovery.json')):
                raise RestoreError('managed audio recovery has no saved project')
    return tuple(sorted(result))


def _source(db, payload):
    producer._studio(payload)
    draft = NarrationDraft.from_dict(payload['draft'])
    reference = payload['source_reference']
    if reference is None or reference['kind'] == 'local-text':
        return
    from ..backup_sidecars import _validate_original_text
    from ..reader.reading_state import _location
    if reference['kind'] == 'saved-reading-copy-snapshot':
        location = reference['source_location']
        if location is not None:
            _validate_original_text(db, _location(location), draft.source_text)
        return
    pages = reference.get('pages')
    if (type(pages) is not list or not 1 <= len(pages) <= 10000
            or set(reference) != {'kind', 'workspace', 'edition', 'revision', 'pages', 'separator'}
            or reference['separator'] != 'form-feed between exact source pages or text segments'):
        raise RestoreError('invalid audio source-page inventory')
    offset, original = 0, draft.source_text
    for index, page in enumerate(pages):
        if (type(page) is not dict or set(page) != {'location', 'source_start', 'source_end', 'verified'}
                or type(page['source_start']) is not int or type(page['source_end']) is not int
                or type(page['verified']) is not bool or page['source_start'] != offset
                or not offset <= page['source_end'] <= len(original)):
            raise RestoreError('invalid audio source-page binding')
        location = _location(page['location'])
        if (location.workspace, location.edition, location.revision) != (
                reference['workspace'], reference['edition'], reference['revision']):
            raise RestoreError('audio source-page scope differs from its saved source')
        _validate_original_text(db, location, original[offset:page['source_end']])
        offset = page['source_end']
        if index + 1 < len(pages):
            if original[offset:offset + 1] != '\f':
                raise RestoreError('invalid audio source-page separator')
            offset += 1
    if offset != len(original):
        raise RestoreError('audio source-page inventory is incomplete')


def validate_metadata(db, name, raw):
    project_id, tail = _parts(name)
    if not tail.startswith('studio/') or type(raw) is not bytes or not 0 < len(raw) <= limit(name):
        raise RestoreError('invalid managed audio metadata')
    value = producer.decode_json(raw.decode('utf-8'))
    if tail.endswith('.recovery.json'):
        if (type(value) is not dict or set(value) != {'kind', 'version', 'project_id', 'base_sha256', 'project'}
                or value['kind'] != 'audio-studio-recovery' or type(value['version']) is not int
                or value['version'] != 1 or value['project_id'] != project_id
                or not isinstance(value['base_sha256'], str)
                or re.fullmatch(_HEX, value['base_sha256']) is None):
            raise RestoreError('invalid managed audio recovery document')
        value = value['project']
    _source(db, value)
    if value['project_id'] != project_id:
        raise RestoreError('managed audio project differs from its owned folder')


def _hash(path, size, cancelled):
    actual = producer._checked_hash(path, maximum=size, cancelled=cancelled)
    if producer._safe(path).st_size != size:
        raise RestoreError('audio artifact differs from its accepted size')
    return actual


def _digest(value):
    return isinstance(value, str) and re.fullmatch(_HEX, value) is not None


def _accepted_cache(paths, cancelled):
    """Read the actual ChunkCache receipt format without repairing the source."""
    accepted = set()
    for name, path in paths.items():
        match = re.fullmatch('audio/cache/(' + _HEX + r')\.json', name)
        if match is None:
            continue
        check_cancelled(cancelled)
        info = producer._read_json(path, budget=65536)
        keys = {'schema_version', 'key', 'sha256', 'size', 'sample_rate', 'channels', 'frames',
                'provenance', 'companions'}
        if (type(info) is not dict or set(info) != keys or type(info['schema_version']) is not int
                or info['schema_version'] != 1 or info['key'] != match[1] or not _digest(info['sha256'])
                or type(info['size']) is not int or not 1 <= info['size'] <= MAX_FILE_BYTES
                or any(type(info[key]) is not int for key in ('sample_rate', 'channels', 'frames'))
                or type(info['provenance']) is not dict or type(info['companions']) is not list
                or len(info['companions']) > 2):
            raise RestoreError('invalid accepted audio chunk receipt')
        wav = name[:-5] + '.wav'
        if wav not in paths or _hash(paths[wav], info['size'], cancelled) != info['sha256']:
            raise RestoreError('accepted audio chunk failed its checksum')
        from .ffmpeg import wav_info
        current = wav_info(paths[wav])
        if (current.rate, current.channels, current.frames) != (info['sample_rate'], info['channels'], info['frames']):
            raise RestoreError('accepted audio chunk format differs from its receipt')
        accepted.update((name, wav))
        seen = set()
        for companion in info['companions']:
            if (type(companion) is not dict or set(companion) != {'name', 'size', 'sha256'}
                    or companion['name'] not in (match[1] + '.mid', match[1] + '.music.json')
                    or companion['name'] in seen or type(companion['size']) is not int
                    or not 1 <= companion['size'] <= MAX_FILE_BYTES or not _digest(companion['sha256'])):
                raise RestoreError('invalid accepted audio companion receipt')
            seen.add(companion['name'])
            relative = 'audio/cache/' + companion['name']
            if relative not in paths or _hash(paths[relative], companion['size'], cancelled) != companion['sha256']:
                raise RestoreError('accepted audio companion failed its checksum')
            accepted.add(relative)
    return {name: path for name, path in paths.items() if not name.startswith('audio/cache/') or name in accepted}


def _validated_paths(db, root, project_id, include_generated, cancelled, *,
                     file_budget=MAX_FILES, byte_budget=MAX_BYTES, strict_inputs=False):
    project = root / DIRECTORY / project_id
    saved, audio = project / 'studio' / 'project.json', project / 'audio'
    paths = producer._inventory(saved, audio if os.path.lexists(audio) else None,
        include_cache=include_generated, include_audio=include_generated,
        file_budget=file_budget, byte_budget=byte_budget, cancelled=cancelled)
    producer.input_assets(saved, audio, strict=strict_inputs, file_budget=file_budget, cancelled=cancelled)
    paths.pop('audio/render-journal.json', None)
    for name in tuple(paths):
        if ((not include_generated and name.startswith('audio/') and not name.startswith('audio/assets/'))
                or (name.startswith('audio/renders/') and not _RENDER.fullmatch(PurePosixPath(name).name))):
            del paths[name]
    for name, path in paths.items():
        if name.startswith('studio/'):
            from ..backup_sidecars import read_regular
            validate_metadata(db, f'{DIRECTORY}/{project_id}/{name}', read_regular(path, MAX_DOCUMENT_BYTES))
        elif re.fullmatch('audio/plan-' + _HEX + r'\.json', name):
            from .models import ProductionPlan
            ProductionPlan.from_dict(producer._read_json(path, budget=16 * 1024 ** 2))
        elif name.startswith('audio/assets/'):
            from .ffmpeg import wav_info
            wav_info(path)
    if include_generated:
        paths = _accepted_cache(paths, cancelled)
        groups = {PurePosixPath(name).parts[2] for name in paths if name.startswith('audio/renders/')}
        for run_key in groups:
            prefix = 'audio/renders/' + run_key + '/'
            selected = {name for name in paths if name.startswith(prefix)}
            if prefix + 'complete.json' not in selected:
                paths = {name: path for name, path in paths.items() if name not in selected}
                continue
            result = producer._read_json(paths[prefix + 'complete.json'], budget=16 * 1024 ** 2)
            records = result.get('files') if type(result) is dict else None
            if type(records) is not list or len(records) > MAX_FILES:
                raise RestoreError('invalid completed audio receipt')
            expected = {prefix + 'complete.json'}
            for record in records:
                if (type(record) is not dict or set(record) != {'name', 'size', 'sha256'}
                        or not isinstance(record['name'], str) or not _RENDER.fullmatch(record['name'])
                        or record['name'] == 'complete.json' or type(record['size']) is not int
                        or not _digest(record['sha256']) or prefix + record['name'] in expected):
                    raise RestoreError('invalid completed audio artifact receipt')
                expected.add(prefix + record['name'])
            from .production import _verified_output
            if expected != selected or _verified_output(audio / 'renders' / run_key, run_key, cancelled) is None:
                raise RestoreError('completed audio failed its producer verification')
    return {f'{DIRECTORY}/{project_id}/{name}': path for name, path in paths.items()}


def _capture(db, root, projects, include_generated, max_bytes, cancelled):
    paths, identities, total, metadata_bytes = {}, {}, 0, 0
    for project_id in projects:
        if len(paths) >= MAX_FILES or total >= min(MAX_BYTES, max_bytes):
            raise RestoreError('managed audio snapshot exceeds its file or byte budget')
        for name, path in _validated_paths(db, root, project_id, include_generated, cancelled,
                file_budget=MAX_FILES - len(paths), byte_budget=min(MAX_BYTES, max_bytes) - total).items():
            check_cancelled(cancelled)
            accepts(name)
            info = producer._safe(path)
            total += info.st_size
            metadata_bytes += len(name.encode('utf-8')) + 128
            if (len(paths) >= MAX_FILES or total > min(MAX_BYTES, max_bytes)
                    or metadata_bytes > producer.MANIFEST_BUDGET
                    or not minimum(name) <= info.st_size <= limit(name)):
                raise RestoreError('managed audio snapshot exceeds its file, byte or metadata budget')
            if not streamed(name) and info.st_nlink != 1:
                raise RestoreError('managed audio metadata cannot be hard linked')
            paths[name], identities[name] = path, _identity(info)
    groups = {}
    for name, identity in identities.items():
        if identity[3] > 1:
            groups.setdefault(identity[:2], []).append((name, identity))
    for group in groups.values():
        if (len({identity for _, identity in group}) != 1 or len(group) != group[0][1][3]
                or len({_parts(name)[0] for name, _ in group}) != 1
                or any(_parts(name)[1].split('/')[1] not in ('cache', 'renders', 'music-jobs') for name, _ in group)):
            raise RestoreError('audio hard links extend outside the verified project inventory')
    return paths, identities


def _copy(source, target, *, maximum, cancelled, expected=None):
    before = _identity(producer._safe(source))
    if not 0 <= before[4] <= maximum:
        raise RestoreError('audio sidecar exceeds its byte budget')
    if target is not None:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(target.parent).free < before[4] + BLOCK:
            raise OSError('There is not enough free disk space for this audio snapshot.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    digest, size = sha256(), 0
    with ExitStack() as streams:
        incoming = streams.enter_context(os.fdopen(os.open(source, flags), 'rb'))
        outgoing = streams.enter_context(target.open('xb') if target is not None else nullcontext(None))
        if _identity(os.fstat(incoming.fileno())) != before:
            raise RestoreError('audio sidecar changed while opening')
        while block := incoming.read(min(BLOCK, before[4] + 1 - size)):
            check_cancelled(cancelled)
            size += len(block)
            if size > before[4]:
                raise RestoreError('audio sidecar grew during copying')
            digest.update(block)
            if outgoing is not None:
                outgoing.write(block)
        if outgoing is not None:
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if _identity(os.fstat(incoming.fileno())) != before:
            raise RestoreError('audio sidecar changed while copying')
    if size != before[4] or _identity(producer._safe(source)) != before:
        raise RestoreError('audio sidecar changed while copying')
    receipt = {'size_bytes': size, 'sha256': digest.hexdigest()}
    if expected is not None and receipt != {key: expected[key] for key in receipt}:
        raise RestoreError('audio sidecar digest or size mismatch')
    check_cancelled(cancelled)
    return receipt


def snapshot(db, root, stage, *, include_generated=False, max_bytes=MAX_BYTES, cancelled=lambda: False):
    """Called under the common library writer; stream only idle owned projects."""
    check_cancelled(cancelled)
    if type(include_generated) is not bool or type(max_bytes) is not int or max_bytes < 0:
        raise RestoreError('invalid managed audio snapshot options')
    cancelled = _guard(cancelled)
    root, stage = Path(root), Path(stage)
    projects = _projects(root)
    managed = ManagedProjects(root)
    with ExitStack() as locks:
        for project_id in projects:
            try:
                locks.enter_context(managed.lock(project_id))
            except RuntimeError as exc:
                raise RestoreError('An audio project is active. Pause it and retry the backup.') from exc
        paths, before = _capture(db, root, projects, include_generated, max_bytes, cancelled)
        result = []
        for name in sorted(paths):
            check_cancelled(cancelled)
            result.append({'filename': name, **_copy(paths[name], stage / name,
                maximum=limit(name), cancelled=cancelled)})
        _current, after = _capture(db, root, projects, include_generated, max_bytes, cancelled)
        if _projects(root) != projects or after != before:
            raise RestoreError('managed audio projects changed during backup')
        # Metadata cannot prove unchanged content on every filesystem. Re-read
        # each source against its copied digest before accepting the snapshot.
        for entry in result:
            name = entry['filename']
            _copy(paths[name], None, maximum=limit(name), cancelled=cancelled, expected=entry)
        _current, after = _capture(db, root, projects, include_generated, max_bytes, cancelled)
        if _projects(root) != projects or after != before:
            raise RestoreError('managed audio projects changed during backup')
        managed.check_root()
        validate_tree(db, stage, result, include_generated=include_generated, cancelled=cancelled)
        return result


def validate_tree(db, root, entries, *, include_generated, cancelled=lambda: False):
    """Validate copied metadata and accepted checkpoints without opening a job."""
    root = Path(root)
    names = {entry['filename'] for entry in entries}
    projects = tuple(sorted({_parts(name)[0] for name in names}))
    actual = set()
    for project_id in projects:
        saved = root / DIRECTORY / project_id / 'studio' / 'project.json'
        if not saved.is_file():
            raise RestoreError('managed audio checkpoint has no saved project')
        actual.update(_validated_paths(db, root, project_id, include_generated, cancelled, strict_inputs=True))
    if actual != names:
        raise RestoreError('managed audio manifest includes unaccepted or incomplete artifacts')


def restore(db, source, stage, entries, *, include_generated, cancelled=lambda: False, copy_files=True):
    """Verify signed source bytes and stage an inert portable producer tree."""
    check_cancelled(cancelled)
    cancelled = _guard(cancelled)
    for entry in entries:
        check_cancelled(cancelled)
        name = entry['filename']
        accepts(name)
        if generated(name) and not include_generated:
            raise RestoreError('generated audio was not declared in this library backup')
        _copy(Path(source) / name, Path(stage) / name if copy_files else None, maximum=limit(name),
              cancelled=cancelled, expected=entry)
    validate_tree(db, stage if copy_files else source, entries,
                  include_generated=include_generated, cancelled=cancelled)


def initialize_restored(root, entries):
    """Restore publication owns this new root; establish only fixed empty jobs."""
    for project_id in {_parts(entry['filename'])[0] for entry in entries}:
        (Path(root) / DIRECTORY / project_id / 'audio').mkdir(mode=0o700, exist_ok=True)
