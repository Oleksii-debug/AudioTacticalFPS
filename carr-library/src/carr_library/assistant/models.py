"""Small, inspectable assistant plans. Documents never define executable tools."""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re


TOOLS = frozenset({
    'library.create', 'library.list', 'library.catalog', 'library.import',
    'library.import_folder', 'library.search', 'library.extract',
    'library.backup', 'audio.produce', 'audio.transcribe',
    'reader.page', 'evidence.search', 'document.convert', 'voice.list',
    'audio.plan', 'audio.effects', 'player.inspect',
})
SCHEMA = 'carr-assistant-plan/1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def text(value, label, *, maximum=8192, empty=False):
    if not isinstance(value, str) or (not value.strip() and not empty) or len(value) > maximum:
        raise ValueError(f'Invalid {label}.')
    if '\x00' in value:
        raise ValueError(f'Invalid {label}.')
    value.encode('utf-8', errors='strict')
    return value


def local_path(value, *, must_exist=True):
    """Reject links and network paths at every action, not just plan creation."""
    p = Path(value).expanduser().absolute()
    if str(value).startswith(('\\\\', '//')) or '://' in str(value):
        raise ValueError('Choose a local filesystem path.')
    for component in (p, *p.parents):
        if component.is_symlink():
            raise ValueError('Symbolic links are outside assistant path permissions.')
        if os.name == 'nt' and component.exists():
            # Reject junctions/reparse points as well as ordinary symlinks.
            if getattr(component.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Windows reparse points are outside assistant path permissions.')
    p = p.resolve(strict=must_exist)
    if must_exist and not p.exists():
        raise ValueError('The selected path is unavailable.')
    return p


@dataclass(frozen=True)
class Scope:
    workspace_id: str | None = None
    input_roots: tuple = ()
    output_root: Path | None = None
    allow_create: bool = False
    allow_full_backup: bool = False
    max_steps: int = 128
    max_files: int = 5000
    max_seconds: float = 3600
    max_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self):
        if self.workspace_id is not None:
            text(self.workspace_id, 'workspace identity', maximum=128)
        if type(self.allow_create) is not bool or type(self.allow_full_backup) is not bool:
            raise ValueError('Invalid create-library permission.')
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 10000:
            raise ValueError('Invalid assistant step budget.')
        if type(self.max_files) is not int or not 1 <= self.max_files <= 1000000:
            raise ValueError('Invalid assistant file budget.')
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise ValueError('Invalid assistant byte budget.')
        if type(self.max_seconds) not in (int, float) or not math.isfinite(self.max_seconds) or self.max_seconds <= 0:
            raise ValueError('Invalid assistant time budget.')
        object.__setattr__(self, 'input_roots', tuple(local_path(p) for p in self.input_roots))
        if self.output_root is not None:
            output = local_path(self.output_root)
            if not output.is_dir():
                raise ValueError('Choose an existing output directory.')
            object.__setattr__(self, 'output_root', output)

    def input(self, value):
        path = local_path(value)
        for root in self.input_roots:
            root = local_path(root)
            if path == root or (root.is_dir() and path.is_relative_to(root)):
                return path
        raise PermissionError('The input is outside the folders selected for this plan.')

    def output(self, name):
        if self.output_root is None:
            raise PermissionError('Choose an output directory for this plan.')
        text(str(name), 'output name', maximum=1024)
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or ':' in str(name) or '\\' in str(name):
            raise PermissionError('Output names must remain inside the selected directory.')
        root = local_path(self.output_root)
        target = local_path(root / relative, must_exist=False)
        if target == root or not target.is_relative_to(root):
            raise PermissionError('Output must be a new child of the selected directory.')
        return target

    def to_dict(self):
        return {**asdict(self), 'input_roots': [str(p) for p in self.input_roots],
                'output_root': str(self.output_root) if self.output_root else None}


@dataclass(frozen=True)
class Step:
    tool: str
    arguments: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.tool not in TOOLS:
            raise ValueError('The assistant tool is not allowed.')
        if type(self.arguments) is not dict:
            raise ValueError('Tool arguments must be an object.')
        # JSON roundtrip rejects executable/opaque objects and non-finite numbers.
        encoded = canonical(self.arguments)
        if len(encoded.encode('utf-8')) > 4 * 1024 * 1024:
            raise ValueError('Assistant step exceeds the text budget.')
        object.__setattr__(self, 'arguments', json.loads(encoded))

    def to_dict(self):
        return {'tool': self.tool, 'arguments': self.arguments}


@dataclass(frozen=True)
class Plan:
    scope: Scope
    steps: tuple
    summary: str = ''
    schema: str = SCHEMA

    def __post_init__(self):
        if self.schema != SCHEMA or not isinstance(self.scope, Scope):
            raise ValueError('Unsupported assistant plan.')
        object.__setattr__(self, 'steps', tuple(self.steps))
        if not self.steps or len(self.steps) > self.scope.max_steps or not all(isinstance(s, Step) for s in self.steps):
            raise ValueError('The plan is empty or exceeds the selected step budget.')
        text(self.summary, 'plan summary', maximum=16384, empty=True)
        creates = sum(s.tool == 'library.create' for s in self.steps)
        if creates > 1 or (creates and not self.scope.allow_create):
            raise PermissionError('This plan does not permit creating a library.')
        if creates and self.scope.workspace_id is not None:
            raise ValueError('Choose an existing library or create one, not both.')

    def to_dict(self):
        return {'schema': self.schema, 'scope': self.scope.to_dict(),
                'steps': [s.to_dict() for s in self.steps], 'summary': self.summary}

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)

    @property
    def digest(self):
        return sha256(canonical(self.to_dict()).encode('utf-8')).hexdigest()

    @classmethod
    def from_dict(cls, value, *, scope=None):
        if type(value) is not dict or set(value) - {'schema', 'scope', 'steps', 'summary'}:
            raise ValueError('Unsupported fields in assistant plan.')
        if value.get('schema', SCHEMA) != SCHEMA:
            raise ValueError('Unsupported assistant plan schema.')
        raw_scope = value.get('scope')
        if scope is None:
            if type(raw_scope) is not dict:
                raise ValueError('Assistant scope is required.')
            scope = Scope(**raw_scope)
        elif raw_scope is not None and raw_scope != scope.to_dict():
            raise PermissionError('A proposed plan cannot expand the selected permissions.')
        raw_steps = value.get('steps')
        if type(raw_steps) is not list:
            raise ValueError('Assistant steps must be an array.')
        steps = []
        for s in raw_steps:
            if type(s) is not dict or set(s) - {'tool', 'arguments'} or 'tool' not in s:
                raise ValueError('Invalid assistant step.')
            steps.append(Step(s['tool'], s.get('arguments', {})))
        return cls(scope, tuple(steps), value.get('summary', ''), value.get('schema', SCHEMA))

    @classmethod
    def from_json(cls, value, *, scope=None):
        text(value, 'plan JSON', maximum=8 * 1024 * 1024)
        return cls.from_dict(json.loads(value), scope=scope)


@dataclass(frozen=True)
class JobReport:
    id: str
    status: str
    completed: int
    total: int
    results: tuple = ()
    message: str = ''
