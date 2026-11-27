"""Versioned, data-only production contracts; scripts never become commands."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import re
from pathlib import PurePosixPath

from .paths import filename_component


SCHEMA = 1
SAMPLE_RATES = (8000, 16000, 22050, 24000, 44100, 48000, 96000)
FORMATS = ('wav', 'flac', 'mp3', 'opus', 'm4a', 'm4b', 'aiff', 'ogg', 'aac', 'wma')


def number(value, low, high, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be a finite number between {low} and {high}.')
    return value


def text(value, name, maximum=4096, *, empty=False, allow_formfeed=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum:
        raise ValueError(f'{name} must be text of at most {maximum} characters.')
    controls = '\n\r\t\f' if allow_formfeed else '\n\r\t'
    if any(ord(c) < 32 and c not in controls for c in value):
        raise ValueError(f'{name} contains a control character.')
    return value


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ValueError('Invalid event or scene identity.')
    return value


def _strict(value, names, kind):
    if not isinstance(value, dict) or set(value) - set(names):
        raise ValueError(f'Unknown or invalid {kind} fields.')
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


# Controls are data-only: (default, minimum, maximum, unit). Existing effects
# retain their previous defaults when a newly exposed optional control is absent.
EFFECT_CONTROLS = {
    'highpass': {'frequency': (80, 20, 20000, 'Hz'), 'q': (0.707, 0.1, 20, 'Q')},
    'lowpass': {'frequency': (12000, 20, 20000, 'Hz'), 'q': (0.707, 0.1, 20, 'Q')},
    'equalizer': {'frequency': (1000, 20, 20000, 'Hz'), 'gain_db': (0, -24, 24, 'dB'),
                  'q': (1, 0.1, 20, 'Q')},
    'gate': {'threshold_db': (-45, -90, 0, 'dBFS'), 'attack_ms': (10, 0.1, 2000, 'ms'),
             'release_ms': (200, 1, 9000, 'ms'), 'ratio': (2, 1, 100, 'ratio'),
             'reduction_db': (-24, -120, 0, 'dB')},
    'compressor': {'threshold_db': (-20, -60, 0, 'dBFS'), 'ratio': (3, 1, 20, 'ratio'),
                   'attack_ms': (20, 0.1, 2000, 'ms'), 'release_ms': (250, 1, 9000, 'ms'),
                   'knee': (2.828427, 1, 8, 'ratio'), 'makeup_db': (0, 0, 24, 'dB'),
                   'mix': (1, 0, 1, 'fraction')},
    'delay': {'delay_ms': (250, 1, 2000, 'ms'), 'decay': (0.25, 0, 0.8, 'fraction'),
              'mix': (0.25, 0, 1, 'fraction')},
    'denoise': {'reduction_db': (10, 0.01, 48, 'dB'), 'floor_db': (-50, -80, -20, 'dBFS')},
    'gain': {'gain_db': (0, -120, 24, 'dB')},
    'lowshelf': {'frequency': (120, 20, 20000, 'Hz'), 'gain_db': (0, -24, 24, 'dB'),
                 'q': (0.707, 0.1, 20, 'Q')},
    'highshelf': {'frequency': (5000, 20, 20000, 'Hz'), 'gain_db': (0, -24, 24, 'dB'),
                  'q': (0.707, 0.1, 20, 'Q')},
    'notch': {'frequency': (50, 20, 20000, 'Hz'), 'q': (10, 0.1, 100, 'Q')},
    'limiter': {'ceiling_db': (-1, -24, 0, 'dBFS'), 'attack_ms': (5, 0.1, 80, 'ms'),
                'release_ms': (50, 1, 8000, 'ms')},
    'declick': {},
    'declip': {},
    'deesser': {'intensity': (0.3, 0, 1, 'fraction'), 'max_reduction': (0.5, 0, 1, 'fraction'),
                'frequency': (0.5, 0, 1, 'normalized frequency')},
    'reverb': {'room_ms': (40, 5, 200, 'ms'), 'decay': (0.4, 0.01, 0.85, 'fraction'),
               'mix': (0.2, 0, 1, 'fraction')},
    'chorus': {'delay_ms': (40, 1, 100, 'ms'), 'depth_ms': (2, 0.1, 10, 'ms'),
               'speed_hz': (0.4, 0.1, 5, 'Hz'), 'mix': (0.25, 0, 1, 'fraction')},
    'tempo': {'factor': (1, 0.25, 4, 'ratio')},
    'pitch': {'semitones': (0, -12, 12, 'semitones')},
    'width': {'factor': (1, 0, 2, 'ratio')},
    'loudness': {'target_lufs': (-18, -40, -9, 'LUFS'), 'peak_db': (-1, -9, 0, 'dBTP'),
                 'range_lu': (7, 1, 20, 'LU')},
}
EFFECT_PARAMETERS = {
    kind: {name: (spec[1], spec[2]) for name, spec in values.items()}
    for kind, values in EFFECT_CONTROLS.items()
}


def effect_controls():
    """Return an independent, readable description for native controls/help."""
    return {kind: {name: {'default': spec[0], 'minimum': spec[1],
                         'maximum': spec[2], 'unit': spec[3]}
                   for name, spec in values.items()}
            for kind, values in EFFECT_CONTROLS.items()}


@dataclass(frozen=True)
class Effect:
    kind: str
    parameters: tuple[tuple[str, float], ...] = ()
    enabled: bool = True

    def __post_init__(self):
        if self.kind not in EFFECT_PARAMETERS or type(self.enabled) is not bool:
            raise ValueError('Unsupported audio effect.')
        allowed = EFFECT_PARAMETERS[self.kind]
        if not isinstance(self.parameters, tuple) or len(self.parameters) > len(allowed):
            raise ValueError('Invalid effect parameters.')
        seen = set()
        for pair in self.parameters:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError('Invalid effect parameter.')
            name, value = pair
            if name not in allowed or name in seen:
                raise ValueError('Unknown or duplicate effect parameter.')
            number(value, *allowed[name], name)
            seen.add(name)

    @classmethod
    def from_dict(cls, value):
        value = _strict(value, ('kind', 'parameters', 'enabled'), 'effect')
        params = value.get('parameters', ())
        if not isinstance(params, (list, tuple)):
            raise ValueError('Effect parameters must be a list of pairs.')
        return cls(value['kind'], tuple(tuple(p) for p in params), value.get('enabled', True))


@dataclass(frozen=True)
class Role:
    name: str
    voice: str = 'sapi5:default'
    language: str = 'en'
    pan: float = 0.0
    rate: int = 0
    pitch: int = 0
    volume: float = 1.0
    effects: tuple[Effect, ...] = ()

    def __post_init__(self):
        text(self.name, 'Role name', 80)
        if any(c in self.name for c in '\n\r:[]'):
            raise ValueError('Role name cannot contain brackets, a colon or line break.')
        text(self.voice, 'Voice', 2048)
        text(self.language, 'Language', 80)
        number(self.pan, -1, 1, 'Pan')
        if type(self.rate) is not int or type(self.pitch) is not int:
            raise ValueError('Speech rate and pitch must be integers.')
        number(self.rate, -10, 10, 'Speech rate')
        number(self.pitch, -10, 10, 'Speech pitch')
        number(self.volume, 0, 1, 'Speech volume')
        if not isinstance(self.effects, tuple) or len(self.effects) > 16 or not all(isinstance(e, Effect) for e in self.effects):
            raise ValueError('Invalid or excessive effects chain.')

    @classmethod
    def from_dict(cls, value):
        value = _strict(value, ('name', 'voice', 'language', 'pan', 'rate', 'pitch', 'volume', 'effects'), 'role')
        return cls(**{**value, 'effects': tuple(Effect.from_dict(e) for e in value.get('effects', ()))})


@dataclass(frozen=True)
class Line:
    id: str
    role: str
    text: str
    literal: bool = False
    source_start: int | None = None
    source_end: int | None = None

    def __post_init__(self):
        identity(self.id)
        text(self.role, 'Line role', 80)
        if type(self.literal) is not bool:
            raise ValueError('Literal narration selection must be a boolean.')
        text(self.text, 'Line text', 100000, empty=self.literal)
        if self.literal:
            if (type(self.source_start) is not int or type(self.source_end) is not int
                    or self.source_start < 0 or self.source_end < self.source_start
                    or self.source_end - self.source_start != len(self.text)):
                raise ValueError('Literal narration requires an exact source span.')
        elif self.source_start is not None or self.source_end is not None:
            raise ValueError('Source spans belong only to literal narration lines.')


@dataclass(frozen=True)
class Pause:
    id: str
    seconds: float

    def __post_init__(self):
        identity(self.id)
        number(self.seconds, 0.001, 86400, 'Pause duration')


@dataclass(frozen=True)
class Sound:
    id: str
    asset: str
    gain: float = 1.0
    pan: float = 0.0

    def __post_init__(self):
        identity(self.id)
        text(self.asset, 'Sound asset', 240)
        filename_component(self.asset)
        if self.asset in ('.', '..') or any(c in self.asset for c in '/\\:\x00\n\r') or self.asset.startswith('.'):
            raise ValueError('Sound asset must be a filename inside the project assets directory.')
        number(self.gain, 0, 2, 'Sound gain')
        number(self.pan, -1, 1, 'Sound pan')


CLIP_TRACKS = ('sfx', 'ambience', 'music', 'microphone')
MAX_SCENE_CLIPS = 32
MAX_CLIP_SECONDS = 31536000


@dataclass(frozen=True)
class Clip:
    """A retained local PCM asset positioned independently of spoken events."""
    id: str
    asset: str
    start: float = 0.0
    gain_db: float = 0.0
    pan: float = 0.0
    track: str = 'sfx'
    trim_start: float = 0.0
    duration: float | None = None
    fade_in: float = 0.0
    fade_out: float = 0.0
    title: str = ''

    def __post_init__(self):
        # Share the existing portable filename boundary; no external path.
        Sound(self.id, self.asset)
        number(self.start, 0, MAX_CLIP_SECONDS, 'Clip start')
        number(self.trim_start, 0, MAX_CLIP_SECONDS, 'Clip trim start')
        if self.duration is not None:
            number(self.duration, 0.000001, MAX_CLIP_SECONDS, 'Clip duration')
        number(self.gain_db, -60, 12, 'Clip gain in dB')
        number(self.pan, -1, 1, 'Clip pan')
        number(self.fade_in, 0, MAX_CLIP_SECONDS, 'Clip fade in')
        number(self.fade_out, 0, MAX_CLIP_SECONDS, 'Clip fade out')
        if self.duration is not None and self.fade_in + self.fade_out > self.duration:
            raise ValueError('Clip fades cannot overlap or exceed the selected duration.')
        if self.track not in CLIP_TRACKS:
            raise ValueError('Choose a sound-effects, ambience, music or microphone track.')
        text(self.title, 'Clip title', 500, empty=True)


@dataclass(frozen=True)
class MusicCue:
    mood: str = 'calm'
    tempo: int = 90
    gain: float = 0.15
    seed: int = 0
    engine: str = 'procedural'
    model_manifest: str | None = None

    def __post_init__(self):
        text(self.mood, 'Music mood', 80)
        if self.mood not in ('calm', 'suspense', 'bright', 'sad', 'neutral', 'energetic', 'dark'):
            raise ValueError('Choose a supported music mood: calm, suspense, bright, sad, neutral, energetic or dark.')
        if type(self.tempo) is not int or type(self.seed) is not int:
            raise ValueError('Music tempo and seed must be integers.')
        number(self.tempo, 30, 240, 'Music tempo')
        number(self.seed, 0, 2147483647, 'Music seed')
        number(self.gain, 0, 0.8, 'Music gain')
        if self.engine not in ('procedural', 'musicgen', 'fluidsynth', 'ace-step'):
            raise ValueError('Choose procedural, or an explicitly reviewed MusicGen, ACE-Step or FluidSynth installation.')
        if self.model_manifest is not None:
            text(self.model_manifest, 'Model manifest', 1024)
            path = PurePosixPath(self.model_manifest)
            if path.is_absolute() or any(c in self.model_manifest for c in '\\:\n\r') or any(p in ('', '.', '..') for p in self.model_manifest.split('/')):
                raise ValueError('Model manifest must be a relative path under the project models directory.')
        if self.engine != 'procedural' and self.model_manifest is None:
            raise ValueError('The selected music engine needs an explicitly reviewed local asset manifest.')
        if self.engine == 'procedural' and self.model_manifest is not None:
            raise ValueError('Procedural music does not use an installed asset manifest.')


@dataclass(frozen=True)
class Scene:
    id: str
    title: str
    events: tuple[Line | Pause | Sound, ...]
    music: MusicCue | None = None
    clips: tuple[Clip, ...] = ()
    minimum_duration: float = 0.0

    def __post_init__(self):
        identity(self.id)
        text(self.title, 'Scene title', 500)
        if not isinstance(self.events, tuple) or len(self.events) > 100000:
            raise ValueError('Split very large scenes into chapters.')
        number(self.minimum_duration, 0, MAX_CLIP_SECONDS, 'Scene minimum duration')
        if (not isinstance(self.clips, tuple) or len(self.clips) > MAX_SCENE_CLIPS
                or not all(isinstance(clip, Clip) for clip in self.clips)):
            raise ValueError('A scene supports at most thirty-two typed clips.')
        if not self.events and not self.clips and self.minimum_duration <= 0:
            raise ValueError('A scene needs spoken events, clips or an explicit duration.')
        if not all(isinstance(e, (Line, Pause, Sound)) for e in self.events):
            raise ValueError('Unknown scene event.')
        if len({e.id for e in (*self.events, *self.clips)}) != len(self.events) + len(self.clips):
            raise ValueError('Duplicate scene event or clip identity.')
        if self.music is not None and not isinstance(self.music, MusicCue):
            raise ValueError('Invalid music cue.')


@dataclass(frozen=True)
class ProductionPlan:
    title: str
    original_script: str
    roles: tuple[Role, ...]
    scenes: tuple[Scene, ...]
    output_format: str = 'wav'
    sample_rate: int = 48000
    export_stems: bool = True
    disk_budget_bytes: int = 32 * 1024 ** 3
    schema_version: int = SCHEMA
    input_kind: str = 'script'

    def __post_init__(self):
        text(self.title, 'Project title', 500)
        if self.input_kind not in ('script', 'narration'):
            raise ValueError('Choose explicit script or literal narration input.')
        text(self.original_script, 'Original script', 16 * 1024 * 1024, empty=True,
             allow_formfeed=self.input_kind == 'narration')
        if type(self.schema_version) is not int or self.schema_version not in (1, 2):
            raise ValueError('Unsupported production project version.')
        if not isinstance(self.roles, tuple) or not 1 <= len(self.roles) <= 64 or not all(isinstance(r, Role) for r in self.roles):
            raise ValueError('Choose one to sixty-four named roles.')
        if len({r.name for r in self.roles}) != len(self.roles):
            raise ValueError('Role names must be unique.')
        if not isinstance(self.scenes, tuple) or not self.scenes or len(self.scenes) > 100000 or not all(isinstance(s, Scene) for s in self.scenes):
            raise ValueError('A production needs scenes.')
        if len({s.id for s in self.scenes}) != len(self.scenes):
            raise ValueError('Scene identities must be unique.')
        if self.schema_version == 1 and any(scene.clips or scene.minimum_duration for scene in self.scenes):
            raise ValueError('Clip editing requires production project version two.')
        if len({clip.id for scene in self.scenes for clip in scene.clips}) != sum(len(scene.clips) for scene in self.scenes):
            raise ValueError('Clip identities must be unique across the project.')
        roles = {r.name for r in self.roles}
        if any(e.role not in roles for s in self.scenes for e in s.events if isinstance(e, Line)):
            raise ValueError('Every spoken line needs a declared role.')
        if self.input_kind == 'narration':
            cursor = 0
            for scene in self.scenes:
                if scene.music is not None or scene.clips or scene.minimum_duration:
                    raise ValueError('Literal narration cannot contain parsed music, duration or clip directives.')
                for event in scene.events:
                    if not isinstance(event, Line) or not event.literal:
                        raise ValueError('Literal narration contains only source-bound text lines.')
                    start, end = event.source_start, event.source_end
                    if (start < cursor or end > len(self.original_script)
                            or any(c != '\f' for c in self.original_script[cursor:start])
                            or self.original_script[start:end] != event.text):
                        raise ValueError('Literal narration changed or omitted source text.')
                    cursor = end
            if any(c != '\f' for c in self.original_script[cursor:]):
                raise ValueError('Literal narration omitted source text at the end.')
        elif any(event.literal for scene in self.scenes for event in scene.events if isinstance(event, Line)):
            raise ValueError('Literal narration lines require narration input mode.')
        if self.output_format not in FORMATS or type(self.sample_rate) is not int or self.sample_rate not in SAMPLE_RATES:
            raise ValueError('Unsupported audio output format or sample rate.')
        if type(self.export_stems) is not bool or type(self.disk_budget_bytes) is not int or self.disk_budget_bytes < 1024 ** 2:
            raise ValueError('Invalid stem selection or disk budget.')

    @property
    def source_sha256(self):
        return hashlib.sha256(self.original_script.encode('utf-8')).hexdigest()

    def to_dict(self):
        data = asdict(self)
        for scene, original in zip(data['scenes'], self.scenes):
            if self.schema_version == 1:
                # Preserve all existing saved-plan/cache fingerprints verbatim.
                scene.pop('clips')
                scene.pop('minimum_duration')
            for event, obj in zip(scene['events'], original.events):
                event['kind'] = {Line: 'line', Pause: 'pause', Sound: 'sound'}[type(obj)]
        return data

    @classmethod
    def from_dict(cls, value):
        try:
            return cls._from_dict(value)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('The production project has missing or invalid fields.') from exc

    @classmethod
    def _from_dict(cls, value):
        value = _strict(value, ('schema_version', 'title', 'original_script', 'roles', 'scenes', 'output_format', 'sample_rate', 'export_stems', 'disk_budget_bytes', 'input_kind'), 'project')
        scenes = []
        for item in value['scenes']:
            fields = ('id', 'title', 'events', 'music') + (('clips', 'minimum_duration') if value.get('schema_version', 1) == 2 else ())
            item = _strict(item, fields, 'scene')
            events = []
            for event in item['events']:
                if not isinstance(event, dict) or event.get('kind') not in ('line', 'pause', 'sound'):
                    raise ValueError('Unknown production event.')
                kind = event['kind']
                names, constructor = {'line': (('id', 'role', 'text', 'literal', 'source_start', 'source_end'), Line), 'pause': (('id', 'seconds'), Pause), 'sound': (('id', 'asset', 'gain', 'pan'), Sound)}[kind]
                _strict(event, (*names, 'kind'), 'event')
                events.append(constructor(**{k: v for k, v in event.items() if k != 'kind'}))
            music = item.get('music')
            if music is not None:
                music = MusicCue(**_strict(music, ('mood', 'tempo', 'gain', 'seed', 'engine', 'model_manifest'), 'music cue'))
            clips = tuple(Clip(**_strict(clip, ('id', 'asset', 'start', 'gain_db', 'pan', 'track',
                'trim_start', 'duration', 'fade_in', 'fade_out', 'title'), 'clip')) for clip in item.get('clips', ()))
            scenes.append(Scene(item['id'], item['title'], tuple(events), music, clips,
                                item.get('minimum_duration', 0.0)))
        return cls(**{**value, 'roles': tuple(Role.from_dict(r) for r in value['roles']), 'scenes': tuple(scenes)})
