"""Durable inert PCM queue inside the current managed Audio Studio project.

The current WavePlayer remains the only player. Queue reads and writes never
open a device, select historical devices, arm timers, or contact a service.
"""
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from time import monotonic

from .ffmpeg import wav_info
from .jobs import (check_cancelled, checked_directory, file_identity, project_lock,
                   publish_noreplace, regular_file, verified_reader, replace_json_file, _sync_directory)
from .managed import ManagedProjects
from .playback_processing import PlaybackControls
from ..reader.narration_text import decode_json
from ..backup_files import optional_present
from ..native_file_metadata import stream_change_time

NAME = 'playback-queue.json'
SCHEMA = 'carr-managed-playback-queue/1'
MAX_ITEMS, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 256, 4 * 1024**3, 32 * 1024**3
MAX_STATE_BYTES, MAX_SECONDS = 2 * 1024**2, 120
HEX = re.compile('[0-9a-f]{64}')


def _stamp(path):
    info = regular_file(path)
    if info.st_nlink != 1:
        raise ValueError('A retained queue file cannot be a hard link.')
    identity = file_identity(info)
    # Windows stat ctime is creation time in the supported runtime. Use the
    # same native ChangeTime authority as the payload/snapshot readers.
    with verified_reader(path, expected_identity=identity) as stream:
        native = stream_change_time(stream)
    return identity + (native,)


def _directory_identity(path):
    info = checked_directory(path).stat()
    return info.st_dev, info.st_ino


def _check_stage(path, parent_identity, stamp):
    if _directory_identity(path.parent) != parent_identity:
        raise ValueError('The owned playback stage directory changed.')
    if _stamp(path) != stamp:
        raise ValueError('The owned playback staging file changed.')
    if _directory_identity(path.parent) != parent_identity:
        raise ValueError('The owned playback stage directory changed.')


def _seal_stage(path, parent_identity, owner, size, expected_sha256, cancelled):
    """Check actual staged bytes once, then bind final checks to native identity."""
    check_cancelled(cancelled)
    if _directory_identity(path.parent) != parent_identity:
        raise ValueError('The owned playback stage directory changed.')
    stamp = _stamp(path)
    if stamp[:2] != owner or stamp[2] != size:
        raise ValueError('The owned playback staging file changed.')
    digest, remaining = sha256(), size
    with verified_reader(path, expected_identity=stamp[:5]) as stream:
        while remaining:
            check_cancelled(cancelled)
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError('The owned playback staging file was truncated.')
            remaining -= len(block)
            digest.update(block)
        if stream.read(1):
            raise ValueError('The owned playback staging file grew.')
        check_cancelled(cancelled)
    if digest.hexdigest() != expected_sha256:
        raise ValueError('The owned playback staging bytes changed.')
    _check_stage(path, parent_identity, stamp)
    return stamp


def _remove_owned_stage(path, parent_identity, owner):
    """Never unlink a replacement name or follow a retargeted parent on cleanup."""
    try:
        if _directory_identity(path.parent) != parent_identity:
            return
        info = regular_file(path)
        if info.st_nlink != 1 or (info.st_dev, info.st_ino) != owner:
            return
        if _directory_identity(path.parent) != parent_identity:
            return
    except (OSError, ValueError, RuntimeError):
        return
    path.unlink(missing_ok=True)


def _check(cancelled):
    deadline = monotonic() + MAX_SECONDS
    def check():
        check_cancelled(cancelled)
        if monotonic() >= deadline:
            raise TimeoutError('Queue review exceeded its two-minute allowance. Select fewer recordings.')
        return False
    return check


def _read(path):
    if not optional_present(path):
        return None, None
    stamp = _stamp(path)
    if stamp[2] > MAX_STATE_BYTES:
        raise ValueError('The saved playback queue exceeds its metadata allowance.')
    with verified_reader(path) as stream:
        data = stream.read(MAX_STATE_BYTES + 1)
    if len(data) > MAX_STATE_BYTES or _stamp(path) != stamp:
        raise ValueError('The saved playback queue changed during its bounded read.')
    return decode_json(data), sha256(data).hexdigest()


@dataclass(frozen=True)
class QueueItem:
    asset: str
    label: str
    sha256: str
    bytes: int
    frames: int
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class QueueState:
    project_id: str
    items: tuple = ()
    index: int | None = None
    frame: int | None = None
    accuracy: str = 'not_started'
    controls: PlaybackControls = PlaybackControls()

    def payload(self):
        return {'schema': SCHEMA, 'project_id': self.project_id,
                'items': [asdict(item) for item in self.items],
                'cursor': {'index': self.index, 'frame': self.frame, 'accuracy': self.accuracy},
                'controls': asdict(self.controls)}


@dataclass(frozen=True)
class QueueReview:
    owner: object
    state: QueueState
    digest: str | None
    pins: tuple


class QueueChangeCommittedError(OSError):
    """Exact published queue state survives a later durability/cleanup error."""
    def __init__(self, review):
        self.review = review
        self.durability_confirmed = False
        super().__init__('The playback queue checkpoint is present, but final durability '
                         'or cleanup was not confirmed. The published state is retained; '
                         'restore and review it before further playback changes.')


def parse_queue(value, project_id):
    if (type(value) is not dict or set(value) != {'schema', 'project_id', 'items', 'cursor', 'controls'}
            or value['schema'] != SCHEMA or value['project_id'] != project_id
            or not isinstance(project_id, str) or not re.fullmatch('[0-9a-f]{32}', project_id)):
        raise ValueError('The playback queue is invalid or belongs to another project.')
    rows = value['items']
    if type(rows) is not list or len(rows) > MAX_ITEMS:
        raise ValueError('A saved queue supports at most 256 recordings.')
    items, total = [], 0
    for row in rows:
        if (type(row) is not dict or set(row) != set(QueueItem.__dataclass_fields__)
                or type(row['sha256']) is not str or not HEX.fullmatch(row['sha256'])
                or row['asset'] != row['sha256'] + '.wav'
                or type(row['label']) is not str or not 1 <= len(row['label']) <= 256
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in row['label'])
                or type(row['bytes']) is not int or not 44 <= row['bytes'] <= MAX_FILE_BYTES
                or type(row['frames']) is not int or not 1 <= row['frames'] <= MAX_FILE_BYTES // 2
                or type(row['sample_rate']) is not int or not 1 <= row['sample_rate'] <= 384000
                or type(row['channels']) is not int or row['channels'] not in (1, 2)
                or row['frames'] * row['channels'] * 2 > row['bytes']):
            raise ValueError('Invalid retained PCM playback item.')
        total += row['bytes']
        items.append(QueueItem(**row))
    if total > MAX_TOTAL_BYTES:
        raise ValueError('The playback queue exceeds its 32 GiB review allowance.')
    cursor, controls = value['cursor'], value['controls']
    if (type(cursor) is not dict or set(cursor) != {'index', 'frame', 'accuracy'}
            or type(controls) is not dict or set(controls) != set(PlaybackControls.__dataclass_fields__)):
        raise ValueError('Invalid saved playback settings.')
    index, frame, accuracy = cursor['index'], cursor['frame'], cursor['accuracy']
    if index is not None and (type(index) is not int or not 0 <= index < len(items)):
        raise ValueError('The saved playback selection is outside the queue.')
    if accuracy not in ('not_started', 'driver_reported', 'completed_buffers', 'external_player', 'historical_cursor'):
        raise ValueError('Invalid saved playback position evidence.')
    if (frame is not None and (type(frame) is not int or index is None
            or not 0 <= frame <= items[index].frames)
            or frame is None and accuracy in ('driver_reported', 'completed_buffers', 'historical_cursor')
            or frame is not None and accuracy in ('not_started', 'external_player')
            or index is None and (frame is not None or accuracy != 'not_started')):
        raise ValueError('The saved cursor must retain its actual position evidence.')
    return QueueState(project_id, tuple(items), index, frame, accuracy, PlaybackControls(**controls))


def verify_items(audio, state, *, cancelled=lambda: False):
    """Used by the existing project/full backup validators and explicit review."""
    audio = checked_directory(audio)
    pins = []
    for item in state.items:
        check_cancelled(cancelled)
        path = audio / 'assets' / item.asset
        stamp = _stamp(path)
        if stamp[2] != item.bytes:
            raise ValueError('A retained playlist recording changed size.')
        digest = sha256()
        with verified_reader(path) as stream:
            info = wav_info(path)
            while block := stream.read(1024 * 1024):
                check_cancelled(cancelled)
                digest.update(block)
            check_cancelled(cancelled)
        if (digest.hexdigest() != item.sha256 or _stamp(path) != stamp
                or (info.frames, info.rate, info.channels) !=
                   (item.frames, item.sample_rate, item.channels)):
            raise ValueError('A retained playlist recording failed its PCM receipt.')
        pins.append((item.asset, stamp))
    return tuple(pins)


def inspect_queue(audio, project_id, *, cancelled=lambda: False):
    value, digest = _read(Path(audio) / NAME)
    if value is None:
        return None
    state = parse_queue(value, project_id)
    verify_items(audio, state, cancelled=cancelled)
    return state


class PlaylistStore:
    """Reuse managed draft/native audio locks; never replace an unknown queue."""
    def __init__(self, library_root, project_id, *, resource_broker=None):
        self.managed = ManagedProjects(library_root)
        # Session-only owner dependency for an explicitly staged import. It is
        # never read from queue/project JSON or restored historical paths.
        self._resource_broker = resource_broker
        self.project_id = project_id
        self.root, self.document, self.audio = self.managed.paths(project_id)
        self.path = self.audio / NAME
        self._root_identity = None

    def _bound(self):
        checked_directory(self.managed.library)
        identity = self.managed.library.stat()
        current = identity.st_dev, identity.st_ino
        if self._root_identity is None:
            self._root_identity = current
        elif current != self._root_identity:
            raise ValueError('The queue library changed. Reopen the saved project.')
        regular_file(self.document)

    def _review(self, cancelled):
        self._bound()
        value, digest = _read(self.path)
        state = QueueState(self.project_id) if value is None else parse_queue(value, self.project_id)
        pins = verify_items(self.audio, state, cancelled=cancelled)
        return QueueReview(self, state, digest, pins)

    def review(self, *, cancelled=lambda: False):
        check = _check(cancelled)
        with self.managed.writer(self.project_id), project_lock(self.audio):
            return self._review(check)

    def _validate_review(self, review):
        self._bound()
        if type(review) is not QueueReview or review.owner is not self:
            raise ValueError('Review the saved queue in this exact project first.')
        state = parse_queue(review.state.payload(), self.project_id)
        if tuple((item.asset, _stamp(self.audio / 'assets' / item.asset)) for item in state.items) != review.pins:
            raise ValueError('A reviewed playlist recording changed. Restore and review the queue again.')
        if _read(self.path)[1] != review.digest:
            raise ValueError('The saved queue changed in another window. Restore its current version first.')

    def _write(self, review, state, cancelled, *, expected_pins=None):
        self._validate_review(review)
        parse_queue(state.payload(), self.project_id)
        data = (json.dumps(state.payload(), ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        if len(data) > MAX_STATE_BYTES:
            raise ValueError('Queue metadata exceeds its bounded save allowance.')
        parent_identity = _directory_identity(self.audio)
        descriptor, name = tempfile.mkstemp(prefix='.queue-', dir=self.audio)
        stage = Path(name)
        created = os.fstat(descriptor)
        owner = created.st_dev, created.st_ino
        published = None
        staged_identity = None
        candidate = None
        try:
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                checksum = sha256(data).hexdigest()
                staged_identity = _seal_stage(stage, parent_identity, owner,
                                              len(data), checksum, cancelled)
                self._validate_review(review)
                pins = (tuple((item.asset, _stamp(self.audio / 'assets' / item.asset))
                              for item in state.items)
                        if expected_pins is None else expected_pins)
                candidate = QueueReview(self, state, checksum, pins)
                def before_commit():
                    check_cancelled(cancelled)
                    self._validate_review(review)
                    _check_stage(stage, parent_identity, staged_identity)
                    if tuple((item.asset, _stamp(self.audio / 'assets' / item.asset))
                             for item in candidate.state.items) != candidate.pins:
                        raise ValueError('A retained playlist recording changed before queue publication.')
                    return False
                if review.digest is None:
                    publish_noreplace(stage, self.path, cancelled=before_commit)
                else:
                    before_commit()
                    replace_json_file(stage, self.path)
                    published = candidate
                    _sync_directory(self.audio)
                published = candidate
            finally:
                _remove_owned_stage(stage, parent_identity, owner)
        except Exception as error:
            if published is None and staged_identity is not None and candidate is not None:
                try:
                    # Confirm this attempt's own inode, not merely equal bytes.
                    # A changed parent or equal-byte create-only competitor is
                    # not this attempt's committed queue.
                    if _directory_identity(self.audio) != parent_identity:
                        raise ValueError('The owned playback stage directory changed.')
                    value, checksum = _read(self.path)
                    if (_stamp(self.path)[:2] == owner
                            and checksum == sha256(data).hexdigest()
                            and parse_queue(value, self.project_id) == state):
                        published = candidate
                except (OSError, ValueError, RuntimeError):
                    pass
            if published is not None:
                raise QueueChangeCommittedError(published) from error
            raise
        return published

    def checkpoint(self, review, *, index=None, frame=None, accuracy='not_started',
                   controls=None, clear=False, cancelled=lambda: False):
        published = None
        try:
            with self.managed.writer(self.project_id), project_lock(self.audio):
                self._validate_review(review)
                if type(clear) is not bool:
                    raise ValueError('Queue reset must be explicit.')
                state = replace(review.state, items=() if clear else review.state.items,
                                index=index, frame=frame, accuracy=accuracy,
                                controls=review.state.controls if controls is None else controls)
                published = self._write(review, state, cancelled)
        except QueueChangeCommittedError:
            raise
        except Exception as error:
            if published is not None:
                raise QueueChangeCommittedError(published) from error
            raise
        return published

    def append(self, paths, *, review=None, labels=None, expected_sha256s=None,
               cancelled=lambda: False):
        if type(paths) not in (tuple, list) or not 1 <= len(paths) <= MAX_ITEMS:
            raise ValueError('Select from one to 256 local PCM recordings.')
        if labels is not None and (type(labels) not in (tuple, list)
                or len(labels) != len(paths) or any(type(label) is not str
                    or not 1 <= len(label) <= 256
                    or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in label)
                    for label in labels)):
            raise ValueError('Provide one bounded plain-text label for each recording.')
        labels = None if labels is None else tuple(labels)
        if expected_sha256s is not None and (type(expected_sha256s) not in (tuple, list)
                or len(expected_sha256s) != len(paths)
                or any(type(value) is not str or not HEX.fullmatch(value) for value in expected_sha256s)):
            raise ValueError('Provide one exact reviewed SHA-256 for each recording.')
        expected_sha256s = None if expected_sha256s is None else tuple(expected_sha256s)
        paths = tuple(paths)
        check = _check(cancelled)
        from ..operations import ResourceBroker, ResourceRequest
        published = None
        try:
            with self.managed.writer(self.project_id), project_lock(self.audio):
                current = self._review(check) if review is None else review
                self._validate_review(current)
                if len(current.state.items) + len(paths) > MAX_ITEMS:
                    raise ValueError('The playback queue supports at most 256 recordings.')
                checked_directory(self.audio / 'assets', create=True)
                sources = tuple(Path(path).absolute() for path in paths)
                sizes = [_stamp(path)[2] for path in sources]
                if any(not 44 <= size <= MAX_FILE_BYTES for size in sizes):
                    raise ValueError('A queue recording must be nonempty PCM and at most 4 GiB.')
                if sum(sizes) + sum(item.bytes for item in current.state.items) > MAX_TOTAL_BYTES:
                    raise ValueError('The queue exceeds its 32 GiB review allowance.')
                broker = (self._resource_broker if self._resource_broker is not None else
                          ResourceBroker.for_app(self.managed.library, cancelled=check))
                request = ResourceRequest(cpu_slots=1, memory_bytes=8 * 1024**2,
                                          disk_bytes=sum(sizes), disk_path=self.audio)
                items = list(current.state.items)
                with broker.reserve(request, operation_id='playback-queue:' + self.project_id,
                                    cancelled=check, timeout=30, run_timeout=MAX_SECONDS):
                    for source_index, (source, size) in enumerate(zip(sources, sizes)):
                        check()
                        identity = _stamp(source)
                        parent_identity = _directory_identity(self.audio / 'assets')
                        descriptor, name = tempfile.mkstemp(prefix='.queue-input-', dir=self.audio / 'assets')
                        stage = Path(name)
                        created = os.fstat(descriptor)
                        owner = created.st_dev, created.st_ino
                        try:
                            digest = sha256()
                            count = 0
                            with os.fdopen(descriptor, 'wb') as target:
                                with verified_reader(source) as stream:
                                    info = wav_info(source)
                                    while block := stream.read(1024 * 1024):
                                        check()
                                        count += len(block)
                                        if count > size:
                                            raise ValueError('A queue input grew during its verified copy.')
                                        target.write(block)
                                        digest.update(block)
                                if _stamp(source) != identity or count != size or info.frames < 1:
                                    raise ValueError('A queue input changed or has no complete PCM frames.')
                                target.flush()
                                os.fsync(target.fileno())
                            check()
                            fingerprint = digest.hexdigest()
                            staged_identity = _seal_stage(stage, parent_identity, owner,
                                                          size, fingerprint, check)
                            if expected_sha256s is not None and fingerprint != expected_sha256s[source_index]:
                                raise ValueError('A selected recording differs from its reviewed SHA-256.')
                            asset = fingerprint + '.wav'
                            label = source.name[:240] if labels is None else labels[source_index]
                            item = QueueItem(asset, label, fingerprint, size, info.frames, info.rate, info.channels)
                            parse_queue(replace(current.state, items=(item,), index=None, frame=None,
                                                accuracy='not_started').payload(), self.project_id)
                            destination = self.audio / 'assets' / asset
                            def before_asset_commit():
                                check()
                                _check_stage(stage, parent_identity, staged_identity)
                                return False
                            before_asset_commit()
                            if optional_present(destination):
                                verify_items(self.audio, replace(current.state, items=(item,)), cancelled=check)
                                # A previous move may have succeeded before its
                                # directory flush failed. Confirm the retained
                                # entry now before a queue can reference it.
                                _sync_directory(destination.parent)
                            else:
                                publish_noreplace(stage, destination, cancelled=before_asset_commit)
                            items.append(item)
                        finally:
                            _remove_owned_stage(stage, parent_identity, owner)
                    check()
                    state = replace(current.state, items=tuple(items))
                    # An earlier new asset can change while a later input is
                    # copied. Verify those actual bytes once at append admission,
                    # then preserve the resulting native pins through the final
                    # metadata callback. Ordinary cursor saves do not rehash PCM.
                    added = replace(state, items=state.items[len(current.state.items):],
                                    index=None, frame=None, accuracy='not_started')
                    pins = current.pins + verify_items(self.audio, added, cancelled=check)
                    published = self._write(current, state, check, expected_pins=pins)
        except QueueChangeCommittedError:
            raise
        except Exception as error:
            if published is not None:
                raise QueueChangeCommittedError(published) from error
            raise
        return published

    def paths(self, review):
        self._validate_review(review)
        return tuple(self.audio / 'assets' / item.asset for item in review.state.items)
