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
from uuid import uuid4

from .models import Plan, Step, SCHEMA as PLAN_SCHEMA, canonical, text
from ..durable_state import (atomic_json, encode, local_path, read_json_snapshot,
                             regular, state_lock)

NAME = 'state/assistant/workflow-templates.json'
SCHEMA = 'carr-assistant-templates/1'
MAX_BYTES = 8 * 1024 * 1024
MAX_TEMPLATES = 256
MAX_STEPS = 10000
KINDS = ('workflow', 'batch-preset', 'keyboard-macro')


def _copy(value):
    return json.loads(canonical(value))


def _id(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise ValueError('Invalid workflow template identity.')
    return value


def _title(value):
    text(value, 'workflow template name', maximum=160)
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Use a visible template name without control characters.')
    return value


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    name: str
    kind: str
    summary: str
    steps: tuple

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
        for step in steps:
            _check_arguments(step)
        object.__setattr__(self, 'steps', steps)
        if len(encode(self.to_dict())) > MAX_BYTES:
            raise ValueError('The workflow template exceeds its text budget.')

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'kind': self.kind,
                'summary': self.summary, 'steps': [step.to_dict() for step in self.steps]}

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {'id', 'name', 'kind', 'summary', 'steps'}:
            raise ValueError('A workflow template may not contain saved permissions or job state.')
        if type(value['steps']) is not list:
            raise ValueError('Workflow steps must be an array.')
        steps = []
        for step in value['steps']:
            if type(step) is not dict or set(step) != {'tool', 'arguments'}:
                raise ValueError('Invalid typed template step.')
            steps.append(Step(step['tool'], step['arguments']))
        return cls(value['id'], value['name'], value['kind'], value['summary'], tuple(steps))

    @classmethod
    def capture(cls, name, plan, *, kind='workflow', identity=None):
        if not isinstance(plan, Plan):
            raise TypeError('Save a reviewed typed plan as a template.')
        return cls(identity or uuid4().hex, name, kind, plan.summary,
                   tuple(Step(step.tool, step.arguments) for step in plan.steps))

    def proposal(self):
        """Scope-free JSON for the existing editable instruction control."""
        return {'schema': PLAN_SCHEMA, 'summary': self.summary,
                'steps': [step.to_dict() for step in self.steps]}

    def review(self, service, scope):
        """Fresh path/workspace/capability checks; never submit or execute."""
        plan = Plan.from_dict(self.proposal(), scope=scope)
        return service.validate(plan)


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
            or len({row.name.casefold() for row in records}) != len(records)):
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
