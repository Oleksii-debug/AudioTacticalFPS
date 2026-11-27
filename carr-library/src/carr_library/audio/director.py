"""Real, resumable script-to-audio execution with immutable disk-backed chunks.

The director invokes local engines through typed interfaces. It never interprets
document text as commands, executes shell text, uploads content or downloads a
model. A completed status requires verified output files, not a model response.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import wave
from uuid import uuid4

from .cleanup import retained_context, retained_state_lock, retained_temporary_directory, _pending_owners

from .projects import (Project, ProjectStore, atomic_json, canonical, digest, directory,
                       identity, now, plain, read_json)


DIRECTOR_VERSION = '1'


def file_sha256(path):
    plain(path)
    value = sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def speech_chunks(text, maximum=700):
    """Lossless source slices; no punctuation or words are invented."""
    position = 0
    while position < len(text):
        end = min(len(text), position + maximum)
        if end < len(text):
            floor = position + maximum // 2
            boundary = max(text.rfind(mark, floor, end) for mark in ('. ', '! ', '? ', '\n', '; ', ', ', ' '))
            if boundary >= floor:
                end = boundary + 1
        yield position, end, text[position:end]
        position = end


def _seconds(frames, rate):
    return frames / rate


def _timecode(seconds, separator=','):
    milliseconds = round(seconds * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    whole, milli = divmod(rest, 1000)
    return f'{hours:02}:{minutes:02}:{whole:02}{separator}{milli:03}'


class ProductionDirector:
    def __init__(self, root, *, registry=None, music_options=None, broker=None):
        self.store = ProjectStore(root)
        self._registry = registry
        self._music_options = None if music_options is None else dict(music_options)
        self._broker = broker

    @property
    def registry(self):
        # Listing saved projects/jobs and verifying an already committed result
        # does not authenticate models or start voice/runtime discovery.
        if self._registry is None:
            from .voices import VoiceRegistry
            self._registry = VoiceRegistry.for_library(self.store.root)
        return self._registry

    @registry.setter
    def registry(self, value):
        self._registry = value

    @property
    def music_options(self):
        if self._music_options is None:
            from .music_settings import load_music_options
            self._music_options = load_music_options(self.store.root)
        return self._music_options

    @music_options.setter
    def music_options(self, value):
        self._music_options = dict(value)

    @property
    def broker(self):
        if self._broker is None:
            from ..operations import ResourceBroker
            self._broker = ResourceBroker.for_app(self.store.root)
        return self._broker

    def _neural_device(self, project, *, activated=False):
        if not any(scene.music and scene.music.engine == 'musicgen' for scene in project.scenes):
            return None
        if activated or self._music_options is not None:
            from .music import MusicGenConfig
            config = self.music_options.get('musicgen_config')
            if not isinstance(config, MusicGenConfig):
                raise ValueError('Configure the selected local MusicGen installation before rendering.')
            device = config.device
        else:
            # Pure existing settings validation reads declarations only; model
            # and executable authentication remain inside the admitted work.
            from .music_settings import MusicSettingsStore
            record = MusicSettingsStore(self.store.root).read()['musicgen']
            if record is None:
                raise ValueError('Configure the selected local MusicGen installation before rendering.')
            device = record['device']
        if device == 'cpu':
            return None
        if device == 'cuda':
            return 'cuda:0'
        if isinstance(device, str) and device in tuple('cuda:' + str(i) for i in range(8)):
            return device
        raise ValueError('Select an explicit supported local MusicGen device.')

    def _resource_request(self, project, folder, output, cancelled):
        """Declare bounded cooperative estimates before any native discovery.

        These estimates do not claim measured speech duration or neural peaks.
        Actual disk and per-worker checks remain authoritative during rendering.
        """
        from ..operations import ResourceRequest, ResourceUnavailable
        from .narration import spoken_cue
        roles = {role.id: role for role in project.roles}
        seconds = 0.0
        for scene in project.scenes:
            for cue in scene.cues:
                if cancelled():
                    raise InterruptedError('Production paused before resource admission.')
                if cue.kind == 'speech':
                    seconds += len(spoken_cue(project, cue)['text']) * .5 / roles[cue.role].rate
                elif cue.kind == 'pause':
                    seconds += cue.seconds
                else:
                    asset = plain(folder / 'assets' / (cue.asset + '.wav'))
                    # Retained assets are decoded PCM. This deliberately uses
                    # the lowest accepted mono byte rate, including header bytes,
                    # so no native decoder or large hash runs before admission.
                    seconds += max(cue.seconds, asset.st_size / 8000)
        seconds += max(0, len(project.scenes) - 1) * project.settings.chapter_gap_seconds
        pcm = math.ceil(max(1, seconds) * project.settings.sample_rate * project.settings.channels * 2)
        stems = len(project.roles) if project.settings.stems else 0
        cache_bytes = pcm * (6 + 2 * stems) + 16 * 1024**2
        output_bytes = pcm * (2 + stems + int(project.settings.format != 'wav')) + 1024**2
        if project.settings.disk_budget_bytes and cache_bytes + output_bytes > project.settings.disk_budget_bytes:
            raise ValueError('The chosen audio disk budget cannot admit the declared production estimate; completed chunks were saved.')
        neural = (any(scene.music and scene.music.engine == 'musicgen' for scene in project.scenes)
                  or any(role.voice_id.startswith(('piper:', 'dordiai-voices:')) for role in project.roles))
        limits, device = self.broker.limits, self._neural_device(project)
        memory = limits.memory_bytes if neural else 512 * 1024**2
        if neural and not memory:
            raise ResourceUnavailable('Configure a positive declared memory budget for local neural production.')
        gpu_bytes = dict(limits.gpu_devices).get(device, 0) if device else 0
        if device and not gpu_bytes:
            raise ResourceUnavailable('The selected music GPU needs an explicit shared memory allowance.')
        request = ResourceRequest(cpu_slots=2, memory_bytes=memory,
            gpu_device=device, gpu_memory_bytes=gpu_bytes,
            disk_bytes=cache_bytes, disk_path=folder / 'cache',
            additional_disks=((output.parent, output_bytes),))
        return request, device

    def _job_path(self, project_id, job_id):
        identity(job_id, 'job identity')
        folder = self.store.path(project_id) / 'jobs'
        plain(folder, directory=True)
        return folder / (job_id + '.json')

    def status(self, project_id, job_id):
        value = read_json(self._job_path(project_id, job_id))
        if (type(value) is not dict or value.get('version') != 1
                or value.get('project_id') != project_id or value.get('id') != job_id
                or value.get('state') not in ('queued','running','paused','failed','complete')):
            raise ValueError('Invalid production journal.')
        project = Project.from_dict(value['project'])
        if value['project_fingerprint'] != project.fingerprint:
            raise ValueError('Production journal no longer matches its original project.')
        return value

    def jobs(self, project_id):
        rows = []
        for path in sorted((self.store.path(project_id) / 'jobs').glob('*.json')):
            value = self.status(project_id, path.stem)
            rows.append({key: value[key] for key in ('id','state','created_at','updated_at','output_dir','progress','message')})
        return tuple(rows)

    def _write_job(self, job):
        job['updated_at'] = now()
        with self.store.transaction():
            atomic_json(self._job_path(job['project_id'], job['id']), job)

    def create_job(self, project_id, output_dir):
        project = self.store.load(project_id)
        output = Path(output_dir).absolute()
        if output.is_symlink():
            raise ValueError('Choose a regular local output directory.')
        plain(output.parent, directory=True)
        if output.exists():
            raise FileExistsError('Choose a new output folder; existing files are never overwritten.')
        resolved = output.parent.resolve() / output.name
        if resolved.is_relative_to(self.store.root.resolve()) or self.store.root.resolve().is_relative_to(resolved):
            raise ValueError('Choose an audio export folder outside the library data directory.')
        job_id = uuid4().hex
        job = {'version':1, 'id':job_id, 'project_id':project.id, 'project':project.to_dict(),
            'project_fingerprint':project.fingerprint, 'output_dir':str(resolved),
            'state':'queued', 'created_at':now(), 'updated_at':now(), 'progress':0,
            'message':'Queued for local production.', 'steps':{}, 'outputs':[], 'result':None}
        self._write_job(job)
        return job_id

    def render(self, project_id, output_dir=None, *, job_id=None, cancelled=None, progress=None):
        from .render import concatenate_audio, convert_audio, mix_tracks, probe_audio, Track
        from .music import generate_music, MusicSpec
        cancelled = cancelled or (lambda: False)
        progress = progress or (lambda value: None)
        if not callable(cancelled) or not callable(progress):
            raise ValueError('Invalid production callbacks.')
        if job_id is None:
            if output_dir is None:
                raise ValueError('Select an output folder before rendering.')
            job_id = self.create_job(project_id, output_dir)
        job = self.status(project_id, job_id)
        if job.get('restore_requires_new_output'):
            raise ValueError('This job was restored from backup. Start a new render in a new output folder; accepted chunks will be reused.')
        if output_dir is not None:
            requested = Path(output_dir).absolute()
            plain(requested.parent, directory=True)
            plain(requested, directory=True, missing=True)
            # Creation binds the resolved parent. Apply the same identity rule
            # on initial execution and resume, including Windows short-name
            # temporary directories and harmless parent/.. spellings.
            requested = requested.parent.resolve() / requested.name
            if requested != Path(job['output_dir']):
                raise ValueError('A resumed job keeps its original output folder.')
        project = Project.from_dict(job['project'])
        # A job is bound to its approved revision. Editing creates another job,
        # reusing unchanged content hashes rather than changing an in-flight plan.
        folder = self.store.path(project_id)
        from ..operations import ResourceCancelled, ResourceTimeout
        with retained_state_lock(folder, 'render'):
            if job['state'] == 'complete':
                return self._run(project, folder, job, cancelled, progress,
                                 Track, mix_tracks, concatenate_audio, convert_audio,
                                 probe_audio, MusicSpec, generate_music)
            try:
                if cancelled():
                    raise InterruptedError('Production paused before resource admission.')
                output = Path(job['output_dir'])
                parent = plain(output.parent, directory=True)
                parent_identity = parent.st_dev, parent.st_ino
                request, expected_device = self._resource_request(project, folder, output, cancelled)
                job['state'], job['message'] = 'queued', 'Waiting for shared local audio resources.'
                self._write_job(job)
                progress({'state': job['state'], 'job_id': job['id'],
                          'message': job['message'], 'progress': job['progress']})
                with retained_context(self.broker.reserve(request,
                        operation_id='director:' + job['id'], cancelled=cancelled, timeout=60)) as lease:
                    def stopped():
                        if cancelled():
                            return True
                        try:
                            lease.checkpoint()
                        except (ResourceCancelled, ResourceTimeout) as exc:
                            raise InterruptedError('Production paused while holding its shared resource reservation.') from exc
                        return False
                    if stopped():
                        raise InterruptedError('Production paused before native audio work.')
                    current = self.status(project_id, job_id)
                    if any(current.get(key) != job.get(key) for key in
                           ('project', 'project_fingerprint', 'output_dir')):
                        raise ValueError('The production plan or output changed while waiting for resources.')
                    current_parent = plain(output.parent, directory=True)
                    if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
                        raise ValueError('The selected production output directory changed while waiting.')
                    if self._neural_device(project, activated=True) != expected_device:
                        raise ValueError('The configured music device changed while waiting; review the job again.')
                    return self._run(project, folder, job, stopped, progress,
                                     Track, mix_tracks, concatenate_audio, convert_audio,
                                     probe_audio, MusicSpec, generate_music)
            except BaseException as exc:
                # Journal failures retain the original pending native owner in
                # their exception chain, so the outer render lock stays held.
                from .render import RenderCancelled
                if job['state'] not in ('complete', 'paused', 'failed'):
                    job['state'] = 'paused' if isinstance(exc, (
                        InterruptedError, RenderCancelled, ResourceCancelled, ResourceTimeout)) else 'failed'
                    job['message'] = str(exc)[:1000]
                    self._write_job(job)
                if isinstance(exc, (ResourceCancelled, ResourceTimeout)):
                    raise InterruptedError('Production paused while waiting for shared resources.') from exc
                raise

    def _run(self, project, folder, job, cancelled, progress, Track, mix_tracks,
             concatenate_audio, convert_audio, probe_audio, MusicSpec, generate_music):
        output = Path(job['output_dir'])
        if job['state'] == 'complete':
            self._verify_outputs(job)
            progress({'state':'complete', 'job_id':job['id'], 'message':'Existing completed output verified.', 'progress':100})
            return job['result']
        available = {voice.id: voice for voice in self.registry.list_voices()}
        for role in project.roles:
            if role.voice_id not in available:
                raise ValueError(f'Local voice is unavailable for {role.name}: {role.voice_id}. Choose an installed voice.')
        runtime_method = getattr(self.registry, 'fingerprint', None)
        runtime = runtime_method() if callable(runtime_method) else digest([asdict(voice) for voice in available.values()])
        from .render import runtime_identity
        from .music import music_runtime_identity
        render_runtime = runtime_identity()
        music_runtime = music_runtime_identity(self.music_options) if any(scene.music for scene in project.scenes) else None
        settings = project.settings
        cache = folder / 'cache'
        plain(cache, directory=True)
        roles = {role.id: role for role in project.roles}
        output_marker = output / '.production-job.json'
        if output.exists():
            plain(output, directory=True)
            marker = read_json(output_marker, 64000)
            if marker != {'job_id':job['id'], 'project_fingerprint':project.fingerprint}:
                raise ValueError('This output folder belongs to another production.')
        else:
            output.mkdir(mode=0o700)
            atomic_json(output_marker, {'job_id':job['id'], 'project_fingerprint':project.fingerprint}, replace_existing=False)
        work = directory(folder / ('work-' + job['id']))
        scene_files, scene_info, all_timings = [], [], []
        role_stems = {role.id: [] for role in project.roles} if settings.stems else {}
        cumulative_frames = 0
        total_steps = sum(len(scene.cues) for scene in project.scenes)
        done_steps = 0
        cache_hits, generated = 0, 0
        job['state'], job['message'] = 'running', 'Producing local audio.'
        self._write_job(job)

        def check():
            if cancelled():
                raise InterruptedError('Production paused; completed chunks can be resumed.')

        def report(message):
            check()
            job['message'] = message
            job['progress'] = min(95, round(done_steps / max(1, total_steps) * 90))
            self._write_job(job)
            progress({'state':job['state'], 'job_id':job['id'], 'message':message, 'progress':job['progress']})

        def disk_check(estimated_bytes=0):
            free = min(shutil.disk_usage(cache).free, shutil.disk_usage(output).free)
            # Keep a reserve for journals and final publication. Existing cache
            # files remain useful when an output budget cannot be met.
            if estimated_bytes + 16 * 1024**2 > free:
                raise ValueError('Insufficient free disk for this audio step; completed chunks were saved.')
            if settings.disk_budget_bytes:
                used = sum(path.stat().st_size for path in cache.iterdir() if path.is_file())
                used += sum(path.stat().st_size for path in output.rglob('*') if path.is_file())
                if used + estimated_bytes > settings.disk_budget_bytes:
                    raise ValueError('The chosen audio disk budget would be exceeded; completed chunks were saved.')

        def cached(kind, inputs, producer):
            nonlocal cache_hits, generated
            check()
            key = digest({'director':DIRECTOR_VERSION, 'kind':kind, 'inputs':inputs,
                          'runtime':render_runtime if kind!='speech' else runtime})
            path, meta_path = cache / (key + '.wav'), cache / (key + '.json')
            valid = False
            if path.exists() and meta_path.exists():
                try:
                    metadata = read_json(meta_path, 256000)
                    if (type(metadata) is not dict or set(metadata) != {
                            'key', 'sha256', 'sample_rate', 'channels', 'frames',
                            'duration_seconds', 'kind'}
                            or metadata['kind'] != kind
                            or any(type(metadata[name]) is not int or metadata[name] <= 0
                                   for name in ('sample_rate', 'channels', 'frames'))
                            or type(metadata['duration_seconds']) not in (int, float)
                            or not math.isfinite(metadata['duration_seconds'])
                            or metadata['duration_seconds'] <= 0):
                        raise ValueError('Invalid completed audio checkpoint metadata.')
                    valid = metadata.get('key') == key and metadata.get('sha256') == file_sha256(path)
                    if valid:
                        measured = probe_audio(path)
                        valid = (metadata['sample_rate'] == measured.sample_rate
                            and metadata['frames'] == measured.frames
                            and metadata['channels'] == measured.channels
                            and abs(metadata['duration_seconds'] - measured.duration_seconds)
                                <= 1 / measured.sample_rate)
                except (OSError, ValueError, KeyError, RuntimeError) as exc:
                    if _pending_owners(exc):
                        raise
                    valid = False
            if valid:
                cache_hits += 1
                return path, metadata
            # Keep damaged evidence for diagnosis; never silently overwrite it.
            with self.store.transaction():
                for old in (path, meta_path):
                    if old.exists():
                        plain(old)
                        old.rename(old.with_name(old.name + '.invalid-' + uuid4().hex))
            disk_check()
            temporary = work / (uuid4().hex + '.wav')

            @contextmanager
            def attempt():
                try:
                    yield
                finally:
                    temporary.unlink(missing_ok=True)

            with retained_context(attempt()):
                producer(temporary)
                check()
                measured = probe_audio(temporary)
                if measured.frames < 1:
                    raise ValueError('The audio engine returned empty output.')
                checksum = file_sha256(temporary)
                metadata = {'key':key, 'sha256':checksum, 'sample_rate':measured.sample_rate,
                    'channels':measured.channels, 'frames':measured.frames,
                    'duration_seconds':measured.duration_seconds, 'kind':kind}
                with self.store.transaction():
                    # Hard-link publishes without overwriting another file.
                    os.link(temporary, path)
                    temporary.unlink()
                    atomic_json(meta_path, metadata, replace_existing=False)
                generated += 1
                job['steps'][key] = {'state':'complete','sha256':checksum,'kind':kind}
                self._write_job(job)
                return path, metadata

        def track_identity(track):
            return {'path_hash':file_sha256(track.path),'start':track.start_seconds,
                'gain':track.gain_db,'pan':track.pan,'kind':track.kind,
                'fade_in':track.fade_in,'fade_out':track.fade_out,
                'preserve_stereo':track.preserve_stereo}

        def mix_timeline(tracks, destination, duration, *, duck_music=True):
            # Engine graph limits apply to simultaneously processed inputs, not
            # chapter duration. Sequential cues are reduced to bounded blocks,
            # then disk-backed lanes preserve role pan and speech sidechain.
            if len(tracks)<=96:
                return mix_tracks(tracks,destination,duration_seconds=duration,
                    sample_rate=settings.sample_rate,channels=settings.channels,
                    cancelled=cancelled,duck_music=duck_music)
            lanes=[]
            for kind in ('voice','music','ambience','sfx'):
                members=sorted((track for track in tracks if track.kind==kind),key=lambda item:item.start_seconds)
                if not members:
                    continue
                blocks=[]
                for offset in range(0,len(members),96):
                    check()
                    selected=members[offset:offset+96]
                    start=0 if offset==0 else round(selected[0].start_seconds*settings.sample_rate)/settings.sample_rate
                    ending=round(members[offset+96].start_seconds*settings.sample_rate)/settings.sample_rate if offset+96<len(members) else duration
                    adjusted=[replace(track,start_seconds=track.start_seconds-start) for track in selected]
                    block,unused=cached('timeline-block',{'kind':kind,'tracks':[track_identity(t) for t in adjusted],
                        'duration':ending-start,'rate':settings.sample_rate,'channels':settings.channels},
                        lambda path, adjusted=adjusted, length=ending-start: mix_tracks(adjusted,path,
                            duration_seconds=length,sample_rate=settings.sample_rate,channels=settings.channels,
                            cancelled=cancelled,duck_music=False))
                    blocks.append(block)
                lane,unused=cached('timeline-lane',{'kind':kind,'blocks':[file_sha256(path) for path in blocks],
                    'rate':settings.sample_rate,'channels':settings.channels},
                    lambda path,blocks=blocks: concatenate_audio(blocks,path,sample_rate=settings.sample_rate,
                        channels=settings.channels,cancelled=cancelled))
                lanes.append(Track(lane,kind=kind,preserve_stereo=True))
            return mix_tracks(lanes,destination,duration_seconds=duration,sample_rate=settings.sample_rate,
                channels=settings.channels,cancelled=cancelled,duck_music=duck_music)

        try:
            for scene_index, scene in enumerate(project.scenes):
                check()
                tracks, timelines, cue_timings = [], {}, []
                cursor_frames = 0
                report(f'Scene {scene_index + 1} of {len(project.scenes)}: {scene.title}')
                for cue in scene.cues:
                    check()
                    if cue.kind == 'pause':
                        cursor_frames += round(cue.seconds * settings.sample_rate)
                    elif cue.kind == 'speech':
                        role = roles[cue.role]
                        cue_start = cursor_frames
                        from .narration import spoken_cue
                        spoken = spoken_cue(project,cue)
                        for start, end, words in speech_chunks(spoken['text'], settings.chunk_characters):
                            if not words.strip():
                                continue
                            voice_input = {'text':words, 'voice_id':role.voice_id, 'language':role.language,
                                           'rate':role.rate, 'volume':role.volume, 'runtime':runtime}
                            voice_file, info = cached('speech', voice_input,
                                lambda destination, words=words, role=role: self.registry.synthesize(
                                    words, destination, voice_id=role.voice_id, language=role.language,
                                    rate=role.rate, volume=role.volume, cancelled=cancelled))
                            track = Track(voice_file, start_seconds=_seconds(cursor_frames, settings.sample_rate),
                                          pan=role.pan, gain_db=cue.gain_db, kind='voice')
                            tracks.append(track)
                            timelines.setdefault(role.id, []).append(track)
                            # Align on output frames, not accumulated floats.
                            cursor_frames += round(info['frames'] * settings.sample_rate / info['sample_rate'])
                        cue_timings.append({'cue_id':cue.id, 'role':role.name, 'text':spoken['text'],
                            'original_text':cue.text, 'reading_copy_sha256':spoken['derivative_sha256'],
                            'source_mapping':spoken['mapping'],
                            'source_start':cue.source_start, 'source_end':cue.source_end,
                            'start_seconds':_seconds(cue_start, settings.sample_rate),
                            'end_seconds':_seconds(cursor_frames, settings.sample_rate),
                            'timing':'measured utterance boundaries; no word alignment'})
                    else:
                        asset_path = folder / 'assets' / (cue.asset + '.wav')
                        if file_sha256(asset_path) != cue.asset:
                            raise ValueError('Imported sound effect checksum changed.')
                        info = probe_audio(asset_path)
                        if cue.seconds and cue.seconds < info.duration_seconds:
                            raise ValueError('Trim an asset explicitly before adding it; no implicit speech or SFX truncation.')
                        tracks.append(Track(asset_path, start_seconds=_seconds(cursor_frames, settings.sample_rate),
                                            gain_db=cue.gain_db, kind='sfx'))
                        cursor_frames += round(info.frames * settings.sample_rate / info.sample_rate)
                    done_steps += 1
                if scene_index < len(project.scenes) - 1:
                    cursor_frames += round(settings.chapter_gap_seconds * settings.sample_rate)
                if cursor_frames < 1:
                    raise ValueError('Scene has no measurable audio or pause duration.')
                duration = _seconds(cursor_frames, settings.sample_rate)
                estimated = cursor_frames * settings.channels * 2 * (2 + len(role_stems))
                disk_check(estimated)
                if scene.music:
                    music_settings = asdict(scene.music)
                    # Real neural context is finite; the product timeline is not.
                    chunk_limit = 25.0 if scene.music.engine == 'musicgen' else 120.0
                    music_frames = 0
                    music_files = []
                    while music_frames < cursor_frames:
                        check()
                        chunk_frames = min(cursor_frames - music_frames, round(chunk_limit * settings.sample_rate))
                        music_duration = _seconds(chunk_frames, settings.sample_rate)
                        # A timeline can end just after a generator boundary. Generate
                        # its minimum supported tail, then crop only the background
                        # in the scene mix to the exact authored sample count.
                        generated_duration = max(0.05, music_duration)
                        seed = (scene.music.seed + len(music_files)) % (2**31)
                        spec = MusicSpec(duration_seconds=generated_duration, tempo_bpm=scene.music.tempo_bpm,
                            key=scene.music.key, mood=scene.music.mood, seed=seed, engine=scene.music.engine,
                            prompt=scene.music.prompt)
                        music_file, music_info = cached('music', {'settings':music_settings, 'seed':seed,
                            'duration_seconds':generated_duration, 'sample_rate':settings.sample_rate,
                            'configuration':music_runtime}, lambda destination, spec=spec:
                            generate_music(spec, destination, sample_rate=settings.sample_rate,
                                           cancelled=cancelled, **self.music_options))
                        tracks.append(Track(music_file, start_seconds=_seconds(music_frames, settings.sample_rate),
                            kind='music', gain_db=scene.music.gain_db,
                            fade_in=min(0.15,music_duration/4), fade_out=min(0.2,music_duration/4)))
                        music_files.append(music_file)
                        music_frames += chunk_frames
                track_inputs = [track_identity(track) for track in tracks]
                scene_file, info = cached('scene', {'tracks':track_inputs, 'frames':cursor_frames,
                    'sample_rate':settings.sample_rate, 'channels':settings.channels},
                    lambda destination: mix_timeline(tracks,destination,duration))
                if abs(info['frames'] - cursor_frames) > 1 or info['sample_rate'] != settings.sample_rate:
                    raise ValueError('Scene render duration does not match its sample timeline.')
                scene_files.append(scene_file)
                scene_info.append({'id':scene.id,'title':scene.title,'start_frame':cumulative_frames,
                    'frames':cursor_frames,'sha256':info['sha256'],'music_engine':scene.music.engine if scene.music else None,
                    'music_continuity':'independent generated chunks with short fades; listening review required' if scene.music else None})
                for timing in cue_timings:
                    timing['start_seconds'] += _seconds(cumulative_frames, settings.sample_rate)
                    timing['end_seconds'] += _seconds(cumulative_frames, settings.sample_rate)
                    timing['scene_id'] = scene.id
                    all_timings.append(timing)
                for role_id in role_stems:
                    selected = timelines.get(role_id, [])
                    stem, stem_info = cached('stem', {'scene':info['sha256'], 'role':role_id,
                        'sample_rate':settings.sample_rate,'frames':cursor_frames},
                        lambda destination, selected=selected: mix_timeline(selected,destination,duration,duck_music=False))
                    role_stems[role_id].append(stem)
                cumulative_frames += cursor_frames
                report(f'Scene {scene_index + 1} saved; {cache_hits} cached steps reused.')
            # The last step streams accepted scene files; no full waveform is
            # concatenated in Python memory, including multi-hour productions.
            report('Assembling master, chapters, subtitles and independent voice stems.')
            master, master_info = cached('master', {'scenes':[file_sha256(path) for path in scene_files],
                'sample_rate':settings.sample_rate,'channels':settings.channels},
                lambda destination: concatenate_audio(scene_files, destination,
                    sample_rate=settings.sample_rate, channels=settings.channels, cancelled=cancelled))
            if abs(master_info['frames'] - cumulative_frames) > len(scene_files):
                raise ValueError('Master duration does not match the accepted scenes.')
            result_outputs = []

            def publish(source, name):
                check()
                target = output / name
                checksum = file_sha256(source)
                if target.exists():
                    if file_sha256(target) != checksum:
                        raise ValueError(f'Existing {name} differs; it was not overwritten.')
                else:
                    stage = output / ('.' + uuid4().hex + '.tmp')
                    try:
                        with Path(source).open('rb') as incoming, stage.open('xb') as outgoing:
                            while block := incoming.read(1024 * 1024):
                                check()
                                outgoing.write(block)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                        if file_sha256(stage) != checksum:
                            raise ValueError('Output checksum changed during publication.')
                        os.link(stage, target)
                    finally:
                        stage.unlink(missing_ok=True)
                item = {'file':name,'sha256':checksum,'size_bytes':target.stat().st_size}
                result_outputs.append(item)
                return target

            publish(master, 'master.wav')
            if settings.format != 'wav':
                compressed = work / ('export.' + settings.format)
                if compressed.exists():
                    # Incomplete optional exports are job-owned, never user sources.
                    plain(compressed)
                    compressed.rename(compressed.with_name(compressed.name + '.incomplete-' + uuid4().hex))
                convert_audio(master, compressed, sample_rate=settings.sample_rate,
                              channels=settings.channels, cancelled=cancelled)
                publish(compressed, 'audio.' + settings.format)
            for index, path in enumerate(scene_files):
                publish(path, f'chapter-{index + 1:04}.wav')
            for role_id, paths in role_stems.items():
                stem, unused = cached('master-stem', {'role':role_id,'chunks':[file_sha256(p) for p in paths],
                    'sample_rate':settings.sample_rate,'channels':settings.channels},
                    lambda destination, paths=paths: concatenate_audio(paths, destination,
                        sample_rate=settings.sample_rate, channels=settings.channels, cancelled=cancelled))
                publish(stem, 'stem-' + role_id + '.wav')
            metadata = {'version':1,'kind':'research-audio-production','project_id':project.id,
                'revision':project.revision,'project_fingerprint':project.fingerprint,
                'script_sha256':project.script_sha256,'job_id':job['id'],'mode':'LOCAL_ONLY',
                'frames':master_info['frames'],'sample_rate':settings.sample_rate,'channels':settings.channels,
                'duration_seconds':master_info['duration_seconds'],'chapters':scene_info,
                'voices':[asdict(role) for role in project.roles], 'timings':all_timings,
                'outputs':list(result_outputs),
                'limitations':['Utterance boundaries are measured; word timing is not inferred.',
                    'Voice pronunciation, music continuity and NVDA speech require independent listening.']}
            # Text artifacts are staged, verified and published like audio.
            atomic_json(work / 'production.json', metadata)
            publish(work / 'production.json', 'production.json')
            subtitle = '\n\n'.join(f'{index + 1}\n{_timecode(row["start_seconds"])} --> {_timecode(row["end_seconds"])}\n{row["text"]}'
                                   for index, row in enumerate(all_timings)) + '\n'
            subtitle_path = work / 'narration.srt'
            subtitle_path.write_text(subtitle, encoding='utf-8')
            publish(subtitle_path, 'narration.srt')
            result = {'job_id':job['id'],'project_id':project.id,'state':'complete','output_dir':str(output),
                      'master':str(output / 'master.wav'),'duration_seconds':master_info['duration_seconds'],
                      'generated_steps':generated,'reused_steps':cache_hits,'outputs':result_outputs}
            job.update(state='complete',progress=100,message='Audio files completed and checksums verified.',
                       outputs=result_outputs,result=result)
            self._verify_outputs(job)
            self._write_job(job)
            progress({'state':'complete','job_id':job['id'],'message':job['message'],'progress':100})
            return result
        except Exception as exc:
            job['state'] = 'paused' if cancelled() or isinstance(exc, InterruptedError) else 'failed'
            # No credentials, provider replies or source paragraphs in job errors.
            message = str(exc)
            job['message'] = (message[:1000] if len(message) < 1000 else type(exc).__name__ + '; see local diagnostic.')
            self._write_job(job)
            progress({'state':job['state'],'job_id':job['id'],'message':job['message'],'progress':job['progress']})
            raise

    def _verify_outputs(self, job):
        output = Path(job['output_dir'])
        plain(output, directory=True)
        if not job['outputs']:
            raise ValueError('A completed job requires actual output files.')
        for row in job['outputs']:
            if (type(row) is not dict or set(row) != {'file','sha256','size_bytes'}
                    or Path(row['file']).name != row['file'] or row['file'] in ('','.','..')):
                raise ValueError('Invalid production output manifest.')
            path = output / row['file']
            if plain(path).st_size != row['size_bytes'] or file_sha256(path) != row['sha256']:
                raise ValueError('Completed production output was changed or removed; it was not overwritten.')

    def import_asset(self, project_id, source, *, cancelled=None):
        """Explicit selected asset, admitted through the existing shared broker."""
        from .render import check_cancelled, convert_audio, local_file, probe_audio
        from ..operations import ResourceRequest
        folder = self.store.path(project_id)
        source = local_file(source)
        stop = (cancelled.is_set if hasattr(cancelled, 'is_set') else cancelled) or (lambda: False)
        if not callable(stop):
            raise ValueError('Invalid asset import cancellation callback or event.')
        check_cancelled(stop)
        def stamp():
            info = plain(source)
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        before = stamp()
        operation = 'director-asset:' + uuid4().hex
        # Inspect under the same shared CPU/RAM policy, then reserve its actual
        # decoded size before creating any private staging/output directory.
        with retained_context(self.broker.reserve(
                ResourceRequest(cpu_slots=2, memory_bytes=512 * 1024**2),
                operation_id=operation + ':inspect', cancelled=stop, timeout=60)) as lease:
            def inspecting():
                if stop():
                    return True
                lease.checkpoint()
                return False
            info = probe_audio(source, cancelled=inspecting)
            if stamp() != before:
                raise ValueError('The selected audio source changed during asset inspection.')
        estimated = math.ceil(info.frames * 24000 / info.sample_rate) * 4 + 65536
        request = ResourceRequest(cpu_slots=2, memory_bytes=512 * 1024**2,
            disk_bytes=2 * estimated + 1024**2, disk_path=folder / 'assets')
        with retained_context(self.broker.reserve(request, operation_id=operation,
                cancelled=stop, timeout=60)) as lease:
            def stopping():
                if stop():
                    return True
                lease.checkpoint()
                return False
            check_cancelled(stopping)
            if stamp() != before:
                raise ValueError('The selected audio source changed while waiting for resources.')
            with retained_temporary_directory(prefix='.asset-', dir=folder) as temp:
                wave_path = Path(temp) / 'asset.wav'
                convert_audio(source, wave_path, sample_rate=24000, channels=2,
                              cancelled=stopping, max_output_bytes=estimated)
                checksum = file_sha256(wave_path)
                target = folder / 'assets' / (checksum + '.wav')
                with self.store.transaction():
                    if target.exists():
                        if file_sha256(target) != checksum:
                            raise ValueError('Existing audio asset is corrupted.')
                    else:
                        os.link(wave_path, target)
                return checksum
