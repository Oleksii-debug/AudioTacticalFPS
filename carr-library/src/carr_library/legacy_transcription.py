"""Inert recovery of canonical ASR work through the current retained-input service.

Only the old data protocol is understood here. Runtime paths, trust flags and
worker checkpoints are retained verbatim, but never executed or copied into the
active transcription-jobs namespace. A new job needs a currently reviewed runtime.
"""
from collections.abc import Mapping
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from uuid import uuid4

from . import legacy_transcription_schema as schema
from .legacy_transcription_values import encode, fields, fingerprint
from .backup_files import local_path, regular, optional_present, sync_directory
from .backup_legacy_state import relative_name, validate_state, _stamp
from .audio.jobs import verified_reader, publish_new, publish_noreplace, file_identity
from .ingest.bundle import _unique_object
from .ingest.writer_guard import exclusive_writer

DIRECTORY = 'state/transcription-imports'
ARCHIVE_SCHEMA = 'carr-canonical-transcription-archive/1'
LINK_SCHEMA = 'carr-canonical-transcription-import/1'
MAX_FILES = 100000
MAX_ARCHIVES = 1000
MAX_METADATA_BYTES = 512 * 1024**2
MAX_BYTES = schema.MAX_INPUT_BYTES + MAX_METADATA_BYTES
MAX_MANIFEST = 8 * 1024**2
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[0-9a-f]{32}\Z')


def _stop(cancelled):
    if cancelled():
        raise InterruptedError('Historical transcription operation cancelled.')


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite historical value.')))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid historical transcription JSON.') from exc


def validate_entries(entries):
    """Use exactly the canonical per-file/input bounds, never a generic increase."""
    validate_state(entries, max_bytes=MAX_BYTES)
    input_bytes = metadata_bytes = 0
    for row in entries:
        name, size = row['filename'], row['size_bytes']
        maximum = schema.limit(name)
        if (not maximum or len(name) > 1024 or len(relative_name(name).parts) > 12
                or not (0 if schema.allows_empty(name) else 1) <= size <= maximum):
            raise ValueError('Invalid canonical transcription path or file size.')
        if schema.is_input(name):
            input_bytes += size
        else:
            metadata_bytes += size
    if input_bytes > schema.MAX_INPUT_BYTES or metadata_bytes > MAX_METADATA_BYTES:
        raise ValueError('Historical transcription exceeds its input/metadata budget.')
    return input_bytes


def _digest(path, row, *, cancelled, collect=False):
    _stop(cancelled)
    path = local_path(path, exists=True)
    before = regular(path)
    if before.st_nlink != 1 or before.st_size != row['size_bytes']:
        raise ValueError('Historical transcription file size or ownership changed.')
    checksum, size = sha256(), 0
    blocks = [] if collect else None
    with verified_reader(path, expected_identity=file_identity(before)) as stream:
        while block := stream.read(min(1024 * 1024, row['size_bytes'] + 1 - size)):
            _stop(cancelled)
            size += len(block)
            if size > row['size_bytes']:
                raise ValueError('Historical transcription file grew during verification.')
            checksum.update(block)
            if blocks is not None:
                blocks.append(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Historical transcription changed during verification.')
    if (_stamp(regular(path)) != _stamp(before) or size != row['size_bytes']
            or checksum.hexdigest() != row['sha256']):
        raise ValueError('Historical transcription checksum or identity mismatch.')
    return b''.join(blocks) if blocks is not None else None


class _Records(Mapping):
    """Decode one bounded metadata record at a time, retaining no text cache."""
    def __init__(self, entries, resolve, cancelled):
        self.rows = {row['filename']: row for row in entries if row['filename'].endswith('.json')}
        self.resolve, self.cancelled = resolve, cancelled

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, name):
        row = self.rows[name]
        raw = _digest(self.resolve(name), row, cancelled=self.cancelled, collect=True)
        return schema.validate(name, raw)


def validate_payload(entries, resolve, *, cancelled=lambda: False):
    """Authenticate only owned payloads; declarations of external paths stay inert."""
    validate_entries(entries)
    for row in entries:
        _stop(cancelled)
        raw = _digest(resolve(row['filename']), row, cancelled=cancelled,
                      collect=not schema.is_input(row['filename']))
        if raw is not None:
            schema.validate(row['filename'], raw)
    records = _Records(entries, resolve, cancelled)
    schema.validate_collection(entries, records)
    return records


def _payload_name(name):
    # Keep the source extension in this new archive. Existing historical archives
    # keep their original hashed names and are read by their own exact mapper.
    return sha256(name.encode('utf-8')).hexdigest() + PurePosixPath(name).suffix


def _manifest(entries):
    raw = encode(dict(schema=ARCHIVE_SCHEMA, source_namespace=schema.PREFIX.rstrip('/'),
                      files=entries))
    if len(raw) > MAX_MANIFEST:
        raise ValueError('Historical transcription manifest exceeds its byte budget.')
    return raw, sha256(raw).hexdigest()


def _read_manifest(archive):
    path = archive / 'manifest.json'
    before = regular(path)
    if not 0 < before.st_size <= MAX_MANIFEST:
        raise ValueError('Invalid historical transcription archive manifest.')
    row = dict(size_bytes=before.st_size, sha256=archive.name)
    raw = _digest(path, row, cancelled=lambda: False, collect=True)
    value = _json(raw)
    fields(value, {'schema', 'source_namespace', 'files'})
    if value['schema'] != ARCHIVE_SCHEMA or value['source_namespace'] != 'state/transcription':
        raise ValueError('Unknown historical transcription archive.')
    validate_entries(value['files'])
    return raw, value['files']


def _bounded_children(directory, maximum):
    regular(directory, directory=True)
    result = []
    with os.scandir(directory) as items:
        for item in items:
            if len(result) >= maximum:
                raise ValueError('Historical transcription namespace exceeds its entry budget.')
            result.append(Path(item.path))
    return tuple(sorted(result))


def _live_inventory(root, *, cancelled):
    base = Path(root) / 'state/transcription'
    if not optional_present(base):
        return {}
    local_path(base, exists=True)
    pending, files, count = [base], {}, 0
    while pending:
        directory = pending.pop()
        regular(directory, directory=True)
        with os.scandir(directory) as children:
            for entry in children:
                _stop(cancelled)
                count += 1
                if count > MAX_FILES * 4:
                    raise ValueError('Historical transcription directory budget exceeded.')
                path = Path(entry.path)
                name = path.relative_to(root).as_posix()
                if directory == base:
                    family = schema.namespace_family(entry.name)
                    if family == 'scoped':
                        # The independent workspace ASR producer retains and validates
                        # these siblings. They never enter canonical recovery history.
                        is_directory = re.fullmatch('[a-f0-9]{64}', entry.name) is not None
                        info = regular(path, directory=is_directory)
                        if not is_directory and info.st_nlink != 1:
                            raise ValueError('Scoped transcription metadata has an alias.')
                        continue
                    if family != 'canonical':
                        raise ValueError('Unknown transcription namespace; recovery was not completed.')
                    regular(path, directory=True)
                if len(relative_name(name).parts) > 12 or len(name) > 1024:
                    raise ValueError('Historical transcription path budget exceeded.')
                if entry.is_dir(follow_symlinks=False):
                    regular(path, directory=True)
                    pending.append(path)
                    continue
                info = regular(path)
                if info.st_nlink != 1:
                    raise ValueError('Historical transcription files cannot be hard links.')
                if schema.ignored(name):
                    continue
                maximum = schema.limit(name)
                if not maximum or not (0 if schema.allows_empty(name) else 1) <= info.st_size <= maximum:
                    raise ValueError('Unsupported historical transcription file.')
                if len(files) >= MAX_FILES:
                    raise ValueError('Too many historical transcription files.')
                files[name] = _stamp(info)
    return dict(sorted(files.items()))


def _live_jobs(root):
    base = Path(root) / 'state/transcription/jobs'
    if not optional_present(base):
        return ()
    result = _bounded_children(base, schema.MAX_JOBS)
    for item in result:
        if not _ID.fullmatch(item.name):
            raise ValueError('Unknown historical transcription job namespace.')
        regular(item, directory=True)
    return result


@contextmanager
def snapshot_guard(root):
    """Outer application writer -> canonical profile -> sorted run/control locks."""
    root = Path(root)
    base = root / 'state/transcription'
    if not optional_present(base):
        yield
        return
    from filelock import FileLock, Timeout
    local_path(base, exists=True)
    jobs = _live_jobs(root)
    directories = []
    profiles = base / 'profiles'
    if optional_present(profiles):
        directories.append((profiles, 'profiles'))
    for job in jobs:
        directories.extend(((job, 'run'), (job, 'control')))
    with ExitStack() as stack:
        for directory, name in directories:
            regular(directory, directory=True)
            lock = directory / ('.' + name + '.lock')
            if optional_present(lock):
                if regular(lock).st_nlink != 1:
                    raise ValueError('Historical transcription lock has an external alias.')
            try:
                stack.enter_context(FileLock(str(lock), timeout=0, mode=0o600))
            except Timeout as exc:
                raise RuntimeError('An earlier transcription operation is active; stop it before recovery or backup.') from exc
            if regular(lock).st_nlink != 1:
                raise ValueError('Historical transcription lock changed during acquisition.')
        if _live_jobs(root) != jobs:
            raise ValueError('Historical transcription jobs changed before their snapshot.')
        yield
        if _live_jobs(root) != jobs:
            raise ValueError('Historical transcription jobs changed during their snapshot.')


@dataclass(frozen=True)
class LiveSnapshot:
    inventory: dict
    files: tuple
    archive_id: str | None


def _copy_archive(entries, resolve, destination, *, cancelled):
    """Publish one immutable data archive. A matching retry also retries fsync."""
    raw, identity = _manifest(entries)
    base = Path(destination) / DIRECTORY / 'archives'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    local_path(base, exists=True)
    target = base / identity
    if optional_present(target):
        stored, existing = _read_manifest(target)
        if stored != raw:
            raise ValueError('Historical transcription archive identity conflict.')
        _validate_archive(target, existing, cancelled=cancelled)
        sync_directory(base)
        return identity
    stage = Path(tempfile.mkdtemp(prefix='.archive-', dir=base))
    try:
        (stage / 'files').mkdir(mode=0o700)
        for row in entries:
            _stop(cancelled)
            source = resolve(row['filename'])
            target_file = stage / 'files' / _payload_name(row['filename'])
            publish_new(source, target_file, cancelled=cancelled)
            _digest(target_file, row, cancelled=cancelled)
        with (stage / 'manifest.json').open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        validate_payload(entries, lambda name: stage / 'files' / _payload_name(name),
                         cancelled=cancelled)
        sync_directory(stage / 'files')
        _stop(cancelled)
        publish_noreplace(stage, target, cancelled=cancelled)
        return identity
    finally:
        if optional_present(stage):
            shutil.rmtree(stage)


def _validate_archive(archive, entries=None, *, cancelled):
    regular(archive, directory=True)
    if {path.name for path in _bounded_children(archive, 3)} != {'manifest.json', 'files'}:
        raise ValueError('Unexpected historical transcription archive contents.')
    if entries is None:
        _, entries = _read_manifest(archive)
    actual = _bounded_children(archive / 'files', MAX_FILES + 1)
    if {path.name for path in actual} != {_payload_name(row['filename']) for row in entries}:
        raise ValueError('Historical transcription archive has missing or unlisted data.')
    return validate_payload(entries, lambda name: archive / 'files' / _payload_name(name),
                            cancelled=cancelled)


def capture_live(root, destination, *, cancelled=lambda: False):
    """Called under snapshot_guard; returns the frozen source identity for a final pass."""
    root = Path(root)
    inventory = _live_inventory(root, cancelled=cancelled)
    if not inventory:
        return LiveSnapshot(inventory, (), None)
    entries = []
    for name, identity in inventory.items():
        _stop(cancelled)
        path = root / name
        checksum = sha256()
        with verified_reader(path, expected_identity=file_identity(regular(path))) as stream:
            size = 0
            while block := stream.read(1024 * 1024):
                _stop(cancelled)
                size += len(block)
                if size > identity[4]:
                    raise ValueError('Historical transcription grew during snapshot.')
                checksum.update(block)
        if _stamp(regular(path)) != identity or size != identity[4]:
            raise ValueError('Historical transcription changed during snapshot.')
        entries.append(dict(filename=name, size_bytes=size, sha256=checksum.hexdigest()))
    validate_payload(entries, lambda name: root / name, cancelled=cancelled)
    # The native execution lock establishes idleness. A stale recorded running
    # label after a crash is retained as history, never promoted to live state.
    identity = _copy_archive(entries, lambda name: root / name, destination, cancelled=cancelled)
    result = LiveSnapshot(inventory, tuple(entries), identity)
    check_live(root, result, cancelled=cancelled)
    return result


def check_live(root, snapshot, *, cancelled=lambda: False):
    if _live_inventory(Path(root), cancelled=cancelled) != snapshot.inventory:
        raise ValueError('Historical transcription inventory changed during backup.')
    if snapshot.files:
        validate_payload(list(snapshot.files), lambda name: Path(root) / name, cancelled=cancelled)
    if _live_inventory(Path(root), cancelled=cancelled) != snapshot.inventory:
        raise ValueError('Historical transcription changed during final verification.')


def is_state_path(name):
    return isinstance(name, str) and bool(re.fullmatch(
        DIRECTORY + r'/(?:archives/[0-9a-f]{64}/(?:manifest\.json|files/[0-9a-f]{64}\.[a-z0-9]+)'
        r'|links/[0-9a-f]{32}\.json)', name))


def state_limit(name):
    if not is_state_path(name):
        return None
    return MAX_MANIFEST if name.endswith('/manifest.json') else (
        65536 if '/links/' in name else schema.MAX_SOURCE_BYTES
        if PurePosixPath(name).suffix in schema.AUDIO_EXTENSIONS else schema.MAX_METADATA_BYTES)


def state_paths(root, *, cancelled=lambda: False):
    base = Path(root) / DIRECTORY
    if not optional_present(base):
        return ()
    local_path(base, exists=True)
    result = []
    for group in _bounded_children(base, 3):
        if group.name not in ('archives', 'links'):
            raise ValueError('Unknown historical transcription recovery namespace.')
        regular(group, directory=True)
        if group.name == 'links':
            for path in _bounded_children(group, schema.MAX_JOBS):
                if re.fullmatch(r'\.link-[A-Za-z0-9_-]{8}\.json', path.name):
                    if regular(path).st_nlink != 1:
                        raise ValueError('Interrupted recovery receipt stage has an alias.')
                    continue
                if not _ID.fullmatch(path.stem) or path.suffix != '.json':
                    raise ValueError('Unknown transcription import link.')
                result.append(path)
        else:
            for archive in _bounded_children(group, MAX_ARCHIVES):
                if re.fullmatch(r'\.archive-[A-Za-z0-9_-]{8}', archive.name):
                    regular(archive, directory=True)
                    continue  # Unpublished interrupted staging is not durable history.
                if not _HASH.fullmatch(archive.name):
                    raise ValueError('Incomplete or unknown historical transcription archive.')
                _, entries = _read_manifest(archive)
                if {p.name for p in _bounded_children(archive, 3)} != {'files', 'manifest.json'}:
                    raise ValueError('Unexpected historical transcription archive contents.')
                actual = _bounded_children(archive / 'files', MAX_FILES + 1)
                if {p.name for p in actual} != {_payload_name(row['filename']) for row in entries}:
                    raise ValueError('Missing or unlisted historical transcription payload.')
                result.append(archive / 'manifest.json')
                result.extend(actual)
    if len(result) > MAX_FILES:
        raise ValueError('Too many historical transcription recovery files.')
    for path in result:
        _stop(cancelled)
        if regular(path).st_nlink != 1:
            raise ValueError('Historical transcription recovery file has an alias.')
    return tuple(sorted(result))


def validate_state_tree(db, root, *, cancelled=lambda: False):
    paths = state_paths(root, cancelled=cancelled)
    for path in paths:
        _stop(cancelled)
        if path.name == 'manifest.json':
            _validate_archive(path.parent, cancelled=cancelled)
        elif path.parent.name == 'links':
            _validate_link(root, path, cancelled=cancelled)


def snapshot_entries(stage, snapshot):
    if snapshot.archive_id is None:
        return []
    raw, identity = _manifest(list(snapshot.files))
    prefix = DIRECTORY + '/archives/' + identity
    return [dict(path=prefix + '/manifest.json', size_bytes=len(raw), sha256=identity),
        *[dict(path=prefix + '/files/' + _payload_name(row['filename']),
               size_bytes=row['size_bytes'], sha256=row['sha256']) for row in snapshot.files]]


def validate_historical(entries, resolve, *, cancelled=lambda: False):
    selected = [row for row in entries if schema.canonical_path(row['filename'])]
    if selected:
        validate_payload(selected, resolve, cancelled=cancelled)


def _archive_source(root, reference):
    fields(reference, {'kind', 'archive', 'job'})
    if (type(reference['archive']) is not str or type(reference['job']) is not str
            or not _HASH.fullmatch(reference['archive']) or not _ID.fullmatch(reference['job'])):
        raise ValueError('Invalid historical transcription selection.')
    if reference['kind'] == 'archive':
        archive = Path(root) / DIRECTORY / 'archives' / reference['archive']
        _, entries = _read_manifest(archive)
        resolve = lambda name: archive / 'files' / _payload_name(name)
    elif reference['kind'] == 'historical':
        from . import backup_historical
        archive = Path(root) / backup_historical.DIRECTORY / reference['archive']
        _, manifest = backup_historical._manifest(archive / 'manifest.json', reference['archive'])
        subdirectory, original, _ = backup_historical.source_inventory(manifest)
        entries = [dict(row, filename=('state/' if subdirectory else '') + row['filename'])
                   for row in original if schema.canonical_path(('state/' if subdirectory else '') + row['filename'])]
        resolve = lambda name: archive / 'files' / backup_historical._file_key(
            name.removeprefix('state/') if subdirectory else name)
    else:
        raise ValueError('Unknown historical transcription archive kind.')
    if not entries:
        raise ValueError('The selected archive has no canonical transcription data.')
    return entries, resolve


def _selected(root, reference, *, cancelled):
    entries, resolve = _archive_source(root, reference)
    records = validate_payload(entries, resolve, cancelled=cancelled)
    name = schema.PREFIX + 'jobs/' + reference['job'] + '/job.json'
    if name not in records:
        raise ValueError('The historical transcription job is absent.')
    return records[name], entries, resolve, records


def jobs(root, *, cancelled=lambda: False):
    """Bounded metadata listing; status is explicitly recorded, not live completion."""
    references = []
    base = Path(root) / DIRECTORY / 'archives'
    if optional_present(base):
        for archive in _bounded_children(base, MAX_ARCHIVES):
            if re.fullmatch(r'\.archive-[A-Za-z0-9_-]{8}', archive.name):
                regular(archive, directory=True)
                continue
            if not _HASH.fullmatch(archive.name):
                raise ValueError('Unknown historical transcription archive.')
            _, entries = _read_manifest(archive)
            references.append(('archive', archive.name, entries,
                               lambda name, a=archive: a / 'files' / _payload_name(name)))
    from . import backup_historical
    historical = Path(root) / backup_historical.DIRECTORY
    if optional_present(historical):
        for archive in _bounded_children(historical, backup_historical.MAX_ARCHIVES):
            if not _HASH.fullmatch(archive.name):
                raise ValueError('Unknown historical backup archive.')
            _, manifest = backup_historical._manifest(archive / 'manifest.json', archive.name)
            subdirectory, original, _ = backup_historical.source_inventory(manifest)
            entries = [dict(row, filename=('state/' if subdirectory else '') + row['filename'])
                       for row in original if schema.canonical_path(('state/' if subdirectory else '') + row['filename'])]
            references.append(('historical', archive.name, entries,
                lambda name, a=archive, sub=subdirectory: a / 'files' / backup_historical._file_key(
                    name.removeprefix('state/') if sub else name)))
    result = []
    for kind, identity, entries, resolve in references:
        for row in entries:
            _stop(cancelled)
            match = schema.JOB.fullmatch(row['filename'])
            if match and match[2] == 'job':
                value = schema.validate(row['filename'], _digest(resolve(row['filename']), row,
                    cancelled=cancelled, collect=True))
                result.append(dict(reference=dict(kind=kind, archive=identity, job=match[1]),
                    title=value['title'], recorded_status=value['status'], source_count=len(value['sources']),
                    result_count=len(value['results']), sources=[dict(id=x['id'], name=x['name'], bytes=x['size'],
                        has_result=any(item['source_id'] == x['id'] for item in value['results']),
                        has_edit=x['id'] in value['edits']) for x in value['sources']], historical=True))
                if len(result) > schema.MAX_JOBS:
                    raise ValueError('Too many historical transcription jobs.')
    return tuple(result)


def retain_live(root, *, cancelled=lambda: False):
    root = local_path(root, exists=True)
    before = _live_inventory(root, cancelled=cancelled)
    if not before:
        return None
    from .operations import ResourceBroker, ResourceRequest
    request = ResourceRequest(cpu_slots=1, memory_bytes=512 * 1024**2,
        disk_path=root, disk_bytes=sum(stamp[4] for stamp in before.values()) + 16 * 1024**2)
    broker = ResourceBroker.for_app(root, cancelled=cancelled)
    with broker.reserve(request, operation_id='document:' + uuid4().hex, cancelled=cancelled, timeout=60) as lease:
        def stopping():
            _stop(cancelled)
            lease.checkpoint()
            return False
        with exclusive_writer(root), snapshot_guard(root):
            if _live_inventory(root, cancelled=stopping) != before:
                raise ValueError('The earlier transcription changed while waiting for recovery resources.')
            return capture_live(root, root, cancelled=stopping).archive_id


def read_transcript(root, reference, source_id, *, edited=False, cancelled=lambda: False):
    job, _, resolve, records = _selected(root, reference, cancelled=cancelled)
    base = schema.PREFIX + 'jobs/' + job['id'] + '/'
    result = next((row for row in job['results'] if row['source_id'] == source_id), None)
    if result is None:
        raise ValueError('The selected historical source has no accepted transcript.')
    receipt = records[base + result['receipt']]
    if edited:
        saved = job['edits'].get(source_id)
        if saved is None:
            raise ValueError('The selected historical source has no saved edit.')
        record = records[base + saved['path']]
        text, path = record['text'], resolve(base + saved['path'])
    else:
        text, path = receipt['result']['text'], resolve(base + result['receipt'])
    return dict(text=text, title=job['title'], historical=True, citation_authority=False,
                provenance=record['provenance'] if edited else receipt['provenance'], original_path=str(path))


def reading_copy(root, reference, source_id, *, edited=False, cancelled=lambda: False):
    result = read_transcript(root, reference, source_id, edited=edited, cancelled=cancelled)
    if len(result['text'].encode('utf-8')) > 8 * 1024**2:
        raise ValueError('Choose a historical transcript of at most 8 MiB for the reading editor.')
    from .reader.narration import new_project
    return new_project(result['text'], title=result['title'][:160] + ' — historical reading copy',
                       original_path=result['original_path'])


def export_text(root, reference, source_id, destination, *, edited=False, approval=None,
                cancelled=lambda: False):
    from .research.export_file import write_export
    value = read_transcript(root, reference, source_id, edited=edited, cancelled=cancelled)
    return write_export(destination, value['text'], private_root=root, approval=approval,
                        cancelled=cancelled)



def export_output(root, reference, source_id, extension, destination, *, approval=None,
                  cancelled=lambda: False):
    """Export an exact accepted historical format, without regenerating inference."""
    if extension not in schema.FORMATS:
        raise ValueError('Choose TXT, SRT, VTT or JSON from the saved historical outputs.')
    job, entries, resolve, records = _selected(root, reference, cancelled=cancelled)
    base = schema.PREFIX + 'jobs/' + job['id'] + '/'
    result = next((row for row in job['results'] if row['source_id'] == source_id), None)
    if result is None:
        raise ValueError('The selected historical source has no accepted transcript.')
    receipt = records[base + result['receipt']]
    output = receipt['files'].get(extension)
    if output is None:
        raise ValueError('That format was not present in the accepted historical receipt.')
    name = base + output['path']
    row = next(row for row in entries if row['filename'] == name)
    raw = _digest(resolve(name), row, cancelled=cancelled, collect=True)
    from .research.export_file import write_binary_export
    return write_binary_export(destination, raw, private_root=root, approval=approval,
                               cancelled=cancelled)

def _validate_link(root, path, *, cancelled):
    info = regular(path)
    if not 0 < info.st_size <= 65536:
        raise ValueError('Invalid historical transcription import receipt.')
    with verified_reader(path) as stream:
        value = _json(stream.read(65537))
    fields(value, {'schema', 'reference', 'sources', 'job', 'definition_sha256'})
    if value['schema'] != LINK_SCHEMA or value['job'] != path.stem or not _ID.fullmatch(value['job']):
        raise ValueError('Invalid historical transcription import receipt identity.')
    job, _, _, _ = _selected(root, value['reference'], cancelled=cancelled)
    if (type(value['sources']) is not list or not value['sources']
            or any(type(key) is not str for key in value['sources'])
            or len(set(value['sources'])) != len(value['sources'])):
        raise ValueError('Invalid imported historical source selection.')
    chosen = {row['id']: row for row in job['sources']}
    if any(type(key) is not str or key not in chosen for key in value['sources']):
        raise ValueError('Imported source is outside its historical job.')
    from .transcription_state import read_definition
    from .audio.models import canonical
    definition = read_definition(Path(root) / 'transcription-jobs' / value['job'], cancelled=cancelled)
    if (sha256(canonical(definition).encode('utf-8')).hexdigest() != value['definition_sha256']
            or [(chosen[key]['sha256'], chosen[key]['size']) for key in value['sources']]
                != [(row['sha256'], row['bytes']) for row in definition['inputs']]):
        raise ValueError('Imported transcription definition no longer matches its retained sources.')
    return value


def import_as_new(service, reference, source_ids, runtime_manifest, settings, *,
                  title='Recovered transcription', expected_manifest_sha256=None,
                  cancelled=lambda: False):
    """Explicit new preparation, never a restored completion or automatic resume."""
    root = service.library_root
    entries, resolve = _archive_source(root, reference)
    row = next((row for row in entries if row['filename'] ==
        schema.PREFIX + 'jobs/' + reference['job'] + '/job.json'), None)
    if row is None:
        raise ValueError('The historical transcription job is absent.')
    job = schema.validate(row['filename'], _digest(resolve(row['filename']), row,
        cancelled=cancelled, collect=True))
    source_ids = tuple(source_ids)
    available = {row['id']: row for row in job['sources']}
    if (not source_ids or len(source_ids) > len(available)
            or any(type(key) is not str or key not in available for key in source_ids)
            or len(set(source_ids)) != len(source_ids)):
        raise ValueError('Choose explicit historical sources for the new job.')
    selected = [available[key] for key in source_ids]
    from .transcription_state import MAX_JOB_BYTES
    total = sum(row['size'] for row in selected)
    if total > MAX_JOB_BYTES:
        raise ValueError('Choose a source subset of at most 32 GiB; preserve the remaining historical sources for another job.')
    from .transcription_receipt import validate_settings
    options = validate_settings(settings)
    from .operations import ResourceBroker, ResourceRequest
    request = ResourceRequest(cpu_slots=1, memory_bytes=512 * 1024**2, disk_path=root,
        disk_bytes=2 * total + sum(sorted((row['size'] for row in selected), reverse=True)[:options['parallel_files']]) + 64 * 1024**2)
    broker = ResourceBroker.for_app(root, cancelled=cancelled)
    with broker.reserve(request, operation_id='document:' + uuid4().hex, cancelled=cancelled, timeout=60) as lease:
        def stopping():
            _stop(cancelled)
            lease.checkpoint()
            return False
        validate_payload(entries, resolve, cancelled=stopping)
        stage = Path(tempfile.mkdtemp(prefix='.legacy-transcription-', dir=root))
        result = None
        try:
            sources = []
            base = schema.PREFIX + 'jobs/' + job['id'] + '/'
            for row in selected:
                stopping()
                target = stage / (row['id'] + PurePosixPath(row['retained_path']).suffix)
                original = resolve(base + row['retained_path'])
                publish_new(original, target, cancelled=stopping)
                _digest(target, dict(size_bytes=row['size'], sha256=row['sha256']), cancelled=stopping)
                sources.append(target)
            identity = service.prepare(sources, runtime_manifest, options, title=title,
                expected_manifest_sha256=expected_manifest_sha256, cancelled=stopping)
            # Preparation has committed. Late Stop or an optional recovery-link
            # failure must not relabel the new immutable job as uncreated.
            result = dict(id=identity, definition=None, historical_source=True, warning=None)
            try:
                definition = service.definition(identity, verify_inputs=False)
                result['definition'] = definition
                from .audio.models import canonical
                link = dict(schema=LINK_SCHEMA, reference=dict(reference), sources=list(source_ids), job=identity,
                            definition_sha256=sha256(canonical(definition).encode('utf-8')).hexdigest())
                with exclusive_writer(root):
                    directory = root / DIRECTORY / 'links'
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    local_path(directory, exists=True)
                    path = directory / (identity + '.json')
                    if optional_present(path):
                        raise FileExistsError('The import receipt already exists.')
                    descriptor, filename = tempfile.mkstemp(prefix='.link-', suffix='.json', dir=directory)
                    link_stage = Path(filename)
                    try:
                        with os.fdopen(descriptor, 'wb') as stream:
                            stream.write(encode(link))
                            stream.flush()
                            os.fsync(stream.fileno())
                        publish_noreplace(link_stage, path)
                    finally:
                        link_stage.unlink(missing_ok=True)
                    _validate_link(root, path, cancelled=lambda: False)
            except Exception:
                result['warning'] = (
                    'The new job was prepared. Reopen it by its ID to review the definition; '
                    'the historical recovery link could not be confirmed. The original archive is retained.')
            return result
        finally:
            try:
                shutil.rmtree(stage)
            except Exception:
                if result is None:
                    raise
                result['warning'] = ((result['warning'] + ' ') if result['warning'] else '') + (
                    'The prepared job is retained; private temporary input cleanup needs attention.')
