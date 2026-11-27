"""Bounded recovery views over canonical journals, without another job queue.

Stored status is reported as stored status. Listing never resumes a worker,
contacts a provider, loads a model, or upgrades a saved receipt to verified audio.
"""
from dataclasses import dataclass, replace
from hashlib import sha256
import math
import os
from pathlib import Path
import re

from ..durable_state import local_path, read_json, regular
from ..assistant.state import MAX_CHECKPOINT_BYTES, read_json as assistant_json
from .models import ResourceStateError


_JOB_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}')
_STATES = {'pending', 'queued', 'preparing', 'running', 'pausing', 'cancelling',
           'paused', 'cancelled', 'interrupted', 'partial', 'attention', 'failed', 'complete',
           'ready', 'stopped', 'completed'}
_TERMINAL = {'complete', 'completed', 'cancelled'}
_MAX_CHECKPOINT = 32 * 1024 * 1024
_MAX_DISCOVERY = 10000
_LOCATIONS = {
    'studio': ('state/studio/jobs', '*/job.json'),
    'assistant': ('state/assistant', '*.json'),
    'import': ('import-batches', '*.json'),
    'translation': ('state/translation/jobs', '*.json'),
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
        scope = sha256(self.workspace.encode('utf-8')).hexdigest() + ':' if self.kind == 'asr' and self.workspace else ''
        return self.kind + ':' + scope + self.id


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
        studio = self.services.get('studio')
        if studio is not None and studio.root != self.root / 'state' / 'studio':
            raise ResourceStateError('The production service belongs to another data folder.')
        transcription = self.services.get('asr')
        if transcription is not None:
            workspace = transcription.workspace
            expected = self.root / 'state' / 'transcription' / sha256(workspace.encode('utf-8')).hexdigest() / 'jobs'
            if (transcription.app.root != self.root
                    or transcription.root != self.root / 'state' / 'transcription'
                    or transcription.jobs_root != expected):
                raise ResourceStateError('The transcription service belongs to another library or data folder.')
        assistant = self.services.get('assistant')
        if assistant is not None and (assistant.app.root != self.root
                or assistant.store.directory != self.root / 'state' / 'assistant'):
            raise ResourceStateError('The assistant service belongs to another data folder.')
        translation = self.services.get('translation')
        if translation is not None and (translation.directory != self.root / 'state' / 'translation'
                or translation.library is not self.app):
            raise ResourceStateError('The translation service belongs to another library or data folder.')

    def _path(self, kind, identity, workspace=None):
        if not isinstance(identity, str) or not _JOB_ID.fullmatch(identity):
            raise ValueError('Choose a known operation and its existing job identity.')
        if kind == 'asr':
            if not re.fullmatch(r'[0-9a-f]{32}', identity):
                raise ValueError('Choose an existing library transcription job.')
            if workspace is None:
                service = self.services.get('asr')
                workspace = service.workspace if service is not None else None
            if not isinstance(workspace, str) or not workspace:
                raise ValueError('Select the transcription library before opening its job.')
            path = (self.root / 'state' / 'transcription'
                    / sha256(workspace.encode('utf-8')).hexdigest() / 'jobs' / identity / 'job.json')
        else:
            if kind not in _LOCATIONS:
                raise ValueError('Choose a known operation and its existing job identity.')
            directory, pattern = _LOCATIONS[kind]
            path = self.root / directory / (identity + '.json' if pattern == '*.json' else identity + '/job.json')
        return local_path(path, root=self.root)

    def _controls(self, kind, status):
        service = self.services.get(kind)
        if service is None or status in _TERMINAL or kind in ('asr', 'translation'):
            # Accepted ASR stops through its active run() callback. A journal
            # rewrite or a fabricated pause/cancel method cannot stop its child.
            return ()
        values = []
        pause = 'request_pause' if kind == 'studio' else 'pause'
        if callable(getattr(service, pause, None)):
            values.append('pause')
        if callable(getattr(service, 'cancel', None)):
            values.append('cancel')
        return tuple(values)

    def _read_view(self, kind, identity, *, workspace=None):
        self._validate_scope()
        if kind == 'asr' and workspace is None:
            service = self.services.get('asr')
            workspace = service.workspace if service is not None else None
        path = self._path(kind, identity, workspace)
        maximum = MAX_CHECKPOINT_BYTES if kind in ('assistant', 'translation') else _MAX_CHECKPOINT
        if regular(path).st_size > maximum:
            raise ResourceStateError('Operation checkpoint exceeds its canonical storage allowance.')
        if kind == 'asr':
            self._authorize(workspace)
            service = self.services.get('asr')
            if service is None or service.workspace != workspace:
                service = self.app.transcription(workspace)
            data = service.get(identity)
            status = data.get('status')
            if status not in ('ready', 'running', 'stopped', 'partial', 'failed', 'completed'):
                raise ResourceStateError('Unknown stored transcription status.')
            completed, total = data.get('done'), len(data['sources'])
            failed, receipts, artifacts = data.get('failed'), data.get('receipts'), data.get('artifacts')
            if (type(completed) is not int or not 0 <= completed <= total
                    or type(failed) is not int or not 0 <= failed <= total
                    or not isinstance(receipts, list) or any(not isinstance(row, str) for row in receipts)
                    or len(receipts) != completed or len(set(receipts)) != len(receipts)
                    or not isinstance(artifacts, list)):
                raise ResourceStateError('Invalid transcription checkpoint completion counts.')
            if status == 'completed':
                required = {'txt', 'srt', 'vtt', 'json'}
                if data['settings']['word_timestamps']:
                    required.update(('words.srt', 'words.vtt'))
                expected = {(row['filename'], format) for row in data['sources'] for format in required}
                if (completed != total or failed or len(artifacts) != len(expected)
                        or any(not isinstance(row, dict) or not isinstance(row.get('source'), str)
                               or not isinstance(row.get('format'), str) for row in artifacts)
                        or {(row['source'], row['format']) for row in artifacts} != expected):
                    raise ResourceStateError('Completed transcription checkpoint has an incomplete output inventory.')
            phase = 'Stored transcription checkpoint; verify results in Local transcription'
        elif kind == 'translation':
            from ..assistant.translation import TranslationService
            service = self.services.get('translation')
            if service is None:
                service = TranslationService(self.root / 'state' / 'translation', library=self.app)
            data = service.get(identity)
            binding = service._binding(data)
            actual_workspace = binding['anchor']['workspace'] if binding is not None else None
            if workspace is not None and actual_workspace not in (None, workspace):
                raise PermissionError('This translation belongs to another selected library.')
            workspace = actual_workspace
            self._authorize(workspace)
            status = data.get('status')
            completed, total = len(data['receipts']), len(data['chunks'])
            phase = 'Stored translation checkpoint; accepted chunks, not current export authority'
        else:
            data = assistant_json(path) if kind == 'assistant' else read_json(path)
            if not isinstance(data, dict):
                raise ResourceStateError('Operation checkpoint is not an object.')
            expected = {'studio': 1, 'assistant': 'carr-assistant-job/1'}
            if kind == 'import':
                if data.get('version') != 1 or data.get('batch_id') != identity or data.get('data_dir') != str(self.root):
                    raise ResourceStateError('Import checkpoint identity or data directory changed.')
            elif data.get('schema') != expected[kind] or data.get('id') != identity:
                raise ResourceStateError('Operation checkpoint schema or identity changed.')
            if kind == 'assistant' and data.get('data_dir') != str(self.root):
                raise ResourceStateError('Assistant checkpoint belongs to another data directory.')
            actual_workspace = data.get('workspace') if kind in ('assistant', 'import') else None
            if workspace is not None and actual_workspace not in (None, workspace):
                raise PermissionError('This saved operation belongs to another selected library.')
            workspace = actual_workspace
            self._authorize(workspace)
            status = data.get('state') if kind == 'import' else data.get('status')
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
            else:
                counts = data.get('counts')
                if not isinstance(counts, dict) or any(type(value) is not int or value < 0 for value in counts.values()):
                    raise ResourceStateError('Invalid import checkpoint counts.')
                completed = sum(counts.get(key, 0) for key in ('complete', 'duplicate', 'excluded'))
                total = data.get('total')
                if type(total) is not int or sum(counts.values()) != total:
                    raise ResourceStateError('Import checkpoint counts differ from the recorded total.')
                phase = 'Stored import checkpoint; accepted or excluded files'
        if not isinstance(status, str) or status not in _STATES:
            raise ResourceStateError('Unknown saved operation state; inspect its canonical service.')
        if workspace is not None and (not isinstance(workspace, str) or not workspace or len(workspace) > 200):
            raise ResourceStateError('Invalid saved operation workspace.')
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
        self._authorize(workspace)
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
            try:
                with self.app.read(workspace):
                    pass
            except (ValueError, OSError):
                raise PermissionError('The selected operation library is unavailable or archived.') from None

    def _discovery(self, workspace, cancelled):
        inspected = 0

        def entries(base):
            nonlocal inspected
            with os.scandir(base) as directory:
                for entry in directory:
                    if cancelled():
                        raise InterruptedError('Loading saved operations was cancelled.')
                    inspected += 1
                    if inspected > _MAX_DISCOVERY:
                        raise ResourceStateError('Operation inventory exceeds its bounded discovery limit.')
                    yield entry

        for kind, (directory, pattern) in _LOCATIONS.items():
            if cancelled():
                raise InterruptedError('Loading saved operations was cancelled.')
            base = self.root / directory
            if not os.path.lexists(base):
                continue
            local_path(base, root=self.root, exists=True)
            regular(base, directory=True)
            for entry in entries(base):
                path = Path(entry.path)
                if pattern == '*.json':
                    identity = path.stem
                    if path.suffix != '.json' or not re.fullmatch(r'[0-9a-f]{32}', identity):
                        continue
                else:
                    identity = path.name
                    if not _JOB_ID.fullmatch(identity) or not entry.is_dir(follow_symlinks=False):
                        continue
                    path = path / 'job.json'
                    if not os.path.lexists(path):
                        continue
                yield kind, identity, path, workspace
        # Active workspace identities come from the existing catalogue; a
        # directory name or an injected journal cannot create library authority.
        workspaces = (workspace,) if workspace is not None else tuple(row.id for row in self.app.workspaces())
        if len(workspaces) > _MAX_DISCOVERY:
            raise ResourceStateError('Operation workspace discovery exceeds its bounded limit.')
        for selected in workspaces:
            if cancelled():
                raise InterruptedError('Loading saved operations was cancelled.')
            self._authorize(selected)
            base = self.root / 'state' / 'transcription' / sha256(selected.encode('utf-8')).hexdigest() / 'jobs'
            if not os.path.lexists(base):
                continue
            local_path(base, root=self.root, exists=True)
            regular(base, directory=True)
            for entry in entries(base):
                identity = entry.name
                if not re.fullmatch(r'[0-9a-f]{32}', identity) or not entry.is_dir(follow_symlinks=False):
                    continue
                path = Path(entry.path) / 'job.json'
                if os.path.lexists(path):
                    yield 'asr', identity, path, selected

    def list(self, *, workspace=None, limit=1000, cancelled=lambda: False):
        self._validate_scope()
        if cancelled():
            raise InterruptedError('Loading saved operations was cancelled.')
        self._authorize(workspace)
        if type(limit) is not int or not 1 <= limit <= _MAX_DISCOVERY:
            raise ValueError('Choose an operation-view limit between 1 and 10000.')
        values = []
        for kind, identity, path, selected in self._discovery(workspace, cancelled):
            if cancelled():
                raise InterruptedError('Loading saved operations was cancelled.')
            self._authorize(selected)
            try:
                value = self._read_view(kind, identity, workspace=selected)
            except InterruptedError:
                raise
            except PermissionError:
                # A valid foreign/archived workspace is not a corrupt checkpoint
                # and must not leak an attention row into the selected library.
                self._authorize(workspace)
                continue
            except (ValueError, OSError, ResourceStateError, KeyError, TypeError, AttributeError):
                if workspace is not None and kind in ('assistant', 'import', 'translation'):
                    # A corrupt global journal has no validated workspace claim.
                    # Recover it through the original workflow, not another scope.
                    continue
                value = OperationView(kind, identity, selected if kind == 'asr' else None,
                    'attention', None, None,
                    'Checkpoint could not be read; inspect the original service for recovery.', '', (),
                    path.relative_to(self.root).as_posix())
            values.append(value)
        self._authorize(workspace)
        return self._live(sorted(values, key=lambda value: (value.kind, value.workspace or '', value.id))[:limit],
                          cancelled=cancelled)

    def get(self, kind, job_id, *, workspace=None):
        self._authorize(workspace)
        value = self._read_view(kind, job_id, workspace=workspace)
        self._authorize(workspace)
        return self._live((value,))[0]

    def control(self, kind, job_id, action, *, workspace=None):
        if action not in ('pause', 'cancel'):
            raise ValueError('The shared view supports only explicit pause/cancel requests; resume uses the original workflow.')
        value = self.get(kind, job_id, workspace=workspace)
        if action not in value.controls:
            raise ValueError('This operation does not expose that control through its current canonical service.')
        service = self.services[kind]
        method = 'request_pause' if action == 'pause' and kind == 'studio' else action
        self._authorize(value.workspace)
        getattr(service, method)(job_id)
        return self.get(kind, job_id, workspace=workspace)
