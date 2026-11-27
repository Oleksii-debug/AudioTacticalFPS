"""Persistent production DAG: actual local voices, music, scene mixes and exports.

Every accepted node is content-addressed and checksum-verified before reuse.
Source text is data. This module never accepts shell commands or provider URLs.
"""
from contextlib import ExitStack
from dataclasses import asdict, replace
from hashlib import sha256
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time
from uuid import uuid4

from ..durable_state import atomic_bytes, atomic_json, digest_file, encode, local_path, read_json, regular, state_lock
from .managed_inputs import project_managed_inputs, resolve_managed_input
from .project import Project, ProjectStore, parse_script, project_effects, _id

ENGINE_SCHEMA = 'studio-local-dag-v1'


def music_installation_identity(runtime, engine):
    """Bind cached scores to the explicitly installed weights and runtime code."""
    if engine in ('musicgen', 'ace-step'):
        from ..audio.music import validate_model
        if engine not in runtime.music_models:
            raise ValueError(f'Configure the installed {engine} model before rendering this score.')
        config, _ = runtime.music_models[engine]
        manifest = validate_model(config)
        return {'engine': engine, 'manifest': manifest,
                'python_sha256': digest_file(config.python),
                'device': config.device, 'max_chunk_seconds': config.max_chunk_seconds}
    if engine == 'midi':
        if not runtime.fluidsynth or not runtime.soundfont:
            raise ValueError('Configure FluidSynth and a reviewed SoundFont first.')
        return {'engine': engine, 'fluidsynth_sha256': digest_file(runtime.fluidsynth),
                'soundfont_sha256': digest_file(runtime.soundfont)}
    return {'engine': engine, 'composer': 'procedural-motif-v1'}


def music_segments(spec, sample_rate, maximum_seconds):
    """Frame-exact independent variations; no claim of model continuation."""
    total = round(spec.duration * sample_rate)
    maximum = max(1, int(maximum_seconds * sample_rate))
    cursor, index = 0, 0
    while cursor < total:
        frames = min(maximum, total - cursor)
        # Keep the last segment within the engine's minimum duration too.
        minimum = max(1, round(.01 * sample_rate))
        if 0 < total - cursor - frames < minimum:
            frames -= minimum - (total - cursor - frames)
        duration = frames / sample_rate
        yield replace(spec, duration=duration, seed=(spec.seed + index) % (2 ** 31),
                      fade_in=min(spec.fade_in, duration), fade_out=min(spec.fade_out, duration))
        cursor += frames
        index += 1


def _stamp(seconds, *, comma=False):
    millis = max(0, round(seconds * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, fraction = divmod(rest, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}{"," if comma else "."}{fraction:03d}'


def _output_name(title):
    name = re.sub(r'[^\w-]+', '-', title, flags=re.UNICODE).strip('-')[:70]
    if not name or name.upper() in {'CON', 'PRN', 'AUX', 'NUL'}:
        name = 'production'
    return name


class ProductionService:
    def __init__(self, directory, *, voices=None, runtime_factory=None, input_roots=(), broker=None,
                 total_disk_bytes=32 * 1024**3):
        """Bound private state/cache, staging and this job's published copies.

        This logical-byte allowance is independent of each AudioBudget output
        limit. Shared retained cache counts once, including earlier revisions;
        verified reuse does not reserve a second copy. Increase the allowance
        explicitly when resuming work that requires more retained space.
        """
        if type(total_disk_bytes) is not int or not 1024**2 <= total_disk_bytes <= 2**63 - 1:
            raise ValueError('Total production disk budget must be an integer of at least one MiB.')
        self.total_disk_bytes = total_disk_bytes
        self._owns_voices = voices is None
        self._closed = False
        self.root = local_path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.projects = ProjectStore(self.root / 'projects')
        self.jobs_root = self.root / 'jobs'
        self.jobs_root.mkdir(exist_ok=True)
        self.cache = self.root / 'cache'
        self.cache.mkdir(exist_ok=True)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir(exist_ok=True)
        self._voices = voices
        self.runtime_factory = runtime_factory
        self._broker = broker
        self.input_roots = tuple(local_path(x, exists=True) for x in input_roots)

    @property
    def broker(self):
        return self._admission_broker()

    def _admission_broker(self, cancelled=lambda: False):
        from ..operations import ResourceBroker, ResourceStateError
        root = local_path(self.root, exists=True)
        app_root = (root.parent.parent if root.name == 'studio'
                    and root.parent.name == 'state' else root.parent)
        if self._broker is None:
            self._broker = ResourceBroker.for_app(app_root, cancelled=cancelled)
        if (not isinstance(self._broker, ResourceBroker)
                or self._broker.root != app_root / 'runtime' / 'resources'
                or self._broker.policy_path != app_root / 'state' / 'operations' / 'resources.json'):
            raise ResourceStateError('The production resource policy belongs to another data folder.')
        return self._broker

    @staticmethod
    def _resource_source_identity(path, cancelled):
        from ..durable_state import regular
        path = local_path(path, exists=True)
        before = regular(path)
        identity = sha256()
        with path.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                if cancelled():
                    raise InterruptedError('Production input review was cancelled.')
                identity.update(block)
            held = os.fstat(stream.fileno())
        after = regular(local_path(path, exists=True))
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, key) != getattr(held, key) or
               getattr(before, key) != getattr(after, key) for key in fields):
            raise ValueError('A selected production input changed during resource review.')
        if cancelled():
            raise InterruptedError('Production input review was cancelled.')
        return identity.hexdigest()

    def _resource_plan(self, project, state, runtime, *, cancelled=lambda: False):
        from ..operations import ResourceRequest, ResourceUnavailable
        limits = self.broker.limits
        neural = {scene.music.engine for scene in project.scenes if scene.music is not None
                  and scene.music.engine in ('musicgen', 'ace-step')}
        devices = set()
        for engine in neural:
            if engine not in runtime.music_models:
                raise ResourceUnavailable(f'Configure the local {engine} model before reserving production resources.')
            config, _ = runtime.music_models[engine]
            if config.device != 'cpu':
                devices.add(config.device + ':0')
        if len(devices) > 1:
            raise ResourceUnavailable('Choose one configured GPU device for this production; mixed GPU jobs are not admitted together.')
        device = next(iter(devices), None)
        capacity = dict(limits.gpu_devices).get(device, 0)
        if device is not None and not capacity:
            raise ResourceUnavailable('The selected music GPU needs an explicit shared resource capacity before rendering.')
        if device is not None and any(os.environ.get(name) for name in
                                      ('CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER', 'HIP_VISIBLE_DEVICES', 'ROCR_VISIBLE_DEVICES')):
            raise ResourceUnavailable('Clear inherited GPU device remapping before reserving the selected music device.')
        # Declared estimates, not RAM measurements or inference requirements.
        memory = limits.memory_bytes if neural else max(512 * 1024 ** 2, 2 * runtime.budget.max_alloc_bytes)
        if neural and memory == 0:
            raise ResourceUnavailable('Configure a positive declared memory budget for neural production.')
        seconds, source_info, source_hashes = 0.0, {}, {}
        for scene in project.scenes:
            duration = max(scene.minimum_duration,
                sum(len(line.text) * .5 + line.pause_after for line in scene.lines))
            for cue in scene.cues:
                source = self._cue_source(cue, cancelled=cancelled, schema=project.schema)
                if source not in source_info:
                    before = self._resource_source_identity(source, cancelled)
                    info = runtime.inspect(source, cancelled=cancelled)
                    if self._resource_source_identity(source, cancelled) != before:
                        raise ValueError('A selected production input changed during audio inspection.')
                    source_info[source], source_hashes[str(source)] = info, before
                info = source_info[source]
                available = info.duration - cue.trim_start
                selected = available if cue.duration is None else cue.duration
                if available <= 0 or selected > available + 1 / info.sample_rate:
                    raise ValueError('An edited clip selection is outside its original audio.')
                if max(cue.fade_in, cue.fade_out) > selected:
                    raise ValueError('An edited clip fade exceeds its selected audio.')
                selected = round(selected * project.sample_rate) / project.sample_rate
                if selected <= 0:
                    raise ValueError('An edited clip must retain at least one output sample.')
                placed = (round(cue.start * project.sample_rate) / project.sample_rate
                          if project.schema in (2, 3) else cue.start)
                duration = max(duration, placed + selected)
            floor = 1 if not scene.lines and not (project.schema in (2, 3) and scene.cues) else 1 / project.sample_rate
            seconds += max(duration, floor)
        pcm = math.ceil(max(1, seconds) * project.sample_rate * 2 * 2)
        roles = len(project.roles) if state['stems'] else 0
        clip_roles = len({cue.role for scene in project.scenes for cue in scene.cues}) if state['stems'] and project.schema in (2, 3) else 0
        music = int(any(scene.music is not None for scene in project.scenes))
        mastered = int(any(effect.enabled for effect in project.effects))
        # A processed master plus its sample-aligned chapter slices are two
        # additional PCM copies; role/clip/scene processing uses existing nodes.
        cache_bytes = pcm * (6 + 2 * roles + 2 * clip_roles + 2 * music + 2 * mastered) + runtime.budget.reserve_disk_bytes
        export_bytes = pcm * (2 + roles + clip_roles + music * int(state['stems']) + int(state['format'] != 'wav')) + 1024 ** 2
        request = ResourceRequest(cpu_slots=runtime.budget.threads, memory_bytes=memory,
            gpu_device=device, gpu_memory_bytes=capacity, disk_bytes=cache_bytes, disk_path=self.cache,
            additional_disks=((Path(state['destination']).parent, export_bytes),))
        return request, {'schema': 1, 'request': request.to_dict(), 'source_sha256': source_hashes,
            'estimated_duration': seconds,
            'cpu_basis': 'actual configured AudioRuntime worker thread count',
            'memory_basis': ('exclusive configured RAM/GPU pool; model peak is not measured' if neural else
                             'declared estimate: at least 512 MiB and twice the FFmpeg per-allocation budget'),
            'disk_basis': 'declared estimate from 0.5 seconds per source character, explicit pauses/durations, '
                          'measured cue source duration with trim/placement, PCM bytes and intermediate/stem/export copies; '
                          'not a measured speech duration or hard output cap',
            'enforcement': 'cooperative admission; existing bounded worker checks still apply'}

    @property
    def voices(self):
        self._check_open()
        if self._voices is None:
            from ..runtime_settings import voice_registry
            self._voices = voice_registry(self.root.parent)
        return self._voices

    def discover_voices(self, *, cancelled=lambda: False):
        """Discover on the caller's worker through the existing registry."""
        self._check_open()
        def stopping():
            return self._closed or bool(cancelled())
        if stopping():
            raise InterruptedError('Local voice discovery was cancelled.')
        if self._voices is None:
            from ..runtime_settings import voice_registry
            registry = voice_registry(self.root.parent, cancelled=stopping)
            self._voices = registry
            if stopping():
                try:
                    registry.close()
                except BaseException:
                    # Refuse new use but retain this owner for a close retry.
                    self._closed = True
                    raise
                self._voices = None
                raise InterruptedError('Local voice discovery was cancelled.')
        if stopping():
            raise InterruptedError('Local voice discovery was cancelled.')
        return self._voices

    def _check_open(self):
        if self._closed:
            raise RuntimeError('The production service is closed.')

    def close(self):
        """Release a lazily owned registry after the caller's worker finishes."""
        if self._closed and self._voices is None:
            return
        self._closed = True
        voices = self._voices
        if self._owns_voices and voices is not None:
            close = getattr(voices, 'close', None)
            if callable(close):
                # Keep the exact owner available to its worker finalizer if
                # native release fails. New production stays closed meanwhile.
                close()
        self._voices = None

    def create(self, text, *, title='Audio production', voice_ids=(), mode='faithful'):
        self._check_open()
        project = parse_script(text, title=title, voice_ids=voice_ids, mode=mode)
        self.projects.save(project)
        return project

    def submit(self, project, destination, *, format='wav', stems=True, operation_id=None):
        self._check_open()
        if format not in ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma'):
            raise ValueError('Choose a supported local audio output format.')
        if type(stems) is not bool or not isinstance(project, Project):
            raise ValueError('Invalid production options.')
        job_id = _id(operation_id) if operation_id else uuid4().hex
        directory = self.jobs_root / job_id
        existing_path = directory / 'job.json'

        def existing_receipt():
            try:
                existing_path.lstat()
            except FileNotFoundError:
                return None
            previous = self.get(job_id)
            # An old absolute destination is inert after restore, even when this
            # is an otherwise idempotent submission of a completed job.
            self._require_origin(previous)
            return previous

        # Keep the retained-input origin check before any output-path access.
        previous = existing_receipt()
        if any(not role.voice_id for role in project.roles if any(
                line.role == role.id and line.text for scene in project.scenes for line in scene.lines)):
            raise ValueError('Assign an installed voice to every speaking role before rendering.')
        speaking = {line.role for scene in project.scenes for line in scene.lines if line.text}
        if speaking:
            available = {voice.id for voice in self.voices.voices()}
            if any(role.voice_id not in available for role in project.roles if role.id in speaking):
                raise ValueError('A selected local voice is no longer available. Reassign the role.')
        destination = local_path(destination, exists=True)
        if not destination.is_dir():
            raise ValueError('Choose an existing local output directory.')
        # Projects and queues remain private; output never replaces their state.
        if destination.is_relative_to(self.root):
            raise ValueError('Choose an output directory outside the private production state.')
        output = destination / f'{_output_name(project.title)}-{job_id[:12]}'

        def already_submitted(previous):
            if previous is None:
                return False
            if (previous['fingerprint'] != project.fingerprint or previous['destination'] != str(output)
                    or previous['format'] != format or previous['stems'] != stems):
                raise ValueError('This operation ID is already bound to a different production plan.')
            return True

        # A committed receipt keeps its identity even while the renderer runs.
        if already_submitted(previous):
            return job_id
        # Reuse the existing render lock for interrupted receipt publication.
        with state_lock(directory, 'render'):
            if already_submitted(existing_receipt()):
                return job_id
            for entry in directory.iterdir():
                if entry.name != '.render.lock' and not entry.name.startswith('.pending-'):
                    raise ValueError('Production job has recovery data but no receipt; review it before resubmitting.')
                regular(entry)
            try:
                output.lstat()
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError('Production output already exists without a matching job receipt.')
            self.projects.save(project)
            state = {'schema': 1, 'id': job_id, 'status': 'pending', 'project': project.to_dict(),
                     'fingerprint': project.fingerprint, 'destination': str(output), 'format': format,
                     'stems': stems, 'nodes': {}, 'artifacts': [], 'progress': '',
                     'created': time.time(), 'updated': time.time(), 'error': '', 'runtime': None}
            if project.schema in (2, 3):
                state['origin'] = self._local_origin()
            atomic_json(existing_path, state)
        return job_id

    def get(self, job_id):
        state = read_json(self.jobs_root / _id(job_id) / 'job.json')
        if state.get('schema') != 1 or state.get('id') != job_id:
            raise ValueError('Invalid production job.')
        project = Project.from_dict(state['project'])
        if state.get('fingerprint') != project.fingerprint:
            raise ValueError('The saved production plan changed.')
        if project.schema in (2, 3):
            recovery = dict(state.get('recovery', {}))
            recovery['requires_output_directory'] = state.get('origin') != self._local_origin()
            state['recovery'] = recovery
        return state

    def _local_origin(self):
        """A fresh local admission identity, deliberately excluded from backup."""
        path = self.root / '.local-origin.json'
        with state_lock(self.root, 'origin'):
            if not path.exists():
                atomic_json(path, {'schema': 'carr-studio-origin/1',
                                   'nonce': uuid4().hex + uuid4().hex})
            value = read_json(path)
        if (not isinstance(value, dict) or set(value) != {'schema', 'nonce'}
                or value.get('schema') != 'carr-studio-origin/1'
                or not isinstance(value.get('nonce'), str)
                or re.fullmatch(r'[0-9a-f]{64}', value['nonce']) is None):
            raise ValueError('The local Studio origin identity is invalid.')
        return {'studio_root': str(self.root), 'nonce': value['nonce']}

    def _require_origin(self, state):
        if state['project'].get('schema') in (2, 3) and state.get('origin') != self._local_origin():
            raise ValueError('This restored production needs a newly selected output directory. '
                             'Approve it before running or inspecting old output files.')

    def relocate_restored_job(self, job_id, destination):
        """Approve a fresh output for portable input bytes; never open old paths."""
        self._check_open()
        job_root = self.jobs_root / _id(job_id)
        with state_lock(job_root, 'render'):
            state = self.get(job_id)
            project = Project.from_dict(state['project'])
            if project.schema not in (2, 3) or not state.get('recovery', {}).get('requires_output_directory'):
                raise ValueError('Only a restored portable production needs output relocation.')
            parent = local_path(destination, exists=True)
            if not parent.is_dir() or parent.is_relative_to(self.root):
                raise ValueError('Choose an existing output directory outside the private production state.')
            output = parent / f'{_output_name(project.title)}-{job_id[:12]}-restored-{uuid4().hex[:12]}'
            if output.exists():
                raise FileExistsError('Choose a fresh output directory for the restored production.')
            recovery = dict(state.get('recovery', {}))
            previous = list(recovery.get('previous', []))
            previous.append({name: state.get(name) for name in (
                'origin', 'destination', 'status', 'nodes', 'artifacts', 'runtime', 'result')})
            state.update(origin=self._local_origin(), destination=str(output), status='pending',
                         nodes={}, artifacts=[], runtime=None, progress='', error='')
            state.pop('result', None)
            state['recovery'] = {'requires_output_directory': False, 'previous': previous,
                                 'output_approved': time.time()}
            if self._broker is not None:
                # Runtime coordination is deliberately absent from a backup.
                # Only this explicit fresh-origin/output review may reopen a
                # previously cached coordinator after a same-path restore.
                # Require the retained current policy; never replace it with
                # defaults. for_app validates paths and retains existing locks.
                from ..operations import ResourceBroker
                broker = self._admission_broker()
                local_path(broker.policy_path, exists=True)
                # The old coordinator remains revoked after a same-path restore.
                # This explicit review admits a fresh coordinator, whose locked
                # constructor validates the retained policy and current namespace.
                self._broker = ResourceBroker.for_app(
                    broker.root.parent.parent, require_existing_policy=True)
            self._save(state)
            return state

    def jobs(self):
        return tuple(self.get(path.parent.name) for path in sorted(self.jobs_root.glob('*/job.json')))

    def _save(self, state):
        state['updated'] = time.time()
        atomic_json(self.jobs_root / state['id'] / 'job.json', state, replace=True)

    def request_pause(self, job_id):
        self.get(job_id)
        # Mutable controls use last-write replacement from their first write.
        # A create-only POSIX link can be visible before its staged link is gone.
        atomic_json(self.jobs_root / job_id / 'control.json', {'action': 'pause'}, replace=True)

    def cancel(self, job_id):
        self.get(job_id)
        atomic_json(self.jobs_root / job_id / 'control.json', {'action': 'cancel'}, replace=True)

    def _runtime(self):
        if self.runtime_factory is not None:
            return self.runtime_factory(self.cache, input_roots=(self.cache, self.inputs, *self.input_roots))
        from ..runtime_settings import audio_runtime
        return audio_runtime(self.cache, state_root=self.root.parent,
                             input_roots=(self.cache, self.inputs, *self.input_roots))

    def _cue_source(self, cue, *, cancelled=lambda: False, schema=2):
        if schema in (2, 3):
            return resolve_managed_input(self.root, cue.path, cancelled=cancelled)
        if schema != 1:
            raise ValueError('Unsupported cue input schema.')
        source = local_path(cue.path, exists=True)
        if not any(source.is_relative_to(root) for root in self.input_roots):
            raise ValueError('A cue is outside the explicitly selected input directories.')
        return source

    def run(self, job_id, *, cancelled=lambda: False, progress=None, source_guard=None):
        self._check_open()
        from ..audio import Track, MusicSpec, AudioError, AudioCancelled
        from ..speech import SpeechSettings
        job_root = self.jobs_root / _id(job_id)
        with state_lock(job_root, 'render'), ExitStack() as reservations:
            state = self.get(job_id)
            self._require_origin(state)
            project = Project.from_dict(state['project'])
            bound_source = any(line.source_ref.startswith('carr-book-audio:')
                               for scene in project.scenes for line in scene.lines)
            if source_guard is not None and not callable(source_guard):
                raise ValueError('The source guard must be callable.')
            if bound_source and source_guard is None:
                raise ValueError('Open this source-bound job from Book audio to recheck its original source.')
            source_provenance = None

            def check_source():
                nonlocal source_provenance
                if source_guard is None:
                    return
                if self._closed or cancelled():
                    raise InterruptedError('Production stopped before source publication.')
                value = source_guard()
                if bound_source:
                    from .book_audio import validate_provenance
                    validate_provenance(project, value)
                    raw = encode(value)
                    if source_provenance is not None and raw != source_provenance:
                        raise ValueError('The reviewed book source provenance changed.')
                    source_provenance = raw
                if self._closed or cancelled():
                    raise InterruptedError('Production stopped during source verification.')

            check_source()
            if state['status'] == 'complete':
                self._verify_artifacts(state, cancelled=cancelled)
                if bound_source and encode(read_json(Path(state['destination']) / 'book-source.json')) != source_provenance:
                    raise ValueError('The published book source provenance differs from its reviewed plan.')
                check_source()
                return state
            control = job_root / 'control.json'
            if control.exists():
                control.unlink()  # Explicit Run/Resume continues the saved job.
            caller_cancelled, observer = cancelled, progress

            def admission_cancelled():
                return self._closed or bool(caller_cancelled()) or control.exists()

            def notify_admission(message):
                nonlocal observer
                if observer is not None:
                    try:
                        observer(message)
                    except Exception:
                        observer = None

            try:
                from ..operations import ResourceRequest
                self._admission_broker(admission_cancelled)
                sources = [self._cue_source(cue, cancelled=admission_cancelled, schema=project.schema)
                           for scene in project.scenes for cue in scene.cues]
                runtime = self._runtime()
                state['status'], state['progress'], state['error'] = 'queued', 'Waiting for shared local resources.', ''
                self._save(state)
                notify_admission(state['progress'])
                # FFprobe is itself an owned bounded child. Reserve inspection
                # before measuring clips, then atomically admit both output
                # volumes without holding a partial render reservation.
                if sources:
                    inspection = ResourceRequest(cpu_slots=runtime.budget.threads,
                        memory_bytes=max(512 * 1024**2, 2 * runtime.budget.max_alloc_bytes))
                    with self.broker.reserve(inspection, operation_id='studio:' + job_id,
                            cancelled=admission_cancelled, timeout=60):
                        request, resource_plan = self._resource_plan(project, state, runtime,
                            cancelled=admission_cancelled)
                else:
                    request, resource_plan = self._resource_plan(project, state, runtime,
                        cancelled=admission_cancelled)
                state['resource_plan'] = resource_plan
                self._save(state)
                lease = reservations.enter_context(self.broker.reserve(request,
                    operation_id='studio:' + job_id, cancelled=admission_cancelled, timeout=60))
                current = self.get(job_id)
                self._require_origin(current)
                if any(current.get(key) != state.get(key) for key in
                       ('project', 'fingerprint', 'destination', 'format', 'stems', 'origin')):
                    raise ValueError('The production plan or output destination changed while waiting.')
                self._admission_broker(admission_cancelled)
                check_source()
                local_path(Path(state['destination']).parent, exists=True)
                for scene in project.scenes:
                    for cue in scene.cues:
                        source = self._cue_source(cue, cancelled=admission_cancelled, schema=project.schema)
                        if self._resource_source_identity(source, admission_cancelled) != resource_plan['source_sha256'].get(str(source)):
                            raise ValueError('A selected production input changed while waiting for resources.')
                lease.checkpoint()
                cancelled = lease.cancelled
                capabilities = (runtime.capabilities(cancelled=admission_cancelled)
                                if project.schema == 3 else runtime.capabilities())
                if project.schema == 3:
                    required_effects = {effect.kind for chain in (
                        project.effects, *(role.effects for role in project.roles),
                        *(scene.effects for scene in project.scenes),
                        *(cue.effects for scene in project.scenes for cue in scene.cues))
                        for effect in chain if effect.enabled}
                    if not required_effects <= set(capabilities['effects']):
                        raise ValueError('The installed audio runtime lacks a selected Studio effect. '
                                         'Review or bypass that effect before rendering.')
            except BaseException as exc:
                from ..operations import ResourceCancelled, ResourceError
                state['status'] = 'attention' if isinstance(exc, ResourceError) else 'failed'
                if isinstance(exc, (ResourceCancelled, InterruptedError, AudioCancelled)):
                    try:
                        action = read_json(control).get('action') if control.exists() else None
                    except (ValueError, OSError):
                        action = None
                    state['status'] = 'cancelled' if action == 'cancel' else 'paused'
                state['error'] = str(exc)[:2000]
                self._save(state)
                if isinstance(exc, ResourceCancelled):
                    raise InterruptedError('Production paused or cancelled while waiting for resources.') from exc
                raise
            # Engine inventory is part of the node identity. A different installed
            # FFmpeg/voice version must not silently reuse incompatible work.
            runtime_identity = sha256(encode(capabilities)).hexdigest()
            state['runtime'] = capabilities
            state['total_disk_bytes'] = self.total_disk_bytes
            state['status'], state['error'] = 'running', ''
            self._save(state)

            def requested_stop():
                return bool(cancelled()) or control.exists()

            last_disk_check, retained = 0.0, 0

            def capacity(additional=0, *, force=False, honor_cancel=True):
                nonlocal last_disk_check, retained
                now = time.monotonic()
                if force or now - last_disk_check >= .2:
                    retained = _retained_bytes((self.root, Path(state['destination'])),
                                               requested_stop if honor_cancel else lambda: False)
                    last_disk_check = now
                if retained + additional > self.total_disk_bytes:
                    raise AudioError('The total production disk budget would be exceeded. '
                                     'Increase total_disk_bytes to resume; completed files are retained.')
                return self.total_disk_bytes - retained - additional

            def stopping():
                if requested_stop():
                    return True
                capacity()
                return False

            def check():
                if stopping():
                    raise InterruptedError('Production paused or cancelled; completed chunks are retained.')
                check_source()

            def report(message):
                check_source()
                state['progress'] = message
                self._save(state)
                notify_admission(message)
                check_source()

            def generate(path, operation):
                remaining = capacity(65536, force=True)
                if remaining < 4096:
                    raise AudioError('The total production disk budget has no room for another audio step.')
                previous_budget = runtime.budget
                runtime.budget = replace(previous_budget,
                    max_output_bytes=min(previous_budget.max_output_bytes, remaining))
                try:
                    operation(path)
                    capacity(force=True, honor_cancel=False)
                except AudioError:
                    # Only the unaccepted UUID attempt belongs to this cleanup.
                    path.unlink(missing_ok=True)
                    raise
                finally:
                    runtime.budget = previous_budget

            def node(kind, identity, operation):
                check()
                key = sha256(encode({'engine': ENGINE_SCHEMA, 'runtime': runtime_identity,
                                     'kind': kind, 'input': identity})).hexdigest()
                receipt_path = self.cache / (key + '.json')
                try:
                    receipt = read_json(receipt_path) if receipt_path.exists() else None
                except (ValueError, UnicodeError):
                    receipt = None
                if not isinstance(receipt, dict):
                    receipt = None
                if receipt and receipt.get('schema') == 1 and receipt.get('key') == key:
                    candidate = local_path(self.cache / receipt['file'], root=self.cache)
                    if candidate.is_file() and digest_file(candidate) == receipt.get('sha256'):
                        info = runtime.inspect(candidate)
                        state['nodes'][key] = {'kind': kind, 'sha256': receipt['sha256'], 'reused': True}
                        self._save(state)
                        return candidate, info
                report(f'{kind}: rendering local audio')
                # Each attempt gets a fresh path, including recovery from corrupt cache bytes.
                path = self.cache / (key + '-' + uuid4().hex[:10] + '.wav')
                generate(path, operation)
                # The provider has atomically completed a real output. Verify
                # and receipt it before honoring cancellation of the next step,
                # so resume cannot silently repeat accepted expensive work.
                info = runtime.inspect(path)
                if info.duration <= 0 or info.channels not in (1, 2):
                    raise ValueError('The audio engine returned an empty or unsupported output.')
                digest = digest_file(path)
                atomic_json(receipt_path, {'schema': 1, 'key': key, 'file': path.name,
                            'sha256': digest, 'duration': info.duration, 'sample_rate': info.sample_rate,
                            'channels': info.channels}, replace=receipt_path.exists())
                state['nodes'][key] = {'kind': kind, 'sha256': digest, 'reused': False}
                self._save(state)
                check()
                return path, info

            def silence(duration):
                duration = max(1 / project.sample_rate, duration)
                return node('silence', [duration, project.sample_rate],
                            lambda target: runtime.mix((), target, duration=duration,
                                sample_rate=project.sample_rate, cancelled=stopping))

            roles = {role.id: role for role in project.roles}
            speaking = any(line.text for scene in project.scenes for line in scene.lines)
            voices = {voice.id: voice for voice in self.voices.voices()} if speaking else {}
            timeline, scene_results, stem_scenes, chapter_data = [], [], {}, []
            clip_timeline, clip_stems = [], {}
            offset = 0.0
            try:
                for scene_index, scene in enumerate(project.scenes, 1):
                    report(f'Scene {scene_index} of {len(project.scenes)}: {scene.title}')
                    clips, clip_roles, scene_lines, cursor = [], [], [], 0.0
                    for line in scene.lines:
                        check()
                        role = roles[line.role]
                        if line.text:
                            voice = voices[role.voice_id]
                            voice_data = asdict(voice) if hasattr(voice, '__dataclass_fields__') else vars(voice)
                            # Only JSON-safe scalar metadata; providers never put secrets here.
                            voice_identity = json_safe(voice_data)
                            fingerprint = getattr(self.voices, 'fingerprint', None)
                            if callable(fingerprint):
                                voice_identity['runtime_fingerprint'] = json_safe(fingerprint(role.voice_id))
                            settings = SpeechSettings(rate=role.rate, volume=role.volume)
                            speech, info = node('speech', [line.text, voice_identity, asdict(settings)],
                                lambda target, line=line, role=role, settings=settings:
                                self.voices.synthesize(line.text, role.voice_id, target,
                                    settings=settings, cancelled=stopping))
                            duration = round((info.duration + line.pause_after) * project.sample_rate) / project.sample_rate
                            placement_identity = [digest_file(speech), duration,
                                                  role.pan, role.gain_db, project.sample_rate]
                            if role.effects:
                                placement_identity.append([effect.to_dict() for effect in role.effects])
                            clip, clip_info = node('voice placement', placement_identity,
                                lambda target, speech=speech, role=role, duration=duration:
                                runtime.mix((Track(speech, gain_db=role.gain_db, pan=role.pan,
                                                   effects=role.effects),), target,
                                            duration=duration, sample_rate=project.sample_rate, cancelled=stopping))
                            scene_lines.append({'scene': scene.id, 'line': line.id, 'role': role.id,
                                'text': line.text, 'start': offset + cursor, 'end': offset + cursor + info.duration,
                                'timing': 'measured line audio; word timings unavailable', 'source_ref': line.source_ref})
                        else:
                            clip, clip_info = silence(line.pause_after)
                        clips.append(clip)
                        clip_roles.append((role.id, clip_info.duration))
                        cursor += clip_info.duration
                    required_duration = scene.minimum_duration
                    cue_tracks = []
                    for cue in scene.cues:
                        source = self._cue_source(cue, cancelled=stopping, schema=project.schema)
                        info = runtime.inspect(source, cancelled=stopping)
                        available = info.duration - cue.trim_start
                        selected = available if cue.duration is None else cue.duration
                        if available <= 0 or selected > available + 1 / info.sample_rate:
                            raise ValueError('An edited clip selection is outside its original audio.')
                        if max(cue.fade_in, cue.fade_out) > selected:
                            raise ValueError('An edited clip fade exceeds its selected audio.')
                        if project.schema in (2, 3):
                            selected = round(selected * project.sample_rate) / project.sample_rate
                        if selected <= 0:
                            raise ValueError('An edited clip must retain at least one output sample.')
                        placed = (round(cue.start * project.sample_rate) / project.sample_rate
                                  if project.schema in (2, 3) else cue.start)
                        required_duration = max(required_duration, placed + selected)
                        cue_tracks.append(Track(source, cue.start, cue.gain_db, cue.pan, cue.role,
                                                cue.trim_start, cue.duration, cue.fade_in, cue.fade_out,
                                                effects=cue.effects))
                        if project.schema in (2, 3):
                            clip_timeline.append({'scene': scene.id, 'id': cue.id, 'role': cue.role,
                                'title': cue.title, 'source_sha256': digest_file(source),
                                'source_duration': info.duration, 'trim_start': cue.trim_start,
                                'duration': selected, 'start': offset + placed,
                                'end': offset + placed + selected, 'gain_db': cue.gain_db,
                                'pan': cue.pan, 'fade_in': cue.fade_in, 'fade_out': cue.fade_out})
                    if not clips:
                        duration = (max(required_duration, 1 / project.sample_rate)
                                    if project.schema in (2, 3) and cue_tracks else max(scene.minimum_duration, 1))
                        clip, info = silence(duration)
                        clips, clip_roles, cursor = [clip], [('', info.duration)], info.duration
                    if required_duration > cursor + .5 / project.sample_rate:
                        clip, info = silence(required_duration - cursor)
                        clips.append(clip)
                        clip_roles.append(('', info.duration))
                        cursor += info.duration
                    voice_scene, voice_info = node('voice scene', [digest_file(p) for p in clips],
                        lambda target: runtime.concatenate(clips, target, sample_rate=project.sample_rate,
                                                           cancelled=stopping))
                    tracks = [Track(voice_scene, routing='stereo')]
                    if scene.music is not None:
                        spec = MusicSpec(duration=voice_info.duration, mood=scene.music.mood,
                                         tempo=scene.music.tempo, seed=scene.music.seed, engine=scene.music.engine,
                                         fade_in=min(.15, voice_info.duration), fade_out=min(.4, voice_info.duration))
                        identity = music_installation_identity(runtime, spec.engine)
                        # Chunk scheduling is durable and bounded for optional neural engines.
                        # Separate variations preserve requested duration, without representing
                        # independent inference as coherent full-length model continuation.
                        maximum = (runtime.music_models[spec.engine][0].max_chunk_seconds
                                   if spec.engine in ('musicgen', 'ace-step') else spec.duration)
                        score_parts = []
                        for segment in music_segments(spec, project.sample_rate, maximum):
                            part, _ = node('instrumental score', {'spec': asdict(segment), 'installation': identity},
                                lambda target, segment=segment: runtime.render_music(segment, target,
                                    sample_rate=project.sample_rate, cancelled=stopping))
                            score_parts.append(part)
                        music = score_parts[0] if len(score_parts) == 1 else node('score assembly',
                            [digest_file(path) for path in score_parts],
                            lambda target: runtime.concatenate(score_parts, target,
                                sample_rate=project.sample_rate, cancelled=stopping))[0]
                        tracks.append(Track(music, gain_db=scene.music.gain_db, role='music', routing='stereo'))
                    tracks.extend(cue_tracks)
                    scene_identity = {'tracks': [track_identity(t) for t in tracks],
                                      'sample_rate': project.sample_rate}
                    if scene.effects:
                        scene_identity['effects'] = [effect.to_dict() for effect in scene.effects]
                    final_scene, final_info = node('scene mix', scene_identity,
                        lambda target: runtime.mix(tracks, target, sample_rate=project.sample_rate,
                                                   duration=voice_info.duration, effects=scene.effects,
                                                   cancelled=stopping))
                    scene_results.append((scene, final_scene, final_info))
                    if state['stems']:
                        for role_id in roles:
                            stem_clips = [clip if belongs == role_id else silence(duration)[0]
                                          for clip, (belongs, duration) in zip(clips, clip_roles)]
                            stem, _ = node('speaker stem', [digest_file(p) for p in stem_clips],
                                lambda target, stem_clips=stem_clips:
                                runtime.concatenate(stem_clips, target, sample_rate=project.sample_rate,
                                                    cancelled=stopping))
                            stem_scenes.setdefault(role_id, []).append(stem)
                        if scene.music is not None:
                            # This is the actual pre-duck score; final scene contains measured sidechain ducking.
                            stem_scenes.setdefault('music', []).append((scene.id, music, final_info.duration))
                        if project.schema in (2, 3):
                            for role in sorted({track.role for track in cue_tracks}):
                                selected_tracks = tuple(track for track in cue_tracks if track.role == role)
                                stem, _ = node('scene clip stem',
                                    {'tracks': [track_identity(track) for track in selected_tracks],
                                     'duration': final_info.duration, 'sample_rate': project.sample_rate},
                                    lambda target, selected_tracks=selected_tracks: runtime.mix(
                                        selected_tracks, target, duration=final_info.duration,
                                        sample_rate=project.sample_rate, cancelled=stopping))
                                clip_stems.setdefault(role, {})[scene.id] = stem
                    timeline.extend(scene_lines)
                    chapter_data.append({'id': scene.id, 'title': scene.title, 'start': offset,
                                         'end': offset + final_info.duration})
                    offset += final_info.duration
                master, master_info = node('master assembly', [digest_file(row[1]) for row in scene_results],
                    lambda target: runtime.concatenate([row[1] for row in scene_results], target,
                        sample_rate=project.sample_rate, cancelled=stopping))
                if any(effect.enabled for effect in project.effects):
                    unprocessed_master = master
                    master, master_info = node('master effects',
                        {'source': digest_file(unprocessed_master), 'sample_rate': project.sample_rate,
                         'effects': [effect.to_dict() for effect in project.effects]},
                        lambda target: runtime.mix((Track(unprocessed_master, routing='stereo'),),
                            target, duration=master_info.duration, sample_rate=project.sample_rate,
                            effects=project.effects, cancelled=stopping))
                # Revalidate local admission and retained originals before any
                # external output path is resolved or created.
                check_source()
                self._require_origin(state)
                for relative in project_managed_inputs(project):
                    resolve_managed_input(self.root, relative, cancelled=stopping)
                output = local_path(state['destination'])
                marker = output / 'production-owner.json'
                if output.exists():
                    if not marker.exists() or read_json(marker) != {'job': state['id'], 'fingerprint': state['fingerprint']}:
                        raise FileExistsError('The output directory is occupied by another project.')
                else:
                    output.mkdir(mode=0o700)
                    atomic_json(marker, {'job': state['id'], 'fingerprint': state['fingerprint']})

                def publish(source, name):
                    check()
                    check_source()
                    target = local_path(output / name, root=output)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    identity = digest_file(source)
                    # A durable intent makes publication retryable after a process crash.
                    artifact = {'path': str(target), 'sha256': identity, 'size': Path(source).stat().st_size}
                    existing = next((a for a in state['artifacts'] if a['path'] == str(target)), None)
                    if existing is not None and existing != artifact:
                        raise ValueError('Previously published output identity changed.')
                    if existing is None:
                        state['artifacts'].append(artifact)
                        self._save(state)
                    if target.exists():
                        if digest_file(target) != identity:
                            raise FileExistsError('An output was changed externally; it will not be overwritten.')
                    else:
                        def reserve_copy(size):
                            # Publication may use a different volume from the
                            # render cache. Preserve the same reserve there,
                            # including bounded filesystem metadata overhead.
                            required = size + runtime.budget.reserve_disk_bytes + 65536
                            if shutil.disk_usage(target.parent).free < required:
                                raise AudioError('Not enough output disk space to publish while preserving the configured reserve.')
                            capacity(size + 65536, force=True)

                        reserve_copy(artifact['size'])
                        temporary = target.parent / ('.pending-' + uuid4().hex)
                        try:
                            with Path(source).open('rb') as read, temporary.open('xb') as write:
                                while block := read.read(1024 * 1024):
                                    check()
                                    reserve_copy(len(block))
                                    write.write(block)
                                write.flush()
                                os.fsync(write.fileno())
                            if digest_file(temporary) != identity:
                                raise ValueError('Audio changed during publication.')
                            # Hashing and flushing large outputs can take time.
                            # Stop before publication, never after the link commits.
                            check_source()
                            check()
                            os.link(temporary, target)
                        finally:
                            temporary.unlink(missing_ok=True)
                    return target

                publish(master, 'master.wav')
                for index, (scene, path, info) in enumerate(scene_results, 1):
                    if any(effect.enabled for effect in project.effects):
                        chapter = chapter_data[index - 1]
                        path, _ = node('mastered chapter',
                            {'source': digest_file(master), 'start': chapter['start'],
                             'end': chapter['end'], 'sample_rate': project.sample_rate},
                            lambda target, chapter=chapter: runtime.convert(master, target,
                                start=chapter['start'], duration=chapter['end'] - chapter['start'],
                                sample_rate=project.sample_rate, cancelled=stopping))
                    publish(path, f'chapters/{index:04d}-{_output_name(scene.title)}.wav')
                if state['format'] != 'wav':
                    # Codec selection is delegated to the verified local runtime inventory.
                    encoding_identity = [digest_file(master), state['format'], runtime_identity]
                    if state['format'] == 'm4b':
                        encoding_identity.append({'schema': 'measured-m4b-chapters/1',
                            'title': project.title, 'chapters': chapter_data})
                    key = sha256(encode(encoding_identity)).hexdigest()
                    encoded = self.cache / (key + '.' + state['format'])
                    receipt = self.cache / (key + '-encoded.json')
                    try:
                        cached = read_json(receipt) if receipt.exists() else None
                    except (ValueError, UnicodeError):
                        cached = None
                    if not isinstance(cached, dict):
                        cached = None
                    if cached and cached.get('file'):
                        encoded = local_path(self.cache / cached['file'], root=self.cache)
                    if not (cached and encoded.exists() and digest_file(encoded) == cached.get('sha256')):
                        if encoded.exists():
                            encoded = self.cache / (key + '-' + uuid4().hex[:10] + '.' + state['format'])
                        if state['format'] == 'm4b':
                            generate(encoded, lambda target: runtime.audiobook(master, target,
                                chapters=chapter_data, title=project.title,
                                sample_rate=project.sample_rate, cancelled=stopping))
                        else:
                            generate(encoded, lambda target: runtime.convert(master, target,
                                sample_rate=project.sample_rate, cancelled=stopping))
                        atomic_json(receipt, {'sha256': digest_file(encoded), 'file': encoded.name}, replace=receipt.exists())
                    publish(encoded, 'audiobook.' + state['format'])
                for role_id, paths in stem_scenes.items():
                    if role_id == 'music':
                        mapping = {scene_id: path for scene_id, path, _ in paths}
                        paths = [mapping.get(scene.id) or silence(info.duration)[0] for scene, _, info in scene_results]
                    stem, _ = node('complete stem', [digest_file(p) for p in paths],
                        lambda target, paths=paths: runtime.concatenate(paths, target,
                            sample_rate=project.sample_rate, cancelled=stopping))
                    publish(stem, 'stems/' + role_id + '.wav')
                for role, mapping in clip_stems.items():
                    paths = [mapping.get(scene.id) or silence(info.duration)[0]
                             for scene, _, info in scene_results]
                    stem, _ = node('complete clip stem', [digest_file(path) for path in paths],
                        lambda target, paths=paths: runtime.concatenate(paths, target,
                            sample_rate=project.sample_rate, cancelled=stopping))
                    publish(stem, 'stems/clips/' + role + '.wav')
                subtitles = ['WEBVTT\n']
                srt = []
                for index, line in enumerate(timeline, 1):
                    subtitles.append(f'{index}\n{_stamp(line["start"])} --> {_stamp(line["end"])}\n{line["text"]}\n')
                    srt.append(f'{index}\n{_stamp(line["start"], comma=True)} --> {_stamp(line["end"], comma=True)}\n{line["text"]}\n')
                report('Measuring the actual completed master')
                meter = runtime.measure(master, cancelled=stopping)
                metadata = {'schema': 1, 'project': project.id, 'revision': project.revision,
                            'source_sha256': sha256(project.original_script.encode('utf-8')).hexdigest(),
                            'chapters': chapter_data, 'lines': timeline, 'duration': master_info.duration,
                            'sample_rate': master_info.sample_rate, 'channels': master_info.channels,
                            'mode': project.mode, 'local_only': True,
                            'loudness': meter.to_dict(),
                            'music_kind': 'procedural/MIDI/local-model as explicitly selected per scene',
                            'music_continuity': 'Neural segments are separate seeded variations with boundary fades; '
                                                'continuous neural composition and perceptually seamless joins are not assumed.',
                            'timing_notice': 'Line-level alignment from actual synthesized durations; no word timing claim.',
                            'lrc_notice': 'LRC uses measured line starts rounded to centiseconds; no word or internal paragraph timing. '
                                          'Literal leading bracket text has a U+2060 boundary to prevent extra LRC time tags.'}
                if project.schema in (2, 3):
                    metadata['clips'] = clip_timeline
                    metadata['clip_stems_notice'] = ('Clip stems align to the full master timeline. '
                        'Music stems are before ducking; recorded clips have no inferred transcript.')
                if project.schema == 3:
                    metadata['effects'] = project_effects(project)
                if source_provenance is not None:
                    metadata['book_source_sha256'] = sha256(source_provenance).hexdigest()
                from ..audio.audiobook import line_lrc
                for name, raw in [('script.lrc', line_lrc(timeline).encode('utf-8')),
                                  ('script.vtt', '\n'.join(subtitles).encode('utf-8')),
                                  ('script.srt', '\n'.join(srt).encode('utf-8')),
                                  ('timeline.json', encode(metadata)),
                                  ('project.json', encode(project.to_dict()))]:
                    source = job_root / name
                    atomic_bytes(source, raw, replace=source.exists())
                    publish(source, name)
                if source_provenance is not None:
                    source = job_root / 'book-source.json'
                    atomic_bytes(source, source_provenance, replace=source.exists())
                    publish(source, 'book-source.json')
                check_source()
                state['result'] = metadata
                state['status'], state['progress'] = 'complete', 'Audio files and checksums verified.'
                self._verify_artifacts(state, cancelled=stopping)
                check_source()
                self._save(state)
                return state
            except BaseException as exc:
                # A stop request is not acknowledgement of cancellation. Keep
                # a real render/publication error even if Pause arrived at the
                # same time; only the runtimes' explicit interruption types can
                # safely describe a resumable pause or cancellation.
                state['status'] = 'failed'
                if isinstance(exc, (InterruptedError, AudioCancelled)):
                    try:
                        action = read_json(control).get('action') if control.exists() else None
                    except (ValueError, OSError):
                        action = None
                    state['status'] = 'cancelled' if action == 'cancel' else 'paused'
                state['error'] = str(exc)[:2000]
                self._save(state)
                raise

    def _verify_artifacts(self, state, *, cancelled=lambda: False):
        if cancelled():
            raise InterruptedError('Production output verification cancelled.')
        root = local_path(state['destination'], exists=True)
        for artifact in state['artifacts']:
            path = local_path(artifact['path'], root=root, exists=True)
            if path.stat().st_size != artifact['size'] or digest_file(path, cancelled=cancelled) != artifact['sha256']:
                raise ValueError('A rendered output is missing or its checksum changed.')

    def assistant_callback(self, arguments, output_directory, cancelled, progress):
        if set(arguments) - {'script', 'text', 'title', 'format', 'stems', 'voice_ids', 'operation_id'}:
            raise ValueError('Unsupported production argument.')
        if 'script' in arguments:
            project = Project.from_dict(arguments['script'])
        else:
            project = parse_script(arguments.get('text', ''), title=arguments.get('title', 'Audio production'),
                                   voice_ids=arguments.get('voice_ids', ()),
                                   project_id=arguments.get('operation_id'))
        job = self.submit(project, output_directory, format=arguments.get('format', 'wav'),
                          stems=arguments.get('stems', True), operation_id=arguments.get('operation_id'))
        result = self.run(job, cancelled=cancelled, progress=progress)
        return {'job_id': job, 'status': result['status'],
                'artifacts': [item['path'] for item in result['artifacts']]}


def _retained_bytes(roots, cancelled):
    """Count disjoint private/output trees without reading waveforms or links."""
    pending, total = list(roots), 0
    while pending:
        if cancelled():
            raise InterruptedError('Production disk accounting cancelled.')
        path = pending.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Production disk accounting cannot follow links or reparse points.')
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
        else:
            raise ValueError('Production state contains an unsupported special file.')
    return total


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [json_safe(x) for x in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def track_identity(track):
    return {'sha256': digest_file(track.path), 'start': track.start, 'gain_db': track.gain_db,
            'pan': track.pan, 'role': track.role, 'routing': track.routing,
            'trim_start': track.trim_start, 'duration': track.duration,
            'fade_in': track.fade_in, 'fade_out': track.fade_out,
            'effects': [effect.to_dict() for effect in track.effects]}
