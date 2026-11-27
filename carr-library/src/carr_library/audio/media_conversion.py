"""Prepared local-media batches, independent originals and verified results.

The queue is data in the current library. Opening/restoring it never launches
FFmpeg; only an explicit run uses the current admitted processor and resources.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from ..backup_files import local_path, optional_present, regular
from ..ingest.writer_guard import exclusive_writer
from ..operations.broker import ResourceBroker
from ..operations.models import ResourceCancelled, ResourceRequest, ResourceStateError
from .capture_types import AudioBudget, AudioCancelled
from .jobs import (RenderCancelled, atomic_json, checked_directory, file_identity, project_lock,
                   publish_new, publish_noreplace, read_json, verified_reader, _sync_directory)
from . import media_worker
from .media_types import (ConversionAllowance, ConversionPreset, DIRECTORY, MAX_ITEMS, MAX_JOBS,
    MAX_METADATA_BYTES, MAX_RECEIPT_BYTES, MediaChoice, digest, identity, metadata_reserve, output_name, text, validate_control,
    validate_definition, validate_job, validate_presets, validate_receipt)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _notify(callback, done, total, state):
    if callback is not None:
        try:
            callback(done, total, state)
        except Exception:
            # Progress is observation; it cannot replace the actual outcome.
            pass


def read_definition(job):
    job = local_path(job, exists=True)
    regular(job, directory=True)
    return validate_definition(read_json(job / 'definition.json', limit=MAX_METADATA_BYTES), job_id=job.name)


def _hash(path, expected, cancelled):
    info = regular(path)
    if info.st_size != expected['bytes']:
        raise ValueError('A retained media file has changed size.')
    with verified_reader(path, expected_identity=file_identity(info)) as stream:
        if media_worker.stream_hash(stream, cancelled) != expected['sha256']:
            raise ValueError('Retained media bytes differ from their verified checksum.')
    return info


def verified_results(job, definition, *, cancelled=lambda: False, verify_inputs=True):
    job = local_path(job, exists=True)
    results = job / 'results'
    expected = {f"{row['index']:06d}" for row in definition['inputs']}
    names = set()
    if optional_present(results):
        regular(results, directory=True)
        with os.scandir(results) as entries:
            for count, entry in enumerate(entries, 1):
                media_worker.check(cancelled)
                if count > len(expected) or entry.name not in expected:
                    raise ValueError('A media result directory contains an unregistered item.')
                regular(Path(entry.path), directory=True)
                names.add(entry.name)
    answer, missing, used = [], False, metadata_reserve(definition)
    for row in definition['inputs']:
        media_worker.check(cancelled)
        if verify_inputs:
            _hash(job / row['retained'], row, cancelled)
        used += row['bytes']
        directory = results / f"{row['index']:06d}"
        if directory.name not in names:
            missing = True
            continue
        if missing:
            raise ValueError('A media batch has an unacknowledged gap in its ordered results.')
        expected_files = {'receipt.json', output_name(row)}
        with os.scandir(directory) as entries:
            found = set()
            for count, entry in enumerate(entries, 1):
                if count > 2 or entry.name not in expected_files:
                    raise ValueError('A completed media item contains an unregistered file.')
                regular(Path(entry.path))
                found.add(entry.name)
        if found != expected_files:
            raise ValueError('A completed media item is missing its audio or receipt.')
        receipt = validate_receipt(read_json(directory / 'receipt.json', limit=MAX_RECEIPT_BYTES), definition, row)
        _hash(directory / output_name(row), receipt['output'], cancelled)
        used += receipt['output']['bytes']
        if used >= definition['allowance']['disk_bytes']:
            raise ValueError('Saved media exceeds its reviewed job disk allowance.')
        answer.append(receipt)
    return tuple(answer)


class MediaConversionService:
    def __init__(self, library_root, *, ffmpeg=None, broker=None):
        self.library_root = local_path(library_root, exists=True)
        info = regular(self.library_root, directory=True)
        self._library_identity = (info.st_dev, info.st_ino)
        self.root = self.library_root / DIRECTORY
        self.ffmpeg = ffmpeg
        self._broker = broker

    def _current(self):
        info = regular(local_path(self.library_root, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != self._library_identity:
            raise ValueError('The selected media library has changed; reopen the tool.')

    def _ensure(self):
        self._current()
        if not optional_present(self.root):
            self.root.mkdir(mode=0o700)
            _sync_directory(self.library_root)
        regular(local_path(self.root, root=self.library_root, exists=True), directory=True)

    def _job(self, job_id):
        self._current()
        return local_path(self.root / identity(job_id), root=self.library_root, exists=True)

    @contextmanager
    def _admission(self, operation_id, *, cancelled, cpu=1, disk=0, disk_path=None):
        self._current()
        broker = self._broker or ResourceBroker.for_app(self.library_root, cancelled=cancelled)
        if (broker.root != self.library_root / 'runtime/resources'
                or broker.policy_path != self.library_root / 'state/operations/resources.json'):
            raise ResourceStateError('The resource coordinator belongs to a different library.')
        self._broker = broker
        limits = broker.limits
        request = ResourceRequest(cpu_slots=cpu, memory_bytes=limits.memory_bytes,
                                  disk_bytes=disk, disk_path=disk_path)
        try:
            with broker.reserve(request, operation_id=operation_id, cancelled=cancelled, timeout=60) as lease:
                self._current()
                yield lease
        except ResourceCancelled as error:
            raise InterruptedError('Media processing stopped during resource admission.') from error

    def inspect(self, paths, *, cancelled=lambda: False, progress=None):
        if type(paths) not in (list, tuple) or not 1 <= len(paths) <= MAX_ITEMS:
            raise ValueError('Choose between one and 1000 explicit local media files.')
        result = []
        with self._admission('media:inspect:' + uuid4().hex, cancelled=cancelled) as lease:
            def check():
                self._current()
                lease.checkpoint()
                return cancelled()
            for number, path in enumerate(paths, 1):
                media_worker.check(check)
                result.append(media_worker.inspect_file(path, self.ffmpeg, cancelled=check))
                _notify(progress, number, len(paths), 'Inspected local audio streams')
        return tuple(result)

    def prepare(self, choices, *, allowance=None, title='Media conversion', cancelled=lambda: False,
                progress=None):
        if (type(choices) not in (tuple, list) or not 1 <= len(choices) <= MAX_ITEMS
                or any(type(row) is not MediaChoice for row in choices)):
            raise ValueError('Inspect and explicitly select a stream for every media input.')
        allowance = allowance or ConversionAllowance()
        if type(allowance) is not ConversionAllowance:
            raise ValueError('Review a typed media resource allowance.')
        text(title, 'Media job title')
        total = sum(choice.source.bytes for choice in choices)
        if total >= allowance.disk_bytes:
            raise ValueError('Increase the job allowance to retain inputs and create verified outputs.')
        if len({row.source.processor for row in choices}) != 1:
            raise ValueError('Inspect all selected media with the same installed processor.')
        job_id = uuid4().hex
        with exclusive_writer(self.library_root):
            self._ensure()
            with project_lock(self.root, lock_name='.state.lock'):
                if len(self.jobs()) >= MAX_JOBS:
                    raise ValueError('The saved media job count is full.')
                with self._admission('media:' + job_id, cancelled=cancelled, disk=total,
                                     disk_path=self.root) as lease:
                    def check():
                        self._current()
                        lease.checkpoint()
                        return cancelled()
                    stage = Path(tempfile.mkdtemp(prefix='.prepare-', dir=self.root))
                    try:
                        (stage / 'inputs').mkdir()
                        (stage / 'results').mkdir()
                        rows = []
                        for number, choice in enumerate(choices, 1):
                            media_worker.check(check)
                            selected = choice.source
                            target = stage / f'inputs/{number:06d}.media'
                            publish_new(selected.path, target, expected_identity=selected.file_identity, cancelled=check)
                            actual = media_worker.inspect_file(target, self.ffmpeg, cancelled=check)
                            if (actual.sha256 != selected.sha256 or actual.bytes != selected.bytes
                                    or actual.container != selected.container or actual.has_video != selected.has_video
                                    or actual.streams != selected.streams or actual.processor != selected.processor):
                                raise ValueError('The copied media differs from the exact reviewed inspection.')
                            rows.append(dict(index=number, name=Path(selected.path).name, origin=selected.path,
                                retained=f'inputs/{number:06d}.media', bytes=actual.bytes, sha256=actual.sha256,
                                container=actual.container, has_video=actual.has_video,
                                streams=[stream.to_dict() for stream in actual.streams],
                                stream_index=choice.stream_index, preset=choice.preset.to_dict()))
                            _notify(progress, number, len(choices), 'Retained the reviewed original')
                        processor = dict(zip(('ffmpeg_sha256', 'ffprobe_sha256'), choices[0].source.processor))
                        definition = validate_definition(dict(schema='carr-media-conversion/1', id=job_id,
                            title=title, created=_now(), processor=processor,
                            allowance=allowance.to_dict(), inputs=rows))
                        atomic_json(stage / 'definition.json', definition)
                        self._save(stage, definition, 'prepared', 0, None, 'Prepared; no conversion has started')
                        _sync_directory(stage / 'inputs')
                        _sync_directory(stage / 'results')
                        media_worker.check(check)
                        publish_noreplace(stage, self.root / job_id, cancelled=check)
                    finally:
                        if optional_present(stage):
                            shutil.rmtree(stage)
        return job_id

    def _save(self, job, definition, status, count, run_id, message):
        state = validate_job(dict(schema='carr-media-job/1', id=definition['id'],
            definition_sha256=digest(definition), status=status, completed=count,
            total=len(definition['inputs']), updated=_now(), run_id=run_id, message=message), definition)
        atomic_json(job / 'job.json', state)

    def definition(self, job_id):
        return read_definition(self._job(job_id))

    def jobs(self, *, cancelled=lambda: False):
        self._current()
        if not optional_present(self.root):
            return ()
        regular(self.root, directory=True)
        answer = []
        with os.scandir(self.root) as entries:
            for count, entry in enumerate(entries, 1):
                media_worker.check(cancelled)
                if count > MAX_JOBS * 2 + 105:
                    raise ValueError('The media namespace exceeds its entry allowance.')
                if entry.name.startswith('.') or entry.name == 'presets.json':
                    continue  # Strict snapshot validation checks transient names separately.
                job_id = identity(entry.name)
                job = self._job(job_id)
                definition = read_definition(job)
                state = validate_job(read_json(job / 'job.json', limit=MAX_METADATA_BYTES), definition)
                answer.append(dict(id=job_id, title=definition['title'], inputs=state['total'],
                                   completed=state['completed'], status=state['status']))
                if len(answer) > MAX_JOBS:
                    raise ValueError('The media job count exceeds its allowance.')
        return tuple(sorted(answer, key=lambda value: value['id']))

    def control(self, job_id, action):
        if action not in ('pause', 'cancel'):
            raise ValueError('Choose Pause or Stop for the current media run.')
        job = self._job(job_id)
        with project_lock(job, lock_name='.control.lock'):
            definition = read_definition(job)
            state = validate_job(read_json(job / 'job.json', limit=MAX_METADATA_BYTES), definition)
            if state['status'] != 'running' or state['run_id'] is None:
                raise ValueError('The selected media job has no active run to stop.')
            value = validate_control(dict(schema='carr-media-control/1', id=job_id,
                                          run_id=state['run_id'], action=action), job_id)
            atomic_json(job / 'control.json', value)

    def run(self, job_id, *, cancelled=lambda: False, progress=None):
        job = self._job(job_id)
        with project_lock(job):
            definition = read_definition(job)
            state = validate_job(read_json(job / 'job.json', limit=MAX_METADATA_BYTES), definition)
            allowance = ConversionAllowance.from_dict(definition['allowance'])
            if not optional_present(job / 'results'):
                (job / 'results').mkdir()
                _sync_directory(job)
            regular(job / 'results', directory=True)
            run_id, requested = uuid4().hex, [None]
            count = state['completed']
            self._save(job, definition, 'running', count, run_id, 'Waiting for shared resources')
            def stopped():
                self._current()
                if cancelled():
                    requested[0] = 'cancel'
                    return True
                if optional_present(job / 'control.json'):
                    control = validate_control(read_json(job / 'control.json'), job_id)
                    if control['run_id'] == run_id:
                        requested[0] = control['action']
                        return True
                return False
            try:
                remaining = allowance.disk_bytes - sum(row['bytes'] for row in definition['inputs']) - metadata_reserve(definition)
                with self._admission('media:' + job_id, cancelled=stopped, cpu=allowance.threads,
                                     disk=remaining, disk_path=job) as lease:
                    def check():
                        lease.checkpoint()
                        return stopped()
                    completed = verified_results(job, definition, cancelled=check)
                    if len(completed) < state['completed']:
                        raise ValueError('A previously acknowledged media output is missing.')
                    count = len(completed)
                    # Also retry a parent-directory sync after a post-move
                    # failure; matching bytes alone are not durable success.
                    _sync_directory(job / 'results')
                    used = metadata_reserve(definition) + sum(row['bytes'] for row in definition['inputs'])
                    used += sum(row['output']['bytes'] for row in completed)
                    for row in definition['inputs'][count:]:
                        media_worker.check(check)
                        _notify(progress, count, len(definition['inputs']), 'Converting the selected audio stream')
                        if allowance.disk_bytes - used < 4096:
                            raise ValueError('The remaining job disk allowance cannot hold another verified result.')
                        budget = AudioBudget(timeout=allowance.timeout_seconds,
                            max_output_bytes=allowance.disk_bytes - used, threads=allowance.threads,
                            max_log_bytes=1024**2, max_alloc_bytes=64 * 1024**2, max_staging_files=4)
                        stage = Path(tempfile.mkdtemp(prefix='.work-', dir=job))
                        try:
                            output = media_worker.convert_item(job, definition, row, stage, self.ffmpeg,
                                cancelled=check, budget=budget)
                            receipt = validate_receipt(dict(schema='carr-media-output/1', job=job_id,
                                definition_sha256=digest(definition), index=row['index'],
                                source={name: row[name] for name in ('name', 'bytes', 'sha256')},
                                stream_index=row['stream_index'], preset=row['preset'],
                                processor=definition['processor'], output=output), definition, row)
                            atomic_json(stage / 'receipt.json', receipt)
                            _hash(stage / output['path'], output, check)
                            media_worker.check(check)
                            publish_noreplace(stage, job / 'results' / f"{row['index']:06d}", cancelled=check)
                            count += 1
                            used += output['bytes']
                            self._save(job, definition, 'running', count, run_id, 'Verified an audio result')
                            _notify(progress, count, len(definition['inputs']), 'Verified an audio result')
                        finally:
                            if optional_present(stage):
                                shutil.rmtree(stage)
                    # A stop arriving after the last publication cannot erase
                    # or falsely report failure for the completed batch.
                    self._save(job, definition, 'complete', count, run_id, 'All results verified')
                    return dict(id=job_id, status='complete', completed=count, total=count)
            except (Exception, KeyboardInterrupt, SystemExit) as error:
                cancelled_run = requested[0] is not None or isinstance(error, (InterruptedError, AudioCancelled, RenderCancelled))
                status = ('paused' if requested[0] == 'pause' else 'cancelled') if cancelled_run else 'failed'
                self._save(job, definition, status, count, run_id,
                           'Stopped; completed results retained' if cancelled_run else 'Failed; completed results retained')
                if cancelled_run:
                    raise InterruptedError('Media job stopped; Resume reuses verified completed items.') from error
                raise

    def verify(self, job_id, *, cancelled=lambda: False):
        job = self._job(job_id)
        with project_lock(job):
            definition = read_definition(job)
            state = validate_job(read_json(job / 'job.json', limit=MAX_METADATA_BYTES), definition)
            results = verified_results(job, definition, cancelled=cancelled)
            if state['completed'] > len(results) or state['status'] == 'complete' and len(results) != state['total']:
                raise ValueError('The media checkpoint has an incomplete verified result inventory.')
            return dict(id=job_id, status=state['status'], completed=len(results), total=state['total'],
                        outputs=[dict(index=row['index'], **row['output']) for row in results])

    def presets(self):
        self._current()
        path = self.root / 'presets.json'
        if not optional_present(path):
            return {}
        return validate_presets(read_json(path, limit=MAX_METADATA_BYTES))['presets']

    def save_preset(self, name, preset):
        text(name, 'Media preset name', 80)
        if type(preset) is not ConversionPreset:
            raise ValueError('Save a typed media preset.')
        with exclusive_writer(self.library_root):
            self._ensure()
            with project_lock(self.root, lock_name='.state.lock'):
                values = self.presets()
                if name in values:
                    raise FileExistsError('Choose a new preset name; saved settings are not overwritten.')
                values[name] = preset.to_dict()
                atomic_json(self.root / 'presets.json', validate_presets(dict(schema='carr-media-presets/1', presets=values)))

    def export(self, job_id, destination, *, cancelled=lambda: False, parent_identity=None):
        job = self._job(job_id)
        destination = local_path(destination)
        parent = checked_directory(destination.parent)
        if destination.is_relative_to(self.library_root):
            raise ValueError('Choose a new output folder outside the private library.')
        if optional_present(destination):
            raise FileExistsError('Choose an absent output folder; existing exports are never replaced.')
        before = regular(parent, directory=True)
        if parent_identity is not None:
            if (type(parent_identity) is not tuple or len(parent_identity) != 2
                    or any(type(value) is not int for value in parent_identity)
                    or parent_identity != (before.st_dev, before.st_ino)):
                raise ValueError('The reviewed export parent changed; choose it again.')
        with project_lock(job):
            definition = read_definition(job)
            results = verified_results(job, definition, cancelled=cancelled)
            if len(results) != len(definition['inputs']):
                raise ValueError('Finish the media batch before exporting its complete results.')
            stage = Path(tempfile.mkdtemp(prefix='.media-export-', dir=parent))
            try:
                for row, receipt in zip(definition['inputs'], results):
                    media_worker.check(cancelled)
                    source = job / 'results' / f"{row['index']:06d}" / output_name(row)
                    target = stage / source.name
                    publish_new(source, target, cancelled=cancelled)
                    _hash(target, receipt['output'], cancelled)
                # Original absolute source paths stay private in definition.json.
                atomic_json(stage / 'media-export.json', dict(schema='carr-media-export/1', job=job_id,
                    definition_sha256=digest(definition), outputs=list(results)))
                after = regular(parent, directory=True)
                if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError('The selected export parent changed; choose it again.')
                publish_noreplace(stage, destination, cancelled=cancelled)
            finally:
                if optional_present(stage):
                    shutil.rmtree(stage)
        return destination
