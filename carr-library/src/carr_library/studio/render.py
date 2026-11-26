"""Persistent production DAG: actual local voices, music, scene mixes and exports.

Every accepted node is content-addressed and checksum-verified before reuse.
Source text is data. This module never accepts shell commands or provider URLs.
"""
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import html
import math
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from ..durable_state import (atomic_bytes, atomic_json, digest_file, encode,
                            local_path, read_json, regular, state_lock, sync_directory)
from .project import Project, ProjectStore, parse_script, _id

ENGINE_SCHEMA = 'studio-local-dag-v1'


@dataclass(frozen=True)
class ProductionBudget:
    """Whole-job limits in addition to the runtime's per-operation AudioBudget."""
    max_cache_bytes: int = 64 * 1024**3
    max_export_bytes: int = 64 * 1024**3
    timeout: float = 86400
    reserve_disk_bytes: int = 32 * 1024**2
    max_cache_files: int = 100000

    def __post_init__(self):
        for name in ('max_cache_bytes', 'max_export_bytes', 'reserve_disk_bytes', 'max_cache_files'):
            value = getattr(self, name)
            minimum = 0 if name == 'reserve_disk_bytes' else 1
            if type(value) is not int or not minimum <= value <= 2**63 - 1:
                raise ValueError(f'Invalid production {name}.')
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not .05 <= self.timeout <= 365 * 86400):
            raise ValueError('Choose a finite whole-production time budget.')


def _bytes_under(root, check, *, max_entries=None):
    total, count, pending = 0, 0, [local_path(root, exists=True)]
    while pending:
        directory = pending.pop()
        regular(directory, directory=True)
        with os.scandir(directory) as entries:
            for entry in entries:
                check()
                count += 1
                if max_entries is not None and count > max_entries:
                    raise ValueError('Production state exceeds the whole-job file and directory entry budget.')
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    regular(path, directory=True)
                    pending.append(path)
                else:
                    total += regular(path).st_size
    return total, count


def _digest(path, check):
    regular(path)
    value = sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            check()
            value.update(block)
    return value.hexdigest()


class _Resources:
    def __init__(self, root, destination, budget, stopping):
        self.root, self.destination, self.budget, self.stopping = root, destination, budget, stopping
        self.deadline = time.monotonic() + budget.timeout
        self.next_scan = 0
        self.used = self.files = 0
        self.check()

    def time_check(self):
        if self.stopping():
            raise InterruptedError('Production paused or cancelled; verified chunks are retained.')
        if time.monotonic() >= self.deadline:
            raise TimeoutError('The whole-production time budget was reached; verified chunks are retained.')

    def free(self, directory, additional=0):
        if shutil.disk_usage(directory).free < additional + self.budget.reserve_disk_bytes:
            raise ValueError('Insufficient free disk for production and its configured recovery reserve.')

    def check(self, *, accepted=False):
        # A returned provider output is verified and receipted before next-step
        # cancellation. Resource limits still apply during that acceptance.
        check = (lambda: None) if accepted else self.time_check
        check()
        self.used, self.files = _bytes_under(self.root, check, max_entries=self.budget.max_cache_files)
        if self.used > self.budget.max_cache_bytes or self.files > self.budget.max_cache_files:
            raise ValueError('Retained production cache and state exceed the whole-job storage budget.')
        self.free(self.root)
        self.next_scan = time.monotonic() + .25

    def poll(self):
        if self.stopping():
            return True
        self.time_check()
        if time.monotonic() >= self.next_scan:
            self.check()
        self.free(self.root)
        return False

    def require_cache(self, additional=0, *, accepted=False):
        self.check(accepted=accepted)
        if self.used + max(0, additional) > self.budget.max_cache_bytes:
            raise ValueError('Production staging would exceed the whole-job storage budget.')
        self.free(self.root, max(0, additional))

    def speech_settings(self, settings):
        self.check()
        # A native utterance and the streaming combined speech WAV coexist.
        # This operational limit deliberately does not enter the audio cache key.
        remaining = min(self.budget.max_cache_bytes - self.used,
                        shutil.disk_usage(self.root).free - self.budget.reserve_disk_bytes)
        maximum = min(settings.max_output_bytes, max(0, (remaining - 65536) // 2))
        if maximum < 4096:
            raise ValueError('Insufficient production storage budget for another speech segment.')
        return replace(settings, max_output_bytes=maximum,
                       timeout_seconds=max(1, min(settings.timeout_seconds, self.deadline - time.monotonic())))

    def require_export(self, additional=0):
        self.time_check()
        size, _ = (_bytes_under(self.destination, self.time_check, max_entries=self.budget.max_cache_files)
                   if self.destination.exists() else (0, 0))
        if size + max(0, additional) > self.budget.max_export_bytes:
            raise ValueError('Production exports exceed the whole-job export byte budget.')
        self.free(self.destination.parent, max(0, additional))


def _caption_text(value):
    # An authored/source line remains exact in project/timeline JSON; caption
    # syntax must not turn its tags or blank lines into markup or extra cues.
    escaped = html.escape(value, quote=False).replace('-->', '--&gt;')
    return '\n'.join(line if line.strip() else '\u200b' for line in escaped.splitlines())


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
    def __init__(self, directory, *, voices=None, runtime_factory=None, input_roots=(), budget=None):
        self.root = local_path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.projects = ProjectStore(self.root / 'projects')
        self.jobs_root = self.root / 'jobs'
        self.jobs_root.mkdir(exist_ok=True)
        self.cache = self.root / 'cache'
        self.cache.mkdir(exist_ok=True)
        self._voices = voices
        self.runtime_factory = runtime_factory
        self.input_roots = tuple(local_path(x, exists=True) for x in input_roots)
        self.budget = ProductionBudget() if budget is None else budget
        if type(self.budget) is not ProductionBudget:
            raise ValueError('A validated ProductionBudget is required.')
        self._explicit_budget = budget is not None

    @property
    def voices(self):
        if self._voices is None:
            from ..runtime_settings import voice_registry
            self._voices = voice_registry(self.root.parent)
        return self._voices

    def create(self, text, *, title='Audio production', voice_ids=(), mode='faithful'):
        project = parse_script(text, title=title, voice_ids=voice_ids, mode=mode)
        self.projects.save(project)
        return project

    def submit(self, project, destination, *, format='wav', stems=True, operation_id=None):
        return self._submit(project, destination, format=format, stems=stems,
                            operation_id=operation_id, save_project=True)

    def _submit(self, project, destination, *, format, stems, operation_id, save_project, cue_assets=None):
        if format not in ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma'):
            raise ValueError('Choose a supported local audio output format.')
        if type(stems) is not bool or not isinstance(project, Project):
            raise ValueError('Invalid production options.')
        if any(not role.voice_id for role in project.roles if any(
                line.role == role.id and line.text for scene in project.scenes for line in scene.lines)):
            raise ValueError('Assign an installed voice to every speaking role before rendering.')
        available = {voice.id for voice in self.voices.voices()}
        if any(role.voice_id not in available for role in project.roles if role.voice_id):
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
        if save_project:
            self.projects.save(project)
        state = {'schema': 1, 'id': job_id, 'status': 'pending', 'project': project.to_dict(),
                 'fingerprint': project.fingerprint, 'destination': str(output), 'format': format,
                 'stems': stems, 'nodes': {}, 'artifacts': [], 'progress': '',
                 'created': time.time(), 'updated': time.time(), 'error': '', 'runtime': None,
                 'budget': asdict(self.budget), 'cue_assets': dict(cue_assets or {})}
        directory = self.jobs_root / job_id
        directory.mkdir()
        atomic_json(directory / 'job.json', state)
        return job_id

    def submit_reexport(self, job_id, destination, *, operation_id=None):
        """Choose a new output directory after restore, preserving the old job.

        The returned new job uses the original saved plan and accepted cues;
        ``run`` reuses verified cache nodes and publishes new immutable exports.
        It never rewrites or retargets an old job's output directory.
        """
        with state_lock(self.root, 'production'):
            previous = self.get(job_id)
            project = Project.from_dict(previous['project'])
            return self._submit(project, destination, format=previous['format'], stems=previous['stems'],
                operation_id=operation_id, save_project=False, cue_assets=previous.get('cue_assets', {}))

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

    def _runtime(self):
        if self.runtime_factory is not None:
            return self.runtime_factory(self.cache, input_roots=(self.cache, *self.input_roots))
        from ..runtime_settings import audio_runtime
        return audio_runtime(self.cache, state_root=self.root.parent,
                             input_roots=(self.cache, *self.input_roots))

    def run(self, job_id, *, cancelled=lambda: False, progress=None):
        from ..audio import Track, MusicSpec
        from ..speech import SpeechSettings
        if not callable(cancelled) or (progress is not None and not callable(progress)):
            raise ValueError('Production cancellation and progress must be callable.')
        if cancelled():
            raise InterruptedError('Production cancelled before opening the saved job.')
        job_root = self.jobs_root / _id(job_id)
        # Cache nodes are shared across jobs. One native cooperative guard also
        # gives backup a stable whole-production snapshot boundary.
        with state_lock(self.root, 'production'), state_lock(job_root, 'render'):
            state = self.get(job_id)
            try:
                budget = self.budget if self._explicit_budget else ProductionBudget(**state.get('budget', {}))
            except TypeError as exc:
                raise ValueError('Invalid saved whole-production budget.') from exc
            destination = local_path(state['destination'])
            if destination.is_relative_to(self.root):
                raise ValueError('Production exports must stay outside private production state.')
            if state['status'] == 'complete':
                resources = _Resources(self.root, destination, budget, cancelled)
                self._verify_artifacts(state, check=resources.time_check)
                return state
            control = job_root / 'control.json'
            if control.exists():
                control.unlink()  # explicit Run/Resume authorizes continuing the saved job

            def requested_stop():
                return bool(cancelled()) or control.exists()

            resources = _Resources(self.root, destination, budget, requested_stop)
            project = Project.from_dict(state['project'])
            runtime = self._runtime()
            capabilities = runtime.capabilities()
            resources.check()
            # Engine inventory is part of the node identity. A different installed
            # FFmpeg/voice version must not silently reuse incompatible work.
            runtime_identity = sha256(encode(capabilities)).hexdigest()
            state['runtime'] = capabilities
            state['status'], state['error'] = 'running', ''
            state['budget'] = asdict(budget)
            state.setdefault('cue_assets', {})
            if type(state['cue_assets']) is not dict:
                raise ValueError('Invalid accepted cue snapshot map.')
            self._save(state)

            def stopping():
                return resources.poll()

            def check():
                resources.time_check()

            def report(message):
                state['progress'] = message
                self._save(state)
                if progress is not None:
                    progress(message)
                resources.check()

            def node(kind, identity, operation):
                resources.check()
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
                    if candidate.is_file() and _digest(candidate, check) == receipt.get('sha256'):
                        info = runtime.inspect(candidate, cancelled=stopping)
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
                resources.check(accepted=True)
                info = runtime.inspect(path)
                if info.duration <= 0 or info.channels not in (1, 2):
                    raise ValueError('The audio engine returned an empty or unsupported output.')
                digest = digest_file(path)
                atomic_json(receipt_path, {'schema': 1, 'key': key, 'file': path.name,
                            'sha256': digest, 'duration': info.duration, 'sample_rate': info.sample_rate,
                            'channels': info.channels}, replace=receipt_path.exists())
                state['nodes'][key] = {'kind': kind, 'sha256': digest, 'reused': False}
                self._save(state)
                resources.check(accepted=True)
                check()
                return path, info

            def cue_source(scene, cue):
                """Bind a job to accepted local bytes; resume never retargets it."""
                check()
                key = sha256(encode({'scene': scene.id, 'cue': cue.id, 'source': cue.path})).hexdigest()
                accepted = state['cue_assets'].get(key)
                if accepted is not None:
                    if (type(accepted) is not dict or set(accepted) != {'file', 'sha256', 'size'}
                            or not isinstance(accepted['file'], str)
                            or not re.fullmatch(r'cue-[0-9a-f]{64}-[0-9a-f]{10}\.[a-z0-9]{1,8}', accepted['file'])
                            or not isinstance(accepted['sha256'], str)
                            or not re.fullmatch(r'[0-9a-f]{64}', accepted['sha256'])
                            or not accepted['file'].startswith('cue-' + accepted['sha256'] + '-')
                            or type(accepted['size']) is not int or accepted['size'] <= 0):
                        raise ValueError('Invalid accepted cue snapshot.')
                    candidate = local_path(self.cache / accepted['file'], root=self.cache)
                    if (candidate.is_file() and candidate.stat().st_size == accepted['size']
                            and _digest(candidate, check) == accepted['sha256']):
                        return candidate, runtime.inspect(candidate, cancelled=stopping)
                source = local_path(cue.path, exists=True)
                if not any(source.is_relative_to(root) for root in self.input_roots):
                    raise ValueError('A cue is outside the explicitly selected input directories.')
                size = regular(source).st_size
                resources.require_cache(size + 8192)
                identity = _digest(source, check)
                if accepted is not None and identity != accepted['sha256']:
                    raise ValueError('The original cue changed; restore its accepted snapshot or submit a new production job.')
                suffix = source.suffix.lower().lstrip('.')
                suffix = suffix if re.fullmatch('[a-z0-9]{1,8}', suffix) else 'audio'
                target = self.cache / f'cue-{identity}-{uuid4().hex[:10]}.{suffix}'
                temporary = self.cache / ('.pending-cue-' + uuid4().hex)
                try:
                    copied = sha256()
                    with source.open('rb') as incoming, temporary.open('xb') as outgoing:
                        while block := incoming.read(1024 * 1024):
                            check()
                            resources.free(self.cache, len(block))
                            copied.update(block)
                            outgoing.write(block)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if copied.hexdigest() != identity or temporary.stat().st_size != size:
                        raise ValueError('The selected cue changed during its accepted snapshot copy.')
                    info = runtime.inspect(temporary, cancelled=stopping)
                    check()
                    os.link(temporary, target)
                    temporary.unlink()
                    sync_directory(self.cache)
                finally:
                    temporary.unlink(missing_ok=True)
                state['cue_assets'][key] = {'file': target.name, 'sha256': identity, 'size': size}
                self._save(state)
                resources.check(accepted=True)
                check()
                return target, info

            def silence(duration):
                duration = max(1 / project.sample_rate, duration)
                return node('silence', [duration, project.sample_rate],
                            lambda target: runtime.mix((), target, duration=duration,
                                sample_rate=project.sample_rate, cancelled=stopping))

            roles = {role.id: role for role in project.roles}
            voices = {voice.id: voice for voice in self.voices.voices()}
            timeline, scene_results, stem_scenes, chapter_data = [], [], {}, []
            offset = 0.0
            try:
                for scene_index, scene in enumerate(project.scenes, 1):
                    report(f'Scene {scene_index} of {len(project.scenes)}: {scene.title}')
                    # Validate/snapshot chosen external inputs before paying the
                    # local synthesis cost for this scene.
                    cue_sources = [cue_source(scene, cue) for cue in scene.cues]
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
                                    settings=resources.speech_settings(settings), cancelled=stopping))
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
                    if not clips:
                        clip, info = silence(max(scene.minimum_duration, 1))
                        clips, clip_roles, cursor = [clip], [('', info.duration)], info.duration
                    required_duration = scene.minimum_duration
                    for cue, (source, info) in zip(scene.cues, cue_sources):
                        required_duration = max(required_duration, cue.start + info.duration)
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
                    for cue, (source, _) in zip(scene.cues, cue_sources):
                        tracks.append(Track(source, cue.start, cue.gain_db, cue.pan, cue.role))
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
                    timeline.extend(scene_lines)
                    chapter_data.append({'id': scene.id, 'title': scene.title, 'start': offset,
                                         'end': offset + final_info.duration})
                    offset += final_info.duration
                master, master_info = node('master assembly', [digest_file(row[1]) for row in scene_results],
                    lambda target: runtime.concatenate([row[1] for row in scene_results], target,
                        sample_rate=project.sample_rate, cancelled=stopping))
                output = local_path(state['destination'])
                marker = output / 'production-owner.json'
                resources.require_export(4096 if not output.exists() else 0)
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
                    identity = _digest(source, check)
                    # A durable intent makes publication retryable after a process crash.
                    artifact = {'path': str(target), 'sha256': identity, 'size': Path(source).stat().st_size}
                    existing = next((a for a in state['artifacts'] if a['path'] == str(target)), None)
                    if existing is not None and existing != artifact:
                        raise ValueError('Previously published output identity changed.')
                    if existing is None:
                        state['artifacts'].append(artifact)
                        self._save(state)
                    resources.require_export(0 if target.exists() else artifact['size'])
                    if target.exists():
                        if _digest(target, check) != identity:
                            raise FileExistsError('An output was changed externally; it will not be overwritten.')
                    else:
                        temporary = target.parent / ('.pending-' + uuid4().hex)
                        try:
                            copied = 0
                            with Path(source).open('rb') as read, temporary.open('xb') as write:
                                while block := read.read(1024 * 1024):
                                    check()
                                    copied += len(block)
                                    if copied > artifact['size']:
                                        raise ValueError('Audio grew beyond its measured publication size.')
                                    resources.free(target.parent, len(block))
                                    write.write(block)
                                write.flush()
                                os.fsync(write.fileno())
                            if _digest(temporary, check) != identity:
                                raise ValueError('Audio changed during publication.')
                            os.link(temporary, target)
                        finally:
                            temporary.unlink(missing_ok=True)
                        sync_directory(target.parent)
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
                subtitles = ['WEBVTT\n']
                srt = []
                for index, line in enumerate(timeline, 1):
                    content = _caption_text(line['text'])
                    subtitles.append(f'{index}\n{_stamp(line["start"])} --> {_stamp(line["end"])}\n{content}\n')
                    srt.append(f'{index}\n{_stamp(line["start"], comma=True)} --> {_stamp(line["end"], comma=True)}\n{content}\n')
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
                for name, raw in [('script.vtt', '\n'.join(subtitles).encode('utf-8')),
                                  ('script.srt', '\n'.join(srt).encode('utf-8')),
                                  ('timeline.json', encode(metadata)),
                                  ('project.json', encode(project.to_dict()))]:
                    source = job_root / name
                    resources.require_cache(len(raw) + 8192)
                    atomic_bytes(source, raw, replace=source.exists())
                    publish(source, name)
                state['result'] = metadata
                state['status'], state['progress'] = 'complete', 'Audio files and checksums verified.'
                self._verify_artifacts(state, check=check)
                self._save(state)
                resources.check()
                return state
            except BaseException as exc:
                action = read_json(control).get('action') if control.exists() else None
                state['status'] = 'cancelled' if action == 'cancel' else ('paused' if requested_stop() or isinstance(exc, InterruptedError) else 'failed')
                state['error'] = str(exc)[:2000]
                try:
                    self._save(state)
                except (OSError, ValueError):
                    pass  # A failed checkpoint must not hide the original render error.
                raise

    def _verify_artifacts(self, state, *, check=lambda: None):
        if not state['artifacts']:
            raise ValueError('The completed production has no verified output artifacts.')
        check()
        try:
            root = local_path(state['destination'], exists=True)
        except FileNotFoundError as exc:
            raise ValueError('Rendered outputs are missing; submit a re-export to a new chosen output directory.') from exc
        for artifact in state['artifacts']:
            check()
            path = local_path(artifact['path'], root=root, exists=True)
            if path.stat().st_size != artifact['size'] or _digest(path, check) != artifact['sha256']:
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
