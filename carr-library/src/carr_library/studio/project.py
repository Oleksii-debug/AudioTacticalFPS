"""Versioned, editable performances kept separate from immutable source quotations."""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import math
from pathlib import Path
import re
from uuid import uuid4

from ..durable_state import atomic_json, encode, local_path, read_json, state_lock

MAX_PROJECT_IMPORT_BYTES = 16 * 1024**2


def _text(value, label, maximum=1_000_000, empty=False):
    if not isinstance(value, str) or '\x00' in value or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f'Invalid {label}.')
    value.encode('utf-8', 'strict')
    return value


def _number(value, label, lower, upper):
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f'{label} must be between {lower} and {upper}.')
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value):
        raise ValueError('Invalid stable project identifier.')
    return value


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    voice_id: str = ''
    language: str = ''
    pan: float = 0
    gain_db: float = -3
    rate: int = 0
    volume: int = 100

    def __post_init__(self):
        _id(self.id)
        _text(self.name, 'role name', 256)
        _text(self.voice_id, 'voice', 2048, True)
        _text(self.language, 'language', 64, True)
        _number(self.pan, 'Pan', -1, 1)
        _number(self.gain_db, 'Gain', -60, 12)
        if type(self.rate) is not int or not -10 <= self.rate <= 10:
            raise ValueError('Speech rate must be an integer between -10 and 10.')
        if type(self.volume) is not int or not 0 <= self.volume <= 100:
            raise ValueError('Voice volume must be an integer between 0 and 100.')


@dataclass(frozen=True)
class Line:
    id: str
    role: str
    text: str
    pause_after: float = .3
    source_ref: str = ''

    def __post_init__(self):
        _id(self.id)
        _id(self.role)
        _text(self.text, 'line text', 100_000, True)
        _text(self.source_ref, 'source reference', 2048, True)
        _number(self.pause_after, 'Pause', 0, 86400)


@dataclass(frozen=True)
class Music:
    mood: str = 'calm'
    tempo: int = 90
    gain_db: float = -24
    engine: str = 'procedural'
    seed: int = 0

    def __post_init__(self):
        if self.mood not in ('calm', 'suspense', 'hopeful', 'sad', 'energetic'):
            raise ValueError('Music mood must be calm, suspense, hopeful, sad, or energetic.')
        _number(self.tempo, 'Tempo', 20, 300)
        _number(self.gain_db, 'Music gain', -60, 0)
        if self.engine not in ('procedural', 'midi', 'musicgen', 'ace-step'):
            raise ValueError('Unknown music engine.')
        if type(self.seed) is not int or not 0 <= self.seed < 2**31:
            raise ValueError('Invalid music seed.')


@dataclass(frozen=True)
class Cue:
    id: str
    path: str
    start: float = 0
    gain_db: float = -6
    pan: float = 0
    role: str = 'sfx'
    trim_start: float = 0
    duration: float | None = None
    fade_in: float = 0
    fade_out: float = 0
    title: str = ''

    def __post_init__(self):
        _id(self.id)
        _text(self.path, 'local cue path', 8192)
        _number(self.start, 'Cue start', 0, 31_536_000)
        _number(self.gain_db, 'Cue gain', -60, 12)
        _number(self.pan, 'Cue pan', -1, 1)
        if self.role not in ('sfx', 'ambience', 'music', 'microphone'):
            raise ValueError('Invalid cue track role.')
        _number(self.trim_start, 'Clip source start', 0, 31_536_000)
        if self.duration is not None:
            _number(self.duration, 'Clip duration', .000001, 31_536_000)
        _number(self.fade_in, 'Clip fade in', 0, 31_536_000)
        _number(self.fade_out, 'Clip fade out', 0, 31_536_000)
        _text(self.title, 'clip title', 512, True)
        if self.duration is not None and max(self.fade_in, self.fade_out) > self.duration:
            raise ValueError('A clip fade exceeds its selected duration.')


@dataclass(frozen=True)
class Scene:
    id: str
    title: str
    lines: tuple[Line, ...]
    music: Music | None = None
    cues: tuple[Cue, ...] = ()
    minimum_duration: float = 0

    def __post_init__(self):
        _id(self.id)
        _text(self.title, 'scene title', 512)
        if not isinstance(self.lines, tuple) or not all(isinstance(x, Line) for x in self.lines):
            raise ValueError('Scene lines must be typed.')
        if len({x.id for x in self.lines}) != len(self.lines):
            raise ValueError('Duplicate line ID.')
        if self.music is not None and not isinstance(self.music, Music):
            raise ValueError('Invalid music cue.')
        if not isinstance(self.cues, tuple) or not all(isinstance(x, Cue) for x in self.cues):
            raise ValueError('Scene cues must be typed.')
        if len({cue.id for cue in self.cues}) != len(self.cues):
            raise ValueError('Duplicate scene clip ID.')
        if not self.lines and not self.cues and not self.music and self.minimum_duration <= 0:
            raise ValueError('A scene needs text, a local cue, music, or an explicit silence duration.')
        _number(self.minimum_duration, 'Scene duration', 0, 31_536_000)


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    roles: tuple[Role, ...]
    scenes: tuple[Scene, ...]
    original_script: str = ''
    mode: str = 'faithful'
    sample_rate: int = 24000
    revision: int = 1
    schema: int = 1

    def __post_init__(self):
        _id(self.id)
        _text(self.title, 'project title', 512)
        _text(self.original_script, 'original script', 16_000_000, True)
        if self.schema not in (1, 2) or type(self.schema) is not int or type(self.revision) is not int or self.revision < 1:
            raise ValueError('Unsupported project schema or revision.')
        if self.mode not in ('faithful', 'authored'):
            raise ValueError('Choose faithful narration or explicitly authored production.')
        if self.sample_rate not in (16000, 22050, 24000, 44100, 48000):
            raise ValueError('Unsupported master sample rate.')
        if not isinstance(self.roles, tuple) or not all(isinstance(x, Role) for x in self.roles):
            raise ValueError('Project roles must be typed.')
        if not isinstance(self.scenes, tuple) or not self.scenes or not all(isinstance(x, Scene) for x in self.scenes):
            raise ValueError('Project needs typed scenes.')
        roles = {role.id for role in self.roles}
        if len(roles) != len(self.roles) or len({s.id for s in self.scenes}) != len(self.scenes):
            raise ValueError('Duplicate role or scene ID.')
        if any(line.role not in roles for scene in self.scenes for line in scene.lines):
            raise ValueError('A line uses an unknown role.')
        if self.schema == 1 and any(cue.role == 'microphone' or cue.trim_start or
                cue.duration is not None or cue.fade_in or cue.fade_out or cue.title
                for scene in self.scenes for cue in scene.cues):
            raise ValueError('Edited clip timing and microphone tracks require project schema 2.')

    @property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()

    def to_dict(self):
        value = asdict(self)
        if self.schema == 1:
            # Keep every pre-editor project/job fingerprint stable. Schema 2
            # explicitly carries the new optional clip selection and fade data.
            for scene in value['scenes']:
                for cue in scene['cues']:
                    for name in ('trim_start', 'duration', 'fade_in', 'fade_out', 'title'):
                        cue.pop(name)
        return value

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError('A project must be a JSON object.')
        try:
            value = dict(value)
            value['roles'] = tuple(Role(**x) for x in value['roles'])
            scenes = []
            for source in value['scenes']:
                scene = dict(source)
                scene['lines'] = tuple(Line(**x) for x in scene['lines'])
                scene['cues'] = tuple(Cue(**x) for x in scene.get('cues', ()))
                if scene.get('music') is not None:
                    scene['music'] = Music(**scene['music'])
                scenes.append(Scene(**scene))
            value['scenes'] = tuple(scenes)
            return cls(**value)
        except (TypeError, KeyError) as exc:
            raise ValueError('Invalid or unknown project field.') from exc


def parse_script(text, *, title='Audio production', voice_ids=(), mode='faithful', project_id=None):
    """Parse a transparent script grammar; unmarked source prose is spoken verbatim.

    [Scene: title], [Music: suspense; tempo=80; gain=-24], [Pause: 2],
    [Duration: 3600] and Speaker [left|center|right]: text are directions.
    No document instruction can become a process, path, URL, or filter graph.
    """
    _text(text, 'script', 16_000_000)
    voices = tuple(voice_ids)
    if any(not isinstance(x, str) for x in voices):
        raise ValueError('Invalid voice choices.')
    roles, role_names, scenes, lines = [], {}, [], []
    scene_title, music, minimum = 'Chapter 1', None, 0

    def role_for(name, pan=None):
        key = name.casefold()
        if key not in role_names:
            index = len(roles)
            role = Role('role-' + str(index + 1), name,
                        voices[index % len(voices)] if voices else '', pan=pan or 0,
                        gain_db=-6 if key in ('narrator', 'оповідач', 'диктор') else -3)
            roles.append(role)
            role_names[key] = role.id
        elif pan is not None:
            from dataclasses import replace
            roles[:] = [replace(r, pan=pan) if r.id == role_names[key] else r for r in roles]
        return role_names[key]

    def flush():
        nonlocal lines, music, minimum
        if lines or music or minimum:
            scenes.append(Scene('scene-' + str(len(scenes) + 1), scene_title, tuple(lines), music,
                                minimum_duration=minimum))
        lines, music, minimum = [], None, 0

    for raw in text.splitlines():
        if not raw.strip():
            continue
        direction = re.fullmatch(r'\s*\[([^:\]]+):\s*(.*?)\]\s*', raw)
        if direction:
            command, body = direction[1].strip().casefold(), direction[2]
            if command in ('scene', 'chapter', 'сцена', 'розділ'):
                flush()
                scene_title = _text(body, 'scene title', 512)
            elif command in ('pause', 'пауза'):
                delay = _number(float(body), 'Pause', 0, 86400)
                lines.append(Line('line-' + str(len(lines) + 1), role_for('Narrator'), '', delay))
            elif command in ('duration', 'тривалість'):
                minimum = _number(float(body), 'Scene duration', 0, 31_536_000)
            elif command in ('music', 'музика'):
                items = [x.strip() for x in body.split(';')]
                options = {}
                for item in items[1:]:
                    key, separator, value = item.partition('=')
                    if not separator or key not in ('tempo', 'gain', 'seed', 'engine') or key in options:
                        raise ValueError('Music options are tempo, gain, seed and engine; use key=value.')
                    options[key] = value
                music = Music(items[0], int(options.get('tempo', 90)), float(options.get('gain', -24)),
                              options.get('engine', 'procedural'), int(options.get('seed', 0)))
            else:
                raise ValueError('Unknown script direction: ' + command)
            continue
        speaker = re.fullmatch(r'([^:\[\]\n]{1,100}?)(?:\s*\[(left|center|right|ліворуч|центр|праворуч)\])?:[ \t]?(.*)', raw)
        is_role = bool(speaker and (mode == 'authored' or speaker[2] is not None
                       or speaker[1].strip().casefold() in role_names
                       or speaker[1].strip().casefold() in ('narrator', 'оповідач', 'диктор')))
        if is_role:
            name = speaker[1].strip()
            pan = {'left': -1, 'center': 0, 'right': 1, 'ліворуч': -1, 'центр': 0, 'праворуч': 1}.get(speaker[2])
            role, spoken = role_for(name, pan), speaker[3]
        else:
            role, spoken = role_for('Narrator'), raw
        lines.append(Line('line-' + str(len(lines) + 1), role, spoken))
    flush()
    return Project(project_id or uuid4().hex, title, tuple(roles), tuple(scenes), text, mode)


def bilingual_project(pairs, *, title, roles, translation_id, source_sha256):
    """Build literal paired narration with passive links to an accepted export.

    This receives already reviewed text, never parses document text as Studio
    directions and grants no source access or quotation authority. The existing
    translation export manifest binds the project to its original and receipts.
    """
    from ..speech.runtime import split_utterances
    if (type(pairs) is not tuple or not 1 <= len(pairs) <= 10000
            or type(roles) is not tuple or len(roles) != 2
            or any(not isinstance(role, Role) or not role.voice_id for role in roles)
            or roles[0].id == roles[1].id
            or type(translation_id) is not str or not re.fullmatch(r'[0-9a-f]{32}', translation_id)
            or type(source_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', source_sha256)):
        raise ValueError('Choose a completed translation and two explicit narration voices.')
    for pair in pairs:
        if type(pair) is not tuple or len(pair) != 2:
            raise ValueError('Each narration pair needs original and translated text.')
        for value in pair:
            _text(value, 'literal narration text', 2_000_000, True)
    if sha256(''.join(pair[0] for pair in pairs).encode('utf-8')).hexdigest() != source_sha256:
        raise ValueError('Bilingual narration no longer matches its original text.')
    if any(sum(len(pair[index]) for pair in pairs) > 2_000_000 for index in (0, 1)):
        raise ValueError('Choose a smaller source range for this bilingual narration project.')
    scenes = []
    for index, pair in enumerate(pairs):
        lines = []
        for role, text in zip(roles, pair):
            parts = tuple(split_utterances(text)) if text.strip() else (text,)
            offset = 0
            for part_index, part in enumerate(parts):
                reference = (f'Translation job {translation_id}; request chunk {index + 1}; '
                             f'{role.id}; chunk text sha256 {sha256(text.encode()).hexdigest()}; '
                             f'characters {offset}-{offset + len(part)}; '
                             'derived narration; no quotation or live source authority')
                lines.append(Line(f'{role.id}-{part_index + 1}', role.id, part,
                                  .5 if part_index == len(parts) - 1 else 0, reference))
                offset += len(part)
        scenes.append(Scene(f'pair-{index + 1}', f'Original and translation — request chunk {index + 1}',
                            tuple(lines)))
    script = '\n\n'.join('ORIGINAL\n' + original + '\nTRANSLATION\n' + translated
                         for original, translated in pairs)
    project = Project(uuid4().hex, title, roles, tuple(scenes), original_script=script)
    if len(encode(project.to_dict())) > MAX_PROJECT_IMPORT_BYTES:
        raise ValueError('The bilingual project exceeds the 16 MiB Studio import budget. '
                         'Export text only or choose fewer source positions for narration.')
    return project


class ProjectStore:
    def __init__(self, directory):
        self.root = local_path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def directory(self, project_id):
        return self.root / _id(project_id)

    def save(self, project, *, expected_revision=None):
        if not isinstance(project, Project):
            raise ValueError('A typed audio project is required.')
        directory = self.directory(project.id)
        with state_lock(directory):
            path = directory / 'project.json'
            previous = Project.from_dict(read_json(path)) if path.exists() else None
            if expected_revision is not None and (previous is None or previous.revision != expected_revision):
                raise ValueError('Project changed since it was opened. Reload before saving.')
            if previous:
                if project.to_dict() == previous.to_dict():
                    return project
                if project.revision != previous.revision + 1:
                    raise ValueError('An edited project must advance its revision exactly once.')
                revisions = directory / 'revisions'
                revisions.mkdir(exist_ok=True)
                historical = revisions / f'{previous.revision:08d}-{previous.fingerprint}.json'
                if not historical.exists():
                    atomic_json(historical, previous.to_dict())
            atomic_json(path, project.to_dict(), replace=previous is not None)
        return project

    def load(self, project_id):
        return Project.from_dict(read_json(self.directory(project_id) / 'project.json'))

    def list(self):
        result = []
        for path in sorted(self.root.glob('*/project.json')):
            result.append(Project.from_dict(read_json(path)))
        return tuple(result)
