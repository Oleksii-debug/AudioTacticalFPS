"""A deterministic LOCAL ONLY assistant; document content is never a command.

Plans are selected by the user, validated completely, reviewed, then approved.
Existing library services remain authoritative for scope, originals and evidence.
The journal coordinates cooperative processes in trusted local folders, not an
adversarial process that can replace filesystem entries during a system call.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile
from types import MappingProxyType
from uuid import uuid4

from .ingest.batch_scan import planned_snapshot
from .ingest.writer_guard import exclusive_writer, _regular_if_present
from .research.export_file import _filename, _linked

VERSION = 1
MAX_STEPS = 32
MAX_PLAN_BYTES = 64 * 1024
MAX_JOB_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 512 * 1024
TOOLS = frozenset(('search', 'list_topics', 'create_topic', 'export_topic', 'import_text', 'backup'))
STATES = frozenset(('ready', 'running', 'paused', 'failed', 'complete'))
STEP_STATES = frozenset(('pending', 'running', 'complete'))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key in the plan or job.')
        result[key] = value
    return result


def _decode(raw):
    return json.loads(raw, object_pairs_hook=_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON number.')))


def _text(value, label, maximum=4000):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 and char not in '\n\t' for char in value)):
        raise ValueError(f'Invalid {label}.')
    value.encode('utf-8', errors='strict')
    return value.strip()


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _directory(value):
    if value is None or value == '':
        return None
    path = Path(value).absolute()
    if '..' in path.parts or str(path).startswith(('\\\\', '//')):
        raise ValueError('Choose a local folder without parent traversal or network shares.')
    for item in (path, *path.parents):
        info = item.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('Allowed folders must be existing directories without links or reparse points.')
    return path.resolve(strict=True)


def _relative(value):
    if not isinstance(value, str) or value != value.strip():
        raise ValueError('Use a relative path without surrounding whitespace.')
    value = _text(value, 'relative path', 1024)
    if value.startswith(('/', '\\')) or '\\' in value or ':' in value:
        raise ValueError('Use a relative path inside the selected folder.')
    parts = value.split('/')
    for part in parts:
        _filename(part)
    return value


def _path(root, value, *, regular=False, absent=False, allowed_links=(1,)):
    if root is None:
        raise ValueError('Select the allowed input or output folder before previewing this plan.')
    root = _directory(root)
    parts = _relative(value).split('/')
    path = root.joinpath(*parts)
    for parent in reversed(path.parent.parents):
        info = parent.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('A selected path contains a link or non-directory.')
    parent_info = path.parent.lstat()
    if _linked(parent_info) or not stat.S_ISDIR(parent_info.st_mode):
        raise ValueError('The selected parent must be an existing plain directory.')
    if not path.parent.resolve(strict=True).is_relative_to(root):
        raise ValueError('The path is outside the selected folder.')
    try:
        info = path.lstat()
    except FileNotFoundError:
        if regular:
            raise ValueError('The selected input file is unavailable.') from None
    else:
        if (_linked(info) or (stat.S_ISREG(info.st_mode) and info.st_nlink not in allowed_links)
                or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                or (regular and not stat.S_ISREG(info.st_mode))):
            raise ValueError('Linked, special or redirected files are not allowed.')
        if absent:
            raise FileExistsError('Choose a new output name; existing output is never overwritten.')
    return path


def _hash_file(path, *, max_bytes=None, links=1):
    before = path.lstat()
    if _linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != links:
        raise ValueError('Linked, special or redirected files are not allowed.')
    if max_bytes is not None and before.st_size > max_bytes:
        raise ValueError('The output exceeds its checked size.')
    digest, size = sha256(), 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_nlink) != (before.st_dev, before.st_ino, links):
            raise ValueError('File changed while opening.')
        while block := stream.read(1024 * 1024):
            size += len(block)
            if max_bytes is not None and size > max_bytes:
                raise ValueError('File grew beyond its checked size.')
            digest.update(block)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, links) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink) or size != before.st_size:
        raise ValueError('File changed during verification.')
    return dict(size=size, sha256=digest.hexdigest())


def _sync_dir(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _export_intent(value):
    if value is None:
        return None
    if (type(value) is not dict or set(value) not in (
            {'kind', 'content'}, {'kind', 'content', 'staging'})
            or value.get('kind') != 'export'):
        raise ValueError('Invalid export checkpoint intent.')
    content = value['content']
    if (type(content) is not dict or set(content) != {'size', 'sha256'}
            or type(content['size']) is not int or content['size'] < 0
            or not isinstance(content['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', content['sha256'])):
        raise ValueError('Invalid export checkpoint content.')
    staging = value.get('staging')
    if 'staging' in value and (not isinstance(staging, str) or not re.fullmatch(
            r'\.assistant-export-[a-z0-9_]{8}', staging)):
        raise ValueError('Invalid export checkpoint staging name.')
    return value


def _export_staging(path, intent, *, reconcile=False):
    """Validate a journaled staging file; unlink only its proven published twin.

    The read-only load path uses the same checks. Reconciliation is requested
    only by the runner holding the job writer guard. Unknown extra hard links
    remain forbidden, even when the output itself has the expected bytes.
    """
    if intent is None or 'staging' not in intent:
        return None
    temporary = path.parent / intent['staging']
    if not os.path.lexists(temporary):
        if path.exists() and path.stat().st_nlink != 1:
            raise ValueError('The published export has an unaccounted hard link.')
        return None
    staged = temporary.lstat()
    if _linked(staged) or not stat.S_ISREG(staged.st_mode):
        raise ValueError('The export staging file is linked or redirected.')
    if os.path.lexists(path):
        published = path.lstat()
        if (_linked(published) or not stat.S_ISREG(published.st_mode)
                or not staged.st_ino or not os.path.samestat(staged, published)
                or staged.st_nlink != 2 or published.st_nlink != 2
                or _hash_file(temporary, max_bytes=intent['content']['size'], links=2) != intent['content']):
            raise ValueError('The published export has an unaccounted or changed staging link.')
        if reconcile:
            temporary.unlink()
            _sync_dir(path.parent)
        return None
    if _hash_file(temporary, max_bytes=intent['content']['size']) != intent['content']:
        raise ValueError('The unpublished export staging bytes changed.')
    return temporary


@dataclass(frozen=True)
class Step:
    tool: str
    arguments: MappingProxyType

    def data(self):
        values = dict(self.arguments)
        if 'manifest' in values:
            values['manifest'] = dict(values['manifest'])
        return dict(tool=self.tool, arguments=values)


@dataclass(frozen=True)
class Plan:
    workspace: str
    input_root: str | None
    output_root: str | None
    allow_full_backup: bool
    steps: tuple[Step, ...]

    def data(self):
        return dict(version=VERSION, workspace=self.workspace, input_root=self.input_root,
                    output_root=self.output_root, allow_full_backup=self.allow_full_backup,
                    steps=[step.data() for step in self.steps])

    @property
    def checksum(self):
        return _digest(self.data())


def describe_plan(plan):
    lines = ['LOCAL ONLY — deterministic guided workflow; no language model or remote API.',
             f'Library: {plan.workspace}', f'Allowed input folder: {plan.input_root or "none"}',
             f'Allowed output folder: {plan.output_root or "none"}',
             'Review every step before choosing Approve and run. Existing output is never overwritten.']
    for index, step in enumerate(plan.steps, 1):
        args = dict(step.arguments)
        if step.tool == 'backup':
            lines.append(f'{index}. BACK UP ALL LIBRARIES AND THEIR ORIGINAL DOCUMENTS: {args["folder"]}/snapshot')
        else:
            visible = {key: value for key, value in args.items() if key not in ('manifest',)}
            lines.append(f'{index}. {step.tool}: {_json(visible)}')
    return '\n'.join(lines)


def describe_job(job):
    lines = [f'Job {job["id"]}: {job["state"]}',
             f'Completed steps: {sum(row["state"] == "complete" for row in job["steps"])}/{len(job["steps"])}']
    if job.get('error'):
        lines.append('Last error: ' + job['error'])
    for index, (definition, state) in enumerate(zip(job['plan']['steps'], job['steps']), 1):
        lines.append(f'{index}. {definition["tool"]}: {state["state"]}')
        if state['result'] is not None:
            lines.append(json.dumps(state['result'], ensure_ascii=False, indent=2))
    return '\n'.join(lines)


class LocalLibraryAssistant:
    def __init__(self, library, workspace, *, input_root=None, output_root=None, allow_full_backup=False):
        self.library, self.workspace = library, _text(workspace, 'library identity', 256)
        self.input_root, self.output_root = _directory(input_root), _directory(output_root)
        if type(allow_full_backup) is not bool:
            raise ValueError('The all-library backup permission must be explicit.')
        self.allow_full_backup = allow_full_backup
        if self.output_root is not None and self.output_root.is_relative_to(library.root):
            raise ValueError('Choose an output folder outside the private library data.')
        if self.input_root is not None and self.input_root.is_relative_to(library.root):
            raise ValueError('Use selected original input files outside private library state.')
        self.journal_root = library.root / 'library-workflows'
        self.journal = self.journal_root / sha256(self.workspace.encode('utf-8')).hexdigest()
        self._authorize()
        if self.journal_root.exists():
            _directory(self.journal_root)
        if self.journal.exists():
            _directory(self.journal)

    def _authorize(self):
        with self.library.read(self.workspace):
            pass

    def _allowed_path(self, root, value, **options):
        path = _path(root, value, **options)
        if path.is_relative_to(self.library.root):
            raise ValueError('A workflow cannot read or write private application files as source or output.')
        return path

    def _parse(self, goal):
        if isinstance(goal, dict):
            data = goal
        else:
            goal = _text(goal, 'goal', MAX_PLAN_BYTES)
            if len(goal.encode('utf-8')) > MAX_PLAN_BYTES:
                raise ValueError('The plan is too large.')
            if goal.lstrip().startswith('{'):
                data = _decode(goal)
            else:
                steps = []
                names = {'search': 'search', 'пошук': 'search', 'знайди': 'search',
                         'topic': 'create_topic', 'тема': 'create_topic',
                         'export': 'export_topic', 'експорт': 'export_topic',
                         'import': 'import_text', 'імпорт': 'import_text',
                         'backup': 'backup', 'копія': 'backup'}
                for line in goal.splitlines():
                    if not line.strip():
                        continue
                    if line.strip().casefold() in ('topics', 'теми'):
                        steps.append(dict(tool='list_topics', arguments={}))
                        continue
                    command, separator, value = line.partition(':')
                    tool = names.get(command.strip().casefold())
                    if not separator or tool is None or not value.strip():
                        raise ValueError('Unsupported command. Use search:, topic:, import:, export:, backup:, topics, or a JSON plan.')
                    value = value.strip()
                    if tool == 'export_topic':
                        topic, arrow, filename = value.partition('->')
                        if not arrow:
                            raise ValueError('Use export: topic-id -> new-file.md (or @0 for the first created topic).')
                        arguments = dict(topic=topic.strip(), filename=filename.strip())
                    else:
                        key = dict(search='query', create_topic='name', import_text='path', backup='folder')[tool]
                        arguments = {key: value}
                    steps.append(dict(tool=tool, arguments=arguments))
                data = dict(steps=steps)
        if type(data) is not dict or set(data) != {'steps'} or type(data['steps']) is not list:
            raise ValueError('Use a JSON object containing only a steps list.')
        if len(_json(data).encode('utf-8')) > MAX_PLAN_BYTES:
            raise ValueError('The plan is too large.')
        return data['steps']

    def _topic(self, identity, prior):
        identity = _text(identity, 'topic identity', 256)
        if identity.startswith('@'):
            if not re.fullmatch(r'@(?:0|[1-9][0-9]*)', identity):
                raise ValueError('Use @0, @1, etc. for a preceding create_topic step.')
            index = int(identity[1:])
            if not 0 <= index < len(prior) or prior[index].tool != 'create_topic':
                raise ValueError('A topic reference must name a preceding create_topic step.')
        elif identity not in {row.id for row in self.library.topics(self.workspace)}:
            raise ValueError('The selected topic is unavailable in this library.')
        return identity

    def _steps(self, rows, *, bind_inputs, fresh_outputs, checkpoints=None):
        if type(rows) is not list or not 1 <= len(rows) <= MAX_STEPS:
            raise ValueError(f'Use between 1 and {MAX_STEPS} steps per reviewed plan.')
        result, outputs = [], set()
        for row in rows:
            if type(row) is not dict or set(row) != {'tool', 'arguments'} or type(row['arguments']) is not dict:
                raise ValueError('Each step must contain only tool and arguments.')
            tool, args = row['tool'], dict(row['arguments'])
            if not isinstance(tool, str) or tool not in TOOLS:
                raise ValueError('This tool is not allowed. Shell, code, network and secret tools are unavailable.')
            allowed = dict(search={'query', 'mode', 'limit'}, list_topics=set(),
                create_topic={'name', 'parent'}, export_topic={'topic', 'filename', 'format'},
                import_text={'path', 'title', 'manifest'} if not bind_inputs else {'path', 'title'},
                backup={'folder'})[tool]
            if set(args) - allowed:
                raise ValueError('Unsupported arguments; library scope and allowed folders cannot be overridden.')
            if tool == 'search':
                args['query'] = _text(args.get('query'), 'search query')
                args.setdefault('mode', 'phrase')
                args.setdefault('limit', 20)
                if args['mode'] not in ('phrase', 'word', 'terms', 'thematic', 'advanced'):
                    raise ValueError('Choose a supported local search mode.')
                if type(args['limit']) is not int or not 1 <= args['limit'] <= 100:
                    raise ValueError('Search results must be between 1 and 100.')
                from .search import literal, thematic
                if args['mode'] == 'thematic':
                    thematic._groups(args['query'])
                else:
                    # Empty allowed editions validate the real parser/bounds
                    # without opening or scanning any document or database.
                    literal.search(None, self.workspace, args['query'], mode=args['mode'],
                                   limit=args['limit'], edition_ids=())
            elif tool == 'create_topic':
                args['name'] = _text(args.get('name'), 'topic name', 512)
                args.setdefault('parent', None)
                if args['parent'] is not None:
                    args['parent'] = self._topic(args['parent'], result)
            elif tool == 'export_topic':
                args['topic'] = self._topic(args.get('topic'), result)
                args['filename'] = _relative(args.get('filename'))
                args.setdefault('format', 'markdown')
                if args['format'] not in ('markdown', 'text', 'ris', 'bibtex', 'csv'):
                    raise ValueError('Use markdown, text, ris, bibtex or csv for this export.')
                checkpoint = checkpoints[len(result)] if checkpoints is not None else None
                intent = _export_intent(checkpoint['intent']) if checkpoint is not None else None
                links = (1, 2) if (checkpoint is not None and checkpoint['state'] == 'running'
                                   and intent is not None and 'staging' in intent) else (1,)
                path = self._allowed_path(self.output_root, args['filename'],
                                          absent=fresh_outputs, allowed_links=links)
                if checkpoint is not None:
                    _export_staging(path, intent)
                output = args['filename'].casefold()
                if output in outputs:
                    raise ValueError('Each output in a plan must have a distinct name.')
                outputs.add(output)
            elif tool == 'backup':
                if not self.allow_full_backup:
                    raise ValueError('A complete backup contains ALL libraries and originals. Enable that explicit permission first.')
                args['folder'] = _relative(args.get('folder'))
                self._allowed_path(self.output_root, args['folder'], absent=fresh_outputs)
                output = args['folder'].casefold()
                if output in outputs:
                    raise ValueError('Each output in a plan must have a distinct name.')
                outputs.add(output)
            elif tool == 'import_text':
                args['path'] = _relative(args.get('path'))
                path = self._allowed_path(self.input_root, args['path'], regular=bind_inputs)
                if path.suffix.casefold() != '.txt':
                    raise ValueError('The guided assistant currently imports UTF-8 TXT only. Use Import Center for PDF, DOCX or EPUB.')
                args['title'] = _text(args.get('title', path.stem), 'source title', 512)
                if bind_inputs:
                    source = planned_snapshot(path)
                    if source.mime != 'text/plain':
                        raise ValueError('Choose a UTF-8 text source.')
                    args['manifest'] = MappingProxyType(source.manifest())
                else:
                    manifest = args.get('manifest')
                    if (type(manifest) is not dict or set(manifest) != {'sha256', 'mime', 'size'}
                            or manifest['mime'] != 'text/plain' or type(manifest['size']) is not int
                            or not 0 < manifest['size'] <= 64 * 1024 * 1024
                            or not isinstance(manifest['sha256'], str)
                            or not re.fullmatch('[0-9a-f]{64}', manifest['sha256'])):
                        raise ValueError('Invalid planned source manifest.')
                    args['manifest'] = MappingProxyType(dict(manifest))
            result.append(Step(tool, MappingProxyType(args)))
        return tuple(result)

    def plan(self, goal):
        self._authorize()
        return Plan(self.workspace, str(self.input_root) if self.input_root else None,
                    str(self.output_root) if self.output_root else None, self.allow_full_backup,
                    self._steps(self._parse(goal), bind_inputs=True, fresh_outputs=True))

    def _restore_plan(self, data, *, fresh_outputs=False, checkpoints=None):
        if (type(data) is not dict or set(data) != {'version', 'workspace', 'input_root',
                'output_root', 'allow_full_backup', 'steps'} or type(data['version']) is not int
                or data['version'] != VERSION or data['workspace'] != self.workspace
                or data['input_root'] != (str(self.input_root) if self.input_root else None)
                or data['output_root'] != (str(self.output_root) if self.output_root else None)
                or type(data['allow_full_backup']) is not bool
                or data['allow_full_backup'] != self.allow_full_backup):
            raise ValueError('The job scope or allowed folders differ from the reviewed plan.')
        return Plan(self.workspace, data['input_root'], data['output_root'], self.allow_full_backup,
                    self._steps(data['steps'], bind_inputs=False, fresh_outputs=fresh_outputs,
                                checkpoints=checkpoints))

    def _job_path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
            raise ValueError('Invalid assistant job identifier.')
        _directory(self.journal)
        return self.journal / (identity + '.json')

    def _save(self, job):
        job['updated_utc'] = _stamp()
        payload = dict(checksum=_digest(job), job=job)
        raw = (_json(payload) + '\n').encode('utf-8')
        if len(raw) > MAX_JOB_BYTES:
            raise ValueError('Assistant journal exceeded its operation budget.')
        target = self._job_path(job['id'])
        _regular_if_present(target)
        temporary = None
        try:
            with NamedTemporaryFile(prefix='.assistant-', dir=self.journal, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            _sync_dir(self.journal)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_file(path):
        _regular_if_present(path)
        if path.stat().st_size > MAX_JOB_BYTES:
            raise ValueError('Assistant journal is too large.')
        with path.open('rb') as stream:
            raw = stream.read(MAX_JOB_BYTES + 1)
        if len(raw) > MAX_JOB_BYTES:
            raise ValueError('Assistant journal grew while reading.')
        value = _decode(raw.decode('utf-8'))
        if (type(value) is not dict or set(value) != {'checksum', 'job'}
                or type(value['job']) is not dict or value['checksum'] != _digest(value['job'])):
            raise ValueError('Assistant journal checksum mismatch.')
        return value['job']

    def start(self, plan, *, approved=False):
        if approved is not True or not isinstance(plan, Plan):
            raise ValueError('Review the complete plan, then explicitly approve it before execution.')
        self._authorize()
        checked = self._restore_plan(plan.data(), fresh_outputs=True)
        # Recheck exact input bytes before creating any durable job state.
        for step in checked.steps:
            if step.tool == 'import_text':
                path = self._allowed_path(self.input_root, step.arguments['path'], regular=True)
                if planned_snapshot(path).manifest() != step.arguments['manifest']:
                    raise ValueError('A selected source changed after preview. Preview a new plan.')
        if self.journal_root.exists():
            _directory(self.journal_root)
        self.journal_root.mkdir(mode=0o700, exist_ok=True)
        self.journal.mkdir(mode=0o700, exist_ok=True)
        _directory(self.journal)
        with exclusive_writer(self.journal):
            identity = uuid4().hex
            job = dict(version=VERSION, id=identity, library_root=str(self.library.root),
                workspace=self.workspace, plan=checked.data(), approved_plan_sha256=checked.checksum,
                state='ready', error='', created_utc=_stamp(), updated_utc=_stamp(),
                steps=[dict(state='pending', result=None, intent=None) for _ in checked.steps])
            self._save(job)
        return job

    def load(self, identity):
        self._authorize()
        try:
            job = self._read_file(self._job_path(identity))
        except FileNotFoundError:
            raise ValueError('The job is unavailable in this library.') from None
        if (set(job) != {'version', 'id', 'library_root', 'workspace', 'plan', 'approved_plan_sha256',
                         'state', 'error', 'created_utc', 'updated_utc', 'steps'}
                or type(job['version']) is not int or job['version'] != VERSION or job['id'] != identity
                or job['library_root'] != str(self.library.root) or job['workspace'] != self.workspace
                or not isinstance(job['state'], str) or job['state'] not in STATES
                or not isinstance(job['error'], str) or type(job['steps']) is not list):
            raise ValueError('Invalid or cross-library assistant job.')
        for key in ('created_utc', 'updated_utc'):
            value = job[key]
            if not isinstance(value, str) or len(value) > 80:
                raise ValueError('Invalid assistant checkpoint timestamp.')
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                raise ValueError('Invalid assistant checkpoint timestamp.') from None
            if parsed.tzinfo is None:
                raise ValueError('Assistant checkpoint timestamps require a timezone.')
        for row in job['steps']:
            if (type(row) is not dict or set(row) != {'state', 'result', 'intent'}
                    or not isinstance(row['state'], str) or row['state'] not in STEP_STATES
                    or (row['state'] == 'complete' and type(row['result']) is not dict)):
                raise ValueError('Invalid assistant checkpoint.')
        if (type(job['plan']) is not dict or type(job['plan'].get('steps')) is not list
                or len(job['steps']) != len(job['plan']['steps'])):
            raise ValueError('Invalid assistant checkpoint count.')
        plan = self._restore_plan(job['plan'], checkpoints=job['steps'])
        if job['approved_plan_sha256'] != plan.checksum:
            raise ValueError('The reviewed plan changed after approval.')
        incomplete = False
        for index, (step, row) in enumerate(zip(plan.steps, job['steps'])):
            if row['state'] == 'complete':
                if incomplete:
                    raise ValueError('Completed assistant steps must form an ordered prefix.')
            else:
                if row['state'] == 'running' and incomplete:
                    raise ValueError('Only the first incomplete assistant step can be running.')
                incomplete = True
            self._validate_checkpoint(job, index, step, row)
        if job['state'] == 'complete' and incomplete:
            raise ValueError('A complete assistant job has unfinished steps.')
        if job['state'] == 'ready' and any(row['state'] != 'pending' for row in job['steps']):
            raise ValueError('A ready assistant job has already executed steps.')
        return job

    def _validate_checkpoint(self, job, index, step, row):
        """Validate typed durable results before any replay or @N resolution."""
        tool, args, result, intent = step.tool, step.arguments, row['result'], row['intent']
        if tool == 'export_topic':
            _export_intent(intent)
        elif intent is not None:
            raise ValueError('This assistant tool cannot have an export intent.')
        if row['state'] != 'complete':
            if result is not None or (row['state'] == 'pending' and intent is not None):
                raise ValueError('An unfinished assistant step has an invalid result or intent.')
            return
        if type(result) is not dict or len(_json(result).encode('utf-8')) > MAX_RESULT_BYTES:
            raise ValueError('Invalid or oversized assistant result checkpoint.')

        def require(condition):
            if not condition:
                raise ValueError(f'Invalid {tool} result checkpoint.')

        def identity(value):
            return (isinstance(value, str) and bool(value.strip()) and len(value) <= 256
                    and '\x00' not in value)

        def digest(value):
            return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None

        if tool == 'search':
            require(set(result) == {'results', 'count', 'notice'})
            records = result['results']
            require(type(records) is list and type(result['count']) is int
                    and result['count'] == len(records) <= args['limit']
                    and isinstance(result['notice'], str))
            fields = {'workspace', 'revision', 'edition_id', 'title', 'author', 'edition',
                      'page', 'quote', 'citation', 'verified', 'reason'}
            for record in records:
                require(type(record) is dict and set(record) == fields)
                require(record['workspace'] == self.workspace and type(record['verified']) is bool
                        and all(isinstance(record[key], str) for key in fields - {'verified'})
                        and identity(record['edition_id']) and digest(record['revision'])
                        and bool(record['quote']))
        elif tool == 'list_topics':
            require(set(result) == {'topics'} and type(result['topics']) is list)
            seen = set()
            for record in result['topics']:
                require(type(record) is dict and set(record) == {'id', 'parent', 'name', 'position', 'archived'})
                require(identity(record['id']) and record['id'] not in seen
                        and (record['parent'] is None or identity(record['parent']))
                        and isinstance(record['name'], str) and bool(record['name'].strip())
                        and len(record['name']) <= 512 and '\x00' not in record['name']
                        and type(record['position']) is int and 0 <= record['position'] <= 2**31 - 1
                        and record['archived'] is False)
                seen.add(record['id'])
        elif tool == 'create_topic':
            parent = self._resolved_topic(args['parent'], job['steps']) if args['parent'] is not None else None
            require(result == dict(topic_id=self._stable_id(job, index, 'topic'),
                                   name=args['name'], parent=parent))
        elif tool == 'export_topic':
            require(set(result) == {'output', 'size', 'sha256', 'recovered'} and intent is not None)
            require(result['output'] == str(self.output_root / args['filename'])
                    and type(result['size']) is int and type(result['recovered']) is bool
                    and dict(size=result['size'], sha256=result['sha256']) == intent['content'])
        elif tool == 'import_text':
            require(set(result) == {'id', 'workspace', 'edition', 'asset', 'state',
                                    'completed', 'total', 'error', 'verified'})
            require(digest(result['id']) and result['workspace'] == self.workspace
                    and result['edition'] == self._stable_id(job, index, 'edition')
                    and result['asset'] == args['manifest']['sha256'] and result['state'] == 'complete'
                    and type(result['completed']) is int and type(result['total']) is int
                    and 0 < result['completed'] == result['total']
                    and result['error'] in (None, '') and result['verified'] is True)
        elif tool == 'backup':
            require(set(result) == {'output', 'manifest_sha256', 'scope'})
            require(result['output'] == str(self.output_root / args['folder'] / 'snapshot')
                    and digest(result['manifest_sha256'])
                    and result['scope'] == 'ALL libraries and registered originals')

    @classmethod
    def open_job(cls, library, workspace, identity):
        probe = cls(library, workspace)
        try:
            job = cls._read_file(probe._job_path(identity))
        except FileNotFoundError:
            raise ValueError('The job is unavailable in this library; it may belong to another library.') from None
        if job.get('workspace') != workspace or job.get('library_root') != str(library.root):
            raise ValueError('The job belongs to another library or data folder.')
        data = job.get('plan', {})
        service = cls(library, workspace, input_root=data.get('input_root'),
                      output_root=data.get('output_root'), allow_full_backup=data.get('allow_full_backup', False))
        service.load(identity)
        return service

    def jobs(self):
        self._authorize()
        if not self.journal.exists():
            return ()
        _directory(self.journal)
        rows = []
        for path in self.journal.glob('*.json'):
            if not re.fullmatch('[0-9a-f]{32}', path.stem):
                continue
            job = self._read_file(path)
            if job.get('workspace') != self.workspace or job.get('library_root') != str(self.library.root):
                continue
            rows.append(dict(id=job['id'], state=job.get('state'), created_utc=job.get('created_utc'),
                             completed=sum(step.get('state') == 'complete' for step in job.get('steps', [])),
                             total=len(job.get('steps', []))))
            if len(rows) > 1000:
                raise ValueError('Too many assistant jobs to display in one view.')
        return tuple(sorted(rows, key=lambda row: (row['created_utc'], row['id']), reverse=True))

    @staticmethod
    def _resolved_topic(value, states):
        return states[int(value[1:])]['result']['topic_id'] if value.startswith('@') else value

    @staticmethod
    def _stable_id(job, index, purpose):
        return sha256(f'{job["id"]}:{index}:{purpose}'.encode()).hexdigest()[:32]

    def _export(self, job, index, step, cancelled):
        args, state = step.arguments, job['steps'][index]
        intent = _export_intent(state['intent'])
        links = (1, 2) if intent is not None and 'staging' in intent else (1,)
        path = self._allowed_path(self.output_root, args['filename'], allowed_links=links)
        temporary = _export_staging(path, intent, reconcile=True)
        if path.exists():
            if type(intent) is not dict or intent.get('kind') != 'export' or _hash_file(path) != intent.get('content'):
                raise FileExistsError('Existing export does not match this job. It was not overwritten.')
            return dict(output=str(path), **intent['content'], recovered=True)
        if temporary is None:
            topic = self._resolved_topic(args['topic'], job['steps'])
            content = self.library.export_topic(self.workspace, topic, format=args['format'], cancelled=cancelled)
            payload = content.encode('utf-8')
            expected = dict(size=len(payload), sha256=sha256(payload).hexdigest())
            if intent is not None and intent['content'] != expected:
                raise ValueError('The source evidence changed after export preparation; preview a new plan.')
            state['intent'] = dict(kind='export', content=expected)
            self._save(job)
        else:
            expected = intent['content']
        try:
            if temporary is None:
                with NamedTemporaryFile(prefix='.assistant-export-', dir=path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                state['intent'] = dict(kind='export', content=expected, staging=temporary.name)
                self._save(job)
            if cancelled():
                raise InterruptedError('Export paused before publication.')
            self._allowed_path(self.output_root, args['filename'], absent=True)
            # Same-directory hard-link publication is exclusive: it never
            # replaces a destination created after preview. Remove staging link.
            os.link(temporary, path)
            temporary.unlink()
            temporary = None
            _sync_dir(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return dict(output=str(path), **expected, recovered=False)

    def _backup(self, job, index, step, cancelled=lambda: False):
        from .library_backup import verify_backup, MANIFEST
        if cancelled():
            raise InterruptedError('Backup paused before starting or verifying its saved snapshot.')
        folder = self._allowed_path(self.output_root, step.arguments['folder'])
        owner = dict(job=job['id'], step=index, plan_sha256=job['approved_plan_sha256'])
        if not folder.exists():
            folder.mkdir(mode=0o700)
            with (folder / 'assistant-owner.json').open('x', encoding='utf-8') as stream:
                stream.write(_json(owner))
                stream.flush()
                os.fsync(stream.fileno())
            _sync_dir(folder)
        _directory(folder)
        owner_file = folder / 'assistant-owner.json'
        _regular_if_present(owner_file)
        if not owner_file.exists() or owner_file.stat().st_size > 4096 or _decode(owner_file.read_text('utf-8')) != owner:
            raise ValueError('Existing backup folder is not owned by this job. It was not changed.')
        target = folder / 'snapshot'
        if target.exists():
            _directory(target)
            manifest_path = target / MANIFEST
            check = _hash_file(manifest_path, max_bytes=8 * 1024 * 1024)
            digest = check['sha256']
        else:
            digest = self.library.backup(target, cancelled=cancelled)
        # Delegate all supported formats/sidecars to the same authoritative
        # validation as restoration, both before completion and on recovery.
        verify_backup(target, digest, cancelled=cancelled)
        return dict(output=str(target), manifest_sha256=digest, scope='ALL libraries and registered originals')

    def _execute(self, job, index, step, cancelled):
        args = step.arguments
        if step.tool == 'search':
            # Keep assistant handles separate from the notebook's current search.
            from .application import LibraryApp
            app = LibraryApp(self.library.root, semantic_ranker=self.library.semantic_ranker)
            hits = app.search(self.workspace, args['query'], mode=args['mode'], limit=args['limit'],
                              cancelled=cancelled)
            rows = [{key: value for key, value in asdict(hit).items() if key != 'ref'} for hit in hits]
            result = dict(results=rows, count=len(rows), notice='Source snapshots at execution time; recheck before using as new evidence.')
        elif step.tool == 'list_topics':
            result = dict(topics=[asdict(row) for row in self.library.topics(self.workspace)])
        elif step.tool == 'create_topic':
            identity = self._stable_id(job, index, 'topic')
            parent = self._resolved_topic(args['parent'], job['steps']) if args['parent'] is not None else None
            with self.library.topic_transaction(self.workspace) as tree:
                prior = next((row for row in tree.list_topics(include_archived=True) if row.id == identity), None)
                if prior is None:
                    tree.create(identity, args['name'], parent=parent)
                elif prior.name != args['name'] or prior.parent != parent or prior.archived:
                    raise ValueError('The topic created by this job has changed. Preview a new plan.')
            result = dict(topic_id=identity, name=args['name'], parent=parent)
        elif step.tool == 'export_topic':
            result = self._export(job, index, step, cancelled)
        elif step.tool == 'import_text':
            path = self._allowed_path(self.input_root, args['path'], regular=True)
            metadata = dict(title=args['title'], work_id=self._stable_id(job, index, 'work'),
                            edition_id=self._stable_id(job, index, 'edition'))
            # This existing orchestration entrypoint compares the planned hash
            # to the exact bytes it retains; no parser or journal is duplicated.
            result = self.library._import_file(self.workspace, path, **metadata,
                _expected_manifest=args['manifest'], cancelled=cancelled)
            if result['state'] in ('failed', 'cancelled') and not cancelled():
                result = self.library._import_file(self.workspace, path, **metadata,
                    _expected_manifest=args['manifest'], _resume_job=result['id'], cancelled=cancelled)
            if result['state'] != 'complete':
                if cancelled() or result['state'] == 'cancelled':
                    raise InterruptedError('Text import paused; its existing journal will resume.')
                raise ValueError('Text import is not complete: ' + result['state'])
            result = dict(result)
        else:
            if cancelled():
                raise InterruptedError('Backup paused before starting.')
            # The authoritative backup owns its safe cancellation checkpoints;
            # final publication remains indivisible to cooperative cancellation.
            result = self._backup(job, index, step, cancelled)
        if len(_json(result).encode('utf-8')) > MAX_RESULT_BYTES:
            raise ValueError('The result is too large for this job. Narrow the query or selected topic.')
        return result

    def _verify_completed(self, job, index, step, cancelled=lambda: False):
        """A resumed job must still have its earlier durable write results."""
        result, args = job['steps'][index]['result'], step.arguments
        if step.tool == 'export_topic':
            path = self._allowed_path(self.output_root, args['filename'], regular=True)
            if _hash_file(path) != dict(size=result.get('size'), sha256=result.get('sha256')):
                raise ValueError('A completed export changed. It was not overwritten.')
        elif step.tool == 'create_topic':
            expected = self._stable_id(job, index, 'topic')
            parent = self._resolved_topic(args['parent'], job['steps']) if args['parent'] is not None else None
            row = next((item for item in self.library.topics(self.workspace) if item.id == expected), None)
            if row is None or row.name != args['name'] or row.parent != parent:
                raise ValueError('A completed topic changed or became unavailable. Review a new plan.')
        elif step.tool == 'import_text':
            edition = self._stable_id(job, index, 'edition')
            with self.library.read(self.workspace) as db:
                found = db.execute('SELECT 1 FROM revision r JOIN sealed s ON s.revision=r.id '
                    'WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=?',
                    (self.workspace, edition, result.get('id'), args['manifest']['sha256'])).fetchone()
                if found is None or result.get('edition') != edition:
                    raise ValueError('A completed text import is unavailable in the selected library.')
                self.library.assets.resolve(args['manifest']['sha256'])
        elif step.tool == 'backup':
            folder = self._allowed_path(self.output_root, args['folder'])
            if not (folder / 'snapshot').is_dir():
                raise ValueError('A completed backup is unavailable. It was not recreated or overwritten.')
            verified = self._backup(job, index, step, cancelled)
            if verified['manifest_sha256'] != result.get('manifest_sha256'):
                raise ValueError('A completed backup manifest changed.')

    def run(self, identity, *, cancelled=lambda: False, progress=None):
        if not callable(cancelled) or (progress is not None and not callable(progress)):
            raise ValueError('Invalid job progress or cancellation callback.')
        with exclusive_writer(self.journal):
            job = self.load(identity)
            plan = self._restore_plan(job['plan'], checkpoints=job['steps'])
            job['state'], job['error'] = 'running', ''
            self._save(job)
            try:
                for index, step in enumerate(plan.steps):
                    self._authorize()
                    if cancelled():
                        raise InterruptedError('Paused. Completed steps are saved; resume when ready.')
                    if job['steps'][index]['state'] == 'complete':
                        self._verify_completed(job, index, step, cancelled)
                        continue
                    job['steps'][index]['state'] = 'running'
                    self._save(job)
                    if progress is not None:
                        progress(f'Step {index + 1} of {len(plan.steps)}: {step.tool}')
                    if cancelled():
                        raise InterruptedError('Paused before the next tool executed.')
                    result = self._execute(job, index, step, cancelled)
                    job['steps'][index].update(state='complete', result=result)
                    self._save(job)
                job['state'] = 'complete'
            except (InterruptedError, TimeoutError) as exc:
                job['state'], job['error'] = 'paused', str(exc)
            except Exception as exc:
                job['state'], job['error'] = 'failed', str(exc)
            self._save(job)
            return job
