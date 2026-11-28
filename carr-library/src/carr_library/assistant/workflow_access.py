"""Named access to existing typed assistant jobs; no additional execution store."""
from dataclasses import dataclass, replace

from .models import Plan, text


@dataclass(frozen=True)
class SavedWorkflow:
    identity: str
    name: str
    digest: str
    status: str
    step_count: int
    restored: bool

    @property
    def label(self):
        return (f'{self.name}; {self.step_count} typed operations; {self.status}'
                + ('; restored — fresh review required' if self.restored else '')
                + '; ' + self.identity)


def workflow_name(value):
    value = text(value, 'workflow name', maximum=200).strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('Use one line for the workflow name.')
    return value


class WorkflowAccess:
    """Save and reuse recipes through the existing AssistantService boundaries.

    A recipe contains steps and a name, never saved filesystem permissions,
    completed-step results, control commands or a pre-authorized execution flag.
    """

    def __init__(self, service):
        self.service = service

    def entries(self, *, cancelled=lambda: False):
        rows = []
        for identity in self.service.store.identities():
            if cancelled():
                raise InterruptedError('Saved workflow review was cancelled.')
            record, plan = self.service.review_saved_plan(identity)
            name = ' '.join(plan.summary.split())[:200] or 'Unnamed saved plan'
            rows.append(SavedWorkflow(identity, name, record['plan_sha256'],
                record['status'], len(plan.steps), record.get('restore_requires_review', False)))
        if cancelled():
            raise InterruptedError('Saved workflow review was cancelled.')
        return tuple(rows)

    def recipe(self, selection):
        if type(selection) is not SavedWorkflow:
            raise ValueError('Choose a saved typed workflow.')
        record, plan = self.service.review_saved_plan(selection.identity)
        if record['plan_sha256'] != selection.digest:
            raise ValueError('The saved workflow changed. Choose and review it again.')
        # Omit the whole historical scope. Plan.from_dict requires the caller's
        # newly selected host scope before these steps can become executable.
        return {'schema': plan.schema, 'summary': plan.summary,
                'steps': [step.to_dict() for step in plan.steps]}

    def save(self, plan, name):
        if not isinstance(plan, Plan):
            raise ValueError('Preview a typed plan before saving a workflow.')
        named = replace(plan, summary=workflow_name(name))
        # Submit persists a pending typed job only; no tool is invoked here.
        return self.service.submit(named)
