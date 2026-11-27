"""Versioned, data-only production contracts; scripts never become commands."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import re
from pathlib import PurePosixPath


SCHEMA = 1
SAMPLE_RATES = (8000, 16000, 22050, 24000, 44100, 48000, 96000)
FORMATS = ('wav', 'flac', 'mp3', 'opus', 'm4a', 'm4b', 'aiff')


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


EFFECT_PARAMETERS = {
    'highpass': {'frequency': (20, 20000)},
    'lowpass': {'frequency': (20, 20000)},
    'equalizer': {'frequency': (20, 20000), 'gain_db': (-24, 24), 'q': (0.1, 20)},
    'gate': {'threshold_db': (-90, 0), 'attack_ms': (0.1, 2000), 'release_ms': (1, 9000)},
    'compressor': {'threshold_db': (-60, 0), 'ratio': (1, 20), 'attack_ms': (0.1, 2000), 'release_ms': (1, 9000)},
    'delay': {'delay_ms': (1, 2000), 'decay': (0, 0.8)},
    'denoise': {'reduction_db': (0.01, 30)},
}


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
        if self.asset in ('.', '..') or any(c in self.asset for c in '/\\:\x00\n\r') or self.asset.startswith('.'):
            raise ValueError('Sound asset must be a filename inside the project assets directory.')
        number(self.gain, 0, 2, 'Sound gain')
        number(self.pan, -1, 1, 'Sound pan')


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
        if self.engine not in ('procedural', 'musicgen'):
            raise ValueError('Choose procedural or an explicitly installed MusicGen engine. ACE-Step is not yet implemented.')
        if self.model_manifest is not None:
            text(self.model_manifest, 'Model manifest', 1024)
            path = PurePosixPath(self.model_manifest)
            if path.is_absolute() or any(c in self.model_manifest for c in '\\:\n\r') or any(p in ('', '.', '..') for p in self.model_manifest.split('/')):
                raise ValueError('Model manifest must be a relative path under the project models directory.')
        if self.engine == 'musicgen' and self.model_manifest is None:
            raise ValueError('MusicGen needs an explicitly reviewed local model manifest.')
        if self.engine == 'procedural' and self.model_manifest is not None:
            raise ValueError('Procedural music does not use a neural model manifest.')


@dataclass(frozen=True)
class Scene:
    id: str
    title: str
    events: tuple[Line | Pause | Sound, ...]
    music: MusicCue | None = None

    def __post_init__(self):
        identity(self.id)
        text(self.title, 'Scene title', 500)
        if not isinstance(self.events, tuple) or not self.events or len(self.events) > 100000:
            raise ValueError('A scene needs events; split very large scenes into chapters.')
        if not all(isinstance(e, (Line, Pause, Sound)) for e in self.events):
            raise ValueError('Unknown scene event.')
        if len({e.id for e in self.events}) != len(self.events):
            raise ValueError('Duplicate scene event identity.')
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
        if type(self.schema_version) is not int or self.schema_version != SCHEMA:
            raise ValueError('Unsupported production project version.')
        if not isinstance(self.roles, tuple) or not 1 <= len(self.roles) <= 64 or not all(isinstance(r, Role) for r in self.roles):
            raise ValueError('Choose one to sixty-four named roles.')
        if len({r.name for r in self.roles}) != len(self.roles):
            raise ValueError('Role names must be unique.')
        if not isinstance(self.scenes, tuple) or not self.scenes or len(self.scenes) > 100000 or not all(isinstance(s, Scene) for s in self.scenes):
            raise ValueError('A production needs scenes.')
        if len({s.id for s in self.scenes}) != len(self.scenes):
            raise ValueError('Scene identities must be unique.')
        roles = {r.name for r in self.roles}
        if any(e.role not in roles for s in self.scenes for e in s.events if isinstance(e, Line)):
            raise ValueError('Every spoken line needs a declared role.')
        if self.input_kind == 'narration':
            segments = self.original_script.split('\f')
            if len(self.scenes) != len(segments):
                raise ValueError('Literal narration must preserve every source segment.')
            cursor = 0
            for scene, segment in zip(self.scenes, segments):
                segment_end = cursor + len(segment)
                if scene.music is not None:
                    raise ValueError('Literal narration cannot contain a parsed music directive.')
                for event in scene.events:
                    if not isinstance(event, Line) or not event.literal:
                        raise ValueError('Literal narration contains only source-bound text lines.')
                    start, end = event.source_start, event.source_end
                    if (start != cursor or end > segment_end
                            or self.original_script[start:end] != event.text):
                        raise ValueError('Literal narration changed or omitted source text.')
                    cursor = end
                if cursor != segment_end:
                    raise ValueError('Literal narration omitted source text at the segment end.')
                cursor = segment_end + 1
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
            item = _strict(item, ('id', 'title', 'events', 'music'), 'scene')
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
            scenes.append(Scene(item['id'], item['title'], tuple(events), music))
        return cls(**{**value, 'roles': tuple(Role.from_dict(r) for r in value['roles']), 'scenes': tuple(scenes)})
