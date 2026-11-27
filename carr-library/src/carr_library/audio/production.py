"""Typed script-to-audio orchestration with durable, independently cached steps."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import inspect
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import time
import uuid

from . import ffmpeg as audio
from .jobs import (ChunkCache, RenderCancelled, atomic_json, checked_directory,
                   check_cancelled, file_identity, project_lock, publish_new,
                   publish_noreplace, read_json, regular_file, sha256_file)
from .models import (Clip, Effect, Line, MusicCue, Pause, ProductionPlan, Role, Scene,
                     Sound, canonical, digest, text)


# Version 1 could seal a step before detecting a changed external input.
PIPELINE_VERSION = 2


SCRIPT_HELP = '''Local audio script format
[master mix=0.5]
[master-effect compressor threshold_db=-20 ratio=3]
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
[clip rain.wav id=rain-bed start=0 trim_start=1 duration=20 gain_db=-18 track=ambience fade_in=1 fade_out=2]
[duration 25]

Clips overlay the chapter timeline without moving spoken lines. Supported clip
tracks: sfx, ambience, music, microphone. Clip start and trim are seconds;
duration is optional (remaining source). Pan is -1 to 1; gain is -60 to +12 dB.
Up to 32 local PCM WAV/RF64 clips per chapter. Ambience/music duck under foreground.

Plain lines use Narrator. Put local PCM WAV effects in the project's assets
folder. Use [music off] for a dry scene. Supported role settings: voice,
language, pan (-1 to 1, left/center/right), rate (-10 to 10), pitch (-10 to
10), volume (0 to 1). Unsupported voice controls fail explicitly.
[effect Narrator highpass frequency=80]
[effect Narrator compressor threshold_db=-20 ratio=3 attack_ms=20 release_ms=250]
[output format=wav sample_rate=48000 stems=true disk_budget_mb=32768]
Effects: highpass, lowpass, equalizer, gate, compressor, delay, denoise, gain,
lowshelf, highshelf, notch, limiter, declick, declip, deesser, reverb, chorus,
tempo, pitch, width, loudness. Controls are numeric and bounded.
Use enabled=false to bypass an effect while retaining its place and parameters.
[effect Narrator gain gain_db=-3 enabled=false]
[effect Narrator pitch semitones=2]
[effect Narrator tempo factor=1.1]
[effect Narrator loudness target_lufs=-18 peak_db=-1 range_lu=7]
Reverb uses four reflections. Pitch is independent of output sample rate.
Optional master directives must precede every timeline event. They create a
version-three plan and process the complete stereo programme once, in order.
Master dry/wet is a linear blend from 0 (dry) to 1 (processed). There is no
automatic gain normalization; a 0.95 safety limiter controls excess peaks.
Tempo and pitch remain role-only so measured subtitle/chapter frames stay fixed.
Master effect tails stop at the final programme boundary. Chapter WAVs are exact
frame slices of the processed master; stems are labelled pre-master.
Output formats: wav, flac, mp3, opus, m4a, m4b, aiff, ogg, aac, wma.
Optional MusicGen: [music engine=musicgen model_manifest=musicgen/manifest.json]
Optional SoundFont: [music engine=fluidsynth model_manifest=fluidsynth/manifest.json]
Optional ACE-Step: [music engine=ace-step model_manifest=ace-step/manifest.json]
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


def _effect_options(kind, tokens):
    from .models import EFFECT_PARAMETERS
    if kind not in EFFECT_PARAMETERS:
        raise ValueError('Choose a supported effect.')
    parameters = _options(tokens, (*EFFECT_PARAMETERS[kind], 'enabled'))
    enabled = parameters.pop('enabled', 'true')
    if enabled not in ('true', 'false'):
        raise ValueError('Effect enabled must be true or false.')
    return Effect(kind, tuple((key, float(value)) for key, value in parameters.items()),
                  enabled=enabled == 'true')


def parse_script(script, *, title='Audio project', voice='sapi5:default', language='en', _ranges=None):
    """Parse explicit, bounded instructions; preserve the original script verbatim."""
    text(script, 'Script', 16 * 1024 * 1024)
    roles = {'Narrator': Role('Narrator', voice=voice, language=language)}
    scenes, events, scene_title, music = [], [], title, None
    clips, minimum_duration, scene_start, version = [], 0.0, 0, 1
    serial = 0
    settings = {}
    used_roles = set()
    master_effects, master_configured = [], False

    def event_id():
        nonlocal serial
        serial += 1
        return f'event-{serial:08d}'

    def finish(end):
        nonlocal events, clips, minimum_duration
        if events or clips or minimum_duration:
            scenes.append(Scene(f'scene-{len(scenes)+1:06d}', scene_title, tuple(events), music,
                                tuple(clips), minimum_duration))
            if _ranges is not None:
                _ranges.append((scene_start, end))
            events, clips, minimum_duration = [], [], 0.0

    script_lines = script.splitlines()
    for lineno, original in enumerate(script_lines, 1):
        line = original.strip()
        if not line:
            continue
        try:
            if line.startswith('# '):
                finish(lineno - 1)
                scene_start = lineno
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
                    if events:
                        raise ValueError('Set scene music before its first event. Start a new chapter to change the music later.')
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
                elif kind == 'clip' and len(parts) >= 2:
                    p = _options(parts[2:], ('id', 'start', 'gain_db', 'pan', 'track', 'trim_start',
                                            'duration', 'fade_in', 'fade_out', 'title'))
                    for key in ('start', 'gain_db', 'trim_start', 'duration', 'fade_in', 'fade_out'):
                        if key in p:
                            p[key] = float(p[key])
                    if 'pan' in p:
                        p['pan'] = _pan(p['pan'])
                    clip_id = p.pop('id', None) or ('clip-' + event_id())
                    clips.append(Clip(clip_id, parts[1], **p))
                    version = max(version, 2)
                elif kind == 'duration' and len(parts) == 2:
                    if minimum_duration:
                        raise ValueError('Set the chapter minimum duration once.')
                    minimum_duration = float(parts[1])
                    from .models import number, MAX_CLIP_SECONDS
                    number(minimum_duration, 0.000001, MAX_CLIP_SECONDS, 'Chapter minimum duration')
                    version = max(version, 2)
                elif kind in ('master', 'master-effect'):
                    if scenes or events or clips or minimum_duration:
                        raise ValueError('Define master processing before the first timeline event.')
                    if kind == 'master':
                        if master_configured:
                            raise ValueError('Set master dry/wet mix once.')
                        parameters = _options(parts[1:], ('mix',))
                        settings['master_mix'] = float(parameters.get('mix', 1))
                        master_configured = True
                    elif len(parts) >= 2:
                        from .models import master_chain
                        candidate = _effect_options(parts[1], parts[2:])
                        master_chain((*master_effects, candidate))
                        master_effects.append(candidate)
                    else:
                        raise ValueError('Choose a master effect.')
                    version = 3
                elif kind == 'effect' and len(parts) >= 3:
                    from .models import EFFECT_PARAMETERS
                    role_name, effect_name = parts[1:3]
                    if role_name in used_roles:
                        raise ValueError('Define a role effect before that role speaks.')
                    if role_name not in roles or effect_name not in EFFECT_PARAMETERS:
                        raise ValueError('Declare the role and choose a supported effect first.')
                    effect = _effect_options(effect_name, parts[3:])
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
    finish(len(script_lines))
    if version == 3:
        settings['master_effects'] = tuple(master_effects)
    return ProductionPlan(title, script, tuple(roles.values()), tuple(scenes), schema_version=version, **settings)


def add_role_effect(script, role_name, effect, *, voice='sapi5:default', language='en'):
    """Insert a typed effect before speech without rewriting the user's text."""
    if not isinstance(effect, Effect) or not effect.enabled:
        raise ValueError('Choose an enabled typed effect to add to the script.')
    plan = parse_script(script, voice=voice, language=language)
    role = next((item for item in plan.roles if item.name == role_name), None)
    if role is None:
        raise ValueError('Choose a role from the current production plan.')
    if len(role.effects) >= 16:
        raise ValueError('This role already has the maximum 16 effects.')
    lines = script.splitlines(keepends=True)
    insertion, declared = 0, False
    for index, original in enumerate(lines):
        line = original.strip()
        if line.startswith('[') and line.endswith(']'):
            parts = shlex.split(line[1:-1])
            if len(parts) >= 2 and parts[0].lower() in ('role', 'effect') and parts[1] == role_name:
                insertion = index + 1
                declared = declared or parts[0].lower() == 'role'
    ending = '\r\n' if '\r\n' in script else '\n'
    name = shlex.quote(role_name)
    prefix = f'[role {name}]{ending}' if not declared and role_name != 'Narrator' else ''
    parameters = ''.join(f' {key}={value!r}' for key, value in effect.parameters)
    directive = f'{prefix}[effect {name} {effect.kind}{parameters}]{ending}'
    if insertion and not lines[insertion - 1].endswith(('\n', '\r')):
        lines[insertion - 1] += ending
    lines.insert(insertion, directive)
    result = ''.join(lines)
    # Keep the same parser and all its per-role/resource/ordering validation.
    parse_script(result, voice=voice, language=language)
    return result


def plan_narration(text, *, title='Narration', voice='sapi5:default', language='en'):
    """Plan literal reading without interpreting any source text as commands.

    Form-feed represents a source segment boundary only. The exact source string,
    including every separator, is retained; each text chunk carries checked
    code-point offsets into it. Empty source segments remain explicit scenes and
    receive one silent output frame because WAV files cannot contain zero
    accepted audio frames. No punctuation, role label, heading or bracketed
    passage is removed or turned into a production instruction.
    """
    from .models import text as validate_text
    source = validate_text(text, 'Narration text', 16 * 1024 * 1024, allow_formfeed=True)
    if source.count('\f') >= 100000:
        raise ValueError('The narration contains too many source segments; split it into separate projects.')
    role = Role('Narrator', voice=voice, language=language)
    scenes, offset = [], 0
    for page_number, page in enumerate(source.split('\f'), 1):
        events, position = [], offset
        # Empty/whitespace chunks remain source-bound data. The renderer emits
        # silence without asking a speech engine to interpret an empty request.
        for part_number, part in enumerate(speech_segments(page) or ('',), 1):
            end = position + len(part)
            events.append(Line(f'segment-{page_number:06d}-text-{part_number:06d}',
                role.name, part, literal=True, source_start=position, source_end=end))
            position = end
        scenes.append(Scene(f'segment-{page_number:06d}', f'Source segment {page_number}', tuple(events)))
        offset += len(page) + 1
    return ProductionPlan(title, source, (role,), tuple(scenes), input_kind='narration')


def _read_json(path, limit=16 * 1024 ** 2):
    return read_json(path, limit=limit)


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


def _production_music_manifest(root, reference, engine_name, *, resolver, cancelled):
    from .model_manifest import LocalModelManifest
    if resolver is None:
        location = checked_directory(root / 'models').joinpath(*reference.split('/'))
        model = LocalModelManifest.load(location, engine=engine_name)
    else:
        model = resolver(reference, engine_name, cancelled=cancelled)
        if not isinstance(model, LocalModelManifest) or model.engine != engine_name:
            raise ValueError('The selected music profile belongs to another engine.')
    model.verify(cancelled=cancelled, require_complete=True)
    return model


def render_project(plan, project_dir, *, engine, ffmpeg, cancelled=lambda: False,
                   on_status=lambda message: None, music_manifest_resolver=None,
                   broker=None, operation_id=None, trust_controller=None):
    """Render actual local audio and checkpoints; successful chunks survive errors.

    The desktop supplies its shared library broker. Standalone callers can pass
    their existing broker explicitly. No path is guessed as a library root.
    """
    if not isinstance(plan, ProductionPlan):
        raise ValueError('Expected a validated production plan.')
    if not callable(getattr(engine, 'synthesize', None)):
        raise ValueError('Choose a configured local speech engine.')
    root = checked_directory(project_dir, create=True)
    check_cancelled(cancelled)
    with project_lock(root):
        if broker is None:
            return _render_locked(plan, root, engine=engine, ffmpeg=ffmpeg, cancelled=cancelled,
                                  on_status=on_status, music_manifest_resolver=music_manifest_resolver,
                                  trust_controller=trust_controller)
        from ..operations import ResourceBroker, ResourceCancelled, ResourceConflict
        from ..operations.admission import production_request
        if not isinstance(broker, ResourceBroker):
            raise TypeError('Use the shared typed local resource coordinator.')
        models, devices, sounds = {}, [], {}
        for scene in plan.scenes:
            cue = scene.music
            if cue is not None and cue.model_manifest is not None:
                key = (cue.model_manifest, cue.engine)
                if key not in models:
                    model = _production_music_manifest(root, *key,
                        resolver=music_manifest_resolver, cancelled=cancelled)
                    models[key] = model.manifest_sha256
                    if cue.engine in ('musicgen', 'ace-step'):
                        devices.append(model.settings.get('device', 'cpu'))
            for event in (*scene.events, *scene.clips):
                if isinstance(event, (Sound, Clip)) and event.asset not in sounds:
                    asset = checked_directory(root / 'assets') / event.asset
                    regular_file(asset)
                    sounds[event.asset] = audio.wav_info(asset).seconds

        def admitted_music(reference, engine_name, *, cancelled):
            model = _production_music_manifest(root, reference, engine_name,
                resolver=music_manifest_resolver, cancelled=cancelled)
            if model.manifest_sha256 != models.get((reference, engine_name)):
                raise ResourceConflict('The music profile changed after resource admission. Review and run again.')
            return model

        request = production_request(plan, root, broker.limits,
                                     music_devices=devices, sound_seconds=sounds)
        identity = operation_id or 'studio:' + hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:32]
        on_status('Waiting for shared local resources.')
        try:
            with broker.reserve(request, operation_id=identity, cancelled=cancelled, timeout=60) as lease:
                # A replacement WAV can change the reservation while waiting.
                for name, duration in sounds.items():
                    if audio.wav_info(checked_directory(root / 'assets') / name).seconds != duration:
                        raise ResourceConflict('A selected sound changed while waiting for resources.')
                return _render_locked(plan, root, engine=engine, ffmpeg=ffmpeg,
                    cancelled=lease.cancelled, on_status=on_status,
                    music_manifest_resolver=admitted_music, trust_controller=trust_controller)
        except ResourceCancelled as exc:
            raise InterruptedError('Audio production stopped while waiting for shared resources.') from exc


def _render_locked(plan, root, *, engine, ffmpeg, cancelled, on_status, music_manifest_resolver=None,
                   trust_controller=None):
    cache = ChunkCache(root / 'cache', cancelled=cancelled, disk_budget_bytes=plan.disk_budget_bytes,
                       budget_root=root)
    cache.reserve(0)
    requested_cancelled, last_budget_check = cancelled, 0.0

    def guarded_cancelled():
        nonlocal last_budget_check
        if requested_cancelled():
            return True
        now = time.monotonic()
        if now - last_budget_check >= 0.25:
            last_budget_check = now
            cache.reserve(0)
        return False

    cancelled = guarded_cancelled
    roles = {role.name: role for role in plan.roles}
    identities = {}
    used_roles = {event.role for scene in plan.scenes for event in scene.events if isinstance(event, Line)}
    for role in plan.roles:
        if role.name not in used_roles:
            continue
        identify = getattr(engine, 'cache_identity', None)
        # Unknown engines cannot reuse prior speech across process invocations.
        check_cancelled(cancelled)
        if callable(identify):
            try:
                inspect.signature(identify).bind(role.voice, cancelled=cancelled)
            except (TypeError, ValueError):
                identities[role.name] = identify(role.voice)
            else:
                identities[role.name] = identify(role.voice, cancelled=cancelled)
        else:
            identities[role.name] = {'unverified_engine_session': uuid.uuid4().hex}
        check_cancelled(cancelled)
    processor = audio.ffmpeg_identity(ffmpeg, cancelled=cancelled)
    assets, asset_identities = {}, {}
    music_models, music_identities = {}, {}

    def selected_music_model(reference, engine_name):
        return _production_music_manifest(root, reference, engine_name,
            resolver=music_manifest_resolver, cancelled=cancelled)

    composer_identity = None
    for scene in plan.scenes:
        if scene.music is not None:
            from . import music as local_music
            composer_identity = {'implementation_sha256': sha256_file(local_music.__file__),
                                 'version': local_music.COMPOSER_VERSION}
            if scene.music.model_manifest is not None and scene.music.model_manifest not in music_models:
                model = selected_music_model(scene.music.model_manifest, scene.music.engine)
                music_models[scene.music.model_manifest] = model
                music_identities[scene.music.model_manifest] = model.cache_identity()
        for event in (*scene.events, *scene.clips):
            if isinstance(event, (Sound, Clip)) and event.asset not in assets:
                asset = checked_directory(root / 'assets') / event.asset
                asset_identities[event.asset] = file_identity(regular_file(asset))
                audio.wav_info(asset)
                assets[event.asset] = sha256_file(asset, cancelled=cancelled,
                    expected_identity=asset_identities[event.asset])
    run_settings = {'pipeline_version': PIPELINE_VERSION, 'plan': plan.to_dict(), 'engines': identities,
                      'processor': processor, 'assets': assets, 'music_models': music_identities,
                      'composer': composer_identity, 'mix_revision': audio.MIX_REVISION}
    if plan.schema_version >= 3:
        from .mastering import MASTER_REVISION
        run_settings['master_revision'] = MASTER_REVISION
    run_key = digest(run_settings)
    renders = checked_directory(root / 'renders', create=True)
    output_dir = renders / run_key
    if output_dir.is_symlink():
        raise ValueError('Unsafe existing output directory.')
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ValueError('The output destination is not a directory.')
        previous = _verified_output(output_dir, run_key, cancelled)
        if previous:
            # The run key above was recomputed from this exact typed plan,
            # current input identities and engines before accepting old audio.
            # Only that successful resume may bind a legacy completion marker.
            plan_hash = digest(plan.to_dict())
            if 'plan_sha256' in previous and previous['plan_sha256'] != plan_hash:
                raise ValueError('The completed production has a different typed-plan receipt.')
            if 'plan_sha256' not in previous:
                previous = {**previous, 'plan_sha256': plan_hash}
                atomic_json(output_dir / 'complete.json', previous)
            # Repair a journal left running/failed by death after directory move.
            atomic_json(root / 'render-journal.json', {'schema_version': 1, 'run_key': run_key,
                'state': 'complete', 'output': str(output_dir / previous['output_name']),
                'duration_seconds': previous['duration_seconds'], 'source_sha256': plan.source_sha256,
                'message': 'Verified previously published production.'})
            on_status('This exact production is already complete; verified its output checksums.')
            return _public_result(previous, output_dir)
    from ..security.admission import admit_new_operation
    authorization = admit_new_operation(trust_controller, 'audio.render')
    # Exact completed outputs above remain readable without a new grant. Keep
    # this immutable admission throughout synthesis; no expiry polling occurs.
    if output_dir.exists():
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

            def verify_dependencies(dependencies, identities=None):
                identities = identities or [file_identity(regular_file(path)) for path, _ in dependencies]
                for (path, metadata), identity in zip(dependencies, identities):
                    if sha256_file(path, cancelled=cancelled, expected_identity=identity) != metadata['sha256']:
                        raise RuntimeError('A cached audio input changed before its dependent step was sealed.')
                return identities

            def cached(settings, create, *, provenance=None, estimate=0, companions=(), dependencies=()):
                key = digest({'pipeline_version': PIPELINE_VERSION, **settings})
                ready = cache.get(key)
                if ready is not None:
                    return ready
                # A generated stage and its independent sealed copy can coexist.
                cache.reserve(estimate * 2 + 65536)
                identities = verify_dependencies(dependencies)
                stage = temp / (key + '.wav')
                if stage.exists():
                    stage.unlink()
                try:
                    create(stage)
                    check_cancelled(cancelled)
                    verify_dependencies(dependencies, identities)
                    cache.reserve(0)
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
                        for piece in speech_segments(event.text) or ('',):
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
                                estimate=round(raw_info['frames'] / raw_info['sample_rate'] * plan.sample_rate * 4) + 80,
                                dependencies=((raw, raw_info),))
                            spoken_parts.append(processed)
                        if len(spoken_parts) == 1:
                            chunk, info = spoken_parts[0]
                        else:
                            chunk, info = cached({'kind': 'long_line', 'parts': [i['sha256'] for _, i in spoken_parts]},
                                lambda path: audio.concatenate([p for p, _ in spoken_parts], path, cancelled=cancelled),
                                estimate=sum(i['frames'] for _, i in spoken_parts) * 4 + 80,
                                dependencies=spoken_parts)
                        role_name = role.name
                        subtitles.append({'role': role.name, 'text': event.text, 'start_frame': position + local_position,
                                          'end_frame': position + local_position + info['frames'], 'event_id': event.id,
                                          'literal_narration': event.literal, 'source_start': event.source_start,
                                          'source_end': event.source_end,
                                          'timing': 'Measured line boundaries; word timings unavailable.'})
                    elif isinstance(event, Pause):
                        chunk, info = quiet(max(1, round(event.seconds * plan.sample_rate)))
                    else:
                        asset_root = checked_directory(root / 'assets')
                        asset = asset_root / event.asset
                        raw_info = audio.wav_info(asset)
                        source_hash = sha256_file(asset, cancelled=cancelled,
                            expected_identity=asset_identities[event.asset])
                        if source_hash != assets[event.asset]:
                            raise RuntimeError('A sound asset changed during the production. Restart to use the new version.')
                        chunk, info = cached({'kind': 'sound', 'source_sha256': source_hash,
                            'sample_rate': plan.sample_rate, 'pan': event.pan, 'gain': event.gain, 'processor': processor},
                            lambda path, source=asset, e=event: audio.process_voice(source, path, ffmpeg=ffmpeg,
                                sample_rate=plan.sample_rate, pan=e.pan, gain=e.gain, cancelled=cancelled,
                                expected_identity=asset_identities[e.asset]),
                            estimate=round(raw_info.seconds * plan.sample_rate * 4) + 80,
                            dependencies=((asset, {'sha256': source_hash}),))
                        if sha256_file(asset, cancelled=cancelled,
                            expected_identity=asset_identities[event.asset]) != source_hash:
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
                positioned, clip_timeline = [], []
                scene_frames = max(local_position, round(scene.minimum_duration * plan.sample_rate))
                if scene.clips:
                    from .clip_audio import clip_window, process_clip, CLIP_MIX_REVISION
                    for clip in scene.clips:
                        check_cancelled(cancelled)
                        asset = checked_directory(root / 'assets') / clip.asset
                        raw_info = audio.wav_info(asset)
                        window = clip_window(clip, raw_info, plan.sample_rate)
                        stamp = asset_identities[clip.asset]
                        source_hash = sha256_file(asset, cancelled=cancelled, expected_identity=stamp)
                        if source_hash != assets[clip.asset]:
                            raise RuntimeError('A retained clip changed during production.')
                        chunk, info = cached({'kind': 'clip_processing', 'source_sha256': source_hash,
                            'clip': asdict(clip), 'sample_rate': plan.sample_rate, 'processor': processor,
                            'clip_revision': CLIP_MIX_REVISION},
                            lambda path, a=asset, c=clip, identity=stamp: process_clip(
                                a, path, c, ffmpeg=ffmpeg, sample_rate=plan.sample_rate,
                                cancelled=cancelled, expected_identity=identity),
                            estimate=window.frames * 4 + 80,
                            dependencies=((asset, {'sha256': source_hash}),))
                        if info['frames'] != window.frames:
                            raise ValueError('A clip checkpoint has different selected sample bounds.')
                        positioned.append((clip, chunk, info, window))
                        scene_frames = max(scene_frames, window.start_frame + info['frames'])
                        clip_timeline.append({'clip_id': clip.id, 'asset': clip.asset, 'track': clip.track,
                            'start_frame': window.start_frame, 'frames': info['frames'],
                            'sha256': info['sha256'], 'source_sha256': source_hash,
                            'source_rate': raw_info.rate, 'source_frames': raw_info.frames,
                            'source_start': window.source_start, 'source_end': window.source_end,
                            'fade_in': window.fade_in, 'fade_out': window.fade_out})
                scene_frames = max(1, scene_frames)
                if scene_frames > local_position:
                    tail = quiet(scene_frames - local_position)
                    segments.append(tail)
                    if plan.export_stems:
                        for parts in stem_parts.values():
                            parts.append(tail)
                core, core_info = cached({'kind': 'scene_narration', 'parts': [i['sha256'] for _, i in segments]},
                    lambda path: audio.concatenate([p for p, _ in segments], path, cancelled=cancelled),
                    estimate=scene_frames * 4 + 80, dependencies=segments)
                music_file = None
                music_info = None
                if scene.music is not None:
                    from .music import generate_music
                    cue = scene.music
                    settings = {'kind': 'music', 'cue': asdict(cue), 'rate': plan.sample_rate,
                        'frames': scene_frames, 'processor': processor, 'composer': composer_identity,
                        'model': music_identities.get(cue.model_manifest)}
                    music_key = digest({'pipeline_version': PIPELINE_VERSION, **settings})
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
                            selected_model = music_models.get(cue.model_manifest)
                            if music_manifest_resolver is not None and cue.model_manifest is not None:
                                selected_model = selected_music_model(cue.model_manifest, cue.engine)
                                if selected_model.cache_identity() != music_identities[cue.model_manifest]:
                                    raise ValueError('The local music profile changed before generation.')
                            generate_music(generated, duration=scene_frames/plan.sample_rate, ffmpeg=ffmpeg,
                                mood=cue.mood, tempo=cue.tempo, seed=cue.seed, engine=cue.engine,
                                gain=0.35, sample_rate=plan.sample_rate, cancelled=cancelled,
                                manifest=selected_model, on_status=on_status,
                                max_output_bytes=max(1, plan.disk_budget_bytes - cache.used))
                        publish_new(generated, path, cancelled=cancelled)
                        for suffix in companions:
                            publish_new(generated.with_suffix(suffix), path.with_suffix(suffix), cancelled=cancelled)

                    music_file, music_info = cached(settings, create_music,
                        provenance={'kind': 'generated_music', 'method': cue.engine, 'cue': asdict(cue),
                                    'model': music_identities.get(cue.model_manifest)},
                        estimate=scene_frames * 6 + 80, companions=companions)
                    music_companions.append((scene_index + 1, music_info))
                if positioned:
                    from .clip_audio import mix_tracks, CLIP_MIX_REVISION
                    mix_settings = {'kind': 'clip_scene_mix', 'voice_sha256': core_info['sha256'],
                        'clips': [{'sha256': info['sha256'], 'start_frame': window.start_frame,
                                   'track': clip.track} for clip, _, info, window in positioned],
                        'music_sha256': music_info['sha256'] if music_info else None,
                        'gain': scene.music.gain if scene.music else None, 'processor': processor,
                        'clip_revision': CLIP_MIX_REVISION, 'frames': scene_frames}
                    mixed, mixed_info = cached(mix_settings,
                        lambda path: mix_tracks(core,
                            [(chunk, window.start_frame, clip.track) for clip, chunk, _, window in positioned],
                            path, ffmpeg=ffmpeg, music=music_file,
                            music_gain=scene.music.gain if scene.music else 0, cancelled=cancelled),
                        estimate=scene_frames * 4 + 80,
                        dependencies=((core, core_info),) + tuple((chunk, info) for _, chunk, info, _ in positioned)
                                     + (((music_file, music_info),) if music_file else ()))
                else:
                    mixed, mixed_info = cached({'kind': 'scene_mix', 'voice_sha256': core_info['sha256'],
                        'music_sha256': music_info['sha256'] if music_info else None,
                        'gain': scene.music.gain if scene.music else None, 'processor': processor,
                        'mix_revision': audio.MIX_REVISION},
                        lambda path: audio.mix_scene(core, music_file, path, ffmpeg=ffmpeg,
                            music_gain=scene.music.gain if scene.music else 0, cancelled=cancelled), estimate=scene_frames * 4 + 80,
                        dependencies=((core, core_info),) + (((music_file, music_info),) if music_file else ()))
                scene_audio.append((mixed, mixed_info))
                stems = {}
                if plan.export_stems:
                    for name, parts in stem_parts.items():
                        if name == 'Sound effects' and not any(isinstance(e, Sound) for e in scene.events):
                            continue
                        stem, metadata = cached({'kind': 'scene_stem', 'parts': [i['sha256'] for _, i in parts]},
                            lambda path, p=parts: audio.concatenate([f for f, _ in p], path, cancelled=cancelled),
                            estimate=scene_frames * 4 + 80, dependencies=parts)
                        stems[name] = (stem, metadata)
                    if music_file:
                        # Music stem is dry, without ducking, for independent remixing.
                        stem, metadata = cached({'kind': 'music_stereo', 'raw_sha256': music_info['sha256'],
                            'gain': scene.music.gain, 'rate': plan.sample_rate, 'processor': processor},
                            lambda path: audio.process_voice(music_file, path, ffmpeg=ffmpeg, sample_rate=plan.sample_rate,
                                gain=scene.music.gain, cancelled=cancelled), estimate=scene_frames * 4 + 80,
                            dependencies=((music_file, music_info),))
                        stems['Music (dry)'] = (stem, metadata)
                    if positioned:
                        from .clip_audio import mix_tracks, CLIP_MIX_REVISION
                        empty, empty_info = quiet(scene_frames)
                        for track in ('sfx', 'ambience', 'music', 'microphone'):
                            chosen = [(clip, chunk, info, window) for clip, chunk, info, window in positioned if clip.track == track]
                            if not chosen:
                                continue
                            stem, metadata = cached({'kind': 'clip_track_stem', 'track': track,
                                'clips': [{'sha256': info['sha256'], 'start_frame': window.start_frame}
                                          for _, _, info, window in chosen],
                                'frames': scene_frames, 'rate': plan.sample_rate, 'processor': processor,
                                'clip_revision': CLIP_MIX_REVISION},
                                lambda path, selected=chosen: mix_tracks(empty,
                                    [(chunk, window.start_frame, clip.track) for clip, chunk, _, window in selected],
                                    path, ffmpeg=ffmpeg, duck=False, cancelled=cancelled),
                                estimate=scene_frames * 4 + 80,
                                dependencies=((empty, empty_info),) + tuple((chunk, info) for _, chunk, info, _ in chosen))
                            label = 'Clip track: ' + track
                            while label in stems:
                                label = 'Clips / ' + label
                            stems[label] = (stem, metadata)
                scene_stems.append(stems)
                chapters.append({'id': scene.id, 'title': scene.title, 'start_frame': position,
                                 'end_frame': position + mixed_info['frames'], 'sample_rate': plan.sample_rate})
                row = {'scene_id': scene.id, 'events': timed}
                if plan.schema_version >= 2:
                    row['clips'] = clip_timeline
                    row['minimum_duration'] = scene.minimum_duration
                timeline.append(row)
                position += mixed_info['frames']
                checkpoint(f'Chapter {scene_index+1} is complete and saved.')

            cache.reserve(position * 4 * (2 + (4 if plan.schema_version >= 3 else 0) + (plan.output_format != 'wav')
                + (len(role_names) + 2 + (4 if any(s.clips for s in plan.scenes) else 0) if plan.export_stems else 0)) + 1024 ** 2)
            scene_identities = verify_dependencies(scene_audio)
            staged_output = checked_directory(temp / 'finished', create=True)
            master = staged_output / 'master.wav'
            master_report = None
            if plan.schema_version >= 3:
                from .mastering import MASTER_REVISION, process_master, split_master
                assembled, assembled_info = cached({'kind': 'master_assembly',
                    'parts': [info['sha256'] for _, info in scene_audio], 'rate': plan.sample_rate},
                    lambda path: audio.concatenate([p for p, _ in scene_audio], path,
                        cancelled=cancelled, on_status=on_status),
                    estimate=position * 4 + 80, dependencies=scene_audio)
                mastered, mastered_info = cached({'kind': 'master_processing',
                    'input_sha256': assembled_info['sha256'], 'effects': [asdict(e) for e in plan.master_effects],
                    'mix': plan.master_mix, 'processor': processor, 'master_revision': MASTER_REVISION},
                    lambda path: process_master(assembled, path, ffmpeg=ffmpeg,
                        effects=plan.master_effects, mix=plan.master_mix, cancelled=cancelled),
                    estimate=position * 4 + 80, dependencies=((assembled, assembled_info),))
                publish_new(mastered, master, cancelled=cancelled)
                if (mastered_info['frames'], mastered_info['sample_rate']) != (position, plan.sample_rate):
                    raise RuntimeError('The mastered cache changed the measured programme timeline.')
                master_report = {'revision': MASTER_REVISION, 'effects': json.loads(canonical([asdict(e) for e in plan.master_effects])),
                    'mix': plan.master_mix, 'input_sha256': assembled_info['sha256'],
                    'output_sha256': mastered_info['sha256'], 'frames': position,
                    'sample_rate': plan.sample_rate, 'tail_policy': 'trim at the measured programme end',
                    'mixing': 'linear dry/wet; no automatic gain normalization',
                    'limiter': '0.95 safety ceiling; automatic level disabled',
                    'chapter_audio': 'exact frame partitions of the processed master',
                    'stems': 'pre-master processing'}
            else:
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
                    stem_labels[filename] = name + (' (pre-master)' if master_report is not None else '')
            if master_report is not None:
                split_master(master, tuple((item['start_frame'], item['end_frame']) for item in chapters),
                             staged_output, cancelled=cancelled)
            else:
                for index, ((path, _), identity) in enumerate(zip(scene_audio, scene_identities), 1):
                    publish_new(path, staged_output / f'chapter-{index:06d}.wav',
                                cancelled=cancelled, expected_identity=identity)
            for scene_number, music_info in music_companions:
                for item in music_info.get('companions', ()):
                    suffix = '.mid' if item['name'].endswith('.mid') else '.music.json'
                    publish_new(cache.root / item['name'], staged_output / f'music-{scene_number:06d}{suffix}', cancelled=cancelled)
            srt = []
            for index, item in enumerate(subtitles, 1):
                srt.extend([str(index), f"{_timestamp(item['start_frame'], plan.sample_rate, srt=True)} --> {_timestamp(item['end_frame'], plan.sample_rate, srt=True)}",
                            item['role'] + ': ' + item['text'], ''])
            _write_text_new(staged_output / 'subtitles.srt', '\n'.join(srt))
            atomic_json(staged_output / 'timeline.json', {'schema_version': plan.schema_version, 'sample_rate': plan.sample_rate,
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
                      'input_kind': plan.input_kind,
                      'source_sha256': plan.source_sha256, 'plan_sha256': digest(plan.to_dict()),
                      'output_name': output_name,
                      'duration_seconds': position / plan.sample_rate, 'sample_rate': plan.sample_rate,
                      'frames': position, 'chapters': chapters, 'stem_names': stem_names, 'stem_labels': stem_labels,
                      'measurements': measurements, 'files': files, 'engines': identities, 'processor': processor,
                      'mix_revision': audio.MIX_REVISION,
                      'music_models': music_identities,
                      'limitations': ['Guided script parser; arbitrary natural-language planning is not enabled.',
                          'Line timing is measured; word alignment is unavailable.',
                          'Music uses only the explicitly selected local engine; quality and continuity require listening assessment.',
                          'Output measurements do not replace listening or Windows/NVDA acceptance.']}
            if master_report is not None:
                result['master_processing'] = master_report
            atomic_json(staged_output / 'complete.json', result)
            check_cancelled(cancelled)
            cache.reserve(0)
            publish_noreplace(staged_output, output_dir, cancelled=cancelled)
            state.update(state='complete', output=str(output_dir / output_name), duration_seconds=result['duration_seconds'])
            atomic_json(journal, state)
            on_status(f"Audio production complete: {result['duration_seconds']:.2f} seconds.")
            return _public_result(result, output_dir)
    except BaseException as exc:
        state.update(state='cancelled' if isinstance(exc, (RenderCancelled, InterruptedError)) else 'failed', message=str(exc)[:2000])
        atomic_json(journal, state)
        raise
