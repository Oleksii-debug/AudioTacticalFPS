"""Bounded recovery views over canonical journals, without another job queue.

Stored status is reported as stored status. Listing never resumes a worker,
contacts a provider, loads a model, or upgrades a saved receipt to verified audio.
"""
from dataclasses import dataclass, field, replace
from hashlib import sha256
import math
import os
from pathlib import Path
import re

from ..durable_state import local_path, read_json, regular
from .models import ResourceStateError


_JOB_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}')
_STATES = {'pending', 'queued', 'preparing', 'running', 'pausing', 'cancelling',
           'paused', 'cancelled', 'interrupted', 'partial', 'attention', 'failed', 'complete',
           'ready', 'completed', 'stopped'}
_TERMINAL = {'complete', 'completed', 'cancelled'}
_MAX_CHECKPOINT = 32 * 1024 * 1024
_MAX_DISCOVERY = 10000
_MAX_DETAIL_ARTIFACTS = 64
_STUDIO_SUMMARY_FIELDS = {'schema', 'id', 'status', 'nodes', 'progress',
                          'created', 'updated', 'error'}
_UNKNOWN_DETAILS = ('Detailed metadata is unavailable for this checkpoint. '
                    'Unknown values do not authorize execution or output reuse.')
_LOCATIONS = {
    'studio': ('state/studio/jobs', '*/job.json'),
    'assistant': ('state/assistant', '*.json'),
    'import': ('import-batches', '*.json'),
    'translation': ('state/translation/jobs', '*.json'),
    'asr': ('state/transcription/jobs', '*/job.json'),
    'asr-receipts': ('state/transcription', '*/jobs/*/job.json'),
}


def _receipt_paths(base):
    """Bound every discovered scope/job entry, including empty unknown folders."""
    inspected = 0
    with os.scandir(base) as scopes:
        for scope in scopes:
            inspected += 1
            if inspected > _MAX_DISCOVERY:
                raise ResourceStateError('Receipt operation inventory exceeds its discovery limit.')
            if not re.fullmatch(r'[0-9a-f]{64}', scope.name):
                continue
            directory = local_path(scope.path, root=base, exists=True)
            regular(directory, directory=True)
            jobs = directory / 'jobs'
            try:
                jobs.lstat()
            except FileNotFoundError:
                continue
            regular(local_path(jobs, root=base, exists=True), directory=True)
            with os.scandir(jobs) as children:
                for child in children:
                    inspected += 1
                    if inspected > _MAX_DISCOVERY:
                        raise ResourceStateError('Receipt operation inventory exceeds its discovery limit.')
                    if not re.fullmatch(r'[0-9a-f]{32}', child.name):
                        continue
                    job = local_path(child.path, root=base, exists=True)
                    regular(job, directory=True)
                    yield job / 'job.json'


@dataclass(frozen=True)
class ResourcePlanView:
    cpu_slots: int
    memory_bytes: int
    gpu_requested: bool
    gpu_memory_bytes: int
    disk_reservation_bytes: tuple
    exclusive_device_count: int
    source_count: int | None
    estimated_duration_seconds: float | None


@dataclass(frozen=True)
class ArtifactIdentityView:
    ordinal: int
    sha256: str
    size: int


@dataclass(frozen=True)
class OperationDetails:
    handler_schema: str | None = None
    adapter: str | None = None
    operation_plan_sha256: str | None = None
    settings_sha256: str | None = None
    project_fingerprint: str | None = None
    resource_plan: ResourcePlanView | None = None
    completed_artifacts: tuple = ()
    completed_artifact_count: int | None = None
    omitted_artifact_count: int | None = None
    restore_requires_new_output: bool | None = None
    note: str = _UNKNOWN_DETAILS


def _studio_details(data, checkpoint):
    """Project typed stored declarations without opening their referenced paths."""
    if type(data.get('schema')) is not int:
        raise ResourceStateError('Invalid production checkpoint schema.')
    # Historical dashboard-only summaries carry no canonical detail envelope.
    # A partially present canonical envelope must not fall back to this case.
    if set(data) <= _STUDIO_SUMMARY_FIELDS:
        return OperationDetails()
    from ..backup_producers import validate
    from ..studio.render import ENGINE_SCHEMA
    try:
        validate(None, checkpoint, data)
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        raise ResourceStateError(
            'Saved production details failed their canonical checks; open Studio to review.') from None

    plan = data.get('resource_plan')
    resources = None
    if plan is not None:
        request = plan['request']
        disks = ((request['disk_bytes'],) if request['disk_path'] is not None else ())
        disks += tuple(size for _, size in request['additional_disks'])
        resources = ResourcePlanView(request['cpu_slots'], request['memory_bytes'],
            request['gpu_device'] is not None, request['gpu_memory_bytes'], disks,
            len(request['devices']), len(plan['source_sha256']) if 'source_sha256' in plan else None,
            plan.get('estimated_duration'))
    identities, count, omitted = (), None, None
    if data['status'] == 'complete':
        rows = data['artifacts']
        count = len(rows)
        identities = tuple(ArtifactIdentityView(index + 1, row['sha256'], row['size'])
            for index, row in enumerate(rows[:_MAX_DETAIL_ARTIFACTS]))
        omitted = count - len(identities)
    note = ('The handler identifies the current canonical production reader. '
            'Other details are stored declarations; artifact bytes and current assets were not checked. '
            'Resource budgets are declarations, not measured use. '
            'No selected backend identity or separate settings digest is recorded.')
    if 'operation_sha256' not in data:
        note += ' This historical record has no saved operation-plan digest.'
    if data.get('restore_requires_new_output'):
        note += ' Its restore marker requires review of a new output in Studio.'
    return OperationDetails(handler_schema=ENGINE_SCHEMA,
        operation_plan_sha256=data.get('operation_sha256'), project_fingerprint=data['fingerprint'],
        resource_plan=resources, completed_artifacts=identities,
        completed_artifact_count=count, omitted_artifact_count=omitted,
        restore_requires_new_output=data.get('restore_requires_new_output'), note=note)


@dataclass(frozen=True)
class OperationView:
    kind: str
    id: str
    workspace: str | None
    status: str
    completed: int | None
    total: int | None
    phase: str
    updated: str
    controls: tuple
    checkpoint: str
    live: bool | None = None
    details: OperationDetails = field(default_factory=OperationDetails)

    @property
    def key(self):
        return self.kind + ':' + self.id


class OperationFacade:
    def __init__(self, app, *, broker=None, studio=None, assistant=None, transcription=None, translation=None):
        self.app = app
        self.root = local_path(app.root, exists=True)
        self.broker = broker
        self.services = {'studio': studio, 'assistant': assistant, 'asr': transcription,
                         'translation': translation}

    def _path(self, kind, identity):
        if kind not in _LOCATIONS or not isinstance(identity, str) or not _JOB_ID.fullmatch(identity):
            raise ValueError('Choose a known operation and its existing job identity.')
        if kind == 'asr' and not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise ValueError('Choose the exact local-file job identity.')
        if kind == 'asr-receipts':
            if not re.fullmatch(r'[0-9a-f]{64}\.[0-9a-f]{32}', identity):
                raise ValueError('Choose the exact workspace-bound receipt operation identity.')
            scope, job = identity.split('.')
            return local_path(self.root / 'state/transcription' / scope / 'jobs' / job / 'job.json',
                              root=self.root)
        directory, pattern = _LOCATIONS[kind]
        path = self.root / directory / (identity + '.json' if pattern == '*.json' else identity + '/job.json')
        return local_path(path, root=self.root)

    def _controls(self, kind, status):
        service = self.services.get(kind)
        if service is None or status in _TERMINAL:
            return ()
        values = []
        pause = 'request_pause' if kind == 'studio' else 'pause'
        if callable(getattr(service, pause, None)):
            values.append('pause')
        if callable(getattr(service, 'cancel', None)):
            values.append('cancel')
        return tuple(values)

    def _read_view(self, kind, identity):
        path = self._path(kind, identity)
        if regular(path).st_size > _MAX_CHECKPOINT:
            raise ResourceStateError('Operation checkpoint exceeds the bounded dashboard reader.')
        data = read_json(path)
        if not isinstance(data, dict):
            raise ResourceStateError('Operation checkpoint is not an object.')
        expected = {'studio': 1, 'assistant': 'carr-assistant-job/1',
                    'translation': 'carr-assistant-job/1', 'asr': 'resonance-transcription-job/1'}
        if kind == 'asr-receipts':
            from ..transcription_contract import validate_job_document
            scope, job = identity.split('.')
            validate_job_document(data, identity=job)
            if sha256(data['workspace'].encode('utf-8')).hexdigest() != scope:
                raise ResourceStateError('Receipt operation belongs to another workspace.')
            self._authorize(data['workspace'])
        elif kind == 'import':
            if data.get('version') != 1 or data.get('batch_id') != identity or data.get('data_dir') != str(self.root):
                raise ResourceStateError('Import checkpoint identity or data directory changed.')
        elif data.get('schema') != expected[kind] or data.get('id') != identity:
            raise ResourceStateError('Operation checkpoint schema or identity changed.')
        if kind == 'assistant' and data.get('data_dir') != str(self.root):
            raise ResourceStateError('Assistant checkpoint belongs to another data directory.')
        if kind == 'translation' and data.get('kind') != 'translation/1':
            raise ResourceStateError('Unsupported translation checkpoint.')
        status = data.get('state') if kind == 'import' else data.get('status')
        if not isinstance(status, str) or status not in _STATES:
            raise ResourceStateError('Unknown saved operation state; inspect its canonical service.')
        workspace = data.get('workspace') if kind in ('assistant', 'import', 'asr-receipts') else None
        if workspace is not None and (not isinstance(workspace, str) or not workspace or len(workspace) > 200):
            raise ResourceStateError('Invalid saved operation workspace.')
        completed = total = None
        details = OperationDetails()
        if kind == 'studio':
            if not isinstance(data.get('nodes'), dict):
                raise ResourceStateError('Invalid production checkpoint nodes.')
            details = _studio_details(data, path.relative_to(self.root).as_posix())
            completed = len(data['nodes'])
            phase = 'Stored production checkpoint; accepted nodes'
        elif kind == 'assistant':
            steps = data.get('steps')
            if (not isinstance(steps, list) or any(not isinstance(row, dict)
                    or row.get('status') not in ('pending', 'running', 'complete', 'failed') for row in steps)):
                raise ResourceStateError('Invalid assistant checkpoint steps.')
            completed, total = sum(row['status'] == 'complete' for row in steps), len(steps)
            phase = 'Stored assistant checkpoint; completed steps'
        elif kind == 'translation':
            if not isinstance(data.get('receipts'), list) or not isinstance(data.get('chunks'), list):
                raise ResourceStateError('Invalid translation checkpoint chunks.')
            completed, total = len(data['receipts']), len(data['chunks'])
            phase = 'Stored translation checkpoint; accepted chunks'
        elif kind == 'import':
            counts = data.get('counts')
            if not isinstance(counts, dict) or any(type(value) is not int or value < 0 for value in counts.values()):
                raise ResourceStateError('Invalid import checkpoint counts.')
            completed = sum(counts.get(key, 0) for key in ('complete', 'duplicate', 'excluded'))
            total = data.get('total')
            if type(total) is not int or sum(counts.values()) != total:
                raise ResourceStateError('Import checkpoint counts differ from the recorded total.')
            phase = 'Stored import checkpoint; accepted or excluded files'
        elif kind == 'asr-receipts':
            completed, total = data['done'], len(data['sources'])
            phase = 'Stored strict-receipt transcription checkpoint; verified input files'
        else:
            completed, total = data.get('done'), data.get('total')
            phase = 'Stored transcription checkpoint; completed input files'
        if ((completed is not None and (type(completed) is not int or completed < 0))
                or (total is not None and (type(total) is not int or total < 0
                    or completed is not None and completed > total))):
            raise ResourceStateError('Invalid operation progress counts.')
        updated = data.get('updated', data.get('created', ''))
        if type(updated) in (int, float):
            if not math.isfinite(updated) or updated < 0:
                raise ResourceStateError('Invalid operation update time.')
            updated = str(updated)
        if not isinstance(updated, str) or len(updated) > 128 or any(ord(char) < 32 for char in updated):
            raise ResourceStateError('Invalid operation update time.')
        return OperationView(kind, identity, workspace, status, completed, total, phase, updated,
                             self._controls(kind, status), path.relative_to(self.root).as_posix(),
                             details=details)

    def _live(self, values):
        if self.broker is None:
            return tuple(values)
        active = {row['operation_id'] for row in self.broker.snapshot()['leases']}
        # Legacy and non-heavy operations need not participate in this broker.
        # Absence is unknown, never proof that their process died.
        return tuple(replace(value, live=True) if value.key in active else value for value in values)

    def _authorize(self, workspace):
        if workspace is not None:
            with self.app.read(workspace):
                pass

    def list(self, *, workspace=None, limit=1000):
        self._authorize(workspace)
        if type(limit) is not int or not 1 <= limit <= _MAX_DISCOVERY:
            raise ValueError('Choose an operation-view limit between 1 and 10000.')
        values, inspected = [], 0
        for kind, (directory, pattern) in _LOCATIONS.items():
            base = self.root / directory
            if not os.path.lexists(base):
                continue
            local_path(base, root=self.root, exists=True)
            regular(base, directory=True)
            paths = _receipt_paths(base) if kind == 'asr-receipts' else base.glob(pattern)
            for path in paths:
                identity = path.stem if pattern == '*.json' else path.parent.name
                if kind == 'asr-receipts':
                    scope = path.parents[2].name
                    if not re.fullmatch(r'[0-9a-f]{64}', scope):
                        continue
                    identity = scope + '.' + identity
                # Control files and unrelated service settings are not jobs.
                if pattern == '*.json' and not re.fullmatch(r'[0-9a-f]{32}', identity):
                    continue
                inspected += 1
                if inspected > _MAX_DISCOVERY:
                    raise ResourceStateError('Operation inventory exceeds its bounded discovery limit.')
                try:
                    value = self._read_view(kind, identity)
                except (ValueError, OSError, ResourceStateError):
                    # Do not print exception bodies, original text, provider
                    # profiles or file contents in a shared recovery dashboard.
                    value = OperationView(kind, identity, None, 'attention', None, None,
                        'Checkpoint could not be read; inspect the original service for recovery.', '', (),
                        path.relative_to(self.root).as_posix())
                if workspace is None or value.workspace in (None, workspace):
                    values.append(value)
        return self._live(sorted(values, key=lambda value: (value.kind, value.id))[:limit])

    def get(self, kind, job_id, *, workspace=None):
        self._authorize(workspace)
        value = self._read_view(kind, job_id)
        if workspace is not None and value.workspace not in (None, workspace):
            raise PermissionError('This saved operation belongs to another selected library.')
        return self._live((value,))[0]

    def control(self, kind, job_id, action, *, workspace=None):
        if action not in ('pause', 'cancel'):
            raise ValueError('The shared view supports only explicit pause/cancel requests; resume uses the original workflow.')
        value = self.get(kind, job_id, workspace=workspace)
        if action not in value.controls:
            raise ValueError('This operation does not expose that control through its current canonical service.')
        service = self.services[kind]
        method = 'request_pause' if action == 'pause' and kind == 'studio' else action
        getattr(service, method)(job_id)
        # A requested stop is not a completed stop. The owning service updates
        # its canonical checkpoint after the worker acknowledges cancellation.
        return self.get(kind, job_id, workspace=workspace)
