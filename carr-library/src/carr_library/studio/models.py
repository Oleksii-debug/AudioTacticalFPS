"""Versioned, non-executable narration plans. Authenticated source text is separate.

All time fields are seconds. Placement is relative to a scene; scenes are joined
in order. A pan value is not a claim of channel-isolated routing.
"""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
from pathlib import Path
import re


class PlanError(ValueError):
    pass


def number(value, name, lower, upper=None):
    if type(value) not in (int, float) or not math.isfinite(value) or value < lower or (upper is not None and value > upper):
        raise PlanError(f'{name} must be a finite number between {lower} and {upper if upper is not None else "the project budget"}.')
    return value


def identity(value, label='ID'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        raise PlanError(f'{label} must contain 1 to 80 letters, digits, underscores or hyphens.')
    return value


def _text(value, label, *, empty=False):
    if not isinstance(value, str) or '\0' in value or (not empty and not value.strip()):
        raise PlanError(f'{label} must be valid text.')
    return value


@dataclass(frozen=True)
class Role:
    id: str
    voice_id: str
    language: str | None = None
    rate: float = 1.0
    pitch: float = 0.0
    volume: float = 1.0
    pan: float = 0.0
    gain_db: float = 0.0
    routing: str = 'pan'

    def __post_init__(self):
        identity(self.id, 'Role ID')
        _text(self.voice_id, 'Voice ID')
        if self.language is not None:
            _text(self.language, 'Language')
        number(self.rate, 'Speech rate', .5, 2)
        number(self.pitch, 'Engine-relative pitch', -1, 1)
        number(self.volume, 'Volume', 0, 1)
        number(self.pan, 'Pan', -1, 1)
        number(self.gain_db, 'Gain', -60, 12)
        if self.routing not in ('pan', 'left', 'right', 'center'):
            raise PlanError('Routing must be pan, left, right, or center.')
        if self.routing != 'pan' and self.pan != 0:
            raise PlanError('Choose either pan or explicit channel routing for a role.')


@dataclass(frozen=True)
class Line:
    id: str
    role: str
    text: str
    pause_after: float = .25
    # A user-requested new take changes the cache identity without editing words.
    take: int = 0

    def __post_init__(self):
        identity(self.id, 'Line ID')
        identity(self.role, 'Role')
        _text(self.text, 'Line text')
        number(self.pause_after, 'Pause', 0)
        if type(self.take) is not int or self.take < 0:
            raise PlanError('Take must be a nonnegative integer.')


@dataclass(frozen=True)
class Clip:
    id: str
    path: str
    role: str = 'music'
    start_seconds: float = 0.0
    gain_db: float = -18.0
    pan: float = 0.0

    def __post_init__(self):
        identity(self.id, 'Clip ID')
        _text(self.path, 'Local audio path')
        if self.role not in ('music', 'effect', 'speech'):
            raise PlanError('Clip role must be music, effect, or speech.')
        number(self.start_seconds, 'Clip start', 0)
        number(self.gain_db, 'Clip gain', -60, 12)
        number(self.pan, 'Clip pan', -1, 1)


@dataclass(frozen=True)
class MusicCue:
    id: str
    prompt: str
    engine: str = 'fluidsynth'
    duration: float | None = None
    start_seconds: float = 0.0
    gain_db: float = -18.0
    seed: int = 0
    tempo: float = 90.0
    root_note: int = 60
    mode: str = 'minor'
    program: int = 0
    chunk_seconds: float = 30.0
    crossfade_seconds: float = .1
    model_dir: str | None = None
    manifest_path: str | None = None
    expected_manifest_sha256: str | None = None
    soundfont: str | None = None
    soundfont_sha256: str | None = None
    soundfont_license: str | None = None

    def __post_init__(self):
        from ..audio.music import MusicRequest
        identity(self.id, 'Music cue ID')
        MusicRequest(self.prompt, self.duration or 1, self.engine, self.seed, self.tempo,
                     self.root_note, self.mode, self.program)
        if self.duration is not None:
            number(self.duration, 'Music duration', .1)
        number(self.start_seconds, 'Music start', 0)
        number(self.gain_db, 'Music gain', -60, 12)
        number(self.chunk_seconds, 'Music chunk duration', .1, 30 if self.engine == 'musicgen' else 300)
        number(self.crossfade_seconds, 'Music crossfade', 0, self.chunk_seconds / 4)
        for name in ('model_dir', 'manifest_path', 'soundfont', 'soundfont_license'):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        for name in ('expected_manifest_sha256', 'soundfont_sha256'):
            digest = getattr(self, name)
            if digest is not None and (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
                raise PlanError(f'{name} must be an exact SHA256.')


@dataclass(frozen=True)
class Scene:
    id: str
    title: str
    lines: tuple[Line, ...] = ()
    clips: tuple[Clip, ...] = ()
    duration: float | None = None
    music: tuple[MusicCue, ...] = ()

    def __post_init__(self):
        identity(self.id, 'Scene ID')
        _text(self.title, 'Scene title')
        if not isinstance(self.lines, tuple) or any(not isinstance(x, Line) for x in self.lines):
            raise PlanError('Scene lines must be Line records.')
        if not isinstance(self.clips, tuple) or any(not isinstance(x, Clip) for x in self.clips):
            raise PlanError('Scene clips must be Clip records.')
        if not isinstance(self.music, tuple) or any(not isinstance(x, MusicCue) for x in self.music):
            raise PlanError('Music cues must be typed MusicCue records.')
        if not self.lines and not self.clips and not self.music and self.duration is None:
            raise PlanError('A scene needs text, prepared audio, or an explicit silence duration.')
        if len({x.id for x in (*self.lines, *self.clips, *self.music)}) != len(self.lines) + len(self.clips) + len(self.music):
            raise PlanError('Line and clip IDs must be unique within a scene.')
        if self.duration is not None:
            number(self.duration, 'Scene duration', .001)


@dataclass(frozen=True)
class Budget:
    # Resource budgets are configurable, not a fixed ceiling on project duration.
    disk_bytes: int = 50 * 1024**3
    wall_seconds: float | None = None
    chunk_characters: int = 3000

    def __post_init__(self):
        if type(self.disk_bytes) is not int or self.disk_bytes < 1024 * 1024:
            raise PlanError('Disk budget must allow at least 1 MiB.')
        if self.wall_seconds is not None:
            number(self.wall_seconds, 'Wall-time budget', .01)
        if type(self.chunk_characters) is not int or not 32 <= self.chunk_characters <= 16000:
            raise PlanError('Speech chunks must contain 32 to 16000 characters.')


@dataclass(frozen=True)
class Project:
    title: str
    roles: tuple[Role, ...]
    scenes: tuple[Scene, ...]
    sample_rate: int = 24000
    channels: int = 2
    budget: Budget = field(default_factory=Budget)
    source_reference: dict | None = None
    schema_version: int = 1
    authored_script: str | None = None

    def __post_init__(self):
        _text(self.title, 'Project title')
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise PlanError('Unsupported narration project version.')
        if self.sample_rate not in (16000, 22050, 24000, 32000, 44100, 48000, 96000):
            raise PlanError('Unsupported sample rate.')
        if type(self.channels) is not int or self.channels not in (1, 2):
            raise PlanError('Choose mono or stereo.')
        if not isinstance(self.roles, tuple) or any(not isinstance(x, Role) for x in self.roles):
            raise PlanError('Roles must be Role records.')
        if not isinstance(self.scenes, tuple) or not self.scenes or any(not isinstance(x, Scene) for x in self.scenes):
            raise PlanError('At least one valid scene is required.')
        if not isinstance(self.budget, Budget):
            raise PlanError('Invalid project budget.')
        role_ids = {x.id for x in self.roles}
        if len(role_ids) != len(self.roles) or len({x.id for x in self.scenes}) != len(self.scenes):
            raise PlanError('Role and scene IDs must be unique.')
        for scene in self.scenes:
            for line in scene.lines:
                if line.role not in role_ids:
                    raise PlanError(f'Unknown role {line.role} in scene {scene.id}.')
        if self.source_reference is not None:
            if type(self.source_reference) is not dict or any(type(k) is not str or type(v) is not str for k, v in self.source_reference.items()):
                raise PlanError('Source reference must contain text fields; it is a link, not quotation approval.')
        if self.authored_script is not None:
            _text(self.authored_script, 'Original authored script', empty=True)

    def to_dict(self):
        return asdict(self)

    def canonical_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

    @property
    def revision(self):
        return sha256(self.canonical_json().encode('utf-8')).hexdigest()

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict:
            raise PlanError('Project must be a JSON object.')
        try:
            data = dict(value)
            data['roles'] = tuple(Role(**r) for r in data.get('roles', ()))
            data['scenes'] = tuple(Scene(**{**s, 'lines': tuple(Line(**l) for l in s.get('lines', ())),
                                           'clips': tuple(Clip(**c) for c in s.get('clips', ())),
                                           'music': tuple(MusicCue(**m) for m in s.get('music', ()))}) for s in data.get('scenes', ()))
            data['budget'] = Budget(**data.get('budget', {}))
            return cls(**data)
        except (TypeError, KeyError, AttributeError) as exc:
            raise PlanError('Unknown, missing or invalid project fields.') from exc


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanError(f'Duplicate JSON field: {key}.')
        result[key] = value
    return result


def load_project(path, *, max_bytes=16 * 1024**2):
    with Path(path).open('rb') as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise PlanError('Project text exceeds the configured loading budget.')
    try:
        return Project.from_dict(json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_object))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PlanError('Project is not valid UTF-8 JSON.') from exc


def split_text(text, max_characters):
    """Bound synthesis memory without dropping, changing or duplicating characters."""
    if type(max_characters) is not int or max_characters < 1:
        raise PlanError('Chunk length must be positive.')
    offset = 0
    while offset < len(text):
        end = min(len(text), offset + max_characters)
        if end < len(text):
            lower = offset + max(1, max_characters // 2)
            candidates = [m.end() + offset for m in re.finditer(r'(?:[.!?…。！？][\s]+|\n\n|\s+)', text[offset:end])
                          if m.end() + offset >= lower]
            if candidates:
                end = candidates[-1]
        yield offset, end, text[offset:end]
        offset = end


def narration_project(text, voice_id, *, title='Narration', language=None, pan=0.0):
    """Create a guided faithful narration; no LLM or quotation rewrite is involved."""
    return Project(title, (Role('narrator', voice_id, language, pan=pan),),
                   (Scene('chapter-1', title, (Line('line-1', 'narrator', text),)),))
