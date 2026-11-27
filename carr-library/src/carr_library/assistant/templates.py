"""Passive named recipes over the existing plan, project and batch contracts.

Templates are data, never execution authority. A keyboard macro opens the same
review as a selected template. Scope, jobs, approval receipts, completion and
credentials are never stored in a recipe or activated by restore.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import os
import re
from uuid import uuid4

from ..durable_state import atomic_json, encode, local_path, read_json, regular, state_lock
from .models import Plan, Scope, Step, canonical, text
from .service import _check_arguments

SCHEMA = 'carr-workflow-template/1'
MAX_TEMPLATE_BYTES = 256 * 1024
MAX_TEMPLATES = 64
MAX_STEPS = 32
MAX_ITEMS = 128
_ID = re.compile(r'[0-9a-f]{32}\Z')
_PARAMETER = re.compile(r'[a-z][a-z0-9_]{0,47}\Z')
KINDS = ('assistant', 'project', 'document-batch')
PARAMETER_KINDS = ('text', 'integer', 'number', 'boolean', 'path', 'voice')
FORMAT_NAMES = ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma')


def _label(value):
    text(value, 'template name', maximum=120)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('A template name must be a single line.')
    return value


def _json_value(value):
    """Reject deep/opaque inputs before serialization; never consume iterators."""
    remaining = [12000]
    def visit(item, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 20:
            raise ValueError('Template data exceeds its bounded structure allowance.')
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if abs(item) > 2**53:
                raise ValueError('A template integer exceeds its exact JSON range.')
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError('Template numbers must be finite.')
            return
        if type(item) is str:
            text(item, 'template text', maximum=MAX_TEMPLATE_BYTES, empty=True)
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or len(key) > 128:
                    raise ValueError('Template fields need bounded text names.')
                visit(child, depth + 1)
            return
        raise ValueError('Templates contain plain JSON data only.')
    visit(value, 0)
    encoded = canonical(value)
    if len(encoded.encode('utf-8')) > MAX_TEMPLATE_BYTES:
        raise ValueError('The template exceeds its 256 KiB allowance.')
    return json.loads(encoded)


def _parameter_value(kind, value):
    if kind in ('text', 'path', 'voice'):
        text(value, 'parameter value', maximum=65536 if kind == 'text' else 8192,
             empty=kind == 'text')
    elif kind == 'integer':
        if type(value) is not int or abs(value) > 2**31 - 1:
            raise ValueError('An integer parameter must be within the signed 32-bit range.')
    elif kind == 'number':
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 10**12:
            raise ValueError('A numeric parameter must be finite and bounded.')
    elif kind == 'boolean':
        if type(value) is not bool:
            raise ValueError('A boolean parameter must be true or false.')
    else:
        raise ValueError('Unsupported template parameter type.')
    return value


def _parameters(value):
    if type(value) is not dict or len(value) > 64:
        raise ValueError('Use at most 64 named typed parameters.')
    for name, spec in value.items():
        if (not _PARAMETER.fullmatch(name) or type(spec) is not dict
                or set(spec) != {'kind', 'label', 'example'}):
            raise ValueError('Each parameter needs a stable name, kind, label and example.')
        _label(spec['label'])
        _parameter_value(spec['kind'], spec['example'])
    return value


def _replace(value, parameters, values, used):
    if type(value) is dict:
        if '$parameter' in value:
            if (set(value) != {'$parameter'} or type(value['$parameter']) is not str
                    or value['$parameter'] not in parameters):
                raise ValueError('A parameter reference must name exactly one declared parameter.')
            name = value['$parameter']
            used.add(name)
            return _parameter_value(parameters[name]['kind'], values[name])
        return {key: _replace(item, parameters, values, used) for key, item in value.items()}
    if type(value) is list:
        return [_replace(item, parameters, values, used) for item in value]
    return value


def _project(value, *, identity='template'):
    from ..studio.project import Project
    if type(value) is not dict or {'id', 'revision'} & set(value):
        raise ValueError('A project template cannot retain a project identity or revision.')
    project = Project.from_dict({**value, 'id': identity, 'revision': 1})
    if any(line.source_ref for scene in project.scenes for line in scene.lines):
        raise ValueError('A reusable project cannot carry old source references into new text.')
    return project


def batch_settings(value):
    """Pure validation shared by presets, UI, CLI and passive backups."""
    from ..conversion.planning import SplitOptions, validate_template
    required = {'options', 'template', 'combined', 'action', 'voice_id', 'language', 'format'}
    if type(value) is not dict or set(value) != required or type(value['options']) is not dict:
        raise ValueError('A batch preset contains split and output settings only.')
    try:
        options = SplitOptions(**value['options'])
    except TypeError as exc:
        raise ValueError('Unknown batch split option.') from exc
    validate_template(value['template'])
    if type(value['combined']) is not bool or value['action'] not in ('text', 'studio'):
        raise ValueError('Choose a supported batch grouping and action.')
    if value['format'] not in FORMAT_NAMES:
        raise ValueError('Choose an existing Studio output format.')
    text(value['voice_id'], 'voice identity', maximum=512, empty=True)
    text(value['language'], 'explicit language', maximum=80, empty=True)
    return {**value, 'options': asdict(options)}


def _checked_definition(kind, value):
    if kind == 'assistant':
        if type(value) is not dict or set(value) != {'steps'}:
            raise ValueError('An assistant template contains typed steps, without a saved scope.')
        if type(value['steps']) is not list or not 1 <= len(value['steps']) <= MAX_STEPS:
            raise ValueError('Use from 1 to 32 typed steps in a workflow template.')
        for raw in value['steps']:
            if type(raw) is not dict or set(raw) != {'tool', 'arguments'}:
                raise ValueError('Each template step needs a fixed tool and typed arguments.')
            step = Step(raw['tool'], raw['arguments'])
            _check_arguments(step)
            if step.tool == 'audio.produce' and 'script' in step.arguments:
                from ..studio.project import Project
                project = Project.from_dict(step.arguments['script'])
                if any(line.source_ref for scene in project.scenes for line in scene.lines):
                    raise ValueError('Reusable audio steps cannot copy old source references.')
        return value
    if kind == 'project':
        if type(value) is not dict or set(value) != {'project', 'format', 'stems'}:
            raise ValueError('A project template needs project data, format and stems.')
        _project(value['project'])
        if value['format'] not in FORMAT_NAMES or type(value['stems']) is not bool:
            raise ValueError('Invalid project output settings.')
        return value
    if kind == 'document-batch':
        return batch_settings(value)
    raise ValueError('Unsupported workflow template kind.')


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    name: str
    kind: str
    definition_json: str
    parameters_json: str = '{}'
    revision: int = 1

    def __post_init__(self):
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise ValueError('Invalid workflow template identity.')
        _label(self.name)
        if type(self.revision) is not int or not 1 <= self.revision < 2**31 or self.kind not in KINDS:
            raise ValueError('Unsupported template kind or revision.')
        for raw in (self.definition_json, self.parameters_json):
            if type(raw) is not str or len(raw.encode('utf-8')) > MAX_TEMPLATE_BYTES:
                raise ValueError('Template JSON exceeds its bounded allowance.')
        definition = _json_value(json.loads(self.definition_json))
        if type(definition) is not dict:
            raise ValueError('A template definition must be a JSON object.')
        parameters = _parameters(_json_value(json.loads(self.parameters_json)))
        used = set()
        examples = {name: spec['example'] for name, spec in parameters.items()}
        resolved = _replace(definition, parameters, examples, used)
        if used != set(parameters):
            raise ValueError('Every declared template parameter must be used.')
        # Tool names are fixed data, never parameterized dispatch.
        if self.kind == 'assistant' and any(type(step.get('tool')) is not str
                for step in definition.get('steps', []) if isinstance(step, dict)):
            raise ValueError('Template tool names cannot be parameterized.')
        _checked_definition(self.kind, resolved)
        object.__setattr__(self, 'definition_json', canonical(definition))
        object.__setattr__(self, 'parameters_json', canonical(parameters))
        if len(encode(self.to_dict())) > MAX_TEMPLATE_BYTES:
            raise ValueError('The template record exceeds its 256 KiB allowance.')

    @property
    def definition(self):
        return json.loads(self.definition_json)

    @property
    def parameters(self):
        return json.loads(self.parameters_json)

    @property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()

    @property
    def command_id(self):
        return 'workflow.template.' + self.id

    def to_dict(self):
        return {'schema': SCHEMA, 'id': self.id, 'name': self.name, 'kind': self.kind,
                'definition': self.definition, 'parameters': self.parameters, 'revision': self.revision}

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) !=
                {'schema', 'id', 'name', 'kind', 'definition', 'parameters', 'revision'}
                or value['schema'] != SCHEMA):
            raise ValueError('Unsupported workflow template fields or schema.')
        checked = _json_value(value)
        return cls(checked['id'], checked['name'], checked['kind'],
                   canonical(checked['definition']), canonical(checked['parameters']), checked['revision'])

    def resolve(self, values):
        selected = _json_value(values)
        parameters = self.parameters
        if type(selected) is not dict or set(selected) != set(parameters):
            raise ValueError('Supply every declared parameter explicitly, without extra fields.')
        return _checked_definition(self.kind, _replace(self.definition, parameters, selected, set()))


def definition_from_plan(plan):
    """Copy typed operations only; saved permissions and execution state are discarded."""
    if not isinstance(plan, Plan):
        raise TypeError('Select a typed, reviewed assistant plan.')
    steps = json.loads(canonical([step.to_dict() for step in plan.steps]))
    for step in steps:
        if step['tool'] == 'audio.produce' and 'script' in step['arguments']:
            script = step['arguments']['script']
            for scene in script.get('scenes', []):
                for line in scene.get('lines', []):
                    line['source_ref'] = ''
            script['id'], script['revision'] = 'template', 1
    return _checked_definition('assistant', {'steps': steps})


def definition_from_project(project, *, format='wav', stems=True):
    from ..studio.project import Project
    if not isinstance(project, Project):
        raise TypeError('Select a typed Studio project.')
    value = json.loads(canonical(project.to_dict()))
    value.pop('id'); value.pop('revision')
    for scene in value['scenes']:
        for line in scene['lines']:
            line['source_ref'] = ''
    return _checked_definition('project', {'project': value, 'format': format, 'stems': stems})


def instantiate_plan(template, scope, values, *, items=None):
    """Build a NEW unsubmitted plan; each item explicitly supplies its overrides.

    There is no interpolation, evaluation, key recording, permission inheritance,
    hidden output path, execution, model request or ambient source discovery.
    """
    if not isinstance(template, WorkflowTemplate) or not isinstance(scope, Scope):
        raise TypeError('A typed template and freshly selected scope are required.')
    if template.kind == 'document-batch':
        raise ValueError('Open this preset in the existing document-batch review.')
    base = _json_value(values)
    if type(base) is not dict:
        raise ValueError('Template bindings must be a JSON object.')
    rows = [{}] if items is None else _json_value(items)
    if type(rows) is not list or not 1 <= len(rows) <= MAX_ITEMS or any(type(row) is not dict for row in rows):
        raise ValueError('Use from 1 to 128 explicit per-item parameter objects.')
    per_row = len(template.definition['steps']) if template.kind == 'assistant' else 1
    if len(rows) * per_row > scope.max_steps:
        raise ValueError('Expanded workflow exceeds the currently selected step budget.')
    steps = []
    for row in rows:
        resolved = template.resolve({**base, **row})
        if template.kind == 'project':
            project = _project(resolved['project'], identity=uuid4().hex)
            raw = [{'tool': 'audio.produce', 'arguments': {
                'script': json.loads(canonical(project.to_dict())), 'format': resolved['format'],
                'stems': resolved['stems']}}]
        else:
            raw = resolved['steps']
        for item in raw:
            args = json.loads(canonical(item['arguments']))
            if item['tool'] == 'audio.produce' and 'script' in args:
                from ..studio.project import Project
                args['script']['id'], args['script']['revision'] = uuid4().hex, 1
                project = Project.from_dict(args['script'])
                # Reusable clip paths receive the new input scope, never old approval.
                for scene in project.scenes:
                    for cue in scene.cues:
                        scope.input(cue.path)
            steps.append(Step(item['tool'], args))
    return Plan(scope, tuple(steps), f'New review from {template.name} (revision {template.revision}); '
                f'{len(rows)} explicitly supplied item(s). Nothing has run.')


class TemplateStore:
    """One passive producer; ordinary domain services remain execution owners."""
    def __init__(self, library_root):
        self.library_root = local_path(library_root, exists=True)
        self.root = local_path(self.library_root / 'state' / 'workflows' / 'templates',
                               root=self.library_root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        local_path(self.root, exists=True)
        self.warning = ''

    def _path(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('Choose a saved template identity.')
        return local_path(self.root / (identity + '.json'), root=self.root)

    def get(self, identity):
        path = self._path(identity)
        before = regular(path)
        value = WorkflowTemplate.from_dict(read_json(path, maximum=MAX_TEMPLATE_BYTES))
        after = regular(path)
        def stamp(info):
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        if stamp(before) != stamp(after) or value.id != identity:
            raise ValueError('Template identity changed while reading. Reload it.')
        return value

    def list(self):
        result = []
        with os.scandir(local_path(self.root, exists=True)) as entries:
            for scanned, entry in enumerate(entries, 1):
                if scanned > 256:
                    raise ValueError('Template inventory exceeds its bounded allowance.')
                if not entry.name.endswith('.json'):
                    continue
                if len(result) >= MAX_TEMPLATES:
                    raise ValueError('The template collection exceeds 64 records.')
                result.append(self.get(entry.name[:-5]))
        return tuple(sorted(result, key=lambda item: (item.name.casefold(), item.id)))

    def save(self, name, kind, definition, parameters=None, *, previous=None):
        self.warning = ''
        if previous is not None and not isinstance(previous, WorkflowTemplate):
            raise ValueError('Review the exact saved template before replacing it.')
        value = WorkflowTemplate(previous.id if previous else uuid4().hex, name, kind,
            canonical(_json_value(definition)), canonical(_json_value({} if parameters is None else parameters)),
            previous.revision + 1 if previous else 1)
        attempted = False
        try:
            with state_lock(self.root, 'workflow-templates'):
                path = self._path(value.id)
                inventory = self.list()
                if any(item.id != value.id and item.name.casefold() == value.name.casefold() for item in inventory):
                    raise ValueError('A workflow with this name already exists. Choose a distinct name.')
                if previous is not None:
                    if self.get(previous.id).fingerprint != previous.fingerprint:
                        raise ValueError('Template changed after review. Reload before saving.')
                elif os.path.lexists(path):
                    raise FileExistsError('The new workflow identity is occupied.')
                elif len(inventory) >= MAX_TEMPLATES:
                    raise ValueError('The template collection has reached 64 records.')
                attempted = True
                atomic_json(path, value.to_dict(), replace=previous is not None)
        except (OSError, RuntimeError, KeyboardInterrupt) as exc:
            if not attempted:
                raise
            try:
                observed = self.get(value.id)
            except (OSError, ValueError, RuntimeError):
                raise exc
            if observed.fingerprint != value.fingerprint:
                raise
            self.warning = ('The exact template is present; save completion could not be confirmed. '
                            'No workflow was run. ' + str(exc)[:1000])
        return value


REQUEST_SCHEMA = 'carr-workflow-review/1'


def review_request(template, values, *, items=None):
    """Portable request data with no inherited input/output scope or approval."""
    if not isinstance(template, WorkflowTemplate):
        raise TypeError('Select a typed workflow template.')
    selected, rows = _json_value(values), None if items is None else _json_value(items)
    if type(selected) is not dict or (rows is not None and
            (type(rows) is not list or not 1 <= len(rows) <= MAX_ITEMS
             or any(type(row) is not dict for row in rows))):
        raise ValueError('Choose explicit parameter bindings and bounded item overrides.')
    for row in [{}] if rows is None else rows:
        template.resolve({**selected, **row})
    return {'schema': REQUEST_SCHEMA, 'template': template.to_dict(), 'values': selected, 'items': rows}


def plan_from_request(value, scope):
    if (type(value) is not dict or set(value) != {'schema', 'template', 'values', 'items'}
            or value['schema'] != REQUEST_SCHEMA):
        raise ValueError('Unsupported workflow review request.')
    return instantiate_plan(WorkflowTemplate.from_dict(value['template']), scope,
                            value['values'], items=value['items'])
