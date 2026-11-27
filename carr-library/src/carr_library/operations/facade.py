"""Bounded recovery views over canonical journals, without another job queue.

Stored status is reported as stored status. Listing never resumes a worker,
contacts a provider, loads a model, or upgrades a saved receipt to verified audio.
"""
from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import re

from ..durable_state import local_path, read_json, regular
from .models import ResourceStateError


_JOB_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}')
_STATES = {'pending', 'queued', 'preparing', 'running', 'pausing', 'cancelling',
           'paused', 'cancelled', 'interrupted', 'partial', 'attention', 'failed', 'complete'}
_TERMINAL = {'complete', 'cancelled'}
_MAX_CHECKPOINT = 32 * 1024 * 1024
_MAX_DISCOVERY = 10000
_LOCATIONS = {
    'studio': ('state/studio/jobs', '*/job.json'),
    'assistant': ('state/assistant', '*.json'),
    'import': ('import-batches', '*.json'),
    'translation': ('state/translation/jobs', '*.json'),
    'asr': ('state/transcription', '*/job.json'),
}


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
        self._validate_scope()

    def _validate_scope(self):
        if self.broker is not None and (
                self.broker.root != self.root / 'runtime' / 'resources'
                or self.broker.policy_path != self.root / 'state' / 'operations' / 'resources.json'):
            raise ResourceStateError('The resource coordinator belongs to another data folder.')
        for kind, name in (('studio', 'studio'), ('asr', 'transcription')):
            service = self.services.get(kind)
            if service is not None and service.root != self.root / 'state' / name:
                raise ResourceStateError('The operation service belongs to another data folder.')
        assistant = self.services.get('assistant')
        if assistant is not None and (assistant.app.root != self.root
                or assistant.store.directory != self.root / 'state' / 'assistant'):
            raise ResourceStateError('The assistant service belongs to another data folder.')
        translation = self.services.get('translation')
        if translation is not None and translation.directory != self.root / 'state' / 'translation':
            raise ResourceStateError('The translation service belongs to another data folder.')

    def _path(self, kind, identity):
        if kind not in _LOCATIONS or not isinstance(identity, str) or not _JOB_ID.fullmatch(identity):
            raise ValueError('Choose a known operation and its existing job identity.')
        directory, pattern = _LOCATIONS[kind]
        if kind == 'asr':
            if re.fullmatch('[0-9a-f]{32}', identity):
                directory = 'state/transcription/jobs'
            elif not re.fullmatch('[0-9a-f]{64}', identity):
                raise ValueError('Choose an exact saved transcription job identity.')
        path = self.root / directory / (identity + '.json' if pattern == '*.json' else identity + '/job.json')
        return local_path(path, root=self.root)

    def _controls(self, kind, status):
        service = self.services.get(kind)
        if service is None or status in _TERMINAL:
            return ()
        if kind == 'asr':
            return ('pause', 'cancel') if callable(getattr(service, 'control', None)) else ()
        values = []
        pause = 'request_pause' if kind == 'studio' else 'pause'
        if callable(getattr(service, pause, None)):
            values.append('pause')
        if callable(getattr(service, 'cancel', None)):
            values.append('cancel')
        return tuple(values)

    def _read_view(self, kind, identity):
        self._validate_scope()
        path = self._path(kind, identity)
        if regular(path).st_size > _MAX_CHECKPOINT:
            raise ResourceStateError('Operation checkpoint exceeds the bounded dashboard reader.')
        if kind == 'asr':
            # The supplied-worker service owns its request digest and completion
            # inventory. This metadata-only view never scans output receipts or
            # upgrades recorded completion into newly verified recognition.
            service = self.services.get('asr')
            if service is None:
                from ..transcription import TranscriptionService
                service = TranscriptionService(self.root / 'state' / 'transcription')
            elif len(identity) == 32 and hasattr(service, '_coordinator'):
                service = service._coordinator()
            summary = service.recorded_status(identity)
            return OperationView(kind, identity, None, summary['status'],
                summary['completed'], summary['total'],
                'Stored transcription checkpoint; verify results in Local transcription', '',
                self._controls(kind, summary['status']), path.relative_to(self.root).as_posix())
        data = read_json(path)
        if not isinstance(data, dict):
            raise ResourceStateError('Operation checkpoint is not an object.')
        expected = {'studio': 1, 'assistant': 'carr-assistant-job/1',
                    'translation': 'carr-assistant-job/1', 'asr': 'resonance-transcription-job/1'}
        if kind == 'import':
            if data.get('version') != 1 or data.get('batch_id') != identity or data.get('data_dir') != str(self.root):
                raise ResourceStateError('Import checkpoint identity or data directory changed.')
        elif data.get('schema') != expected[kind] or data.get('id') != identity:
            raise ResourceStateError('Operation checkpoint schema or identity changed.')
        if kind == 'assistant' and data.get('data_dir') != str(self.root):
            raise ResourceStateError('Assistant checkpoint belongs to another data directory.')
        source_binding = None
        if kind == 'translation':
            if data.get('kind') == 'translation/2':
                # Validate the saved source identity without constructing a
                # service, reading source assets or checking provider receipts.
                # This remains a stored-status view, never new quotation or
                # translation authority.
                from ..assistant.translation import TranslationService
                try:
                    source_binding = TranslationService._binding(data)
                except (KeyError, TypeError, ValueError):
                    raise ResourceStateError('Invalid source-bound translation checkpoint.') from None
            elif data.get('kind') != 'translation/1':
                raise ResourceStateError('Unsupported translation checkpoint.')
        status = data.get('state') if kind == 'import' else data.get('status')
        if not isinstance(status, str) or status not in _STATES:
            raise ResourceStateError('Unknown saved operation state; inspect its canonical service.')
        workspace = data.get('workspace') if kind in ('assistant', 'import') else None
        if source_binding is not None:
            workspace = source_binding['anchor']['workspace']
        if workspace is not None and (not isinstance(workspace, str) or not workspace or len(workspace) > 200):
            raise ResourceStateError('Invalid saved operation workspace.')
        completed = total = None
        if kind == 'studio':
            if not isinstance(data.get('nodes'), dict):
                raise ResourceStateError('Invalid production checkpoint nodes.')
            completed = len(data['nodes'])
            phase = 'Stored production checkpoint; accepted nodes'
        elif kind == 'assistant':
            steps = data.get('steps')
            if (not isinstance(steps, list) or any(not isinstance(row, dict)
                    or not isinstance(row.get('status'), str)
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
                             self._controls(kind, status), path.relative_to(self.root).as_posix())

    def _live(self, values, *, cancelled=lambda: False):
        if self.broker is None:
            return tuple(values)
        active = {row['operation_id'] for row in self.broker.snapshot(cancelled=cancelled)['leases']}
        # Legacy and non-heavy operations need not participate in this broker.
        # Absence is unknown, never proof that their process died.
        return tuple(replace(value, live=True) if value.key in active else value for value in values)

    def _authorize(self, workspace):
        if workspace is not None:
            with self.app.read(workspace):
                pass

    def list(self, *, workspace=None, limit=1000, cancelled=lambda: False):
        self._validate_scope()
        if cancelled():
            raise InterruptedError('Loading saved operations was cancelled.')
        self._authorize(workspace)
        if type(limit) is not int or not 1 <= limit <= _MAX_DISCOVERY:
            raise ValueError('Choose an operation-view limit between 1 and 10000.')
        values, inspected = [], 0
        locations = [(kind, *value) for kind, value in _LOCATIONS.items()]
        locations.append(('asr', 'state/transcription/jobs', '*/job.json'))
        for kind, directory, pattern in locations:
            base = self.root / directory
            if not os.path.lexists(base):
                continue
            local_path(base, root=self.root, exists=True)
            regular(base, directory=True)
            for path in base.glob(pattern):
                if cancelled():
                    raise InterruptedError('Loading saved operations was cancelled.')
                identity = path.stem if pattern == '*.json' else path.parent.name
                # Control files and unrelated service settings are not jobs.
                if pattern == '*.json' and not re.fullmatch(r'[0-9a-f]{32}', identity):
                    continue
                if kind == 'asr':
                    expected = r'[0-9a-f]{32}' if directory.endswith('/jobs') else r'[0-9a-f]{64}'
                    if not re.fullmatch(expected, identity):
                        continue
                inspected += 1
                if inspected > _MAX_DISCOVERY:
                    raise ResourceStateError('Operation inventory exceeds its bounded discovery limit.')
                try:
                    value = self._read_view(kind, identity)
                except InterruptedError:
                    raise
                except (ValueError, OSError, ResourceStateError):
                    # Do not print exception bodies, original text, provider
                    # profiles or file contents in a shared recovery dashboard.
                    value = OperationView(kind, identity, None, 'attention', None, None,
                        'Checkpoint could not be read; inspect the original service for recovery.', '', (),
                        path.relative_to(self.root).as_posix())
                if workspace is None or value.workspace in (None, workspace):
                    values.append(value)
        return self._live(sorted(values, key=lambda value: (value.kind, value.id))[:limit], cancelled=cancelled)

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
        if kind == 'asr':
            service.control(job_id, action)
        else:
            getattr(service, method)(job_id)
        # A requested stop is not a completed stop. The owning service updates
        # its canonical checkpoint after the worker acknowledges cancellation.
        return self.get(kind, job_id, workspace=workspace)
