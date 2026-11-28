"""Named passive workflows over the existing typed assistant plan.

A template stores no Scope, job, approval, output receipt, control request or
credential. Loading/review uses a freshly selected Scope and the existing
AssistantService validator. Submission is always a separate explicit action.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import re
import unicodedata
from uuid import uuid4

from .models import Plan, Scope, Step, SCHEMA as PLAN_SCHEMA, canonical, text
from .template_bindings import (_json_value, _parameters, _replace as bind_parameters,
                                explicit_rows, MAX_ITEMS)
from ..durable_state import (atomic_json, encode, local_path, read_json_snapshot,
                             regular, state_lock)

NAME = 'state/assistant/workflow-templates.json'
SCHEMA = 'carr-assistant-templates/1'
MAX_BYTES = 8 * 1024 * 1024
MAX_TEMPLATES = 256
MAX_STEPS = 10000
KINDS = ('workflow', 'batch-preset', 'keyboard-macro')
REQUEST_SCHEMA = 'carr-assistant-template-review/1'
AUDIO_TOOLS = ('audio.produce', 'audio.render')


def _copy(value):
    return json.loads(canonical(value))


def _id(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise ValueError('Invalid workflow template identity.')
    return value


def name_key(value):
    """Keep names distinct when native speech normalizes Unicode and spacing."""
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


def _title(value):
    text(value, 'workflow template name', maximum=160)
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Use a visible template name without control characters.')
    return value


def _project_arguments(arguments, *, fresh=False):
    """Drop old line/source claims and project identity; retain editable media references."""
    from ..studio.project import Project
    copied = _copy(arguments)
    if 'script' not in copied:
        return copied
    script = copied['script']
    if type(script) is not dict:
        raise ValueError('A reusable project must be typed project data.')
    script['id'], script['revision'] = (uuid4().hex if fresh else 'template'), 1
    if type(script.get('scenes')) is not list:
        raise ValueError('A reusable project needs a scene array.')
    for scene in script['scenes']:
        if type(scene) is not dict:
            raise ValueError('Invalid reusable project scene.')
        if type(scene.get('lines')) is not list:
            raise ValueError('A reusable scene needs a line array.')
        for line in scene['lines']:
            if type(line) is not dict:
                raise ValueError('Invalid reusable project line.')
            line['source_ref'] = ''
    project = Project.from_dict(script)
    copied['script'] = _copy(project.to_dict())
    return copied


def _safe_step(step, *, fresh=False):
    return Step(step.tool, _project_arguments(step.arguments, fresh=fresh)
                if step.tool in AUDIO_TOOLS and 'script' in step.arguments else step.arguments)


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    name: str
    kind: str
    summary: str
    steps: tuple
    parameters_json: str = '{}'

    def __post_init__(self):
        from .service import _check_arguments
        _id(self.id)
        _title(self.name)
        if self.kind not in KINDS:
            raise ValueError('Choose a workflow, batch preset or typed keyboard macro.')
        text(self.summary, 'workflow summary', maximum=16384, empty=True)
        steps = tuple(self.steps)
        if not 1 <= len(steps) <= MAX_STEPS or not all(isinstance(step, Step) for step in steps):
            raise ValueError('A template requires a bounded sequence of allowed assistant steps.')
        # Reuse the same typed argument policy as ordinary reviewed jobs.
        # This check is passive: no scope, input path, runtime or vault opens.
        if type(self.parameters_json) is not str or len(self.parameters_json.encode('utf-8')) > 256 * 1024:
            raise ValueError('Typed template parameters must be JSON text.')
        parameters = _parameters(_json_value(json.loads(self.parameters_json)))
        used, examples = set(), {name: spec['example'] for name, spec in parameters.items()}
        for step in steps:
            resolved = Step(step.tool, bind_parameters(step.arguments, parameters, examples, used))
            _check_arguments(resolved)
            # Passive Project parsing opens no cue paths or source references.
            _safe_step(resolved)
        if used != set(parameters):
            raise ValueError('Every declared parameter must be used in a typed argument.')
        object.__setattr__(self, 'steps', steps)
        object.__setattr__(self, 'parameters_json', canonical(parameters))
        if len(encode(self.to_dict())) > MAX_BYTES:
            raise ValueError('The workflow template exceeds its text budget.')

    @property
    def parameters(self):
        return json.loads(self.parameters_json)

    @property
    def command_id(self):
        return 'assistant.template.' + self.id

    @property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()

    def to_dict(self):
        value = {'id': self.id, 'name': self.name, 'kind': self.kind,
                 'summary': self.summary, 'steps': [step.to_dict() for step in self.steps]}
        # Legacy records retain their exact field shape and passive bytes.
        if self.parameters:
            value['parameters'] = self.parameters
        return value

    @classmethod
    def from_dict(cls, value):
        required = {'id', 'name', 'kind', 'summary', 'steps'}
        if type(value) is not dict or set(value) not in (required, required | {'parameters'}):
            raise ValueError('A workflow template may not contain saved permissions or job state.')
        if type(value['steps']) is not list or not 1 <= len(value['steps']) <= MAX_STEPS:
            raise ValueError('Workflow steps must be a bounded nonempty array.')
        steps = []
        for step in value['steps']:
            if (type(step) is not dict or set(step) != {'tool', 'arguments'}
                    or type(step['tool']) is not str):
                raise ValueError('Invalid typed template step.')
            steps.append(Step(step['tool'], step['arguments']))
        return cls(value['id'], value['name'], value['kind'], value['summary'], tuple(steps),
                   canonical(_parameters(_json_value(value.get('parameters', {})))))

    @classmethod
    def capture(cls, name, plan, *, kind='workflow', identity=None):
        if not isinstance(plan, Plan):
            raise TypeError('Save a reviewed typed plan as a template.')
        return cls(identity or uuid4().hex, name, kind, plan.summary,
                   tuple(_safe_step(step) for step in plan.steps))

    @classmethod
    def define(cls, name, steps, parameters=None, *, kind='workflow', summary='', identity=None):
        """Author fixed tools with explicitly declared whole-value bindings."""
        return cls.from_dict({'id': identity or uuid4().hex, 'name': name, 'kind': kind,
            'summary': summary, 'steps': steps, 'parameters': {} if parameters is None else parameters})

    @classmethod
    def from_project(cls, name, project, *, format='wav', stems=True, tool='audio.produce',
                     kind='workflow', identity=None):
        from ..studio.project import Project
        if not isinstance(project, Project) or tool not in AUDIO_TOOLS:
            raise TypeError('Choose a typed Studio project and existing audio operation.')
        return cls.define(name, [{'tool': tool, 'arguments': _project_arguments({
            'script': project.to_dict(), 'format': format, 'stems': stems})}],
            kind=kind, summary='New performance from reusable project settings.', identity=identity)

    def _expanded(self, values=None, items=None, *, maximum=MAX_STEPS):
        rows = explicit_rows(self.parameters, values, items)
        if len(rows) * len(self.steps) > maximum:
            raise ValueError('Expanded workflow exceeds the currently selected step budget.')
        steps, expanded_bytes = [], 0
        for row in rows:
            for step in self.steps:
                arguments = bind_parameters(step.arguments, self.parameters, row, set())
                current = _safe_step(Step(step.tool, arguments), fresh=True)
                expanded_bytes += len(canonical(current.to_dict()).encode('utf-8'))
                if expanded_bytes > MAX_BYTES:
                    raise ValueError('Expanded workflow exceeds its total text budget.')
                steps.append(current)
        return tuple(steps)

    def proposal(self, values=None, *, items=None):
        """Fresh scope-free operations for the existing editable instruction."""
        return {'schema': PLAN_SCHEMA, 'summary': self.summary,
                'steps': [step.to_dict() for step in self._expanded(values, items)]}

    def review(self, service, scope, values=None, *, items=None):
        """Fresh path/workspace/capability checks; never submit or execute."""
        return service.validate(instantiate_plan(self, scope, values, items=items))


def validate_catalog(value):
    """Pure validation for local load and typed backup; no saved path access."""
    if (type(value) is not dict or set(value) != {'schema', 'revision', 'templates'}
            or value['schema'] != SCHEMA or type(value['revision']) is not int
            or not 0 <= value['revision'] < 2**63
            or type(value['templates']) is not list
            or len(value['templates']) > MAX_TEMPLATES):
        raise ValueError('Invalid workflow template catalogue.')
    records = tuple(WorkflowTemplate.from_dict(row) for row in value['templates'])
    if (len({row.id for row in records}) != len(records)
            or len({name_key(row.name) for row in records}) != len(records)):
        raise ValueError('Workflow template names and identities must be unique.')
    if len(encode(value)) > MAX_BYTES:
        raise ValueError('The workflow template catalogue exceeds its storage budget.')
    return records


class TemplateStore:
    """One CAS-protected document in the existing assistant state directory."""
    def __init__(self, library_root):
        self.root = local_path(library_root, exists=True)
        info = regular(self.root, directory=True)
        self.identity = info.st_dev, info.st_ino
        self.path = self.root / NAME

    def _guard(self):
        info = regular(local_path(self.root, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('The assistant library location changed.')
        local_path(self.path, root=self.root)

    def _load(self):
        self._guard()
        if not os.path.lexists(self.path):
            return {'schema': SCHEMA, 'revision': 0, 'templates': []}, None
        value, token = read_json_snapshot(self.path, max_bytes=MAX_BYTES)
        validate_catalog(value)
        self._guard()
        return value, token

    def list(self):
        value, token = self._load()
        return validate_catalog(value), token

    def get(self, identity, *, expected_token=None):
        _id(identity)
        records, token = self.list()
        if expected_token is not None and token != expected_token:
            raise ValueError('The template catalogue changed. Select the current version again.')
        for record in records:
            if record.id == identity:
                return record, token
        raise ValueError('The selected workflow template is no longer available.')

    def _replace(self, mutate, expected_token):
        if expected_token is not None and (type(expected_token) is not str
                or re.fullmatch(r'[0-9a-f]{64}', expected_token) is None):
            raise ValueError('A current template snapshot is required.')
        self._guard()
        with state_lock(self.path.parent, 'workflow-templates'):
            self._guard()
            value, token = self._load()
            if token != expected_token:
                raise ValueError('Another window changed the templates. Reload before saving.')
            updated = mutate(_copy(value))
            updated['revision'] = value['revision'] + 1
            validate_catalog(updated)
            atomic_json(self.path, updated, replace=token is not None)
            self._guard()
        return sha256(encode(updated)).hexdigest()

    def save(self, record, *, expected_token):
        if not isinstance(record, WorkflowTemplate):
            raise TypeError('A validated workflow template is required.')
        captured = WorkflowTemplate.from_dict(record.to_dict())
        def mutate(value):
            rows = value['templates']
            found = next((index for index, row in enumerate(rows) if row['id'] == captured.id), None)
            if found is None:
                rows.append(captured.to_dict())
            else:
                rows[found] = captured.to_dict()
            return value
        return self._replace(mutate, expected_token)

    def delete(self, identity, *, expected_token):
        _id(identity)
        def mutate(value):
            if not any(row['id'] == identity for row in value['templates']):
                raise ValueError('The selected workflow template is no longer available.')
            value['templates'] = [row for row in value['templates'] if row['id'] != identity]
            return value
        return self._replace(mutate, expected_token)


def instantiate_plan(template, scope, values=None, *, items=None):
    if not isinstance(template, WorkflowTemplate) or not isinstance(scope, Scope):
        raise TypeError('Choose a typed template and freshly selected scope.')
    steps = template._expanded(values, items, maximum=scope.max_steps)
    from .service import _check_arguments
    for step in steps:
        _check_arguments(step)
        if step.tool in AUDIO_TOOLS and 'script' in step.arguments:
            from ..studio.project import Project
            project = Project.from_dict(step.arguments['script'])
            for scene in project.scenes:
                for cue in scene.cues:
                    scope.input(cue.path)
    return Plan(scope, steps, template.summary)


def review_request(template, values=None, *, items=None):
    """A passive proposal; every use still needs new Scope and service validation."""
    if not isinstance(template, WorkflowTemplate):
        raise TypeError('Choose a typed workflow template.')
    rows = explicit_rows(template.parameters, values, items)
    if len(rows) * len(template.steps) > MAX_STEPS:
        raise ValueError('Expanded workflow exceeds the template step budget.')
    # Fully validate actual values now; project IDs here are disposable review
    # data. The eventual fresh Plan receives its own distinct IDs.
    from .service import _check_arguments
    for step in template._expanded(values, items):
        _check_arguments(step)
    return {'schema': REQUEST_SCHEMA, 'template': template.to_dict(),
            'values': _json_value({} if values is None else values),
            'items': None if items is None else _json_value(items)}


def plan_from_request(value, scope):
    if (type(value) is not dict or set(value) != {'schema', 'template', 'values', 'items'}
            or value['schema'] != REQUEST_SCHEMA):
        raise ValueError('Unsupported template review request.')
    return instantiate_plan(WorkflowTemplate.from_dict(value['template']), scope,
                            value['values'], items=value['items'])
