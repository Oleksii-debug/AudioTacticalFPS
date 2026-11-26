"""Actual, local script -> speech chunks -> scenes -> audiobook orchestration.

Successful stages are checksum verified before reuse. A changed line only
invalidates its synthesis and dependent mixes. No waveform is loaded in memory.
"""
from dataclasses import asdict, replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import time
import uuid

from .models import PlanError, Project, split_text
from .store import ProjectStore, atomic_text, digest_file
from ..audio.ffmpeg import _local_path


PIPELINE_VERSION = 'local-studio-2'


class RenderPaused(RuntimeError):
    pass


class RenderCancelled(RuntimeError):
    pass


def _key(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                             default=str, allow_nan=False).encode('utf-8')).hexdigest()


def _report(value):
    result = asdict(value) if not isinstance(value, dict) else dict(value)
    result['path'] = str(result['path'])
    return result


def _timestamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    whole, ms = divmod(rest, 1000)
    return f'{hours:02}:{minutes:02}:{whole:02},{ms:03}'


def _checked_digest(path, check):
    """Hash large selected inputs with the same stop/budget checks as copying."""
    digest = sha256()
    with Path(path).open('rb') as stream:
        while True:
            check()
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _write_once(path, text):
    path = Path(path)
    if path.exists():
        if path.is_symlink() or path.read_text(encoding='utf-8') != text:
            raise FileExistsError(f'Existing project output differs: {path.name}')
    else:
        atomic_text(path, text)


class Director:
    """One renderer per project; independent projects can run concurrently."""
    def __init__(self, store: ProjectStore, *, audio=None, speech_renderer=None, engine_identity=None):
        self.store = store
        if audio is None:
            from ..audio import FFmpegAudio
            audio = FFmpegAudio(allowed_roots=(store.root,))
        self.audio = audio
        self._native_speech = speech_renderer is None
        if speech_renderer is None or engine_identity is None:
            from ..speech import engine_fingerprint, render_speech
            speech_renderer = speech_renderer or render_speech
            engine_identity = engine_identity or engine_fingerprint
        self.speech_renderer, self.engine_identity = speech_renderer, engine_identity

    def _snapshot_clip(self, supplied, asset_root, allowed_roots, check):
        supplied = _local_path(supplied)
        source = (asset_root / supplied if not supplied.is_absolute() else supplied)
        if source.is_symlink():
            raise PlanError('Prepared audio cannot be a symbolic link.')
        source = source.resolve(strict=True)
        if not any(source.is_relative_to(root) for root in allowed_roots):
            raise PlanError('Prepared audio is outside the explicitly selected audio folders.')
        if not source.is_file():
            raise PlanError('Prepared audio must be a regular local file.')
        before = source.stat()
        digest = _checked_digest(source, check)
        key = _key(('input-audio', digest))
        cached = self.store.cached(key)
        if cached is not None:
            return cached, 0
        check(before.st_size)
        target = self.store.cache / f'input-{digest}-{uuid.uuid4().hex[:8]}{source.suffix.lower()}'
        try:
            with source.open('rb') as incoming, target.open('xb') as output:
                copied = 0
                while block := incoming.read(1024 * 1024):
                    check()
                    copied += len(block)
                    if copied > before.st_size:
                        raise PlanError('Prepared audio changed while it was being copied.')
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            after = source.stat()
            if ((before.st_size, before.st_mtime_ns, before.st_ino) !=
                    (after.st_size, after.st_mtime_ns, after.st_ino) or _checked_digest(target, check) != digest):
                raise PlanError('Prepared audio changed while it was being copied.')
            report = {'path': str(target), 'sha256': digest, 'size_bytes': target.stat().st_size}
            self.store.accept_chunk(key, target, report)
            return report, report['size_bytes']
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def render(self, project: Project, *, asset_root=None, allowed_audio_roots=(), cancelled=None,
               paused=None, progress=None):
        from ..audio import Effect, Track
        from ..speech import SpeechRequest
        if not isinstance(project, Project):
            raise PlanError('Validate the project before rendering.')
        asset_root = _local_path(asset_root or self.store.root).resolve(strict=True)
        roots = tuple(_local_path(p).resolve(strict=True) for p in allowed_audio_roots) or (asset_root,)
        cancelled = cancelled or (lambda: False)
        paused = paused or (lambda: False)
        notify = progress or (lambda data: None)
        with self.store.render_lock():
            revision = self.store.save(project)
            job = self.store.start_job(revision)
            started = time.monotonic()
            used_bytes = sum(p.stat().st_size for p in self.store.root.rglob('*') if p.is_file() and not p.is_symlink())
            state = {'job_id': job, 'revision': revision, 'scene': 0, 'scene_count': len(project.scenes),
                     'completed_chunks': 0, 'reused_chunks': 0, 'message': 'Validating local render'}

            def check(estimate=0, *, allow_stop=True):
                if allow_stop and cancelled():
                    raise RenderCancelled('Render cancelled. Verified chunks are retained for resume.')
                if allow_stop and paused():
                    raise RenderPaused('Render paused. Reopen the project to resume.')
                if project.budget.wall_seconds is not None and time.monotonic() - started > project.budget.wall_seconds:
                    raise RenderPaused('Configured run-time budget reached. Verified chunks are retained.')
                if used_bytes + estimate > project.budget.disk_bytes:
                    raise RenderPaused('Configured project disk budget reached. Increase it before resuming.')
                if shutil.disk_usage(self.store.root).free < estimate + 16 * 1024**2:
                    raise RenderPaused('Not enough free disk for this render stage. Verified chunks are retained.')

            def stop():
                check()
                return False

            def accept(cache_key, path, report):
                nonlocal used_bytes
                size = path.stat().st_size
                # The renderer must not publish a new cache entry over budget,
                # including custom renderers supplied by a caller.
                try:
                    check(size, allow_stop=False)
                    self.store.accept_chunk(cache_key, path, report)
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
                used_bytes += size

            def audio_stage(operation, *args, **kwargs):
                """Apply the remaining project allowance to real PCM stages."""
                check()
                previous = self.audio.max_output_bytes
                remaining = max(1, project.budget.disk_bytes - used_bytes)
                self.audio.max_output_bytes = min(previous, remaining) if previous is not None else remaining
                try:
                    return operation(*args, **kwargs)
                except ValueError as exc:
                    if 'byte budget' in str(exc):
                        raise RenderPaused('Configured project disk budget reached. Increase it before resuming.') from exc
                    raise
                finally:
                    self.audio.max_output_bytes = previous

            def announce(message):
                state['message'] = message
                state['elapsed_seconds'] = round(time.monotonic() - started, 3)
                self.store.checkpoint(job, progress=state)
                notify(dict(state))

            roles = {r.id: r for r in project.roles}
            voices = {}
            scene_reports, captions, chapters, music_reports = [], [], [], []
            global_time = 0.0
            try:
                check()
                audio_identity = self.audio.engine_fingerprint(cancelled=stop)
                for role in project.roles:
                    voices[role.id] = self.engine_identity(role.voice_id)
                for scene_number, scene in enumerate(project.scenes, 1):
                    state['scene'] = scene_number
                    announce(f'Scene {scene_number} of {len(project.scenes)}: {scene.title}')
                    check()
                    cursor = 0.0
                    tracks, track_keys, track_durations, local_captions = [], [], [], []
                    for line in scene.lines:
                        role = roles[line.role]
                        for start, end, text in split_text(line.text, project.budget.chunk_characters):
                            check()
                            if not text.strip():
                                continue
                            request = SpeechRequest(text=text, voice_id=role.voice_id, language=role.language,
                                                    rate=role.rate, pitch=role.pitch, volume=role.volume)
                            cache_key = _key((PIPELINE_VERSION, 'speech', asdict(request), voices[role.id], line.take))
                            cached = self.store.cached(cache_key)
                            if cached is None:
                                announce(f'Rendering {scene.title}, {line.role}, characters {start + 1} to {end}')
                                path = self.store.new_chunk_path(cache_key)
                                speech_options = {'cancelled': stop}
                                if self._native_speech:
                                    speech_options['max_output_bytes'] = max(1, project.budget.disk_bytes - used_bytes)
                                from ..speech.local import SpeechBudgetExceeded
                                try:
                                    result = self.speech_renderer(request, path, **speech_options)
                                except SpeechBudgetExceeded as exc:
                                    raise RenderPaused('Configured project disk budget reached. Increase it before resuming.') from exc
                                cached = _report(result)
                                if cached.get('engine_fingerprint') != voices[role.id]:
                                    raise PlanError('The voice engine changed during synthesis. Reopen and render the revised engine explicitly.')
                                accept(cache_key, path, cached)
                                state['completed_chunks'] += 1
                            else:
                                state['reused_chunks'] += 1
                            duration = cached['frames'] / cached['sample_rate']
                            # Align every event to the final sample grid, never approximate with text length.
                            duration = round(duration * project.sample_rate) / project.sample_rate
                            tracks.append(Track(Path(cached['path']), start_seconds=cursor, gain_db=role.gain_db,
                                                pan=role.pan, role='speech', routing=role.routing))
                            track_keys.append((cached['sha256'], cursor, role.pan, role.gain_db, role.routing))
                            track_durations.append(duration)
                            local_captions.append(dict(scene=scene.id, line=line.id, role=line.role, text=text,
                                source_start=start, source_end=end, start=cursor, end=cursor + duration,
                                timing='rendered_chunk_boundaries', original_text_unchanged=True))
                            cursor += duration
                        cursor += round(line.pause_after * project.sample_rate) / project.sample_rate
                    for clip in scene.clips:
                        snapshot, added_bytes = self._snapshot_clip(clip.path, asset_root, roots, check)
                        used_bytes += added_bytes
                        # Normalize through the actual decoder, then reuse the immutable normalized artifact.
                        normalized_key = _key((PIPELINE_VERSION, audio_identity, 'prepared-audio', snapshot['sha256'], project.sample_rate, project.channels))
                        normalized = self.store.cached(normalized_key)
                        if normalized is None:
                            target = self.store.new_chunk_path(normalized_key)
                            normalized = _report(audio_stage(self.audio.convert, Path(snapshot['path']), target,
                                sample_rate=project.sample_rate, channels=project.channels, cancelled=stop))
                            accept(normalized_key, target, normalized)
                        tracks.append(Track(Path(normalized['path']), start_seconds=clip.start_seconds,
                                            gain_db=clip.gain_db, pan=clip.pan, role=clip.role))
                        track_keys.append((normalized['sha256'], clip.start_seconds, clip.gain_db, clip.pan, clip.role))
                        track_durations.append(normalized['duration_seconds'])
                        cursor = max(cursor, clip.start_seconds + normalized['duration_seconds'])
                    for cue in scene.music:
                        from ..audio.music import MusicRequest, render_music, music_engine_fingerprint
                        cue_duration = cue.duration if cue.duration is not None else (scene.duration or cursor) - cue.start_seconds
                        if cue_duration <= 0:
                            raise PlanError('A music-only scene needs an explicit cue or scene duration.')
                        assets = {}
                        for name in ('model_dir', 'manifest_path', 'soundfont'):
                            raw = getattr(cue, name)
                            if raw is not None:
                                supplied = _local_path(raw)
                                selected = asset_root / supplied if not supplied.is_absolute() else supplied
                                if selected.is_symlink():
                                    raise PlanError('Music assets cannot be symbolic links.')
                                selected = selected.resolve(strict=True)
                                if not any(selected.is_relative_to(root) for root in roots):
                                    raise PlanError('Music assets are outside the explicitly selected audio folders.')
                                assets[name] = selected
                        for name in ('expected_manifest_sha256', 'soundfont_sha256', 'soundfont_license'):
                            if getattr(cue, name) is not None:
                                assets[name] = getattr(cue, name)
                        # Evenly sized windows avoid an invalid sub-100ms tail
                        # at an engine boundary, while keeping every inference
                        # at or below that engine's real context window.
                        windows = max(1, math.ceil((cue_duration - cue.crossfade_seconds) /
                                                  (cue.chunk_seconds - cue.crossfade_seconds)))
                        window_length = (cue_duration + (windows - 1) * cue.crossfade_seconds) / windows
                        offset, window = 0.0, 0
                        while offset < cue_duration - 1 / project.sample_rate:
                            check()
                            piece = min(window_length, cue_duration - offset)
                            request = MusicRequest(cue.prompt, piece, cue.engine, (cue.seed + window) % 2**32,
                                cue.tempo, cue.root_note, cue.mode, cue.program, project.sample_rate)
                            engine_id = music_engine_fingerprint(request, **assets)
                            music_key = _key((PIPELINE_VERSION, 'generated-music', asdict(request), engine_id))
                            generated = self.store.cached(music_key)
                            if generated is None:
                                check(int(piece * project.sample_rate * 4) + 8192)
                                announce(f'Generating local {cue.engine} music, scene {scene_number}, window {window + 1}')
                                target = self.store.new_chunk_path(music_key)
                                generated = _report(render_music(request, target, **assets, cancelled=stop,
                                    allowed_roots=roots + (self.store.root,)))
                                accept(music_key, target, generated)
                                state['completed_chunks'] += 1
                            else:
                                state['reused_chunks'] += 1
                            last = offset + piece >= cue_duration - 1 / project.sample_rate
                            effects = []
                            if cue.crossfade_seconds and window:
                                effects.append(Effect('fade_in', {'seconds': min(cue.crossfade_seconds, piece)}))
                            if cue.crossfade_seconds and not last:
                                effects.append(Effect('fade_out', {'start_seconds': piece - cue.crossfade_seconds,
                                                                  'seconds': cue.crossfade_seconds}))
                            tracks.append(Track(Path(generated['path']), start_seconds=cue.start_seconds + offset,
                                gain_db=cue.gain_db, role='music', effects=tuple(effects), routing='preserve'))
                            track_keys.append((generated['sha256'], cue.start_seconds + offset, cue.gain_db, asdict(cue), window))
                            track_durations.append(generated['duration_seconds'])
                            window += 1
                            offset = cue_duration if last else offset + piece - cue.crossfade_seconds
                        music_reports.append(dict(scene=scene.id, cue=cue.id, engine=cue.engine, windows=window,
                            duration_seconds=cue_duration, generation='independent seeded windows' if cue.engine == 'musicgen' else 'deterministic MIDI arpeggio',
                            actual_neural_continuation=False, crossfade_seconds=cue.crossfade_seconds,
                            repeated_audio_loops=False, listening_continuity_review='NOT_RUN'))
                        cursor = max(cursor, cue.start_seconds + cue_duration)
                    if scene.duration is not None and scene.duration + 1 / project.sample_rate < cursor:
                        raise PlanError(f'Scene {scene.title} is longer than its requested duration. No speech, clip or music was truncated.')
                    duration = scene.duration if scene.duration is not None else cursor
                    if duration <= 0:
                        raise PlanError('An audio scene must have positive duration.')
                    # Hierarchical role stems bound simultaneous decoders for
                    # arbitrarily many narration/music chunks. A music stem is
                    # kept separate until the final speech-driven ducking pass.
                    grouped = {kind: [] for kind in ('speech', 'music', 'effect')}
                    for track, signature, length in zip(tracks, track_keys, track_durations):
                        grouped[track.role].append((track, signature, length))
                    assembled = []
                    for kind, entries in grouped.items():
                        entries.sort(key=lambda entry: entry[0].start_seconds)
                        while len(entries) > 32:
                            reduced = []
                            for begin in range(0, len(entries), 32):
                                group = entries[begin:begin + 32]
                                if len(group) == 1:
                                    reduced.extend(group)
                                    continue
                                origin = group[0][0].start_seconds
                                span = max(t.start_seconds + d for t, _, d in group) - origin
                                group_key = _key((PIPELINE_VERSION, audio_identity, 'role-stem', kind, [k for _, k, _ in group], origin,
                                                  span, project.sample_rate, project.channels))
                                stem = self.store.cached(group_key)
                                if stem is None:
                                    check(int(span * project.sample_rate * project.channels * 2) + 8192)
                                    target = self.store.new_chunk_path(group_key)
                                    stem = _report(audio_stage(self.audio.render_timeline,
                                        [replace(t, start_seconds=t.start_seconds - origin) for t, _, _ in group],
                                        target, duration=span, sample_rate=project.sample_rate,
                                        channels=project.channels, cancelled=stop, ducking=False))
                                    accept(group_key, target, stem)
                                reduced.append((Track(Path(stem['path']), start_seconds=origin, role=kind, routing='preserve'),
                                                (group_key, stem['sha256']), stem['duration_seconds']))
                            entries = reduced
                        assembled.extend(entries)
                    tracks = [t for t, _, _ in assembled]
                    estimate = int(duration * project.sample_rate) * project.channels * 2 + 8192
                    mix_key = _key((PIPELINE_VERSION, audio_identity, 'scene-mix', track_keys, duration, project.sample_rate, project.channels))
                    mixed = self.store.cached(mix_key)
                    if mixed is None:
                        check(estimate)
                        announce(f'Mixing {scene.title}; {duration:.1f} seconds')
                        target = self.store.new_chunk_path(mix_key)
                        mixed = _report(audio_stage(self.audio.render_timeline, tracks, target, duration=duration,
                            sample_rate=project.sample_rate, channels=project.channels, cancelled=stop))
                        accept(mix_key, target, mixed)
                    else:
                        state['reused_chunks'] += 1
                    chapters.append(dict(id=scene.id, title=scene.title, start_seconds=global_time,
                                         duration_seconds=mixed['duration_seconds'], sha256=mixed['sha256'], path=mixed['path']))
                    for caption in local_captions:
                        captions.append({**caption, 'start': caption['start'] + global_time, 'end': caption['end'] + global_time})
                    global_time += mixed['frames'] / mixed['sample_rate']
                    scene_reports.append(mixed)
                    announce(f'Completed scene {scene_number} of {len(project.scenes)}')
                final_key = _key((PIPELINE_VERSION, audio_identity, 'master', [r['sha256'] for r in scene_reports], project.sample_rate, project.channels))
                master = self.store.cached(final_key)
                if master is None:
                    check(int(global_time * project.sample_rate * project.channels * 2) + 8192)
                    announce('Assembling the audiobook master')
                    target = self.store.new_chunk_path(final_key)
                    master = _report(audio_stage(self.audio.concatenate_segments, [Path(r['path']) for r in scene_reports], target,
                        sample_rate=project.sample_rate, channels=project.channels, cancelled=stop))
                    accept(final_key, target, master)
                else:
                    state['reused_chunks'] += 1
                output_dir = self.store.renders / revision
                if output_dir.is_symlink():
                    raise PlanError('Render folder must not be a symbolic link.')
                output_dir.mkdir(exist_ok=True)
                final_path = output_dir / 'master.wav'
                if os.path.lexists(final_path):
                    if final_path.is_symlink() or digest_file(final_path) != master['sha256']:
                        raise PlanError('Existing master differs; original output was retained.')
                else:
                    os.link(master['path'], final_path)
                manifest = dict(schema_version=1, pipeline=f'{PIPELINE_VERSION}:{audio_identity}', project_revision=revision,
                    title=project.title, mode='LOCAL_ONLY', path_base='project_root',
                    master={**master, 'path': str(final_path.relative_to(self.store.root))},
                    chapters=[{**chapter, 'path': str(Path(chapter['path']).relative_to(self.store.root))} for chapter in chapters],
                    captions=captions, voice_fingerprints=voices, music=music_reports,
                    source_reference=project.source_reference, source_text_unchanged=True,
                    listening_review='NOT_RUN', word_alignment='NOT_RUN', nvda_review='NOT_RUN')
                _write_once(output_dir / 'production.json', json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
                srt = '\n\n'.join(f'{i}\n{_timestamp(c["start"])} --> {_timestamp(c["end"])}\n{c["text"].strip()}'
                                    for i, c in enumerate(captions, 1)) + '\n'
                _write_once(output_dir / 'captions.srt', srt)
                result = {**manifest, 'master': {**master, 'path': str(final_path)}, 'manifest_path': str(output_dir / 'production.json'),
                          'captions_path': str(output_dir / 'captions.srt'), 'job_id': job,
                          'completed_chunks': state['completed_chunks'], 'reused_chunks': state['reused_chunks']}
                state['message'] = 'Render complete; master and production manifest verified.'
                self.store.checkpoint(job, state='complete', progress=state, result=result)
                notify(dict(state))
                return result
            except BaseException as exc:
                status = 'paused' if isinstance(exc, (RenderPaused, KeyboardInterrupt)) else 'cancelled' if isinstance(exc, RenderCancelled) else 'failed'
                self.store.checkpoint(job, state=status, progress=state, error=str(exc))
                raise
