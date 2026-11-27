"""Bounded metadata views over the existing managed job stores.

These are journal observations. A row marked complete does not certify its
outputs, revive an archived approval, or prove that an unreserved worker died.
No provider, model, input document or output transcript is opened by listing.
"""
from dataclasses import dataclass, replace
from datetime import datetime
import os
from pathlib import Path
import re

from ..audio.jobs import read_json
from ._state import local_path, regular
from .models import ResourceLimits, ResourceRequest, ResourceStateError

_ID = re.compile(r'[0-9a-f]{32}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_MAX_DISCOVERY = 10000
_MAX_METADATA_BYTES = 256 * 1024 * 1024
_LOCATIONS = {
    'assistant': ('state/assistant', False, 64 * 1024 * 1024),
    'asr': ('transcription-jobs', True, 16 * 1024 * 1024),
    'document': ('import-requests', False, 64000),
    'import': ('import-batches', False, 16 * 1024 * 1024),
    'studio': ('audio-projects', True, 1024 * 1024),
    'translation': ('state/translation', False, 64 * 1024 * 1024),
    'recording': ('state/studio/recordings', False, 65536),
}
_TERMINAL = {'complete', 'cancelled', 'failed', 'attention', 'superseded',
             'quarantined', 'conflict', 'timed_out'}


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Loading saved operations was cancelled.')


def _count(value, *, maximum=2**63 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('Invalid recorded operation count.')
    return value


def _stamp(value):
    if value == '':
        return ''
    if type(value) is not str or len(value) > 80:
        raise ValueError('Invalid recorded operation timestamp.')
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError('Recorded operation timestamps require a timezone.')
    return parsed.isoformat()


def _workspace(value):
    if value is not None and (type(value) is not str
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value) is None):
        raise ValueError('Invalid recorded workspace identity.')
    return value


class _Budget:
    def __init__(self, cancelled):
        self.cancelled, self.entries, self.bytes = cancelled, 0, 0

    def entry(self):
        _check(self.cancelled)
        self.entries += 1
        if self.entries > _MAX_DISCOVERY:
            raise ResourceStateError('Operation inventory exceeds its bounded discovery limit.')

    def read(self, path, maximum):
        _check(self.cancelled)
        info = regular(path)
        if info.st_size > maximum:
            raise ValueError('Operation checkpoint exceeds its metadata budget.')
        self.bytes += info.st_size
        if self.bytes > _MAX_METADATA_BYTES:
            raise ResourceStateError('Saved operation metadata exceeds the dashboard read budget.')
        value = read_json(path, limit=maximum)
        _check(self.cancelled)
        return value


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
    unit: str = 'steps'
    verification: str = 'not_performed'

    @property
    def key(self):
        return self.kind + ':' + self.id


def resource_view(snapshot):
    """Remove disk paths, native owner tokens and arbitrary operation labels."""
    if type(snapshot) is not dict or snapshot.get('schema') != 'carr-resource-snapshot/1':
        raise ResourceStateError('Unsupported resource snapshot.')
    limits = ResourceLimits.from_dict(snapshot['limits'])
    raw = snapshot['leases']
    if type(raw) is not list or len(raw) > 4096:
        raise ResourceStateError('Invalid resource reservation inventory.')
    leases = []
    for row in raw:
        if (type(row) is not dict or type(row.get('id')) is not str
                or not _ID.fullmatch(row['id'])):
            raise ResourceStateError('Invalid resource reservation identity.')
        request = ResourceRequest.from_dict(row['request'])
        operation = row.get('operation_id')
        if (type(operation) is not str or re.fullmatch(
                r'(?:asr|studio|document|assistant|translation|import|recording):[0-9a-f]{32}', operation) is None):
            operation = 'other'
        leases.append({
            'id': row['id'], 'operation_id': operation,
            'cpu_slots': request.cpu_slots, 'memory_bytes': request.memory_bytes,
            'disk_bytes': request.total_disk_bytes,
            'gpu_device': request.gpu_device, 'gpu_memory_bytes': request.gpu_memory_bytes,
            'devices': list(request.devices),
        })
    usage = {name: sum(row[name] for row in leases)
             for name in ('cpu_slots', 'memory_bytes', 'disk_bytes')}
    return {
        'schema': 'carr-resource-view/1', 'limits': limits.to_dict(),
        'usage': usage, 'leases': leases,
        'gpu_devices_reserved': sorted(row['gpu_device'] for row in leases if row['gpu_device'] is not None),
        'exclusive_devices': sorted(device for row in leases for device in row['devices']),
        'measurement': 'declared cooperative reservations, not measured CPU/RAM/VRAM usage',
    }


class OperationFacade:
    def __init__(self, app, *, broker=None, assistant=None, transcription=None):
        self.app = app
        self.root = local_path(app.root, exists=True)
        info = regular(self.root, directory=True)
        self._root_identity = (info.st_dev, info.st_ino)
        self.broker = broker
        self.services = {'assistant': assistant, 'asr': transcription}
        self._validate_scope()

    def _validate_scope(self):
        info = regular(self.root, directory=True)
        if (Path(self.app.root).absolute() != self.root
                or (info.st_dev, info.st_ino) != self._root_identity):
            raise ResourceStateError('The selected data directory changed. Reopen Tasks and resources.')
        if self.broker is not None and (
                self.broker.root != self.root / 'runtime' / 'resources'
                or self.broker.policy_path != self.root / 'state' / 'operations' / 'resources.json'):
            raise ResourceStateError('The resource coordinator belongs to another data directory.')
        asr = self.services['asr']
        if asr is not None and (Path(asr.library_root).absolute() != self.root
                or Path(asr.root).absolute() != self.root / 'transcription-jobs'):
            raise ResourceStateError('The transcription service belongs to another data directory.')
        assistant = self.services['assistant']
        if assistant is not None and (Path(assistant.app.root).absolute() != self.root
                or Path(assistant.store.directory).absolute() != self.root / 'state' / 'assistant'):
            raise ResourceStateError('The assistant service belongs to another data directory.')

    def _path(self, kind, identity):
        if kind not in _LOCATIONS or type(identity) is not str or not _ID.fullmatch(identity):
            raise ValueError('Choose a known operation and its existing 32-character identity.')
        directory, nested, _ = _LOCATIONS[kind]
        if kind == 'studio':
            suffix = identity + '/audio/render-journal.json'
        else:
            suffix = identity + ('/job.json' if nested else '.json')
        return local_path(self.root / directory / suffix, root=self.root)

    def _authorize(self, workspace):
        if workspace is not None:
            _workspace(workspace)
            with self.app.read(workspace):
                pass

    @staticmethod
    def _controls(kind, status, *, bound=True):
        if not bound or status in _TERMINAL:
            return ()
        if kind == 'assistant' and status in ('queued', 'running', 'paused'):
            return ('cancel',) if status == 'paused' else ('pause', 'cancel')
        if kind == 'asr' and status in ('verifying', 'running', 'paused'):
            return ('cancel',) if status == 'paused' else ('pause', 'cancel')
        return ()

    def _read_view(self, kind, identity, budget):
        self._validate_scope()
        path = self._path(kind, identity)
        data = budget.read(path, _LOCATIONS[kind][2])
        if type(data) is not dict:
            raise ValueError('An operation checkpoint must be an object.')
        workspace, completed, total, updated, bound = None, None, None, '', True
        unit = 'steps'
        if kind == 'assistant':
            from ..assistant.validation import validate_job
            validate_job(data, identity=identity)
            workspace = _workspace(data['workspace'])
            bound = data['data_dir'] == str(self.root) and not data.get('requires_reapproval', False)
            status = data['status'] if bound else 'attention'
            completed, total = sum(row['status'] == 'complete' for row in data['steps']), len(data['steps'])
            updated = _stamp(data['updated'])
            phase = ('Stored assistant checkpoint; completed steps.' if bound else
                     'Archived assistant approval needs review in Local assistant.')
        elif kind == 'translation':
            from ..assistant.translation_state import validate_translation_job, split_text
            validate_translation_job(data, identity=identity)
            bound = data['library_root'] == str(self.root)
            status = data['status'] if bound else 'attention'
            completed, total = len(data['receipts']), len(split_text(data['source_text']))
            updated, unit = _stamp(data['updated']), 'chunks'
            phase = ('Stored translation checkpoint; accepted chunks.' if bound else
                     'Archived translation binding; reopen Translation to review access.')
        elif kind == 'recording':
            from ..audio.recording import validate_recording_receipt
            validate_recording_receipt(data, recording_id=identity)
            status, completed, total = 'complete', data['audio']['frames'], data['audio']['frames']
            updated, unit = _stamp(data['created']), 'PCM frames'
            phase = 'Stored capture receipt; opening the saved recording verifies its PCM bytes.'
        elif kind == 'import':
            from ..ingest.batches import ImportBatchStore
            # _validate is the current store's pure validator; do not call its
            # directory-creating list/get or its external-request reconciliation.
            ImportBatchStore(self.app)._validate(data, identity)
            workspace, status = _workspace(data['workspace']), data['state']
            completed = sum(data['counts'][key] for key in ('complete', 'duplicate', 'excluded'))
            total, updated, unit = data['total'], _stamp(data['updated']), 'files'
            phase = 'Stored batch checkpoint; completed, duplicate or excluded files.'
        elif kind == 'document':
            # The existing worker's metadata envelope, without opening its
            # selected source or treating a publication hint as a receipt.
            if (type(data.get('version')) is not int or data['version'] != 1
                    or data.get('request_id') != identity or data.get('data_dir') != str(self.root)):
                raise ValueError('Import request binding changed.')
            workspace = _workspace(data.get('workspace'))
            if workspace is None:
                raise ValueError('An import request needs a workspace.')
            status = data.get('state')
            if type(status) is not str or status not in (
                    'pending', 'running', 'cancelled', 'failed', 'quarantined',
                    'complete', 'superseded', 'conflict', 'timed_out'):
                raise ValueError('Invalid recorded import request state.')
            result = data.get('result')
            if result is not None:
                if type(result) is not dict:
                    raise ValueError('Invalid recorded import result.')
                if result.get('completed') is not None:
                    completed = _count(result['completed'])
                if result.get('total') is not None:
                    total = _count(result['total'])
            unit, phase = 'records', 'Stored import request; verify its publication in Import centre.'
        elif kind == 'studio':
            if (type(data.get('schema_version')) is not int or data['schema_version'] != 1
                    or type(data.get('run_key')) is not str or not _HASH.fullmatch(data['run_key'])
                    or type(data.get('source_sha256')) is not str or not _HASH.fullmatch(data['source_sha256'])
                    or type(data.get('state')) is not str
                    or data['state'] not in ('running', 'complete', 'cancelled', 'failed')):
                raise ValueError('Invalid managed render checkpoint.')
            status = data['state']
            if 'completed_steps' in data:
                completed = _count(data['completed_steps'])
            if 'scene_count' in data:
                _count(data['scene_count'])
            # Cache, scene, stem and final steps share this counter. The number
            # of scenes is not its denominator and must not form a percentage.
            total, unit = None, 'checkpoints'
            phase = 'Stored render checkpoints; no comparable total. Verify the master in Audio Studio.'
        else:
            from ..transcription_state import SCHEMA, validate_definition
            validate_definition(data, identity=identity)
            total, updated, unit = len(data['inputs']), _stamp(data['created']), 'files'
            execution_path = path.parent / 'execution.json'
            if not os.path.lexists(execution_path):
                status, completed = 'prepared', 0
            else:
                execution = budget.read(local_path(execution_path, root=self.root), 16 * 1024 * 1024)
                if (type(execution) is not dict
                        or set(execution) != {'schema', 'state', 'run_id', 'exit_code', 'updated'}
                        or execution['schema'] != SCHEMA or type(execution['run_id']) is not str
                        or not _ID.fullmatch(execution['run_id'])
                        or type(execution['state']) is not str
                        or execution['state'] not in ('queued', 'verifying', 'running', 'paused',
                                                      'cancelled', 'failed', 'complete')):
                    raise ValueError('Invalid transcription execution record.')
                status, updated = execution['state'], _stamp(execution['updated'])
                if (status == 'complete' and (type(execution['exit_code']) is not int or execution['exit_code'] != 0)
                        or status != 'complete' and execution['exit_code'] is not None):
                    raise ValueError('Invalid recorded transcription exit state.')
                status_path = path.parent / 'job_status.json'
                if os.path.lexists(status_path):
                    progress = budget.read(local_path(status_path, root=self.root), 16 * 1024 * 1024)
                    if (type(progress) is dict
                            and progress.get('protocol') == 'supplied-whisper-v43-hardened-1'
                            and progress.get('run_id') == execution['run_id']
                            and type(progress.get('done')) is int
                            and type(progress.get('total')) is int
                            and progress['total'] == total and 0 <= progress['done'] <= total):
                        completed = progress['done']
            phase = 'Stored transcription checkpoint; verify outputs in Local speech transcription.'
        if completed is not None:
            _count(completed)
        if total is not None and (completed is not None and completed > total):
            raise ValueError('Recorded operation counts are inconsistent.')
        _check(budget.cancelled)
        return OperationView(kind, identity, workspace, status, completed, total, phase, updated,
            self._controls(kind, status, bound=bound), path.relative_to(self.root).as_posix(), unit=unit)

    def _live(self, values, *, cancelled):
        if self.broker is None:
            return tuple(values)
        active = {row['operation_id'] for row in resource_view(
            self.broker.snapshot(cancelled=cancelled))['leases']}
        # Non-heavy and older operations may not participate in this broker.
        return tuple(replace(value, live=True) if value.key in active else value for value in values)

    def list(self, *, workspace=None, limit=1000, cancelled=lambda: False):
        self._validate_scope()
        _check(cancelled)
        self._authorize(workspace)
        if type(limit) is not int or not 1 <= limit <= _MAX_DISCOVERY:
            raise ValueError('Choose a dashboard limit between 1 and 10000.')
        values, budget = [], _Budget(cancelled)
        for kind, (directory, nested, _) in _LOCATIONS.items():
            base = self.root / directory
            if not os.path.lexists(base):
                continue
            local_path(base, root=self.root, exists=True)
            regular(base, directory=True)
            with os.scandir(base) as entries:
                for entry in entries:
                    # Count every entry, including non-job control/temporary
                    # files, before filtering. Unknown names cannot defeat the cap.
                    budget.entry()
                    identity = entry.name if nested else entry.name[:-5] if entry.name.endswith('.json') else ''
                    if not _ID.fullmatch(identity):
                        continue
                    try:
                        path = self._path(kind, identity)
                        if nested and not os.path.lexists(path):
                            # A managed draft may not yet have a render journal.
                            continue
                        value = self._read_view(kind, identity, budget)
                    except (InterruptedError, ResourceStateError):
                        raise
                    except (ValueError, OSError, TypeError, KeyError, RecursionError):
                        suffix = identity + ('/audio/render-journal.json' if kind == 'studio'
                            else '/job.json' if nested else '.json')
                        value = OperationView(kind, identity, None, 'attention', None, None,
                            'Checkpoint needs review in its original workflow.', '', (),
                            directory + '/' + suffix)
                    if workspace is None or value.workspace in (None, workspace):
                        values.append(value)
        _check(cancelled)
        return self._live(sorted(values, key=lambda row: (row.kind, row.id))[:limit], cancelled=cancelled)

    def get(self, kind, identity, *, workspace=None, cancelled=lambda: False):
        self._authorize(workspace)
        value = self._read_view(kind, identity, _Budget(cancelled))
        if workspace is not None and value.workspace not in (None, workspace):
            raise PermissionError('The saved operation belongs to another workspace.')
        return self._live((value,), cancelled=cancelled)[0]

    def resources(self, *, cancelled=lambda: False):
        self._validate_scope()
        if self.broker is None:
            raise ResourceStateError('Open the resource coordinator to inspect live reservations.')
        return resource_view(self.broker.snapshot(cancelled=cancelled))

    def control(self, kind, identity, action, *, workspace=None, cancelled=lambda: False):
        if action not in ('pause', 'cancel'):
            raise ValueError('Only pause/cancel requests are supported here; resume uses the original workflow.')
        value = self.get(kind, identity, workspace=workspace, cancelled=cancelled)
        if action not in value.controls:
            raise ValueError('This workflow does not expose that control from the shared view.')
        _check(cancelled)
        service = self.services.get(kind)
        if service is None:
            if kind == 'asr':
                from ..transcription_service import TranscriptionService
                service = TranscriptionService(self.root)
            else:
                from ..assistant import AssistantService
                service = AssistantService(self.app)
            self.services[kind] = service
        self._validate_scope()
        if kind == 'asr':
            service.control(identity, 'stop' if action == 'cancel' else 'pause')
        else:
            # This retains the owning service's scope and reapproval checks.
            getattr(service, action)(identity)
        return {'operation_id': value.key, 'action': action, 'requested': True,
                'acknowledged': False}
