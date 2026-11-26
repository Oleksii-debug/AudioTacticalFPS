"""Typed script-to-audio orchestration with durable, independently cached steps."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import uuid

from . import ffmpeg as audio
from .jobs import (ChunkCache, RenderCancelled, atomic_json, checked_directory,
                   check_cancelled, project_lock, publish_new, regular_file, sha256_file)
from .models import (Effect, Line, MusicCue, Pause, ProductionPlan, Role, Scene,
                     Sound, canonical, digest, text)


SCRIPT_HELP = '''Local audio script format
# Chapter title
[role Narrator voice="sapi5:default" pan=center language=en]
[role Alice voice="sapi5:default" pan=left]
[role Bob voice="sapi5:default" pan=right]
[music mood=calm tempo=90 gain=0.15 seed=7]
Narrator: This exact text will be spoken.
Alice: Hello.
[pause 2]
Bob: Hello again.
[sound door.wav gain=0.5 pan=right]

Plain lines use Narrator. Put local PCM WAV effects in the project's assets
folder. Use [music off] for a dry scene. Supported role settings: voice,
language, pan (-1 to 1, left/center/right), rate (-10 to 10), pitch (-10 to
10), volume (0 to 1). Unsupported voice controls fail explicitly.
[effect Narrator highpass frequency=80]
[effect Narrator compressor threshold_db=-20 ratio=3 attack_ms=20 release_ms=250]
[output format=wav sample_rate=48000 stems=true disk_budget_mb=32768]
Effects: highpass, lowpass, equalizer, gate, compressor, delay, denoise.
Optional MusicGen: [music engine=musicgen model_manifest=musicgen/manifest.json]
The manifest and its reviewed assets must be installed under project/models.
This is a guided local script parser. It does not interpret arbitrary prose
as editing instructions and never executes shell commands or document code.
'''


def _options(tokens, allowed):
    result = {}
    for token in tokens:
        if '=' not in token:
            raise ValueError('Use name=value for directive settings.')
        key, value = token.split('=', 1)
        if key not in allowed or key in result or not value:
            raise ValueError(f'Unknown, empty or repeated directive setting: {key}')
        result[key] = value
    return result


def _pan(value):
    return {'left': -1.0, 'center': 0.0, 'right': 1.0}.get(value, None) if value in ('left', 'center', 'right') else float(value)


def parse_script(script, *, title='Audio project', voice='sapi5:default', language='en'):
    """Parse explicit, bounded instructions; preserve the original script verbatim."""
    text(script, 'Script', 16 * 1024 * 1024)
    roles = {'Narrator': Role('Narrator', voice=voice, language=language)}
    scenes, events, scene_title, music = [], [], title, None
    serial = 0
    settings = {}
    used_roles = set()

    def event_id():
        nonlocal serial
        serial += 1
        return f'event-{serial:08d}'

    def finish():
        nonlocal events
        if events:
            scenes.append(Scene(f'scene-{len(scenes)+1:06d}', scene_title, tuple(events), music))
            events = []

    for lineno, original in enumerate(script.splitlines(), 1):
        line = original.strip()
        if not line:
            continue
        try:
            if line.startswith('# '):
                finish()
                scene_title, music = line[2:].strip(), None
                text(scene_title, 'Chapter title', 500)
                continue
            if line.startswith('['):
                if not line.endswith(']'):
                    raise ValueError('Close the production directive with ].')
                parts = shlex.split(line[1:-1], posix=True)
                if not parts:
                    raise ValueError('Empty production directive.')
                kind = parts[0]
                if kind == 'pause' and len(parts) == 2:
                    events.append(Pause(event_id(), float(parts[1])))
                elif kind == 'role' and len(parts) >= 2:
                    name = parts[1]
                    if name in used_roles:
                        raise ValueError('Define role settings before that role speaks. Use a different role name for a later voice change.')
                    p = _options(parts[2:], ('voice', 'language', 'pan', 'rate', 'pitch', 'volume'))
                    for key in ('rate', 'pitch'):
                        if key in p:
                            p[key] = int(p[key])
                    for key in ('volume',):
                        if key in p:
                            p[key] = float(p[key])
                    if 'pan' in p:
                        p['pan'] = _pan(p['pan'])
                    roles[name] = replace(roles.get(name, Role(name, voice=voice, language=language)), **p)
                elif kind == 'music':
                    if parts[1:] == ['off']:
                        music = None
                    else:
                        p = _options(parts[1:], ('mood', 'tempo', 'gain', 'seed', 'engine', 'model_manifest'))
                        for key in ('tempo', 'seed'):
                            if key in p:
                                p[key] = int(p[key])
                        if 'gain' in p:
                            p['gain'] = float(p['gain'])
                        music = MusicCue(**p)
                elif kind == 'sound' and len(parts) >= 2:
                    p = _options(parts[2:], ('gain', 'pan'))
                    if 'gain' in p:
                        p['gain'] = float(p['gain'])
                    if 'pan' in p:
                        p['pan'] = _pan(p['pan'])
                    events.append(Sound(event_id(), parts[1], **p))
                elif kind == 'effect' and len(parts) >= 3:
                    from .models import EFFECT_PARAMETERS
                    role_name, effect_name = parts[1:3]
                    if role_name in used_roles:
                        raise ValueError('Define a role effect before that role speaks.')
                    if role_name not in roles or effect_name not in EFFECT_PARAMETERS:
                        raise ValueError('Declare the role and choose a supported effect first.')
                    p = _options(parts[3:], EFFECT_PARAMETERS[effect_name])
                    effect = Effect(effect_name, tuple((key, float(value)) for key, value in p.items()))
                    roles[role_name] = replace(roles[role_name], effects=(*roles[role_name].effects, effect))
                elif kind == 'output':
                    p = _options(parts[1:], ('format', 'sample_rate', 'stems', 'disk_budget_mb'))
                    if 'format' in p:
                        settings['output_format'] = p['format']
                    if 'sample_rate' in p:
                        settings['sample_rate'] = int(p['sample_rate'])
                    if 'stems' in p:
                        if p['stems'] not in ('true', 'false'):
                            raise ValueError('Stems must be true or false.')
                        settings['export_stems'] = p['stems'] == 'true'
                    if 'disk_budget_mb' in p:
                        settings['disk_budget_bytes'] = int(p['disk_budget_mb']) * 1024 ** 2
                else:
                    raise ValueError(f'Unsupported production directive: {kind}')
                continue
            if ':' in line:
                name, content = line.split(':', 1)
                if name in roles or (len(name) <= 80 and not any(c in name for c in '/\\[]')):
                    roles.setdefault(name, Role(name, voice=voice, language=language))
                    events.append(Line(event_id(), name, content[1:] if content.startswith(' ') else content))
                    used_roles.add(name)
                    continue
            events.append(Line(event_id(), 'Narrator', original))
            used_roles.add('Narrator')
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f'Script line {lineno}: {exc}') from exc
    finish()
    return ProductionPlan(title, script, tuple(roles.values()), tuple(scenes), **settings)


def _read_json(path, limit=16 * 1024 ** 2):
    if regular_file(path).st_size > limit:
        raise ValueError('Audio project manifest is too large.')
    return json.loads(Path(path).read_text('utf-8'))


def save_plan(plan, path):
    if not isinstance(plan, ProductionPlan):
        raise ValueError('Expected a validated production plan.')
    atomic_json(path, plan.to_dict())


def load_plan(path):
    return ProductionPlan.from_dict(_read_json(path))


def _timestamp(frames, rate, *, srt=False):
    ms = round(frames * 1000 / rate)
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02}{"," if srt else "."}{ms:03}'


def speech_segments(value, maximum=7000):
    """Bound requests at sentence/word boundaries without dropping a character.

    Splitting is an execution detail. The source and line-level subtitle retain
    the full exact string; this is not a paraphrasing or punctuation model.
    """
    if not isinstance(value, str) or type(maximum) is not int or maximum < 1:
        raise ValueError('Invalid speech segment limit or text.')
    pieces, start = [], 0
    while start < len(value):
        end = min(len(value), start + maximum)
        if end < len(value):
            lower = start + maximum // 2
            stops = [value.rfind(marker, lower, end) for marker in ('. ', '! ', '? ', '\n', ' ', '\t')]
            boundary = max(stops)
            if boundary >= lower:
                end = boundary + 1
        pieces.append(value[start:end])
        start = end
    return tuple(pieces)


def _metadata_escape(value):
    return str(value).replace('\\', '\\\\').replace('\r', '').replace('\n', '\\\n').replace('=', '\\=').replace(';', '\\;').replace('#', '\\#')


def _write_text_new(path, value):
    # Caller writes only inside its private stage. Final directory is published last.
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _verified_output(directory, run_key, cancelled):
    marker = directory / 'complete.json'
    if not marker.exists():
        return None
    try:
        result = _read_json(marker)
        if result.get('schema_version') != 1 or result.get('run_key') != run_key or result.get('status') != 'complete':
            return None
        names = set()
        for record in result['files']:
            name = record['name']
            if not isinstance(name, str) or Path(name).name != name or name in ('.', '..'):
                return None
            path = directory / name
            if regular_file(path).st_size != record['size'] or sha256_file(path, cancelled=cancelled) != record['sha256']:
                return None
            if name in names:
                return None
            names.add(name)
        if result.get('output_name') not in names or not isinstance(result.get('stem_names'), list):
            return None
        if any(not isinstance(name, str) or name not in names for name in result['stem_names']):
            return None
        if 'subtitles.srt' not in names:
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _public_result(result, directory):
    return {**result, 'output': str(directory / result['output_name']),
            'manifest': str(directory / 'complete.json'),
            'subtitles': str(directory / 'subtitles.srt'),
            'stems': [str(directory / name) for name in result['stem_names']]}


def render_project(plan, project_dir, *, engine, ffmpeg, cancelled=lambda: False,
                   on_status=lambda message: None):
    """Render actual local audio and checkpoints; successful chunks survive errors.

    Engine adapters own local voice setup. This core never downloads a model,
    invokes an external API, evaluates script code or silently overwrites output.
    """
    if not isinstance(plan, ProductionPlan):
        raise ValueError('Expected a validated production plan.')
    if not callable(getattr(engine, 'synthesize', None)):
        raise ValueError('Choose a configured local speech engine.')
    root = checked_directory(project_dir, create=True)
    check_cancelled(cancelled)
    with project_lock(root):
        return _render_locked(plan, root, engine=engine, ffmpeg=ffmpeg, cancelled=cancelled, on_status=on_status)


def _render_locked(plan, root, *, engine, ffmpeg, cancelled, on_status):
    roles = {role.name: role for role in plan.roles}
    identities = {}
    used_roles = {event.role for scene in plan.scenes for event in scene.events if isinstance(event, Line)}
    for role in plan.roles:
        if role.name not in used_roles:
            continue
        identify = getattr(engine, 'cache_identity', None)
        # Unknown engines cannot reuse prior speech across process invocations.
        identities[role.name] = identify(role.voice) if callable(identify) else {'unverified_engine_session': uuid.uuid4().hex}
    processor = audio.ffmpeg_identity(ffmpeg)
    assets = {}
    music_models, music_identities = {}, {}
    composer_identity = None
    for scene in plan.scenes:
        if scene.music is not None:
            from . import music as local_music
            composer_identity = {'implementation_sha256': sha256_file(local_music.__file__),
                                 'version': local_music.COMPOSER_VERSION}
            if scene.music.model_manifest is not None and scene.music.model_manifest not in music_models:
                from .model_manifest import LocalModelManifest
                location = checked_directory(root / 'models').joinpath(*scene.music.model_manifest.split('/'))
                model = LocalModelManifest.load(location, engine=scene.music.engine)
                model.verify(cancelled=cancelled)
                music_models[scene.music.model_manifest] = model
                music_identities[scene.music.model_manifest] = model.cache_identity()
        for event in scene.events:
            if isinstance(event, Sound) and event.asset not in assets:
                asset = checked_directory(root / 'assets') / event.asset
                audio.wav_info(asset)
                assets[event.asset] = sha256_file(asset, cancelled=cancelled)
    run_key = digest({'pipeline_version': 1, 'plan': plan.to_dict(), 'engines': identities,
                      'processor': processor, 'assets': assets, 'music_models': music_identities,
                      'composer': composer_identity})
    cache = ChunkCache(root / 'cache', cancelled=cancelled, disk_budget_bytes=plan.disk_budget_bytes)
    renders = checked_directory(root / 'renders', create=True)
    output_dir = renders / run_key
    if output_dir.is_symlink():
        raise ValueError('Unsafe existing output directory.')
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ValueError('The output destination is not a directory.')
        previous = _verified_output(output_dir, run_key, cancelled)
        if previous:
            on_status('This exact production is already complete; verified its output checksums.')
            return _public_result(previous, output_dir)
        quarantine = checked_directory(root / 'quarantine', create=True)
        output_dir.rename(quarantine / (run_key + '.' + uuid.uuid4().hex))
    journal = root / 'render-journal.json'
    atomic_json(root / ('plan-' + run_key + '.json'), plan.to_dict())
    state = {'schema_version': 1, 'run_key': run_key, 'state': 'running', 'completed_steps': 0,
             'scene_count': len(plan.scenes), 'source_sha256': plan.source_sha256}
    atomic_json(journal, state)
    completed = 0
    scene_audio, scene_stems, chapters, subtitles, timeline, music_companions = [], [], [], [], [], []
    position = 0
    role_names = [r.name for r in plan.roles if r.name in used_roles]

    def checkpoint(message):
        nonlocal completed
        completed += 1
        state.update(completed_steps=completed, message=message)
        atomic_json(journal, state)
        on_status(message)
        check_cancelled(cancelled)

    try:
        with tempfile.TemporaryDirectory(prefix='.work-', dir=root) as temporary:
            temp = Path(temporary)

            def cached(settings, create, *, provenance=None, estimate=0, companions=()):
                key = digest({'pipeline_version': 1, **settings})
                ready = cache.get(key)
                if ready is not None:
                    return ready
                cache.reserve(estimate)
                stage = temp / (key + '.wav')
                if stage.exists():
                    stage.unlink()
                try:
                    create(stage)
                    check_cancelled(cancelled)
                    result = cache.seal(key, stage, provenance=provenance, companions=companions)
                    checkpoint('Completed and saved an audio step.')
                    return result
                finally:
                    if stage.exists():
                        stage.unlink()
                    for suffix in companions:
                        stage.with_suffix(suffix).unlink(missing_ok=True)

            def quiet(frames):
                return cached({'kind': 'silence', 'frames': frames, 'rate': plan.sample_rate},
                    lambda path: audio.silence(path, frames, sample_rate=plan.sample_rate, cancelled=cancelled),
                    estimate=frames * 4 + 80)

            for scene_index, scene in enumerate(plan.scenes):
                check_cancelled(cancelled)
                on_status(f'Rendering chapter {scene_index+1} of {len(plan.scenes)}: {scene.title}')
                segments, timed, stem_parts = [], [], {name: [] for name in [*role_names, 'Sound effects']}
                local_position = 0
                for event in scene.events:
                    check_cancelled(cancelled)
                    role_name = None
                    if isinstance(event, Line):
                        role = roles[event.role]
                        spoken_parts = []
                        for piece in speech_segments(event.text):
                            if not piece.strip():
                                spoken_parts.append(quiet(1))
                                continue
                            speech_settings = {'kind': 'speech', 'text': piece, 'voice': role.voice,
                                               'language': role.language, 'rate': role.rate, 'pitch': role.pitch,
                                               'volume': role.volume, 'engine': identities[role.name]}
                            raw, raw_info = cached(speech_settings,
                                lambda path, content=piece, r=role: engine.synthesize(content, path, voice=r.voice,
                                    language=r.language, rate=r.rate, pitch=r.pitch, volume=r.volume, cancelled=cancelled),
                                provenance={'kind': 'speech', 'engine': identities[role.name], 'text_sha256': hashlib.sha256(piece.encode('utf-8')).hexdigest()})
                            processed = cached({'kind': 'voice_processing', 'raw_sha256': raw_info['sha256'],
                                'sample_rate': plan.sample_rate, 'pan': role.pan, 'effects': [asdict(e) for e in role.effects], 'processor': processor},
                                lambda path, source=raw, r=role: audio.process_voice(source, path, ffmpeg=ffmpeg,
                                    sample_rate=plan.sample_rate, pan=r.pan, effects=r.effects, cancelled=cancelled),
                                estimate=round(raw_info['frames'] / raw_info['sample_rate'] * plan.sample_rate * 4) + 80)
                            spoken_parts.append(processed)
                        if len(spoken_parts) == 1:
                            chunk, info = spoken_parts[0]
                        else:
                            chunk, info = cached({'kind': 'long_line', 'parts': [i['sha256'] for _, i in spoken_parts]},
                                lambda path: audio.concatenate([p for p, _ in spoken_parts], path, cancelled=cancelled),
                                estimate=sum(i['frames'] for _, i in spoken_parts) * 4 + 80)
                        role_name = role.name
                        subtitles.append({'role': role.name, 'text': event.text, 'start_frame': position + local_position,
                                          'end_frame': position + local_position + info['frames'], 'event_id': event.id,
                                          'timing': 'Measured line boundaries; word timings unavailable.'})
                    elif isinstance(event, Pause):
                        chunk, info = quiet(max(1, round(event.seconds * plan.sample_rate)))
                    else:
                        asset_root = checked_directory(root / 'assets')
                        asset = asset_root / event.asset
                        raw_info = audio.wav_info(asset)
                        source_hash = sha256_file(asset, cancelled=cancelled)
                        if source_hash != assets[event.asset]:
                            raise RuntimeError('A sound asset changed during the production. Restart to use the new version.')
                        chunk, info = cached({'kind': 'sound', 'source_sha256': source_hash,
                            'sample_rate': plan.sample_rate, 'pan': event.pan, 'gain': event.gain, 'processor': processor},
                            lambda path, source=asset, e=event: audio.process_voice(source, path, ffmpeg=ffmpeg,
                                sample_rate=plan.sample_rate, pan=e.pan, gain=e.gain, cancelled=cancelled),
                            estimate=round(raw_info.seconds * plan.sample_rate * 4) + 80)
                        if sha256_file(asset, cancelled=cancelled) != source_hash:
                            raise RuntimeError('A sound asset changed while it was being rendered. Restart the production.')
                        role_name = 'Sound effects'
                    segments.append((chunk, info))
                    timed.append({'event_id': event.id, 'kind': type(event).__name__.lower(),
                                  'start_frame': local_position, 'frames': info['frames'], 'sha256': info['sha256']})
                    if plan.export_stems:
                        silent = None
                        for name in stem_parts:
                            if name == role_name:
                                stem_parts[name].append((chunk, info))
                            else:
                                silent = silent or quiet(info['frames'])
                                stem_parts[name].append(silent)
                    local_position += info['frames']
                scene_frames = local_position
                core, core_info = cached({'kind': 'scene_narration', 'parts': [i['sha256'] for _, i in segments]},
                    lambda path: audio.concatenate([p for p, _ in segments], path, cancelled=cancelled), estimate=scene_frames * 4 + 80)
                music_file = None
                music_info = None
                if scene.music is not None:
                    from .music import generate_music
                    cue = scene.music
                    settings = {'kind': 'music', 'cue': asdict(cue), 'rate': plan.sample_rate,
                        'frames': scene_frames, 'processor': processor, 'composer': composer_identity,
                        'model': music_identities.get(cue.model_manifest)}
                    music_key = digest({'pipeline_version': 1, **settings})
                    companions = ('.mid', '.music.json') if cue.engine == 'procedural' else ('.music.json',)

                    def create_music(path):
                        # Persist the engine's own accepted chunks independently of
                        # this run's disposable staging folder.
                        work = checked_directory(root / 'music-jobs' / music_key, create=True)
                        generated = work / 'cue.wav'
                        valid = False
                        if generated.exists():
                            try:
                                record = _read_json(generated.with_suffix('.music.json'))
                                current = audio.wav_info(generated)
                                valid = (record.get('sha256') == sha256_file(generated, cancelled=cancelled)
                                    and current.frames == scene_frames and current.rate == plan.sample_rate
                                    and record.get('engine') == cue.engine and record.get('mood') == cue.mood
                                    and record.get('tempo') == cue.tempo and record.get('seed') == cue.seed
                                    and all(generated.with_suffix(suffix).is_file() for suffix in companions))
                            except (ValueError, OSError, KeyError, TypeError):
                                valid = False
                        if not valid:
                            for old in (generated, *(generated.with_suffix(suffix) for suffix in companions)):
                                if old.exists() or old.is_symlink():
                                    if old.is_symlink():
                                        raise ValueError('Unsafe music checkpoint link.')
                                    old.rename(work / (old.name + '.invalid-' + uuid.uuid4().hex))
                            generate_music(generated, duration=scene_frames/plan.sample_rate, ffmpeg=ffmpeg,
                                mood=cue.mood, tempo=cue.tempo, seed=cue.seed, engine=cue.engine,
                                gain=0.35, sample_rate=plan.sample_rate, cancelled=cancelled,
                                manifest=music_models.get(cue.model_manifest), on_status=on_status,
                                max_output_bytes=plan.disk_budget_bytes)
                        publish_new(generated, path)
                        for suffix in companions:
                            publish_new(generated.with_suffix(suffix), path.with_suffix(suffix))

                    music_file, music_info = cached(settings, create_music,
                        provenance={'kind': 'generated_music', 'method': cue.engine, 'cue': asdict(cue),
                                    'model': music_identities.get(cue.model_manifest)},
                        estimate=scene_frames * 6 + 80, companions=companions)
                    music_companions.append((scene_index + 1, music_info))
                mixed, mixed_info = cached({'kind': 'scene_mix', 'voice_sha256': core_info['sha256'],
                    'music_sha256': music_info['sha256'] if music_info else None,
                    'gain': scene.music.gain if scene.music else None, 'processor': processor},
                    lambda path: audio.mix_scene(core, music_file, path, ffmpeg=ffmpeg,
                        music_gain=scene.music.gain if scene.music else 0, cancelled=cancelled), estimate=scene_frames * 4 + 80)
                scene_audio.append((mixed, mixed_info))
                stems = {}
                if plan.export_stems:
                    for name, parts in stem_parts.items():
                        if name == 'Sound effects' and not any(isinstance(e, Sound) for e in scene.events):
                            continue
                        stem, metadata = cached({'kind': 'scene_stem', 'parts': [i['sha256'] for _, i in parts]},
                            lambda path, p=parts: audio.concatenate([f for f, _ in p], path, cancelled=cancelled),
                            estimate=scene_frames * 4 + 80)
                        stems[name] = (stem, metadata)
                    if music_file:
                        # Music stem is dry, without ducking, for independent remixing.
                        stem, metadata = cached({'kind': 'music_stereo', 'raw_sha256': music_info['sha256'],
                            'gain': scene.music.gain, 'rate': plan.sample_rate, 'processor': processor},
                            lambda path: audio.process_voice(music_file, path, ffmpeg=ffmpeg, sample_rate=plan.sample_rate,
                                gain=scene.music.gain, cancelled=cancelled), estimate=scene_frames * 4 + 80)
                        stems['Music (dry)'] = (stem, metadata)
                scene_stems.append(stems)
                chapters.append({'id': scene.id, 'title': scene.title, 'start_frame': position,
                                 'end_frame': position + mixed_info['frames'], 'sample_rate': plan.sample_rate})
                timeline.append({'scene_id': scene.id, 'events': timed})
                position += mixed_info['frames']
                checkpoint(f'Chapter {scene_index+1} is complete and saved.')

            cache.reserve(position * 4 * (2 + (len(role_names) + 2 if plan.export_stems else 0)) + 1024 ** 2)
            staged_output = checked_directory(temp / 'finished', create=True)
            master = staged_output / 'master.wav'
            audio.concatenate([p for p, _ in scene_audio], master, cancelled=cancelled, on_status=on_status)
            measurements = audio.measure_peak(master, cancelled=cancelled)
            measurements.update(audio.measure_loudness(master, ffmpeg=ffmpeg, cancelled=cancelled))
            if measurements['clipped']:
                raise RuntimeError('The final output contains clipped samples; lower the mix level before exporting.')
            stem_names, stem_labels = [], {}
            if plan.export_stems:
                all_stems = list(dict.fromkeys(name for stems in scene_stems for name in stems))
                for index, name in enumerate(all_stems, 1):
                    parts = []
                    for stems, (_, scene_info) in zip(scene_stems, scene_audio):
                        parts.append(stems[name][0] if name in stems else quiet(scene_info['frames'])[0])
                    filename = f'stem-{index:03d}.wav'
                    audio.concatenate(parts, staged_output / filename, cancelled=cancelled)
                    stem_names.append(filename)
                    stem_labels[filename] = name
            for index, (path, _) in enumerate(scene_audio, 1):
                # New hardlink retains the sealed PCM exactly; final integrity is checked.
                publish_new(path, staged_output / f'chapter-{index:06d}.wav')
            for scene_number, music_info in music_companions:
                for item in music_info.get('companions', ()):
                    suffix = '.mid' if item['name'].endswith('.mid') else '.music.json'
                    publish_new(cache.root / item['name'], staged_output / f'music-{scene_number:06d}{suffix}')
            srt = []
            for index, item in enumerate(subtitles, 1):
                srt.extend([str(index), f"{_timestamp(item['start_frame'], plan.sample_rate, srt=True)} --> {_timestamp(item['end_frame'], plan.sample_rate, srt=True)}",
                            item['role'] + ': ' + item['text'], ''])
            _write_text_new(staged_output / 'subtitles.srt', '\n'.join(srt))
            atomic_json(staged_output / 'timeline.json', {'schema_version': 1, 'sample_rate': plan.sample_rate,
                'source_sha256': plan.source_sha256, 'chapters': chapters, 'lines': subtitles, 'scenes': timeline})
            metadata = [';FFMETADATA1', 'title=' + _metadata_escape(plan.title)]
            for chapter in chapters:
                metadata += ['[CHAPTER]', f'TIMEBASE=1/{plan.sample_rate}',
                    f"START={chapter['start_frame']}", f"END={chapter['end_frame']}", 'title=' + _metadata_escape(chapter['title'])]
            _write_text_new(staged_output / 'chapters.ffmeta', '\n'.join(metadata) + '\n')
            output_name = 'master.wav'
            if plan.output_format != 'wav':
                output_name = 'master.' + plan.output_format
                audio.transcode(master, staged_output / output_name, ffmpeg=ffmpeg, format=plan.output_format,
                                metadata=staged_output / 'chapters.ffmeta', cancelled=cancelled)
            files = [{'name': path.name, 'size': regular_file(path).st_size,
                      'sha256': sha256_file(path, cancelled=cancelled)} for path in sorted(staged_output.iterdir())]
            result = {'schema_version': 1, 'status': 'complete', 'run_key': run_key,
                      'source_sha256': plan.source_sha256, 'output_name': output_name,
                      'duration_seconds': position / plan.sample_rate, 'sample_rate': plan.sample_rate,
                      'frames': position, 'chapters': chapters, 'stem_names': stem_names, 'stem_labels': stem_labels,
                      'measurements': measurements, 'files': files, 'engines': identities, 'processor': processor,
                      'music_models': music_identities,
                      'limitations': ['Guided script parser; arbitrary natural-language planning is not enabled.',
                          'Line timing is measured; word alignment is unavailable.',
                          'Music uses only the explicitly selected local engine; quality and continuity require listening assessment.',
                          'Output measurements do not replace listening or Windows/NVDA acceptance.']}
            atomic_json(staged_output / 'complete.json', result)
            check_cancelled(cancelled)
            # A nonempty existing final directory is never overwritten. The OS lock
            # prevents two cooperative renderers from racing this publication.
            if output_dir.exists() or output_dir.is_symlink():
                raise FileExistsError('The production output appeared during rendering.')
            staged_output.rename(output_dir)
            state.update(state='complete', output=str(output_dir / output_name), duration_seconds=result['duration_seconds'])
            atomic_json(journal, state)
            on_status(f"Audio production complete: {result['duration_seconds']:.2f} seconds.")
            return _public_result(result, output_dir)
    except BaseException as exc:
        state.update(state='cancelled' if isinstance(exc, (RenderCancelled, InterruptedError)) else 'failed', message=str(exc)[:2000])
        atomic_json(journal, state)
        raise
