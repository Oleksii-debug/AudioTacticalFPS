"""Explicit import-as-new from passive canonical queues into the current owner.

The existing PlaylistStore retains the recordings and the existing WavePlayer
remains the player. No archived output device, timer, executor or path setting
is activated. Publication moves one complete private managed project.
"""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from time import monotonic
from uuid import uuid4

from filelock import FileLock, Timeout

from ..backup_files import optional_present
from ..ingest.writer_guard import exclusive_writer
from ..reader.narration_text import NarrationDraft
from .jobs import (checked_directory, file_identity, publish_noreplace, regular_file,
                   _sync_directory)
from .legacy_playlist_state import (
    NAME, MAX_ITEMS, MAX_STATE_BYTES, MAX_PROVENANCE_BYTES, PROVENANCE_NAME,
    decode_queue, read_bytes, provenance, verify_pcm, native_identity)

MAX_SECONDS = 120
MAX_ARCHIVES = 1000
MAX_LIST_BYTES = 64 * 1024**2
_HASH = re.compile(r'[0-9a-f]{64}\Z')


@dataclass(frozen=True)
class QueueSource:
    kind: str
    identity: str
    label: str


@dataclass(frozen=True)
class QueueImportReview:
    library: Path
    library_identity: tuple
    source_root: Path
    source_identity: tuple
    queue_path: Path
    queue_identity: tuple
    raw: bytes
    assets: tuple
    origin: dict
    manifest_path: Path | None
    manifest_identity: tuple | None
    voice: str
    language: str
    title: str

    @property
    def sha256(self):
        return sha256(self.raw).hexdigest()

    def description(self):
        queue = decode_queue(self.raw)
        amount = sum(item['size_bytes'] for item in queue['items'])
        selected = 'none' if queue['index'] is None else str(queue['index'] + 1)
        return (f"Import {len(queue['items'])} recordings in their saved order ({amount:,} bytes) "
                f"as a new library audio project.\nSaved selection: {selected}; "
                f"historical PCM frame: {queue['frame']}.\n"
                "The exact old queue, labels, controls and device declaration are retained as provenance. "
                "Current controls use their defaults. Select a current output and choose Play or Resume "
                "separately; this action starts no playback.")


@dataclass(frozen=True)
class QueueImportResult:
    project_id: str
    project_path: Path
    queue_sha256: str
    items: int
    durability_confirmed: bool = True
    warning: str = ''


class QueueImportCommittedError(OSError):
    """A new project exists; reopening its receipt must precede any retry."""
    def __init__(self, result):
        super().__init__('The imported queue was published; a later cleanup or durability step failed. '
                         'Reopen the existing project instead of importing it again.')
        self.result = result


def _identity(path):
    info = checked_directory(path).stat()
    return info.st_dev, info.st_ino


def _check(cancelled):
    deadline = monotonic() + MAX_SECONDS
    def stopping():
        if cancelled():
            raise InterruptedError('Historical queue import cancelled.')
        if monotonic() >= deadline:
            raise TimeoutError('Queue import exceeded two minutes. Choose a smaller saved queue.')
        return False
    return stopping


def _settings(value, maximum):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 or 127 <= ord(c) < 160 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('Choose bounded current project settings.')
    return value


def sources(library_root, *, cancelled=lambda: False):
    """List only metadata from the current root and typed inert archives."""
    from ..backup_historical import DIRECTORY, _manifest, root_entries
    root = checked_directory(library_root)
    result, used = [], 0
    if optional_present(root / NAME):
        raw = read_bytes(root / NAME, MAX_STATE_BYTES, cancelled=cancelled)
        decode_queue(raw)
        result.append(QueueSource('current-root', '', 'Saved canonical queue in this library'))
    base = root / DIRECTORY
    if not optional_present(base):
        return tuple(result)
    checked_directory(base)
    selected = []
    with os.scandir(base) as entries:
        for entry in entries:
            if len(selected) >= MAX_ARCHIVES or not _HASH.fullmatch(entry.name):
                raise ValueError('Historical archive listing exceeds its typed namespace.')
            selected.append(Path(entry.path))
    for archive in sorted(selected):
        if cancelled():
            raise InterruptedError('Historical queue listing cancelled.')
        checked_directory(archive)
        path = archive / 'manifest.json'
        used += regular_file(path).st_size
        if used > MAX_LIST_BYTES:
            raise ValueError('Historical queue listing exceeds its metadata allowance.')
        _, manifest = _manifest(path, archive.name)
        if any(row['filename'] == NAME for row in root_entries(manifest)):
            result.append(QueueSource('archive', archive.name,
                                      'Restored queue — archive ' + archive.name[:12]))
    return tuple(result)


@contextmanager
def _source_lock(source, *, live):
    if not live:
        yield
        return
    parent = checked_directory(Path(source) / 'state' / 'speech')
    path = parent / '.playback-queue.lock'
    if optional_present(path) and regular_file(path).st_nlink != 1:
        raise ValueError('The historical queue lock is an alias.')
    try:
        with FileLock(path, timeout=0, mode=0o600):
            if regular_file(path).st_nlink != 1:
                raise ValueError('The historical queue lock changed.')
            yield
    except Timeout as error:
        raise RuntimeError('The canonical queue is being saved. Retry after that operation.') from error


def _review(library_root, source_root, *, manifest_id=None, voice, language,
            title, cancelled):
    from ..backup_historical import DIRECTORY, _file_key, _manifest, source_inventory
    from ..operations import ResourceBroker, ResourceRequest
    from .ffmpeg import wav_info
    library = checked_directory(library_root)
    _settings(voice, 2048)
    _settings(language, 80)
    _settings(title, 256)
    check = _check(cancelled)
    broker = ResourceBroker.for_app(library, cancelled=check)
    with broker.reserve(ResourceRequest(cpu_slots=1, memory_bytes=128 * 1024**2,
                                       disk_bytes=0),
                        operation_id='legacy-queue-review', cancelled=check,
                        timeout=30, run_timeout=MAX_SECONDS):
        if manifest_id is None:
            source = checked_directory(source_root)
            origin = dict(kind='selected-root', original_root=str(source), queue_member=NAME)
            manifest_path = manifest_identity = None
            queue_path = source / NAME
            resolve = lambda name: source / name
            inventory = None
        else:
            if type(manifest_id) is not str or not _HASH.fullmatch(manifest_id):
                raise ValueError('Choose one exact historical archive.')
            source = checked_directory(library / DIRECTORY / manifest_id)
            manifest_path = source / 'manifest.json'
            manifest_identity = file_identity(regular_file(manifest_path))
            _, manifest = _manifest(manifest_path, manifest_id)
            subdir, entries, _ = source_inventory(manifest)
            inventory = {('state/' if subdir else '') + row['filename']: row for row in entries}
            def resolve(name):
                original = name.removeprefix('state/') if subdir else name
                return source / 'files' / _file_key(original)
            if NAME not in inventory:
                raise ValueError('This archive has no saved canonical queue.')
            queue_path = resolve(NAME)
            origin = dict(kind='historical-backup', manifest_sha256=manifest_id, queue_member=NAME)
        with _source_lock(source, live=manifest_id is None):
            raw = read_bytes(queue_path, MAX_STATE_BYTES, cancelled=check)
            queue = decode_queue(raw)
            if inventory is not None:
                metadata = inventory[NAME]
                if (metadata['size_bytes'], metadata['sha256']) != (len(raw), sha256(raw).hexdigest()):
                    raise ValueError('The historical queue differs from its archive manifest.')
            assets = []
            for item in queue['items']:
                check()
                if inventory is not None:
                    entry = inventory.get(item['filename'])
                    if entry is None or (entry['size_bytes'], entry['sha256']) != (
                            item['size_bytes'], item['sha256']):
                        raise ValueError('A historical queue recording has no matching archive member.')
                if item['sample_width'] != 2 or item['sample_rate'] > 384000:
                    raise ValueError('This current player imports PCM16 up to384000 Hz. '
                                     'The original queue remains preserved; use the explicit media converter '
                                     'for other PCM widths or rates before making a new current queue.')
                path = resolve(item['filename'])
                identity = verify_pcm(path, item, cancelled=check)
                info = wav_info(path)
                if (info.frames, info.rate, info.channels) != (
                        item['frames'], item['sample_rate'], item['channels']):
                    raise ValueError('The historical recording differs from current PCM validation.')
                assets.append((item['filename'], path, identity))
            check()
            result = QueueImportReview(library, _identity(library), source, _identity(source),
                queue_path, file_identity(regular_file(queue_path)), raw, tuple(assets), origin,
                manifest_path, manifest_identity, voice, language, title)
            _guard(result, check)
            return result


def review_root(library_root, selected_root, *, voice='sapi5', language='en-US',
                title='Imported playback queue', cancelled=lambda: False):
    return _review(library_root, selected_root, voice=voice, language=language,
                   title=title, cancelled=cancelled)


def review_archive(library_root, manifest_sha256, *, voice='sapi5', language='en-US',
                   title='Imported playback queue', cancelled=lambda: False):
    return _review(library_root, None, manifest_id=manifest_sha256, voice=voice,
                   language=language, title=title, cancelled=cancelled)


def _guard(review, cancelled):
    cancelled()
    if type(review) is not QueueImportReview:
        raise TypeError('Review a canonical queue before importing it.')
    if (_identity(review.library) != review.library_identity
            or _identity(review.source_root) != review.source_identity
            or file_identity(regular_file(review.queue_path)) != review.queue_identity
            or read_bytes(review.queue_path, MAX_STATE_BYTES, cancelled=cancelled) != review.raw):
        raise ValueError('The reviewed library or saved queue changed. Review it again.')
    queue = decode_queue(review.raw)
    _settings(review.voice, 2048)
    _settings(review.language, 80)
    _settings(review.title, 256)
    if review.manifest_path is None:
        expected_origin = dict(kind='selected-root', original_root=str(review.source_root), queue_member=NAME)
        resolve = lambda name: review.source_root / name
    else:
        from ..backup_historical import DIRECTORY, _file_key, _manifest, source_inventory
        manifest_id = review.origin.get('manifest_sha256') if type(review.origin) is dict else None
        if (type(manifest_id) is not str or not _HASH.fullmatch(manifest_id)
                or review.source_root != review.library / DIRECTORY / manifest_id
                or review.manifest_path != review.source_root / 'manifest.json'):
            raise ValueError('The historical queue origin differs from the selected archive.')
        _, manifest = _manifest(review.manifest_path, manifest_id)
        subdir, entries, _ = source_inventory(manifest)
        members = {('state/' if subdir else '') + row['filename']: row for row in entries}
        if NAME not in members:
            raise ValueError('The historical archive no longer declares its queue.')
        def resolve(name):
            original = name.removeprefix('state/') if subdir else name
            return review.source_root / 'files' / _file_key(original)
        expected_origin = dict(kind='historical-backup', manifest_sha256=manifest_id, queue_member=NAME)
    if review.origin != expected_origin or review.queue_path != resolve(NAME):
        raise ValueError('The reviewed queue provenance or member path changed.')
    if len(review.assets) != len(queue['items']):
        raise ValueError('The reviewed queue inventory changed.')
    for (name, path, identity), item in zip(review.assets, queue['items']):
        if (name != item['filename'] or path != resolve(name)
                or native_identity(path) != identity):
            raise ValueError('A reviewed historical queue recording changed.')
    if review.manifest_path is not None:
        if (file_identity(regular_file(review.manifest_path)) != review.manifest_identity
                or sha256(read_bytes(review.manifest_path, 8 * 1024**2,
                                     cancelled=cancelled)).hexdigest() != review.origin['manifest_sha256']):
            raise ValueError('The reviewed historical archive changed.')
    provenance(review.raw, review.origin, '0' * 32)


def _write(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > MAX_PROVENANCE_BYTES:
        raise ValueError('Imported queue metadata exceeds its bounded representation.')
    with Path(path).open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return sha256(raw).hexdigest()


def import_reviewed(review, *, cancelled=lambda: False):
    from .managed import ManagedProjects
    from .playlist import PlaylistStore
    from .playback_processing import PlaybackControls
    from ..operations import ResourceBroker, ResourceRequest
    if type(review) is not QueueImportReview:
        raise TypeError('Review a canonical queue before importing it.')
    check = _check(cancelled)
    _guard(review, check)
    queue = decode_queue(review.raw)
    project_id = uuid4().hex
    final, document, audio = ManagedProjects(review.library).paths(project_id)
    broker = ResourceBroker.for_app(review.library, cancelled=check)
    stage = Path(tempfile.mkdtemp(prefix='.legacy-playlist-', dir=review.library.parent))
    stage_identity = _identity(stage)
    published = None
    try:
        with _source_lock(review.source_root, live=review.manifest_path is None):
            _guard(review, check)
            managed = ManagedProjects(stage)
            staged_root, staged_document, staged_audio = managed.paths(project_id)
            with managed.writer(project_id):
                draft = NarrationDraft('', source_id='canonical-playback-queue:' + review.sha256)
                payload = dict(kind='research-audio-studio', version=2, mode='LOCAL ONLY',
                    project_id=project_id, title=review.title, voice=review.voice,
                    language=review.language, input_kind='narration', output_dir=str(audio),
                    source_reference=None, draft=draft.to_dict())
                document_sha = _write(staged_document, payload)
                provenance_sha = _write(staged_audio / PROVENANCE_NAME,
                       provenance(review.raw, review.origin, project_id))
                _sync_directory(staged_document.parent)
                _sync_directory(staged_audio)
            store = PlaylistStore(stage, project_id, resource_broker=broker)
            saved = store.review(cancelled=check)
            if queue['items']:
                saved = store.append([row[1] for row in review.assets], review=saved,
                    labels=[row['label'] for row in queue['items']],
                    expected_sha256s=[row['sha256'] for row in queue['items']], cancelled=check)
            saved = store.checkpoint(saved, index=queue['index'],
                frame=queue['frame'] if queue['index'] is not None else None,
                accuracy='historical_cursor' if queue['index'] is not None else 'not_started',
                controls=PlaybackControls(), cancelled=check)
            with broker.reserve(ResourceRequest(cpu_slots=1, memory_bytes=8 * 1024**2,
                                               disk_bytes=0),
                                operation_id='legacy-queue-verify:' + project_id,
                                cancelled=check, timeout=30, run_timeout=MAX_SECONDS):
                frozen = _freeze_stage(staged_root, queue,
                    {'studio/project.json': document_sha,
                     'audio/' + PROVENANCE_NAME: provenance_sha,
                     'audio/playback-queue.json': saved.digest}, check)
            _sync_directory(staged_root)
            own_project = _identity(staged_root)
            with exclusive_writer(review.library, timeout=0):
                _guard(review, check)
                checked_directory(final.parent, create=True)
                def final_guard():
                    # External cancellation may run arbitrary observer code.
                    # Admit it once, before ALL final pins. The finite native
                    # checks below must not expose a later mutation callback.
                    check()
                    quiet = lambda: False
                    _guard(review, quiet)
                    if (_identity(stage) != stage_identity
                            or _identity(staged_root) != own_project):
                        raise ValueError('The private queue import stage changed.')
                    _assert_stage(staged_root, frozen, quiet)
                    return False
                try:
                    publish_noreplace(staged_root, final, cancelled=final_guard)
                except BaseException:
                    if not optional_present(staged_root) and optional_present(final) and _identity(final) == own_project:
                        published = QueueImportResult(project_id, document, review.sha256,
                            len(queue['items']), False,
                            'The project was published but its final directory durability was not acknowledged.')
                    raise
                published = QueueImportResult(project_id, document, review.sha256, len(queue['items']))
    except BaseException as error:
        if published is not None:
            raise QueueImportCommittedError(published) from error
        raise
    finally:
        try:
            if optional_present(stage) and _identity(stage) == stage_identity:
                shutil.rmtree(stage)
        except BaseException as error:
            if published is not None:
                raise QueueImportCommittedError(published) from error
            raise
    return published


def _namespace(root, check):
    """Admit only this operation's finite complete managed-project tree."""
    root = checked_directory(root)
    directories, files, pending = {}, {}, [root]
    while pending:
        directory = pending.pop()
        check()
        relative = directory.relative_to(root).as_posix()
        if relative not in ('.', 'studio', 'audio', 'audio/assets'):
            raise ValueError('The private queue stage contains an unexpected directory.')
        directories[relative] = _identity(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                check()
                path = Path(entry.path)
                name = path.relative_to(root).as_posix()
                if entry.is_dir(follow_symlinks=False):
                    if name not in ('studio', 'audio', 'audio/assets') or len(pending) + len(directories) >= 4:
                        raise ValueError('The private queue stage exceeds its directory inventory.')
                    checked_directory(path)
                    pending.append(path)
                else:
                    if len(files) >= MAX_ITEMS + 5:
                        raise ValueError('The private queue stage exceeds its file inventory.')
                    info = regular_file(path)
                    if info.st_nlink != 1:
                        raise ValueError('The private queue stage contains an aliased file.')
                    files[name] = path
    return directories, files


def _freeze_stage(root, queue, metadata, check):
    directories, files = _namespace(root, check)
    records = {'audio/assets/' + row['sha256'] + '.wav': row for row in queue['items']}
    optional_locks = {'.draft.lock', 'audio/.render.lock'} & set(files)
    if set(files) != set(metadata) | set(records) | optional_locks:
        raise ValueError('The private queue stage contains missing or unexpected payloads.')
    pins = {}
    for name, expected in metadata.items():
        check()
        pin = native_identity(files[name])
        raw = read_bytes(files[name], MAX_PROVENANCE_BYTES, cancelled=check)
        if sha256(raw).hexdigest() != expected or native_identity(files[name]) != pin:
            raise ValueError('A staged queue document differs from its committed bytes.')
        pins[name] = pin
    for name, row in records.items():
        pins[name] = verify_pcm(files[name], row, cancelled=check)
    for name in optional_locks:
        check()
        if regular_file(files[name]).st_size > 1:
            raise ValueError('The private queue stage contains an invalid native lock.')
        pins[name] = native_identity(files[name])
    frozen = directories, pins
    _assert_stage(root, frozen, check)
    return frozen


def _assert_stage(root, frozen, check):
    expected_directories, expected_files = frozen
    directories, files = _namespace(root, check)
    if directories != expected_directories or set(files) != set(expected_files):
        raise ValueError('The private queue stage changed after content verification.')
    for name, path in files.items():
        check()
        if native_identity(path) != expected_files[name]:
            raise ValueError('A private queue payload changed after content verification.')
