"""Versioned, private narration projects and an intentionally small script language.

Source text is immutable within each saved revision. Performance cues refer to
source offsets; the source/citation database is never opened by this module.
Only explicitly authored scripts interpret directives. Imported book text must
use ``directives=False`` so embedded instructions remain spoken data.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, fields as dataclass_fields, is_dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from filelock import FileLock, Timeout

from ..data_paths import guard_data_directory
from .effect_presets import checked_chain, read_effect
from .models import Effect


MAX_PROJECT_BYTES = 32 * 1024 * 1024
MAX_CUES = 100_000  # A resource bound, not a duration ceiling.
MAX_DRAFTS = 1024
ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(value, label='identity'):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(f'Invalid {label}.')
    return value


def text(value, label, maximum=4000, *, empty=False):
    if (not isinstance(value, str) or (not empty and not value.strip())
            or len(value) > maximum or '\x00' in value
            or any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)):
        raise ValueError(f'Invalid {label}.')
    return value


def number(value, label, minimum, maximum):
    if type(value) not in (float, int) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'Invalid {label}; expected {minimum} through {maximum}.')
    return value


def local_path(value):
    """Validate every existing ancestor before any local filesystem mutation."""
    raw = os.fspath(value)
    if (not isinstance(raw, str) or not raw or raw.startswith(('\\\\', '//'))
            or '://' in raw or any(ord(char) < 32 for char in raw)):
        raise ValueError('Audio state requires a direct local path.')
    path = Path(raw).absolute()
    if '..' in path.parts:
        raise ValueError('Audio state paths cannot traverse parent directories.')
    if os.name == 'nt':
        import ctypes
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        get_drive_type.argtypes = (ctypes.c_wchar_p,)
        get_drive_type.restype = ctypes.c_uint
        if get_drive_type(path.anchor) == 4:
            raise ValueError('Mapped network drives are unavailable for local audio state.')
    for ancestor in (*reversed(path.parents), path):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Audio state paths cannot contain symbolic links or reparse points.')
        if ancestor != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('An audio state path ancestor is not a directory.')
    return path


def plain(path, *, directory=False, missing=False):
    path = local_path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not valid or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Audio state must use regular files in private local directories.')
    return info


def directory(path):
    path = local_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    plain(path, directory=True)
    return path


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON property.')
        result[key] = value
    return result


def read_json(path, maximum=MAX_PROJECT_BYTES):
    path = local_path(path)
    before = plain(path)
    if not 0 < before.st_size <= maximum:
        raise ValueError('Audio state file exceeds its resource budget.')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size, opened.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_nlink, before.st_size, before.st_mtime_ns):
            raise ValueError('Audio state changed while being opened; retry the operation.')
        raw = stream.read(maximum + 1)
    after = plain(path)
    if (len(raw) != before.st_size or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns or before.st_ino != after.st_ino):
        raise ValueError('Audio state changed while being read; retry the operation.')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON number.')))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid audio state.') from exc


def atomic_json(path, value, *, replace_existing=True):
    path = local_path(path)
    plain(path.parent, directory=True)
    plain(path, missing=True)
    raw = (canonical(value) + '\n').encode('utf-8')
    if len(raw) > MAX_PROJECT_BYTES:
        raise ValueError('Audio state exceeds its resource budget.')
    temporary = path.with_name('.' + uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace_existing:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def timeline_effects(values, sample_rate=192000):
    """Use the existing Scene DSP vocabulary without changing authored time."""
    result = checked_chain(values, sample_rate)
    if any(effect.enabled and effect.kind == 'tempo' for effect in result):
        raise ValueError('Timeline effect chains preserve duration. Apply tempo with Convert before assembling a project.')
    return result


def _effect_fields(value):
    if type(value) is not dict:
        raise ValueError('Saved role and scene properties must be objects.')
    result = dict(value)
    if 'effects' in result:
        effects = result['effects']
        if not isinstance(effects, (tuple, list)) or len(effects) > 32:
            raise ValueError('Saved effects must be an ordered list of at most 32 items.')
        result['effects'] = tuple(read_effect(item) for item in effects)
    return result


def _serialized(value):
    # Same typed Effect serialization as Scene projects, retaining asdict's
    # detached dictionaries and tuple/list shape for every legacy field.
    if isinstance(value, Effect):
        return value.to_dict()
    if is_dataclass(value):
        return {item.name: _serialized(getattr(value, item.name)) for item in dataclass_fields(value)}
    if isinstance(value, dict):
        return {deepcopy(key): _serialized(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_serialized(item) for item in value)
    if isinstance(value, list):
        return [_serialized(item) for item in value]
    return deepcopy(value)


EFFECTS_STEMS_NOTICE = (
    'Role effects are included in voice stems. Scene and master effects belong '
    'to the mixed output and its chapters; stems do not contain those bus effects. '
    'Effect tails are bounded by the existing sample timeline.')


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    voice_id: str
    language: str = 'en'
    rate: float = 1.0
    volume: float = 1.0
    pan: float = 0.0
    effects: tuple[Effect, ...] = ()

    def __post_init__(self):
        identity(self.id, 'role identity')
        text(self.name, 'role name', 200)
        text(self.voice_id, 'voice identity', 2000)
        if not isinstance(self.language, str) or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*', self.language):
            raise ValueError('Use an explicit language code for every role.')
        number(self.rate, 'speech rate', 0.5, 2)
        number(self.volume, 'voice volume', 0, 1)
        number(self.pan, 'pan', -1, 1)
        object.__setattr__(self, 'effects', timeline_effects(self.effects))

    def to_dict(self):
        value = _serialized(self)
        if not self.effects:
            value.pop('effects')
        return value


@dataclass(frozen=True)
class Cue:
    id: str
    kind: str
    text: str = ''
    role: str = ''
    seconds: float = 0
    asset: str = ''
    gain_db: float = 0
    source_start: int = 0
    source_end: int = 0

    def __post_init__(self):
        identity(self.id, 'cue identity')
        if self.kind not in ('speech', 'pause', 'asset'):
            raise ValueError('Unsupported cue kind.')
        text(self.text, 'cue text', MAX_PROJECT_BYTES, empty=self.kind != 'speech')
        if self.kind == 'speech':
            identity(self.role, 'speech role')
        elif self.text or self.role:
            raise ValueError('Only speech cues contain text and a role.')
        number(self.seconds, 'cue duration', 0, 7 * 24 * 3600)
        if self.kind == 'pause' and self.seconds <= 0:
            raise ValueError('Pause duration must be positive.')
        if self.kind == 'asset':
            if not isinstance(self.asset, str) or not DIGEST.fullmatch(self.asset):
                raise ValueError('Audio assets must be imported and identified by their content hash.')
        elif self.asset:
            raise ValueError('Only asset cues refer to an audio asset.')
        number(self.gain_db, 'cue gain', -60, 6)
        if (type(self.source_start) is not int or type(self.source_end) is not int
                or not 0 <= self.source_start <= self.source_end <= MAX_PROJECT_BYTES):
            raise ValueError('Invalid source offsets.')


@dataclass(frozen=True)
class Music:
    engine: str = 'procedural'
    mood: str = 'calm'
    tempo_bpm: int = 80
    key: str = 'A'
    seed: int = 0
    gain_db: float = -18
    prompt: str = ''

    def __post_init__(self):
        if self.engine not in ('procedural', 'fluidsynth', 'musicgen'):
            raise ValueError('Music engine is not installed by this version.')
        text(self.mood, 'music mood', 100)
        number(self.tempo_bpm, 'music tempo', 30, 240)
        text(self.key, 'music key', 10)
        if type(self.seed) is not int or not 0 <= self.seed < 2**31:
            raise ValueError('Invalid music seed.')
        number(self.gain_db, 'music level', -60, 0)
        text(self.prompt, 'music direction', 4000, empty=True)


@dataclass(frozen=True)
class Scene:
    id: str
    title: str
    cues: tuple[Cue, ...]
    music: Music | None = None
    effects: tuple[Effect, ...] = ()

    def __post_init__(self):
        identity(self.id, 'scene identity')
        text(self.title, 'scene title', 500)
        if (not isinstance(self.cues, tuple) or not self.cues or len(self.cues) > MAX_CUES
                or any(type(cue) is not Cue for cue in self.cues)
                or len({cue.id for cue in self.cues}) != len(self.cues)):
            raise ValueError('A scene needs distinct validated cues.')
        if self.music is not None and type(self.music) is not Music:
            raise ValueError('Invalid scene music configuration.')
        object.__setattr__(self, 'effects', timeline_effects(self.effects))


@dataclass(frozen=True)
class Settings:
    sample_rate: int = 24000
    channels: int = 2
    format: str = 'wav'
    stems: bool = True
    chunk_characters: int = 700
    chapter_gap_seconds: float = 0.75
    disk_budget_bytes: int = 0  # Zero means use measured available disk, not unlimited memory.

    def __post_init__(self):
        if type(self.sample_rate) is not int or self.sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000):
            raise ValueError('Unsupported production sample rate.')
        if type(self.channels) is not int or self.channels not in (1, 2):
            raise ValueError('Choose mono or stereo output.')
        if self.format not in ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff'):
            raise ValueError('Unsupported export format.')
        if type(self.stems) is not bool:
            raise ValueError('Invalid stem option.')
        if type(self.chunk_characters) is not int or not 100 <= self.chunk_characters <= 3000:
            raise ValueError('Speech chunks must contain between 100 and 3000 characters.')
        number(self.chapter_gap_seconds, 'chapter gap', 0, 3600)
        if type(self.disk_budget_bytes) is not int or self.disk_budget_bytes < 0:
            raise ValueError('Invalid disk budget.')


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    script: str
    roles: tuple[Role, ...]
    scenes: tuple[Scene, ...]
    settings: Settings = field(default_factory=Settings)
    revision: int = 1
    created_at: str = field(default_factory=now)
    source: dict = field(default_factory=dict)
    kind: str = 'research-audio-project'
    version: int = 1
    narration: dict = field(default_factory=dict)
    effects: tuple[Effect, ...] = ()

    def __post_init__(self):
        identity(self.id, 'project identity')
        text(self.title, 'project title', 500)
        text(self.script, 'original script', MAX_PROJECT_BYTES)
        if self.kind != 'research-audio-project' or type(self.version) is not int or self.version != 1:
            raise ValueError('Unsupported audio project format.')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('Invalid project revision.')
        text(self.created_at, 'creation date', 80)
        if type(self.settings) is not Settings:
            raise ValueError('Invalid project settings.')
        if (not isinstance(self.roles, tuple) or not 1 <= len(self.roles) <= 128
                or any(type(role) is not Role for role in self.roles)
                or len({role.id for role in self.roles}) != len(self.roles)):
            raise ValueError('A project needs distinct validated voices/roles.')
        if (not isinstance(self.scenes, tuple) or not self.scenes
                or any(type(scene) is not Scene for scene in self.scenes)
                or len({scene.id for scene in self.scenes}) != len(self.scenes)
                or sum(len(scene.cues) for scene in self.scenes) > MAX_CUES):
            raise ValueError('Invalid scene list or cue resource budget exceeded.')
        object.__setattr__(self, 'effects', timeline_effects(self.effects, self.settings.sample_rate))
        for chain in (*(role.effects for role in self.roles), *(scene.effects for scene in self.scenes)):
            timeline_effects(chain, self.settings.sample_rate)
        roles = {role.id for role in self.roles}
        for scene in self.scenes:
            for cue in scene.cues:
                if cue.kind == 'speech':
                    if cue.role not in roles or self.script[cue.source_start:cue.source_end] != cue.text:
                        raise ValueError('Speech must refer to an exact original script slice and a known role.')
        if (type(self.source) is not dict or any(key not in
                ('workspace', 'edition', 'revision', 'kind', 'index', 'text_sha256') for key in self.source)
                or len(canonical(self.source)) > 4000):
            raise ValueError('Source links contain identifiers only, never a quotation approval.')
        for value in self.source.values():
            if type(value) not in (str, int):
                raise ValueError('Invalid original source identity.')
        if self.source.get('text_sha256') is not None and self.source['text_sha256']!=self.script_sha256:
            raise ValueError('The reading project must retain its exact original source text.')
        if self.narration:
            from .narration import validate_narration
            validate_narration(self)
        elif type(self.narration) is not dict:
            raise ValueError('Invalid narration derivative collection.')

    @property
    def script_sha256(self):
        return sha256(self.script.encode('utf-8')).hexdigest()

    @property
    def fingerprint(self):
        return digest(self.to_dict())

    def to_dict(self):
        value = _serialized(self)
        if not self.effects:
            value.pop('effects')
        for role, row in zip(self.roles, value['roles']):
            if not role.effects:
                row.pop('effects')
        for scene, row in zip(self.scenes, value['scenes']):
            if not scene.effects:
                row.pop('effects')
        value['script_sha256'] = self.script_sha256
        return value

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict:
            raise ValueError('Invalid audio project.')
        fields = {'id','title','script','roles','scenes','settings','revision','created_at','source','kind','version','script_sha256'}
        if not fields <= set(value) or set(value) - fields - {'narration', 'effects'}:
            raise ValueError('Unsupported audio project properties.')
        try:
            rows = _effect_fields(value)
            expected = rows.pop('script_sha256')
            rows['roles'] = tuple(Role(**_effect_fields(row)) for row in rows['roles'])
            rows['scenes'] = tuple(Scene(row['id'], row['title'], tuple(Cue(**cue) for cue in row['cues']),
                Music(**row['music']) if row['music'] is not None else None,
                _effect_fields(row).get('effects', ())) for row in rows['scenes']
                if set(row) in ({'id','title','cues','music'}, {'id','title','cues','music','effects'}))
            if len(rows['scenes']) != len(value['scenes']):
                raise ValueError('Invalid scene properties.')
            rows['settings'] = Settings(**rows['settings'])
            result = cls(**rows)
        except (TypeError, KeyError, AttributeError) as exc:
            raise ValueError('Invalid audio project properties.') from exc
        if expected != result.script_sha256:
            raise ValueError('Original script checksum mismatch.')
        return result


def parse_script(script, voices, *, title='Audio project', language='en', project_id=None,
                 previous=None, directives=True, settings=None, source=None):
    """Parse authored role lines, # chapters, @pause and @music without executing text.

    Plain prose becomes a narrator track. Existing role choices survive edits.
    Unsupported @commands fail visibly instead of being interpreted as tools.
    Voice strings are discovered registry IDs, never executable commands.
    """
    text(script, 'script', MAX_PROJECT_BYTES)
    if isinstance(voices, (str, bytes)):
        raise ValueError('Provide an explicit list of local voices.')
    voices = tuple(voices)
    if not voices:
        raise ValueError('Install or select at least one local voice before creating a production.')
    voice_ids = tuple(getattr(voice, 'id', voice) for voice in voices)
    for voice_id in voice_ids:
        text(voice_id, 'voice identity', 2000)
    from .voices import supports_language
    default_voice_ids = tuple(getattr(voice, 'id', voice) for voice in voices
        if isinstance(voice, str) or supports_language(voice, language))
    prior = {role.name.casefold(): role for role in previous.roles} if previous else {}
    roles, scenes, cues = {}, [], []
    scene_title, music, offset = title, None, 0
    music_prompt_allowed = False

    def role_for(name):
        key = name.casefold()
        if key not in roles:
            known = prior.get(key)
            if known is None and not default_voice_ids:
                raise ValueError('No configured local voice declares the requested language. Select a matching voice before adding a role.')
            roles[key] = known or Role('role-' + sha256(key.encode('utf-8')).hexdigest()[:16],
                name, default_voice_ids[len(roles) % len(default_voice_ids)], language)
        return roles[key]

    def finish():
        nonlocal cues, music
        if cues:
            scenes.append(Scene(f'scene-{len(scenes) + 1}', scene_title, tuple(cues), music))
            cues = []
        music = None

    for raw in script.splitlines(keepends=True):
        line = raw.rstrip('\r\n')
        stripped = line.strip()
        if not stripped:
            offset += len(raw)
            continue
        following_music = music_prompt_allowed
        music_prompt_allowed = False
        if directives and stripped.startswith('# '):
            finish()
            scene_title = stripped[2:].strip()
            text(scene_title, 'chapter title', 500)
        elif directives and stripped.startswith('@pause '):
            try:
                seconds = float(stripped[7:])
            except ValueError as exc:
                raise ValueError('Use @pause followed by a number of seconds.') from exc
            cues.append(Cue(f'cue-{offset}', 'pause', seconds=seconds))
        elif directives and stripped.startswith('@music '):
            parts = stripped[7:].split()
            engine = parts.pop(0) if parts and parts[0] in ('procedural','fluidsynth','musicgen') else 'procedural'
            if not 1 <= len(parts) <= 2:
                raise ValueError('Use @music calm 80 or @music musicgen calm 80; engine and tempo are optional.')
            try:
                bpm = int(parts[1]) if len(parts) == 2 else 80
            except ValueError as exc:
                raise ValueError('Music tempo must be a whole number.') from exc
            music = Music(engine=engine,mood=parts[0], tempo_bpm=bpm)
            music_prompt_allowed = True
        elif directives and stripped.startswith('@music-prompt '):
            if not following_music or music is None:
                raise ValueError('Place one @music-prompt immediately after its @music directive.')
            music = replace(music,prompt=stripped[len('@music-prompt '):])
        elif directives and stripped.startswith('@'):
            raise ValueError('Unknown script directive. Supported: # chapter, @pause seconds, @music [engine] mood tempo, @music-prompt text.')
        else:
            name = 'Narrator'
            start = len(line) - len(line.lstrip())
            if directives:
                match = re.match(r'\s*([^:\r\n]{1,80}):[ \t]*', line)
                if match and not match.group(1).strip().lower() in ('http', 'https', 'file'):
                    name = match.group(1).strip()
                    start = match.end()
            end = len(line.rstrip())
            if end > start:
                role = role_for(name)
                cues.append(Cue(f'cue-{offset}', 'speech', line[start:end], role.id,
                    source_start=offset + start, source_end=offset + end))
        offset += len(raw)
    finish()
    if not roles:
        role_for('Narrator')
    if not scenes:
        raise ValueError('The script has no speech, asset or pause cues to render.')
    if previous:
        # A changed script may move a chapter. Carry its reviewed bus by a
        # unique title, never by a reused ordinal that now names another scene.
        prior_effects = {}
        for old in previous.scenes:
            if old.effects:
                if (sum(item.title == old.title for item in previous.scenes) != 1
                        or sum(item.title == old.title for item in scenes) != 1):
                    raise ValueError('A scene with saved effects was renamed, removed or became ambiguous. Clear or explicitly reassign its effects before editing the script.')
                prior_effects[old.title] = old.effects
        scenes = [replace(scene, effects=prior_effects.get(scene.title, ())) for scene in scenes]
    project = Project(project_id or (previous.id if previous else uuid4().hex), title, script,
        tuple(roles.values()), tuple(scenes), settings or (previous.settings if previous else Settings()),
        previous.revision + 1 if previous else 1,
        previous.created_at if previous else now(), source or (previous.source if previous else {}),
        effects=previous.effects if previous else ())
    if previous and previous.narration:
        from .narration import retained_narration
        keep=retained_narration(previous,(cue for scene in project.scenes for cue in scene.cues),project.id)
        if keep:
            project=replace(project,narration=keep)
    return project


class ProjectStore:
    """Short atomic state writes; render work never holds the state lock."""

    def __init__(self, root):
        self.root = guard_data_directory(local_path(root))
        directory(self.root)
        self.studio = directory(self.root / 'studio')
        self.projects = directory(self.studio / 'projects')

    @contextmanager
    def transaction(self):
        lock_path = self.studio / '.state.lock'
        plain(lock_path, missing=True)
        try:
            with FileLock(lock_path, mode=0o600).acquire(timeout=5):
                yield
        except Timeout as exc:
            raise ValueError('Audio state is busy; retry saving shortly.') from exc

    def path(self, project_id):
        identity(project_id, 'project identity')
        path = self.projects / project_id
        plain(path, directory=True)
        return path

    def create(self, project):
        if type(project) is not Project or project.revision != 1:
            raise ValueError('Create a validated first project revision.')
        with self.transaction():
            folder = local_path(self.projects / project.id)
            folder.mkdir(mode=0o700)
            for name in ('revisions', 'cache', 'jobs', 'assets'):
                directory(folder / name)
            atomic_json(folder / 'project.json', project.to_dict(), replace_existing=False)
        return project

    def load(self, project_id):
        value = Project.from_dict(read_json(self.path(project_id) / 'project.json'))
        if value.id != project_id:
            raise ValueError('Project identity mismatch.')
        return value

    def save(self, project, *, expected_fingerprint=None):
        if type(project) is not Project:
            raise ValueError('Save a validated audio project.')
        with self.transaction():
            before = self.load(project.id)
            if (project.revision != before.revision + 1
                    or (expected_fingerprint is not None and before.fingerprint != expected_fingerprint)):
                raise ValueError('This project changed in another window. Reopen it before saving.')
            folder = self.path(project.id)
            revision_path = folder / 'revisions' / f'{before.revision:08d}.json'
            if revision_path.exists():
                if Project.from_dict(read_json(revision_path)) != before:
                    raise ValueError('Conflicting project history; current changes were kept unsaved.')
            else:
                atomic_json(revision_path, before.to_dict(), replace_existing=False)
            atomic_json(folder / 'project.json', project.to_dict())
            # Legacy drafts have no window identity. Never delete another
            # editor's recovery data, or turn cleanup failure into a false
            # "project not saved" after the atomic commit has succeeded.
        return project

    def list_projects(self):
        rows = []
        for folder in sorted(self.projects.iterdir()):
            if ID.fullmatch(folder.name):
                project = self.load(folder.name)
                rows.append({'id': project.id, 'title': project.title, 'revision': project.revision,
                             'scenes': len(project.scenes), 'roles': len(project.roles)})
        return tuple(rows)

    @staticmethod
    def validate_draft(value, *, standalone=False):
        if type(value) is not dict:
            raise ValueError('Invalid audio draft.')
        if type(value.get('version')) is int and value['version'] == 3:
            fields = {'version', 'id', 'project_id', 'script', 'title', 'language', 'format',
                      'source', 'directives', 'base_revision', 'base_fingerprint', 'saved_at',
                      'revision', 'sha256'}
            if set(value) != fields:
                raise ValueError('Unsupported per-window audio draft fields.')
            if not isinstance(value['id'], str) or not re.fullmatch('[0-9a-f]{32}', value['id']):
                raise ValueError('Invalid audio draft window identity.')
            if value['project_id'] is not None:
                identity(value['project_id'], 'draft project identity')
            script = value['script']
            if (not isinstance(script, str) or len(script) > MAX_PROJECT_BYTES
                    or any(0xD800 <= ord(char) <= 0xDFFF for char in script)):
                raise ValueError('Invalid recovery draft text.')
            for key, maximum in (('title', 500), ('language', 80)):
                if not isinstance(value[key], str) or len(value[key]) > maximum:
                    raise ValueError('Invalid recovery draft metadata.')
            Settings(format=value['format'])
            text(value['saved_at'], 'draft date', 80)
            if (type(value['revision']) is not int or not 1 <= value['revision'] < 2**63
                    or type(value['base_revision']) is not int or not 0 <= value['base_revision'] < 2**63):
                raise ValueError('Invalid recovery draft revision.')
            fingerprint = value['base_fingerprint']
            if fingerprint is not None and (not isinstance(fingerprint, str) or not DIGEST.fullmatch(fingerprint)):
                raise ValueError('Invalid recovery draft base fingerprint.')
            if value['project_id'] is None and (value['base_revision'] != 0 or fingerprint is not None):
                raise ValueError('An unsaved project draft has no committed base.')
            if value['project_id'] is not None and value['base_revision'] < 1:
                raise ValueError('A project draft needs its committed base revision.')
            source = value['source']
            if (type(source) is not dict or set(source) - {'workspace','edition','revision','kind','index','text_sha256'}
                    or any(type(item) not in (str, int) for item in source.values())
                    or len(canonical(source)) > 4000 or type(value['directives']) is not bool
                    or (source and value['directives'])):
                raise ValueError('Invalid recovery draft source.')
            if source.get('text_sha256') is not None and source['text_sha256'] != sha256(script.encode('utf-8')).hexdigest():
                raise ValueError('The recovery draft must retain its exact original source text.')
            payload = {key: item for key, item in value.items() if key != 'sha256'}
            if value['sha256'] != digest(payload):
                raise ValueError('Recovery draft checksum mismatch.')
            return value
        legacy={'version','script','saved_at'}
        fields=legacy|{'base_revision','title','language','format'}
        if standalone:
            fields|={'id','source','directives'}
        if type(value.get('version')) is int and value['version']==1 and not standalone and set(value) in (legacy,legacy|{'base_revision'}):
            pass
        elif type(value.get('version')) is not int or value['version']!=2 or set(value)!=fields:
            raise ValueError('Unsupported audio draft fields.')
        text(value['script'],'draft script',MAX_PROJECT_BYTES,empty=True)
        text(value['saved_at'],'draft date',80)
        base=value.get('base_revision',0)
        if type(base) is not int or base<0 or (standalone and base!=0):
            raise ValueError('Invalid draft base revision.')
        if value['version']==2:
            text(value['title'],'draft title',500,empty=True)
            text(value['language'],'draft language',80,empty=True)
            Settings(format=value['format'])
        if standalone:
            if not re.fullmatch('[0-9a-f]{32}',value['id']):
                raise ValueError('Invalid unsaved draft identity.')
            source=value['source']
            if (type(source) is not dict or set(source)-{'workspace','edition','revision','kind','index','text_sha256'}
                    or any(type(v) not in (str,int) for v in source.values()) or len(canonical(source))>4000
                    or type(value['directives']) is not bool or (source and value['directives'])):
                raise ValueError('Invalid unsaved draft source.')
        return value

    def save_draft(self, project_id, script, *, title=None, language=None, format=None, base_revision=None,
                   session_id=None, base_fingerprint=None, source=None, directives=None):
        with self.transaction():
            current=self.load(project_id)
            if session_id is not None:
                return self._save_window_draft(session_id, script, project_id=project_id,
                    title=current.title if title is None else title,
                    language=current.roles[0].language if language is None else language,
                    format=current.settings.format if format is None else format,
                    base_revision=current.revision if base_revision is None else base_revision,
                    base_fingerprint=(current.fingerprint if base_revision in (None, current.revision)
                                      and base_fingerprint is None else base_fingerprint),
                    source=current.source if source is None else source,
                    directives=not bool(current.source) if directives is None else directives)
            if base_revision is not None and base_revision!=current.revision:
                raise ValueError('The project changed in another window. Its saved draft was not replaced.')
            draft={'version':2,'script':script,'saved_at':now(),'base_revision':current.revision,
                'title':current.title if title is None else title,
                'language':current.roles[0].language if language is None else language,
                'format':current.settings.format if format is None else format}
            atomic_json(self.path(project_id)/'draft.json',self.validate_draft(draft))

    def save_new_draft(self, draft_id, script, *, title, language, format, source=None, directives=True):
        with self.transaction():
            draft = self._save_window_draft(draft_id, script, title=title, language=language,
                format=format, source=source or {}, directives=directives)
        return draft

    def _save_window_draft(self, session_id, script, *, title, language, format,
                           project_id=None, base_revision=0, base_fingerprint=None,
                           source=None, directives=True):
        if not isinstance(session_id, str) or not re.fullmatch('[0-9a-f]{32}', session_id):
            raise ValueError('Invalid audio draft window identity.')
        folder = directory(self.studio / 'drafts')
        path = folder / (session_id + '.json')
        revision = 1
        if os.path.lexists(path):
            previous = self.validate_draft(read_json(path), standalone=True)
            if previous.get('id') != session_id or previous.get('project_id') != project_id:
                raise ValueError('An audio recovery window cannot overwrite a different project draft.')
            revision = previous.get('revision', 0) + 1
        payload = dict(version=3, id=session_id, project_id=project_id, script=script,
                       title=title, language=language, format=format, source=source or {},
                       directives=directives, base_revision=base_revision,
                       base_fingerprint=base_fingerprint, saved_at=now(), revision=revision)
        draft = {**payload, 'sha256': digest(payload)}
        self.validate_draft(draft, standalone=True)
        atomic_json(path, draft)
        return draft

    def _window_drafts(self):
        folder=self.studio/'drafts'
        local_path(folder)
        if not os.path.lexists(folder):
            return ()
        plain(folder,directory=True)
        rows=[]
        total = 0
        for index, path in enumerate(folder.iterdir()):
            if index >= MAX_DRAFTS * 2:
                raise ValueError('Audio recovery draft directory exceeds its entry budget.')
            if re.fullmatch('[0-9a-f]{32}\\.json',path.name):
                total += plain(path).st_size
                if len(rows) >= MAX_DRAFTS or total > 128 * 1024**2:
                    raise ValueError('Audio recovery drafts exceed their resource budget.')
                value=self.validate_draft(read_json(path),standalone=True)
                if value['id']!=path.stem:
                    raise ValueError('Unsaved draft identity mismatch.')
                rows.append(value)
        return tuple(sorted(rows,key=lambda row:row['saved_at'],reverse=True))

    def new_drafts(self):
        with self.transaction():
            return tuple(row for row in self._window_drafts() if row.get('project_id') is None)

    def project_drafts(self, project_id):
        identity(project_id, 'project identity')
        with self.transaction():
            rows = [row for row in self._window_drafts() if row.get('project_id') == project_id]
            legacy = self.path(project_id) / 'draft.json'
            if os.path.lexists(legacy):
                rows.append(self.validate_draft(read_json(legacy)))
            return tuple(sorted(rows, key=lambda row: row['saved_at'], reverse=True))

    def discard_new_draft(self, draft_id):
        if not isinstance(draft_id,str) or not re.fullmatch('[0-9a-f]{32}',draft_id):
            raise ValueError('Invalid unsaved draft identity.')
        with self.transaction():
            path=self.studio/'drafts'/(draft_id+'.json')
            if path.exists():
                plain(path.parent,directory=True)
                plain(path)
                value = self.validate_draft(read_json(path), standalone=True)
                if value['id'] != draft_id:
                    raise ValueError('Recovery draft identity mismatch; nothing was discarded.')
                path.unlink()

    def export(self, project_id, destination):
        destination = Path(destination)
        project = self.load(project_id)
        atomic_json(destination, project.to_dict(), replace_existing=False)
        return project.fingerprint

    def import_project(self, path):
        project = Project.from_dict(read_json(path))
        # Import as a distinct editable project. Source identities remain unchanged.
        identity = uuid4().hex
        # Cue reading copies are bound to the project as well as the original
        # text. Rebind only that local identity after full import validation;
        # keep source text, hashes, offsets, accepted edits and history exact.
        narration = {cue: {**snapshot, 'source_id': f'{identity}:{cue}'}
                     for cue, snapshot in project.narration.items()}
        return self.create(replace(project, id=identity, narration=narration,
                                   revision=1, created_at=now()))
