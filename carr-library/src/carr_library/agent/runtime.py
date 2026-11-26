"""Scoped, typed local tools with reviewable plans and durable result receipts.

Documents are data. This runner never interprets imported text as instructions,
executes shell commands, requests credentials, publishes, or contacts a service.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time

from filelock import FileLock
from ..studio.models import identity, unique_object


class AgentError(ValueError):
    pass


# Only these typed operations can be emitted by a guided or optional model planner.
TOOLS = {
    'library.create': ({'name'}, {'description'}),
    'library.list': (set(), set()),
    'library.import': ({'workspace', 'path'}, {'title', 'language'}),
    'library.search': ({'workspace', 'query'}, {'mode', 'limit'}),
    'research.create_topic': ({'workspace', 'name'}, {'parent'}),
    'library.backup': ({'destination'}, set()),
    'studio.render': ({'project', 'project_dir'}, {'asset_root'}),
    'audio.convert': ({'source', 'destination'}, {'sample_rate', 'channels'}),
}


@dataclass(frozen=True)
class Step:
    id: str
    tool: str
    arguments: dict

    def __post_init__(self):
        identity(self.id, 'Tool step ID')
        if self.tool not in TOOLS:
            raise AgentError(f'Unsupported local tool: {self.tool}.')
        required, optional = TOOLS[self.tool]
        if type(self.arguments) is not dict or not required <= self.arguments.keys() or self.arguments.keys() - required - optional:
            raise AgentError(f'Unexpected or missing arguments for {self.tool}.')


@dataclass(frozen=True)
class Plan:
    goal: str
    steps: tuple[Step, ...]
    version: int = 1

    def __post_init__(self):
        if type(self.goal) is not str or not self.goal.strip() or '\0' in self.goal:
            raise AgentError('A local plan needs a clear goal.')
        if type(self.version) is not int or self.version != 1:
            raise AgentError('Unsupported local plan version.')
        if not isinstance(self.steps, tuple) or not self.steps or any(not isinstance(s, Step) for s in self.steps):
            raise AgentError('A local plan needs typed tool steps.')
        seen = set()
        for step in self.steps:
            if step.id in seen:
                raise AgentError('Tool step IDs must be unique.')
            for value in step.arguments.values():
                if type(value) is dict:
                    if set(value) != {'step', 'field'} or value['step'] not in seen or type(value['field']) is not str:
                        raise AgentError('Tool references must name an earlier result and one field.')
                elif value is not None and type(value) not in (str, bool, int, float):
                    raise AgentError('Tool arguments must be scalar values or typed prior-result references.')
            seen.add(step.id)

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict or set(data) - {'goal', 'steps', 'version'}:
            raise AgentError('Unexpected local plan fields.')
        try:
            return cls(data['goal'], tuple(Step(**s) for s in data['steps']), data.get('version', 1))
        except (TypeError, KeyError) as exc:
            raise AgentError('Malformed local tool plan.') from exc

    def to_dict(self):
        return asdict(self)

    @property
    def digest(self):
        raw = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        return sha256(raw.encode('utf-8')).hexdigest()

    def preview(self):
        return {'goal': self.goal, 'mode': 'LOCAL_ONLY', 'approval_sha256': self.digest,
                'steps': self.to_dict()['steps'], 'tools': sorted(TOOLS),
                'permissions': 'Only selected libraries and folders. No deletion, shell, publishing or network tools.'}


def load_plan(path, max_bytes=8 * 1024**2):
    with Path(path).open('rb') as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise AgentError('Tool plan exceeds the configured input budget.')
    return Plan.from_dict(json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_object))


class LocalAgent:
    def __init__(self, library, journal_root, *, read_roots=(), output_roots=(), workspaces=(), allow_create=False):
        self.library = library
        self.root = Path(journal_root).absolute()
        if self.root.is_symlink():
            raise AgentError('Agent journal cannot be a symbolic link.')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / 'agent.sqlite'
        if self.database.is_symlink():
            raise AgentError('Agent database cannot be a symbolic link.')
        self.read_roots = tuple(Path(p).resolve(strict=True) for p in read_roots)
        self.output_roots = tuple(Path(p).resolve(strict=True) for p in output_roots)
        self.workspaces = set(workspaces)
        self.allow_create = allow_create
        with sqlite3.connect(self.database) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS receipts(plan TEXT NOT NULL,step TEXT NOT NULL,tool TEXT NOT NULL,state TEXT NOT NULL,result TEXT,error TEXT,updated REAL NOT NULL,PRIMARY KEY(plan,step))')

    def _path(self, value, *, output=False):
        if type(value) is not str or not value or '\0' in value or '://' in value:
            raise AgentError('Tools accept explicit local paths only.')
        raw = Path(value).absolute()
        if raw.is_symlink():
            raise AgentError('A tool path cannot be a symbolic link.')
        if output:
            # The final component can itself be '..'. Normalize it before
            # containment checks; lexical prefix checks alone permit escape.
            candidate = (raw.parent.resolve(strict=True) / raw.name).resolve(strict=False)
            roots = self.output_roots
        else:
            candidate = raw.resolve(strict=True)
            roots = self.read_roots
        if not any(candidate.is_relative_to(root) for root in roots):
            raise AgentError('Tool path is outside the selected folders.')
        return candidate

    def _workspace(self, value):
        if type(value) is not str or value not in self.workspaces:
            raise AgentError('Tool library is outside the approved plan scope.')
        with self.library.read(value):
            pass
        return value

    def _preflight(self, tool, arguments):
        args = dict(arguments)
        if 'workspace' in args:
            args['workspace'] = self._workspace(args['workspace'])
        for field in ('path', 'source', 'project', 'asset_root'):
            if field in args:
                args[field] = self._path(args[field])
        for field in ('destination', 'project_dir'):
            if field in args:
                args[field] = self._path(args[field], output=True)
        if tool == 'library.create' and not self.allow_create:
            raise AgentError('Creating a library was not approved for this plan.')
        if tool == 'library.backup':
            current = {w.id for w in self.library.workspaces(include_archived=True)}
            if not current <= self.workspaces:
                raise AgentError('A complete backup requires all current libraries, including archives, in the approved scope.')
        for field in ('name', 'query'):
            if field in args and (type(args[field]) is not str or not args[field].strip()):
                raise AgentError(f'{field} must contain text.')
        if tool == 'library.search':
            if args.get('mode', 'phrase') not in ('phrase', 'word', 'terms', 'thematic', 'semantic', 'advanced'):
                raise AgentError('Unsupported search mode.')
            limit = args.get('limit', 20)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise AgentError('Choose 1 to 100 results for one search step.')
        return args

    def _call(self, tool, args, cancelled, progress):
        if tool == 'library.create':
            workspace = self.library.create_workspace(args['name'], args.get('description', ''))
            self.workspaces.add(workspace)
            return {'workspace': workspace}
        if tool == 'library.list':
            return {'libraries': [asdict(w) for w in self.library.workspaces() if w.id in self.workspaces]}
        if tool == 'library.import':
            result = self.library.import_file(**args, cancelled=cancelled)
            if result.get('state') not in ('complete', 'duplicate'):
                raise AgentError('Import requires review or recovery; no subsequent tool was run.')
            return result
        if tool == 'library.search':
            return {'results': [asdict(row) for row in self.library.search(**args, cancelled=cancelled)]}
        if tool == 'research.create_topic':
            return {'topic': self.library.create_topic(**args)}
        if tool == 'library.backup':
            # The snapshot gate rechecks this scope inside the actual backup;
            # another window may create a library after agent preflight.
            return {'manifest_sha256': self.library.backup(args['destination'], authorized_workspaces=self.workspaces),
                    'path': str(args['destination'])}
        if tool == 'studio.render':
            from ..studio import ProjectStore, load_project
            from ..studio.director import Director
            project = load_project(args['project'])
            return Director(ProjectStore(args['project_dir'])).render(project, cancelled=cancelled,
                asset_root=args.get('asset_root', args['project'].parent), allowed_audio_roots=self.read_roots,
                progress=progress)
        if tool == 'audio.convert':
            from ..audio import FFmpegAudio
            backend = FFmpegAudio(allowed_roots=self.read_roots + self.output_roots)
            result = backend.convert(args['source'], args['destination'], sample_rate=args.get('sample_rate', 24000),
                                     channels=args.get('channels', 2), cancelled=cancelled)
            return asdict(result)
        raise AgentError('Unsupported local tool.')

    def run(self, plan, *, approval_sha256, cancelled=None, progress=None):
        if not isinstance(plan, Plan):
            raise AgentError('Review and approve this exact plan before executing it.')
        # frozen dataclasses still contain mutable argument dictionaries. Take
        # one private deep snapshot before approval and use its digest for the
        # lock and every receipt, including during progress callback reentry.
        plan = Plan.from_dict(plan.to_dict())
        digest = plan.digest
        if approval_sha256 != digest:
            raise AgentError('Review and approve this exact plan before executing it.')
        cancelled = cancelled or (lambda: False)
        progress = progress or (lambda status: None)
        results = {}
        with FileLock(str(self.root / (digest + '.lock')), timeout=0):
            for step in plan.steps:
                if cancelled():
                    raise InterruptedError('Plan paused before the next tool. Completed results were retained.')
                args = {key: results[value['step']][value['field']] if type(value) is dict else value
                        for key, value in step.arguments.items()}
                # Recheck current scope even when reusing a successful receipt.
                args = self._preflight(step.tool, args)
                with sqlite3.connect(self.database) as db:
                    receipt = db.execute('SELECT state,result FROM receipts WHERE plan=? AND step=?', (digest, step.id)).fetchone()
                if receipt is not None:
                    if receipt[0] == 'complete':
                        # Read tools are permission and revision dependent.
                        # Refresh selections after process restart and never
                        # replay names from a previously wider selected scope.
                        results[step.id] = (self._call(step.tool, args, cancelled, progress)
                                            if step.tool in ('library.list', 'library.search') else json.loads(receipt[1]))
                        if step.tool == 'library.create':
                            self.workspaces.add(results[step.id]['workspace'])
                        progress({'step': step.id, 'state': 'reused', 'tool': step.tool})
                        continue
                    # Some tools commit outside this journal's transaction. Never
                    # duplicate a possibly committed action after a hard crash.
                    raise AgentError(f'Step {step.id} has an interrupted or failed receipt. Review its actual result before creating a replacement plan.')
                with sqlite3.connect(self.database) as db:
                    db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?)', (digest, step.id, step.tool, 'running', None, None, time.time()))
                progress({'step': step.id, 'state': 'running', 'tool': step.tool})
                try:
                    result = self._call(step.tool, args, cancelled, progress)
                    raw = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)
                    with sqlite3.connect(self.database) as db:
                        db.execute('UPDATE receipts SET state=?,result=?,updated=? WHERE plan=? AND step=?', ('complete', raw, time.time(), digest, step.id))
                    results[step.id] = json.loads(raw)
                    progress({'step': step.id, 'state': 'complete', 'tool': step.tool})
                except BaseException as exc:
                    with sqlite3.connect(self.database) as db:
                        db.execute('UPDATE receipts SET state=?,error=?,updated=? WHERE plan=? AND step=?', ('needs_review', str(exc), time.time(), digest, step.id))
                    raise
        return {'plan_sha256': digest, 'state': 'complete', 'results': results}


def import_plan(name, paths):
    """Deterministic guided planner; selected files never supply tool instructions."""
    steps = [Step('create', 'library.create', {'name': name})]
    steps.extend(Step(f'import-{i}', 'library.import', {'workspace': {'step': 'create', 'field': 'workspace'},
                                                        'path': str(Path(p).absolute())}) for i, p in enumerate(paths, 1))
    return Plan(f'Create library {name} and import the selected local documents.', tuple(steps))
