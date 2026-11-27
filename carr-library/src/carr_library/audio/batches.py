"""Durable local audio/video and multi-book work through existing producers."""
from contextlib import contextmanager, ExitStack
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

from ..durable_state import state_lock
from ..ingest.writer_guard import exclusive_writer
from ..operations import ResourceBroker, ResourceRequest, ResourceCancelled
from ..security.admission import admitted_operation, checked_controller
from . import media_conversion as media
from .batch_models import (AudioPreset, BatchControl, BatchItem, BatchState, InputAsset,
                           InputSnapshot, Selection, MAX_INPUT_BYTES, MAX_ITEMS,
                           MAX_STATE_BYTES, canonical, digest, identity, output_names,
                           receipt_value, text)
from .cleanup import AudioCleanupPending, wait_for_cleanup
from .ffmpeg import wav_info
from .jobs import (RenderCancelled, atomic_json, checked_directory, check_cancelled,
                   file_identity, publish_new, publish_noreplace, read_json,
                   regular_file, sha256_file, verified_reader, _sync_directory)
from .managed import ManagedProjects

DIRECTORY = 'audio-batches'
MAX_BATCHES = 1000


def document_plan(payload):
    """Use the existing strict Studio document and literal/script planner."""
    from .backup import _studio
    from .production import parse_script, plan_narration
    from ..reader.narration_text import NarrationDraft
    _studio(payload)
    draft = NarrationDraft.from_dict(payload['draft'])
    kind = payload.get('input_kind', 'narration' if payload['source_reference'] is not None else 'script')
    planner = plan_narration if kind == 'narration' else parse_script
    return planner(draft.text, title=payload['title'], voice=payload['voice'], language=payload['language'])


def child_document(payload, item_id):
    # Preserve the original draft/source reference exactly. Old absolute output
    # paths are inert; ManagedProjects rebinds this clone on every machine.
    return {**payload, 'project_id': item_id, 'output_dir': None}


def _file_record(path, *, cancelled):
    info = regular_file(path)
    if info.st_nlink != 1 or not 0 < info.st_size <= MAX_INPUT_BYTES:
        raise ValueError('Retained batch inputs must be bounded independent regular files.')
    original = file_identity(info)
    return info.st_size, sha256_file(path, cancelled=cancelled, expected_identity=original), original


@contextmanager
def producer_guard(library_root, *, create=False):
    """Use the existing library writer order, then retain the producer lock."""
    library = checked_directory(library_root)
    root = library / DIRECTORY
    with ExitStack() as stack:
        with exclusive_writer(library):
            if not root.exists() and not create:
                raise FileNotFoundError('No saved audio queues exist in this library.')
            checked_directory(root, create=create)
            stack.enter_context(state_lock(root, 'batch'))
        yield root


class AudioBatchService:
    """One cooperative producer; UI, CLI and complete backup share its records."""

    def __init__(self, library_root, *, ffmpeg=None, registry=None, broker=None, trust_controller=None):
        self.library = checked_directory(library_root)
        self.root = self.library / DIRECTORY
        self.ffmpeg = Path(ffmpeg).absolute() if ffmpeg is not None else None
        self.registry = registry
        self.broker = broker or ResourceBroker.for_app(self.library)
        if (not isinstance(self.broker, ResourceBroker)
                or self.broker.root != self.library / 'runtime' / 'resources'
                or self.broker.policy_path != self.library / 'state' / 'operations' / 'resources.json'):
            raise ValueError('The audio queue resource coordinator belongs to another library.')
        self.trust_controller = checked_controller(trust_controller)
        self._library_identity = file_identity(self.library.stat())[:2]

    def _scope(self):
        checked_directory(self.library)
        if file_identity(self.library.stat())[:2] != self._library_identity:
            raise ValueError('The selected audio queue library changed. Reopen the queue.')

    def path(self, batch_id):
        identity(batch_id)
        self._scope()
        return self.root / batch_id

    def load(self, batch_id):
        root = checked_directory(self.path(batch_id))
        value = BatchState.from_dict(read_json(root / 'batch.json', limit=MAX_STATE_BYTES))
        if value.id != batch_id:
            raise ValueError('The saved audio queue belongs to another folder.')
        return value

    def _write(self, batch):
        self._scope()
        BatchState.from_dict(json.loads(canonical(batch.to_dict())))
        atomic_json(self.path(batch.id) / 'batch.json', batch.to_dict())
        return batch

    def _control(self, batch_id):
        control = BatchControl.from_dict(read_json(self.path(batch_id) / 'control.json', limit=8192))
        if control.batch_id != batch_id:
            raise ValueError('The saved control belongs to another audio queue.')
        return control

    def list(self):
        self._scope()
        if not os.path.lexists(self.root):
            return ()
        checked_directory(self.root)
        result, metadata_bytes = [], 0
        for index, path in enumerate(sorted(self.root.iterdir())):
            if index > MAX_BATCHES * 2:
                raise ValueError('The saved audio queue inventory exceeds its allowance.')
            if path.name == '.batch.lock':
                regular_file(path)
                continue
            if re.fullmatch(r'\.pending-create-[A-Za-z0-9_-]+', path.name):
                checked_directory(path)
                continue
            identity(path.name)
            metadata_bytes += regular_file(path / 'batch.json').st_size
            if metadata_bytes > 32 * 1024**2:
                raise ValueError('The queue list exceeds its metadata allowance; open one explicit queue ID.')
            result.append(self.load(path.name))
        if len(result) > MAX_BATCHES:
            raise ValueError('The saved audio queue inventory exceeds its allowance.')
        return tuple(result)

    def describe(self, batch_id):
        value = self.load(batch_id)
        control = self._control(batch_id)
        return {
            'schema': value.schema, 'id': value.id, 'title': value.title, 'status': value.status,
            'plan_sha256': value.plan_sha256, 'runtime': asdict(value.runtime),
            'control': asdict(control), 'control_acknowledged': control.sequence <= value.control_sequence,
            'items': [{
                'id': item.id, 'ordinal': item.ordinal, 'title': item.title, 'kind': item.kind,
                'state': item.state, 'attempt': item.attempt, 'error': item.error,
                'preset': item.preset.to_dict(), 'audio_stream': item.audio_stream,
                'source_name': item.source.original_name, 'source_sha256': item.source.sha256,
                'source_project_id': item.source.project_id,
                'child_project_id': item.id if item.kind == 'book' else None,
                'outputs': [str(self.path(value.id) / 'outputs' / item.id / name) for name in item.outputs],
            } for item in value.items],
            'retained_originals': 'Independent exact local snapshots; selected originals are never edited.',
            'resume': 'Select the same FFmpeg bytes explicitly. Verified completed items are reused.',
        }

    def create(self, selections, *, title='Local audio queue', cancelled=lambda: False):
        text(title, 'queue title')
        if (not isinstance(selections, (list, tuple)) or not 1 <= len(selections) <= MAX_ITEMS
                or any(not isinstance(item, Selection) for item in selections)):
            raise ValueError('Choose between one and 128 typed local items with per-item presets.')
        runtime = media.runtime_identity(self.ffmpeg, cancelled=cancelled)
        batch_id = uuid4().hex
        with producer_guard(self.library, create=True):
            if len(self.list()) >= MAX_BATCHES:
                raise ValueError('The saved audio queue limit has been reached.')
            captures, retained_bytes = [], 0
            for ordinal, selected in enumerate(selections, 1):
                check_cancelled(cancelled)
                assets = []
                if selected.kind == 'book':
                    _, source, audio = ManagedProjects(self.library).paths(selected.source)
                    checked_directory(source.parent)
                else:
                    source, _ = media.selected_file(selected.source, selected.kind)
                size, source_hash, original = _file_record(source, cancelled=cancelled)
                item_id = digest({'batch': batch_id, 'ordinal': ordinal, 'kind': selected.kind,
                                  'source': source_hash, 'preset': selected.preset.to_dict(),
                                  'stream': selected.audio_stream})[:32]
                if selected.kind == 'book':
                    with verified_reader(source, expected_identity=original):
                        payload = read_json(source, limit=MAX_STATE_BYTES)
                        plan = document_plan(payload)
                        if payload['project_id'] != selected.source:
                            raise ValueError('The selected saved book belongs to another project.')
                        from .backup import input_assets
                        for name, asset in input_assets(source, audio, cancelled=cancelled).items():
                            if not name.startswith('audio/assets/') or '/' in name[len('audio/assets/'):]:
                                raise ValueError('A Studio asset is outside its existing retained-input namespace.')
                            asset_size, asset_hash, asset_identity = _file_record(asset, cancelled=cancelled)
                            record = InputAsset(asset.name, item_id + '-' + digest({'name': asset.name, 'sha256': asset_hash})[:32] + '.wav', asset_size, asset_hash)
                            assets.append((record, asset, asset_identity))
                            retained_bytes += asset_size
                    snapshot = InputSnapshot(item_id + '.studio.json', size, source_hash,
                        selected.source + '.studio.json', selected.source,
                        sha256(canonical(child_document(payload, item_id))).hexdigest(),
                        tuple(row[0] for row in assets))
                    names = output_names(item_id, ordinal, selected.title, selected.preset, len(plan.scenes))
                else:
                    suffix = source.suffix.lower()
                    snapshot = InputSnapshot(item_id + suffix, size, source_hash, source.name)
                    names = output_names(item_id, ordinal, selected.title, selected.preset)
                item = BatchItem(item_id, ordinal, selected.kind, selected.title,
                                 selected.preset, selected.audio_stream, snapshot, names)
                captures.append((item, source, original, assets))
                retained_bytes += size
            batch = BatchState.create(batch_id, title, runtime, tuple(row[0] for row in captures))
            request = ResourceRequest(cpu_slots=1, memory_bytes=64 * 1024**2,
                                      disk_bytes=retained_bytes + 2 * MAX_STATE_BYTES, disk_path=self.root)
            with self.broker.reserve(request, operation_id='audio-batch:' + batch_id,
                                     cancelled=cancelled, timeout=60) as lease:
                with tempfile.TemporaryDirectory(prefix='.pending-create-', dir=self.root) as temporary:
                    stage = Path(temporary) / batch_id
                    sources = checked_directory(stage / 'sources', create=True)
                    checked_directory(stage / 'outputs', create=True)
                    for item, source, original, assets in captures:
                        publish_new(source, sources / item.source.file, cancelled=lease.cancelled,
                                    expected_identity=original)
                        self._verify_file(sources / item.source.file, item.source.bytes, item.source.sha256, lease.cancelled)
                        for record, path, original in assets:
                            target = sources / record.file
                            publish_new(path, target, cancelled=lease.cancelled, expected_identity=original)
                            self._verify_file(target, record.bytes, record.sha256, lease.cancelled)
                    atomic_json(stage / 'batch.json', batch.to_dict())
                    atomic_json(stage / 'control.json', asdict(BatchControl(batch_id=batch_id)))
                    _sync_directory(sources)
                    check_cancelled(lease.cancelled)
                    publish_noreplace(stage, self.path(batch_id), cancelled=lease.cancelled)
            # A cancellation arriving after the complete queue directory moved
            # cannot retroactively turn these retained originals into a failure.
            return self.load(batch_id)

    @staticmethod
    def _verify_file(path, size, expected, cancelled=lambda: False):
        info = regular_file(path)
        if (info.st_nlink != 1 or info.st_size != size
                or sha256_file(path, cancelled=cancelled, expected_identity=file_identity(info)) != expected):
            raise ValueError('Retained audio input or completed output changed; no overwrite was attempted.')

    def verify_sources(self, batch, item, *, cancelled=lambda: False):
        source = checked_directory(self.path(batch.id) / 'sources') / item.source.file
        self._verify_file(source, item.source.bytes, item.source.sha256, cancelled)
        for asset in item.source.assets:
            self._verify_file(source.parent / asset.file, asset.bytes, asset.sha256, cancelled)
        if item.kind == 'book':
            payload = read_json(source, limit=MAX_STATE_BYTES)
            plan = document_plan(payload)
            if payload['project_id'] != item.source.project_id:
                raise ValueError('The retained book snapshot belongs to another source project.')
            child = child_document(payload, item.id)
            if sha256(canonical(child)).hexdigest() != item.source.child_document_sha256:
                raise ValueError('The retained book snapshot lost its exact child-document binding.')
            if output_names(item.id, item.ordinal, item.title, item.preset, len(plan.scenes)) != item.outputs:
                raise ValueError('The saved chapter/name preview changed.')
            return source, child, plan
        return source, None, None

    def verified_receipt(self, batch, item, *, cancelled=lambda: False, durable=False):
        output = self.path(batch.id) / 'outputs' / item.id
        if not os.path.lexists(output):
            return None
        checked_directory(output)
        value = receipt_value(read_json(output / 'receipt.json', limit=MAX_STATE_BYTES), batch, item)
        if item.kind == 'book':
            from .models import digest as production_digest
            payload = read_json(self.path(batch.id) / 'sources' / item.source.file, limit=MAX_STATE_BYTES)
            plan = document_plan(payload)
            if (value['render']['plan_sha256'] != production_digest(plan.to_dict())
                    or value['render']['source_sha256'] != plan.source_sha256):
                raise ValueError('The completed audiobook receipt belongs to another exact Studio plan.')
        if {path.name for path in output.iterdir()} != {*item.outputs, 'receipt.json'}:
            raise ValueError('The completed audio output directory contains undeclared files.')
        for record in value['files']:
            path = output / record['name']
            self._verify_file(path, record['bytes'], record['sha256'], cancelled)
            if item.preset.output_format == 'wav':
                info = wav_info(path)
                if (info.sample_rate, info.channels, info.frames) != (
                        record['sample_rate'], record['channels'], record['frames']):
                    raise ValueError('The completed WAV receipt disagrees with actual PCM.')
        if durable:
            _sync_directory(output)
            _sync_directory(output.parent)
        return value

    def control(self, batch_id, action):
        if action not in ('pause', 'cancel'):
            raise ValueError('Use explicit run/resume/retry for starting work; controls only pause or cancel.')
        with exclusive_writer(self.library):
            self.load(batch_id)
            with state_lock(self.path(batch_id), 'control'):
                previous = self._control(batch_id)
                value = BatchControl(batch_id=batch_id, sequence=previous.sequence + 1, action=action)
                atomic_json(self.path(batch_id) / 'control.json', asdict(value))
        return {'id': batch_id, 'action': action, 'requested': True, 'acknowledged': False,
                'sequence': value.sequence}

    def _continue(self, batch_id):
        with state_lock(self.path(batch_id), 'control'):
            previous = self._control(batch_id)
            value = BatchControl(batch_id=batch_id, sequence=previous.sequence + 1)
            atomic_json(self.path(batch_id) / 'control.json', asdict(value))
            return value

    def _registry(self):
        if self.registry is None:
            from ..runtime_settings import configured_voice_registry
            self.registry = configured_voice_registry(self.library, ffmpeg=self.ffmpeg)
        return self.registry

    def _book(self, batch, item, child, plan, *, cancelled, on_status):
        managed = ManagedProjects(self.library)
        _, path, audio = managed.paths(item.id)
        expected = item.source.child_document_sha256
        if not os.path.lexists(path):
            managed.write(item.id, path, canonical(child).decode('utf-8'), cancelled=cancelled)
        self._verify_file(path, len(canonical(child)), expected, cancelled)
        if item.source.assets:
            destination = checked_directory(audio / 'assets', create=True)
            for asset in item.source.assets:
                target = destination / asset.name
                if not os.path.lexists(target):
                    publish_new(self.path(batch.id) / 'sources' / asset.file, target, cancelled=cancelled)
                self._verify_file(target, asset.bytes, asset.sha256, cancelled)
        from .production import render_project
        from ..runtime_settings import EngineProfileStore
        with verified_reader(path, expected_identity=file_identity(regular_file(path))):
            result = render_project(plan, audio, engine=self._registry(), ffmpeg=self.ffmpeg,
                broker=self.broker, operation_id='studio:' + item.id,
                trust_controller=self.trust_controller, cancelled=cancelled, on_status=on_status,
                music_manifest_resolver=lambda reference, engine, cancelled: EngineProfileStore(
                    self.library).music_manifest(reference, engine, cancelled=cancelled))
            if result.get('status') != 'complete':
                raise ValueError('The existing Studio producer did not confirm a completed book.')
            self._verify_file(path, len(canonical(child)), expected, cancelled)
        master = Path(result['manifest']).parent / 'master.wav'
        info = wav_info(master)
        if info.seconds > item.preset.max_seconds:
            raise ValueError('The rendered book exceeds its selected batch duration allowance.')
        records = {row['name']: row for row in result['files']}
        if 'master.wav' not in records:
            raise ValueError('The existing Studio receipt has no exact programme master.')
        self._verify_file(master, records['master.wav']['size'], records['master.wav']['sha256'], cancelled)
        chapters = result['chapters']
        if len(chapters) != len(plan.scenes):
            raise ValueError('The completed chapter inventory differs from the reviewed book plan.')
        previous = 0
        bounds = []
        for chapter in chapters:
            if (type(chapter['start_frame']) is not int or type(chapter['end_frame']) is not int
                    or chapter['start_frame'] != previous or not previous < chapter['end_frame'] <= info.frames):
                raise ValueError('The existing Studio chapter sample boundaries are not contiguous.')
            bounds.append((chapter['start_frame'], chapter['end_frame']))
            previous = chapter['end_frame']
        if previous != info.frames:
            raise ValueError('The completed chapter sample boundaries do not cover the master.')
        binding = {key: result[key] for key in ('run_key', 'plan_sha256', 'source_sha256')}
        return master, tuple(bounds) if item.preset.split == 'chapters' else (None,), binding

    def _convert(self, batch, item, source, child, plan, *, cancelled, on_status):
        if item.kind == 'book':
            with media.selected_runtime(self.ffmpeg, batch.runtime, cancelled=cancelled):
                source, bounds, binding = self._book(batch, item, child, plan, cancelled=cancelled, on_status=on_status)
            demuxer, stream_index = 'wav', 0
        else:
            _, demuxer = media.selected_file(source, item.kind)
            bounds, binding, stream_index = (None,), None, item.audio_stream
        request = ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2,
                                  disk_bytes=item.preset.pcm_budget * 4 + 2 * MAX_STATE_BYTES,
                                  disk_path=self.path(batch.id))
        with self.broker.reserve(request, operation_id='audio-batch:' + batch.id,
                                 cancelled=cancelled, timeout=60) as lease:
            with tempfile.TemporaryDirectory(prefix='.pending-item-' + item.id + '-', dir=self.path(batch.id)) as name:
                stage = Path(name)
                atomic_json(stage / 'owner.json', {'batch_id': batch.id, 'item_id': item.id,
                                                  'plan_sha256': batch.plan_sha256})
                output = checked_directory(stage / 'publish', create=True)
                work = checked_directory(stage / 'work', create=True)
                with media.selected_runtime(self.ffmpeg, batch.runtime, cancelled=lease.cancelled):
                    files = []
                    original = file_identity(regular_file(source))
                    with verified_reader(source, expected_identity=original):
                        for ordinal, (filename, selection) in enumerate(zip(item.outputs, bounds), 1):
                            on_status(f'Converting item {item.ordinal} of {len(batch.items)}, output {ordinal} of {len(item.outputs)}.')
                            decoded = work / 'decoded.wav'
                            media.decode(source, decoded, demuxer=demuxer, stream_index=stream_index,
                                         ffmpeg=self.ffmpeg, preset=item.preset, cancelled=lease.cancelled,
                                         sample_bounds=selection)
                            target = output / filename
                            media.encode(decoded, target, ffmpeg=self.ffmpeg, preset=item.preset, cancelled=lease.cancelled)
                            decoded.unlink()
                            files.append(media.verify_encoded(target, work / 'verified.wav', ffmpeg=self.ffmpeg,
                                                              preset=item.preset, cancelled=lease.cancelled))
                        self.verify_sources(batch, item, cancelled=lease.cancelled)
                    value = {'schema': 'carr-audio-batch-receipt/1', 'batch_id': batch.id,
                             'item_id': item.id, 'plan_sha256': batch.plan_sha256, 'item_sha256': item.fingerprint,
                             'source_sha256': item.source.sha256, 'runtime_sha256': batch.runtime.sha256,
                             'files': files, 'render': binding}
                    receipt_value(value, batch, item)
                    atomic_json(output / 'receipt.json', value)
                check_cancelled(lease.cancelled)
                publish_noreplace(output, self.path(batch.id) / 'outputs' / item.id,
                                  cancelled=lease.cancelled)
                # Finish the committed result even if Cancel arrives after the
                # no-replace directory move. Next items still observe the request.
                return self.verified_receipt(batch, item, durable=True)

    def run(self, batch_id, *, action='run', retry_items=(), cancelled=lambda: False,
            on_status=lambda message: None):
        if action not in ('run', 'resume', 'retry'):
            raise ValueError('Choose run, resume, or retry explicitly.')
        with producer_guard(self.library):
            batch = self.load(batch_id)
            if action == 'run' and batch.status in ('paused', 'cancelled'):
                raise ValueError('This queue was stopped; use explicit Resume to continue it.')
            selected = set(retry_items)
            if selected - {item.id for item in batch.items}:
                raise ValueError('Retry selection contains an item from another queue.')
            if selected and any(item.state != 'failed' for item in batch.items if item.id in selected):
                raise ValueError('Only failed items can be explicitly retried.')
            if action != 'retry' and selected:
                raise ValueError('A retry item selection requires the retry action.')
            if action in ('resume', 'retry'):
                control = self._continue(batch_id)
                items = tuple(replace(item, state='pending', error=None) if (
                    item.state in ('paused', 'cancelled', 'running')
                    or action == 'retry' and item.state == 'failed' and (not selected or item.id in selected))
                    else item for item in batch.items)
                batch = self._write(replace(batch, status='queued', items=items,
                                            control_sequence=control.sequence))
            control = self._control(batch_id)
            requested = [control]

            def stop():
                self._scope()
                requested[0] = self._control(batch_id)
                if requested[0].sequence < batch.control_sequence:
                    raise ValueError('The durable audio control sequence moved backwards.')
                return cancelled() or requested[0].action in ('pause', 'cancel')

            def stopped(value):
                control = requested[0]
                status = ('complete' if all(item.state == 'complete' for item in value.items)
                          else 'paused' if control.action == 'pause' else 'cancelled')
                items = tuple(replace(item, state=status, error=None)
                              if item.state in ('running', 'pending') else item for item in value.items)
                return self._write(replace(value, status=status, items=items,
                                           control_sequence=control.sequence))

            if stop():
                return stopped(batch)
            if media.runtime_identity(self.ffmpeg, cancelled=stop) != batch.runtime:
                raise ValueError('Resume requires the exact reviewed FFmpeg runtime.')
            batch = self._write(replace(batch, status='running'))
            for index, original_item in enumerate(batch.items):
                item = batch.items[index]
                if stop():
                    return stopped(batch)
                try:
                    source, child, plan = self.verify_sources(batch, item, cancelled=stop)
                    receipt = self.verified_receipt(batch, item, cancelled=stop, durable=True)
                    if receipt is None:
                        if item.state == 'complete':
                            raise ValueError('A completed item lost its verified outputs; they were not silently replaced.')
                        if item.state == 'failed':
                            continue
                        item = replace(item, state='running', attempt=item.attempt + 1, error=None)
                        items = list(batch.items)
                        items[index] = item
                        batch = self._write(replace(batch, items=tuple(items)))
                        # Each fresh item is one admitted operation. The
                        # existing producer reuses this exact authority lease.
                        with admitted_operation(self.trust_controller, 'audio.render'):
                            self._convert(batch, item, source, child, plan,
                                          cancelled=stop, on_status=on_status)
                    item = replace(item, state='complete', error=None)
                except AudioCleanupPending as error:
                    wait_for_cleanup(error)
                    raise
                except (RenderCancelled, ResourceCancelled, InterruptedError):
                    stop()
                    return stopped(batch)
                except Exception as error:
                    message = ''.join(c if ord(c) >= 32 else ' ' for c in (str(error) or type(error).__name__))
                    item = replace(item, state='failed', error=message[:2048] or 'Audio conversion failed.')
                items = list(batch.items)
                items[index] = item
                batch = self._write(replace(batch, items=tuple(items)))
            status = 'complete' if all(item.state == 'complete' for item in batch.items) else 'failed'
            batch = self._write(replace(batch, status=status, control_sequence=self._control(batch_id).sequence))
            return batch

    def export_file(self, batch_id, item_id, filename, destination, *, approval, cancelled=lambda: False):
        """Protected data export: a verified independent copy, always no-replace."""
        from ..research.export_file import capture_export_destination, ExportDestination
        batch = self.load(batch_id)
        identity(item_id)
        item = next((item for item in batch.items if item.id == item_id), None)
        if item is None or filename not in item.outputs:
            raise ValueError('Choose one verified file from this saved queue item.')
        self.verify_sources(batch, item, cancelled=cancelled)
        receipt = self.verified_receipt(batch, item, cancelled=cancelled, durable=True)
        if receipt is None:
            raise ValueError('This item has no verified completed output to export.')
        destination = Path(destination).absolute()
        if type(approval) is not ExportDestination or not approval.require_absent:
            raise ValueError('Select and capture a new export filename; existing files are never replaced.')
        def guard():
            if cancelled():
                return True
            if capture_export_destination(destination, private_root=self.library) != approval:
                raise ValueError('The chosen export destination changed; select it again.')
            return False
        check_cancelled(guard)
        record = next(row for row in receipt['files'] if row['name'] == filename)
        source = self.path(batch_id) / 'outputs' / item_id / filename
        original = file_identity(regular_file(source))
        request = ResourceRequest(cpu_slots=1, memory_bytes=64 * 1024**2,
                                  disk_bytes=record['bytes'] + 1024**2, disk_path=destination.parent)
        with self.broker.reserve(request, operation_id='audio-export:' + uuid4().hex,
                                 cancelled=guard, timeout=60) as lease:
            def export_guard():
                return lease.cancelled() or guard()
            publish_new(source, destination, cancelled=export_guard, expected_identity=original)
        self._verify_file(destination, record['bytes'], record['sha256'])
        return {'status': 'complete', 'output': str(destination), 'sha256': record['sha256'],
                'source_batch': batch_id, 'source_item': item_id}
