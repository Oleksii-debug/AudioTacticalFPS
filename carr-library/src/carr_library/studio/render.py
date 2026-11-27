"""Persistent production DAG: actual local voices, music, scene mixes and exports.

Every accepted node is content-addressed and checksum-verified before reuse.
Source text is data. This module never accepts shell commands or provider URLs.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
from uuid import uuid4

from ..durable_state import atomic_bytes, atomic_json, digest_file, encode, local_path, read_json, regular, state_lock
from .project import Project, ProjectStore, parse_script, _id

ENGINE_SCHEMA = 'studio-local-dag-v1'


def production_runtime_identity(runtime, capabilities, *, cancelled=lambda: False):
    """Bind reusable nodes to executable and implementation bytes, not banners."""
    from ..audio import dsp, models, music, process
    from ..speech._io import digest
    modules = {'production': Path(__file__), 'audio': Path(dsp.__file__),
               'models': Path(models.__file__), 'music': Path(music.__file__),
               'process': Path(process.__file__),
               'windows_job': Path(process.__file__).with_name('windows_job.py'),
               'cleanup': Path(process.__file__).with_name('cleanup.py')}
    identity = {'capabilities': capabilities,
                'executables': {name: digest(getattr(runtime, name), cancelled=cancelled)
                                for name in ('ffmpeg', 'ffprobe')},
                'implementation': {name: digest(path, cancelled=cancelled)
                                   for name, path in modules.items()}}
    return sha256(encode(identity)).hexdigest()


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
    def __init__(self, directory, *, voices=None, runtime_factory=None, input_roots=(), broker=None):
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
        self._owns_voices = voices is None
        self._closed = threading.Event()
        self._voice_state = threading.Lock()
        self._voice_discovery = threading.Lock()
        self.runtime_factory = runtime_factory
        self.input_roots = (local_path(self.inputs, exists=True),
                            *(local_path(x, exists=True) for x in input_roots))
        self._broker = broker

    @property
    def broker(self):
        return self._admission_broker()

    def _admission_broker(self, cancelled=lambda: False):
        from ..operations import ResourceBroker, ResourceStateError
        app_root = (self.root.parent.parent if self.root.name == 'studio'
                    and self.root.parent.name == 'state' else self.root.parent)
        if self._broker is None:
            self._broker = ResourceBroker.for_app(app_root, cancelled=cancelled)
        if (self._broker.root != app_root / 'runtime' / 'resources'
                or self._broker.policy_path != app_root / 'state' / 'operations' / 'resources.json'):
            raise ResourceStateError('The production resource policy belongs to another data folder.')
        return self._broker

    @contextmanager
    def _audio_admission(self, runtime, *, operation_id, disk_bytes=0,
                         disk_path=None, cancelled=lambda: False):
        """Use the shared broker for bounded clip inspection, retention or preview."""
        from ..audio.models import AudioCancelled, AudioCleanupPending
        from ..operations import ResourceCancelled, ResourceRequest
        def stopping():
            return self._closed.is_set() or bool(cancelled())
        broker = self._admission_broker(stopping)
        request = ResourceRequest(cpu_slots=runtime.budget.threads,
            memory_bytes=max(512 * 1024**2, 2 * runtime.budget.max_alloc_bytes),
            disk_bytes=disk_bytes, disk_path=disk_path)
        with ExitStack() as reservations:
            try:
                lease = reservations.enter_context(broker.reserve(request,
                    operation_id=operation_id, cancelled=stopping, timeout=60))
                yield lease.cancelled
            except AudioCleanupPending as exc:
                retained = reservations.pop_all()
                exc.cleanup_owner.add_quiescent_callback(retained.close)
                raise
            except ResourceCancelled as exc:
                raise AudioCancelled('Audio operation cancelled while waiting for shared local resources.') from exc

    def _resource_plan(self, project, state, runtime, *, clip_info=None):
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
        # These are declared admission estimates, not RAM measurements or
        # claimed inference requirements. A neural project takes the whole
        # configured RAM/GPU pool because its peak has not been measured here.
        memory = limits.memory_bytes if neural else max(512 * 1024 ** 2, 2 * runtime.budget.max_alloc_bytes)
        if neural and memory == 0:
            raise ResourceUnavailable('Configure a positive declared memory budget for neural production.')
        clip_info = clip_info or {}
        seconds = 0
        for scene in project.scenes:
            duration = max(scene.minimum_duration,
                           sum(len(line.text) * .5 + line.pause_after for line in scene.lines))
            if not scene.lines and not scene.cues:
                duration = max(duration, 1)
            for cue in scene.cues:
                info = clip_info[cue.path]
                available = info.duration - cue.trim_start
                selected = available if cue.duration is None else cue.duration
                if available <= 0 or selected > available + 1 / info.sample_rate:
                    raise ValueError('An edited clip selection is outside its original audio.')
                if max(cue.fade_in, cue.fade_out) > selected:
                    raise ValueError('An edited clip fade exceeds its selected audio.')
                duration = max(duration, cue.start + selected)
            seconds += max(duration, 1 / project.sample_rate)
        pcm = math.ceil(max(1, seconds) * project.sample_rate * 2 * 2)
        roles = (len(project.roles) + (len({cue.role for scene in project.scenes for cue in scene.cues})
                                      if project.schema >= 2 else 0)) if state['stems'] else 0
        music = int(any(scene.music is not None for scene in project.scenes))
        cache_bytes = pcm * (6 + 2 * roles + 2 * music) + runtime.budget.reserve_disk_bytes
        export_bytes = pcm * (2 + roles + music * int(state['stems']) + int(state['format'] != 'wav')) + 1024 ** 2
        request = ResourceRequest(cpu_slots=runtime.budget.threads, memory_bytes=memory,
            gpu_device=device, gpu_memory_bytes=capacity, disk_bytes=cache_bytes, disk_path=self.cache,
            devices=('accelerator:cuda',) if device is not None and device.startswith('cuda:') else (),
            additional_disks=((Path(state['destination']).parent, export_bytes),))
        return request, {'schema': 1, 'request': request.to_dict(),
            'cpu_basis': 'actual configured AudioRuntime worker thread count',
            'memory_basis': ('exclusive configured RAM/GPU pool; model peak is not measured' if neural else
                             'declared estimate: at least 512 MiB and twice the FFmpeg per-allocation budget'),
            'disk_basis': 'declared estimate from 0.5 seconds per source character, explicit pauses/durations, '
                          'verified clip durations/placement, PCM bytes and intermediate/stem/export copies; '
                          'not a measured speech duration or hard output cap',
            'enforcement': 'cooperative admission; existing bounded worker checks still apply'}

    @property
    def voices(self):
        return self.discover_voices()

    def discover_voices(self, *, cancelled=lambda: False):
        """Discover once on the caller's worker, with cancellable admission."""
        def stopping():
            return self._closed.is_set() or bool(cancelled())
        if stopping():
            raise InterruptedError('Production voice discovery cancelled or closed.')
        with self._voice_state:
            if self._voices is not None:
                return self._voices
        while not self._voice_discovery.acquire(timeout=.05):
            if stopping():
                raise InterruptedError('Production voice discovery cancelled or closed.')
        try:
            if stopping():
                raise InterruptedError('Production voice discovery cancelled or closed.')
            with self._voice_state:
                if self._voices is not None:
                    return self._voices
            from ..runtime_settings import voice_registry
            found = voice_registry(self.root.parent, cancelled=stopping)
            try:
                with self._voice_state:
                    if not stopping():
                        self._voices, found = found, None
                        return self._voices
            finally:
                if found is not None:
                    cleanup = getattr(found, 'close', None)
                    if callable(cleanup):
                        cleanup()
            raise InterruptedError('Production voice discovery cancelled or closed.')
        finally:
            self._voice_discovery.release()

    def close(self):
        """Release owned discovery after queued work; supplied registries are borrowed.

        Canonical providers own a bounded process for each call and retain no
        background worker between calls. Optional registry implementations may
        expose close() for their own resources.
        """
        self._closed.set()
        with self._voice_state:
            owned = self._voices if self._owns_voices else None
        if owned is not None:
            cleanup = getattr(owned, 'close', None)
            if not callable(cleanup) or cleanup() is not False:
                with self._voice_state:
                    if self._voices is owned:
                        self._voices = None

    def create(self, text, *, title='Audio production', voice_ids=(), mode='faithful'):
        project = parse_script(text, title=title, voice_ids=voice_ids, mode=mode)
        self.projects.save(project)
        return project

    def submit(self, project, destination, *, format='wav', stems=True, operation_id=None):
        if format not in ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma'):
            raise ValueError('Choose a supported local audio output format.')
        if type(stems) is not bool or not isinstance(project, Project):
            raise ValueError('Invalid production options.')
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
        job_id = _id(operation_id) if operation_id else uuid4().hex
        output = destination / f'{_output_name(project.title)}-{job_id[:12]}'
        existing_path = self.jobs_root / job_id / 'job.json'
        if existing_path.exists():
            previous = self.get(job_id)
            if (previous['fingerprint'] != project.fingerprint or previous['destination'] != str(output)
                    or previous['format'] != format or previous['stems'] != stems):
                raise ValueError('This operation ID is already bound to a different production plan.')
            return job_id
        self.register_project_inputs(project)
        self.projects.save(project)
        state = {'schema': 1, 'id': job_id, 'status': 'pending', 'project': project.to_dict(),
                 'fingerprint': project.fingerprint, 'destination': str(output), 'format': format,
                 'stems': stems, 'nodes': {}, 'artifacts': [], 'progress': '',
                 'created': time.time(), 'updated': time.time(), 'error': '', 'runtime': None}
        directory = self.jobs_root / job_id
        directory.mkdir()
        atomic_json(directory / 'job.json', state)
        return job_id

    def get(self, job_id):
        state = read_json(self.jobs_root / _id(job_id) / 'job.json')
        if state.get('schema') != 1 or state.get('id') != job_id:
            raise ValueError('Invalid production job.')
        project = Project.from_dict(state['project'])
        if state.get('fingerprint') != project.fingerprint:
            raise ValueError('The saved production plan changed.')
        return state

    def jobs(self):
        return tuple(self.get(path.parent.name) for path in sorted(self.jobs_root.glob('*/job.json')))

    def register_project_inputs(self, project, *, imported=False):
        """Declare unreviewed external paths without opening those paths."""
        from .backup_inputs import input_review, register_pending_inputs
        review = input_review(self.root)
        pending = []
        for scene in project.scenes:
            for cue in scene.cues:
                path = Path(cue.path)
                simple = path.is_absolute() and '..' not in path.parts
                managed = (simple and path.parent == self.inputs
                           and re.fullmatch(r'[0-9a-f]{64}\.audio', path.name))
                selected = (simple and any(path.is_relative_to(root)
                            for root in self.input_roots if root != self.inputs))
                if not managed and (imported or not selected):
                    if imported or cue.path not in review['approved']:
                        pending.append(cue.path)
        if pending:
            register_pending_inputs(self.root, pending)

    def relocate_restored_job(self, job_id, destination):
        """Accept a fresh output parent explicitly selected after backup restore."""
        directory = self.jobs_root / _id(job_id)
        with state_lock(directory, 'render'):
            state = self.get(job_id)
            if state.get('recovery', {}).get('requires_output_directory') is not True:
                raise ValueError('Only a restored job awaiting an output directory can be relocated.')
            parent = local_path(destination, exists=True)
            if not parent.is_dir() or parent.is_relative_to(self.root):
                raise ValueError('Choose an existing output directory outside private production state.')
            project = Project.from_dict(state['project'])
            output = parent / f'{_output_name(project.title)}-{job_id[:12]}-restored-{uuid4().hex[:8]}'
            if os.path.lexists(output):
                raise FileExistsError('The new restored-job output folder is already occupied.')
            state['destination'] = str(output)
            state['recovery']['requires_output_directory'] = False
            state['status'], state['progress'] = 'pending', 'New output directory approved; ready to resume.'
            self._save(state)
            return state

    def restored_inputs_to_review(self, job_id):
        from .backup_inputs import input_review, _input_paths
        project = Project.from_dict(self.get(job_id)['project'])
        return tuple(sorted(_input_paths(project) & set(input_review(self.root)['pending'])))

    def approve_restored_inputs(self, selected_paths, *, cancelled=lambda: False):
        from .backup_inputs import approve_input_review
        return approve_input_review(self.root, selected_paths, cancelled=cancelled)

    def _cue_source(self, cue, *, cancelled=lambda: False):
        from .backup_inputs import input_review, _checked_digest
        review = input_review(self.root)
        if cue.path in review['pending']:
            raise ValueError('A cue is outside the selected input approvals; review and explicitly approve it before rendering.')
        declared = Path(cue.path)
        if not declared.is_absolute() or '..' in declared.parts:
            raise ValueError('A cue needs an explicitly selected absolute local path.')
        source = local_path(cue.path, exists=True)
        if source.is_relative_to(self.inputs):
            match = re.fullmatch(r'([0-9a-f]{64})\.audio', source.name)
            if source.parent != self.inputs or match is None or _checked_digest(source, regular(source), cancelled) != match[1]:
                raise ValueError('A restored Studio input differs from its verified content identity.')
        elif cue.path in review['approved']:
            if review['approved'][cue.path] != {
                    'size_bytes': source.stat().st_size, 'sha256': _checked_digest(source, regular(source), cancelled)}:
                raise ValueError('A reviewed legacy Studio input changed; select it again before rendering.')
        elif not any(source.is_relative_to(root) for root in self.input_roots):
            raise ValueError('A cue is outside the explicitly selected input directories.')
        return source

    def _save(self, state):
        state['updated'] = time.time()
        atomic_json(self.jobs_root / state['id'] / 'job.json', state, replace=True)

    def request_pause(self, job_id):
        self.get(job_id)
        atomic_json(self.jobs_root / job_id / 'control.json', {'action': 'pause'},
                    replace=(self.jobs_root / job_id / 'control.json').exists())

    def cancel(self, job_id):
        self.get(job_id)
        atomic_json(self.jobs_root / job_id / 'control.json', {'action': 'cancel'},
                    replace=(self.jobs_root / job_id / 'control.json').exists())

    def _runtime(self, *, reviewed_roots=()):
        if self.runtime_factory is not None:
            return self.runtime_factory(self.cache, input_roots=(self.cache, *self.input_roots, *reviewed_roots))
        from ..runtime_settings import audio_runtime
        from ..audio import AudioBudget
        # Only choose the previously unspecified default FFmpeg thread count.
        # Explicit caller-supplied runtimes retain their exact declared budget.
        budget = AudioBudget(threads=min(AudioBudget().threads, self.broker.limits.cpu_slots))
        return audio_runtime(self.cache, state_root=self.root.parent,
                             input_roots=(self.cache, *self.input_roots, *reviewed_roots), budget=budget)

    def run(self, job_id, *, cancelled=lambda: False, progress=None):
        from ..audio import AudioCancelled, AudioCleanupPending, Track, MusicSpec
        from ..audio.cleanup import retained_state_lock
        from ..speech import SpeechSettings
        job_root = self.jobs_root / _id(job_id)
        with retained_state_lock(job_root, 'render'), ExitStack() as reservations:
            state = self.get(job_id)
            if (state.get('status') == 'waiting-input'
                    or state.get('recovery', {}).get('requires_output_directory') is True):
                raise ValueError('Choose a new output directory for this restored production job first.')
            if state['status'] == 'complete':
                self._verify_artifacts(state)
                return state
            project = Project.from_dict(state['project'])
            control = job_root / 'control.json'
            if control.exists():
                control.unlink()  # explicit Run/Resume authorizes continuing the saved job

            def admission_cancelled():
                return self._closed.is_set() or bool(cancelled()) or control.exists()

            try:
                self._admission_broker(admission_cancelled)
                sources = [self._cue_source(cue, cancelled=admission_cancelled)
                           for scene in project.scenes for cue in scene.cues]
                runtime = self._runtime(reviewed_roots=tuple({source.parent for source in sources}))
                clip_info = {}
                if sources:
                    with self._audio_admission(runtime, operation_id='studio-inspect:' + job_id,
                                               cancelled=admission_cancelled) as inspecting:
                        inspected = {}
                        for cue in (cue for scene in project.scenes for cue in scene.cues):
                            source = self._cue_source(cue, cancelled=inspecting)
                            if source not in inspected:
                                inspected[source] = runtime.inspect(source, cancelled=inspecting)
                            self._cue_source(cue, cancelled=inspecting)
                            clip_info[cue.path] = inspected[source]
                request, resource_plan = self._resource_plan(project, state, runtime, clip_info=clip_info)
                state['resource_plan'] = resource_plan
                state['status'], state['progress'], state['error'] = 'queued', 'Waiting for shared local resources.', ''
                self._save(state)
                if progress is not None:
                    progress(state['progress'])
                lease = reservations.enter_context(self.broker.reserve(request,
                    operation_id='studio:' + job_id, cancelled=admission_cancelled, timeout=60))
            except AudioCleanupPending as exc:
                # The inspector already transferred its resource lease. Keep
                # this exception intact so the outer render lock transfers too,
                # even if writing the failure journal is no longer possible.
                state['status'], state['error'] = 'failed', str(exc)[:2000]
                try:
                    self._save(state)
                except BaseException as status_error:
                    exc.add_note('Cleanup status could not be saved: ' + type(status_error).__name__)
                    exc.status_publication_error = status_error
                raise
            except Exception as exc:
                from ..operations import ResourceCancelled
                action = read_json(control).get('action') if control.exists() else None
                state['status'] = ('cancelled' if action == 'cancel' else 'paused'
                                   if admission_cancelled() or isinstance(exc, ResourceCancelled) else 'attention')
                state['error'] = str(exc)[:2000]
                self._save(state)
                if isinstance(exc, ResourceCancelled):
                    raise InterruptedError('Production paused or cancelled while waiting for resources.') from exc
                raise
            state['status'], state['error'] = 'running', ''
            self._save(state)

            def stopping():
                return self._closed.is_set() or lease.cancelled()

            def check():
                if stopping():
                    raise InterruptedError('Production paused or cancelled; completed chunks are retained.')

            def report(message):
                state['progress'] = message
                self._save(state)
                if progress is not None:
                    progress(message)

            def node(kind, identity, operation, *, cached_check=None):
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
                        if cached_check is not None:
                            cached_check()
                        state['nodes'][key] = {'kind': kind, 'sha256': receipt['sha256'], 'reused': True}
                        self._save(state)
                        return candidate, info
                report(f'{kind}: rendering local audio')
                # Each attempt gets a fresh path, including recovery from corrupt cache bytes.
                path = self.cache / (key + '-' + uuid4().hex[:10] + '.wav')
                operation(path)
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
            timeline, scene_results, stem_scenes, chapter_data = [], [], {}, []
            clip_timeline, clip_stems = [], {}
            offset = 0.0
            try:
                check()
                # Recheck exact retained inputs after resource admission; use
                # the same runtime and budget that own the current reservation.
                for scene in project.scenes:
                    for cue in scene.cues:
                        self._cue_source(cue, cancelled=stopping)
                capabilities = runtime.capabilities()
                state['runtime'] = capabilities
                runtime_identity = production_runtime_identity(runtime, capabilities, cancelled=stopping)
                self._save(state)
                speaking = any(line.text for scene in project.scenes for line in scene.lines)
                voices = ({voice.id: voice for voice in self.discover_voices(cancelled=stopping).voices()}
                          if speaking else {})
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
                                voice_identity['runtime_fingerprint'] = json_safe(fingerprint(role.voice_id, cancelled=stopping))
                            settings = SpeechSettings(rate=role.rate, volume=role.volume)

                            def verify_voice(role=role, expected=voice_identity.get('runtime_fingerprint')):
                                if callable(fingerprint) and fingerprint(role.voice_id, cancelled=stopping) != expected:
                                    raise ValueError('The installed voice changed before accepting cached speech. Retry with its current identity.')

                            def speak(target, line=line, role=role, settings=settings,
                                      expected=voice_identity.get('runtime_fingerprint')):
                                options = {'settings': settings, 'cancelled': stopping}
                                if expected is not None:
                                    options['expected_fingerprint'] = expected
                                return self.voices.synthesize(line.text, role.voice_id, target, **options)

                            speech, info = node('speech', [line.text, voice_identity, asdict(settings)],
                                speak, cached_check=verify_voice)
                            duration = round((info.duration + line.pause_after) * project.sample_rate) / project.sample_rate
                            clip, clip_info = node('voice placement', [digest_file(speech), duration,
                                                   role.pan, role.gain_db, project.sample_rate],
                                lambda target, speech=speech, role=role, duration=duration:
                                runtime.mix((Track(speech, gain_db=role.gain_db, pan=role.pan),), target,
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
                        source = self._cue_source(cue, cancelled=stopping)
                        info = runtime.inspect(source, cancelled=stopping)
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
                                  if project.schema >= 2 else cue.start)
                        required_duration = max(required_duration, placed + selected)
                        track = Track(source, cue.start, cue.gain_db, cue.pan, cue.role,
                                      cue.trim_start, cue.duration, cue.fade_in, cue.fade_out)
                        cue_tracks.append(track)
                        if project.schema >= 2:
                            clip_timeline.append({'scene': scene.id, 'id': cue.id, 'role': cue.role, 'title': cue.title,
                                'source_sha256': digest_file(source), 'source_duration': info.duration,
                                'trim_start': cue.trim_start, 'duration': selected,
                                'start': offset + placed, 'end': offset + placed + selected,
                                'gain_db': cue.gain_db, 'pan': cue.pan,
                                'fade_in': cue.fade_in, 'fade_out': cue.fade_out})
                    if not clips:
                        duration = (max(required_duration, 1 / project.sample_rate)
                                    if project.schema >= 2 and cue_tracks else max(scene.minimum_duration, 1))
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
                    final_scene, final_info = node('scene mix',
                        {'tracks': [track_identity(t) for t in tracks], 'sample_rate': project.sample_rate},
                        lambda target: runtime.mix(tracks, target, sample_rate=project.sample_rate,
                                                   duration=voice_info.duration, cancelled=stopping))
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
                        if project.schema >= 2:
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
                        temporary = target.parent / ('.pending-' + uuid4().hex)
                        try:
                            with Path(source).open('rb') as read, temporary.open('xb') as write:
                                while block := read.read(1024 * 1024):
                                    check()
                                    write.write(block)
                                write.flush()
                                os.fsync(write.fileno())
                            if digest_file(temporary) != identity:
                                raise ValueError('Audio changed during publication.')
                            os.link(temporary, target)
                        finally:
                            temporary.unlink(missing_ok=True)
                    return target

                publish(master, 'master.wav')
                for index, (scene, path, info) in enumerate(scene_results, 1):
                    publish(path, f'chapters/{index:04d}-{_output_name(scene.title)}.wav')
                if state['format'] != 'wav':
                    # Codec selection is delegated to the verified local runtime inventory.
                    key = sha256(encode([digest_file(master), state['format'], runtime_identity])).hexdigest()
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
                        runtime.convert(master, encoded, sample_rate=project.sample_rate, cancelled=stopping)
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
                            'timing_notice': 'Line-level alignment from actual synthesized durations; no word timing claim.'}
                if project.schema >= 2:
                    metadata['clips'] = clip_timeline
                    metadata['clip_stems_notice'] = ('Clip stems align to the full master timeline. '
                        'Music stems are before ducking; recorded clips have no inferred transcript.')
                for name, raw in [('script.vtt', '\n'.join(subtitles).encode('utf-8')),
                                  ('script.srt', '\n'.join(srt).encode('utf-8')),
                                  ('timeline.json', encode(metadata)),
                                  ('project.json', encode(project.to_dict()))]:
                    source = job_root / name
                    atomic_bytes(source, raw, replace=source.exists())
                    publish(source, name)
                state['result'] = metadata
                self._verify_artifacts(state)
                # All native work and output validation have finished. Release
                # before saving completion so disposable metadata cleanup can
                # be reported separately from the durable production result.
                lease.release()
                state['status'], state['progress'] = 'complete', 'Audio files and checksums verified.'
                if lease.cleanup_pending:
                    state['progress'] += ' Resource reservation metadata cleanup is pending.'
                self._save(state)
                return state
            except BaseException as exc:
                if isinstance(exc, AudioCleanupPending):
                    retained = reservations.pop_all()
                    exc.cleanup_owner.add_quiescent_callback(retained.close)
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
                try:
                    self._save(state)
                except BaseException as status_error:
                    if not isinstance(exc, AudioCleanupPending):
                        raise
                    exc.add_note('Cleanup status could not be saved: ' + type(status_error).__name__)
                    exc.status_publication_error = status_error
                raise

    def _verify_artifacts(self, state):
        root = local_path(state['destination'], exists=True)
        for artifact in state['artifacts']:
            path = local_path(artifact['path'], root=root, exists=True)
            if path.stat().st_size != artifact['size'] or digest_file(path) != artifact['sha256']:
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
