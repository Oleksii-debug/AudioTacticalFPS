"""Bounded, durable execution of approved typed local assistant plans."""
from dataclasses import asdict, is_dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from time import monotonic
from uuid import NAMESPACE_URL, uuid4, uuid5

from .models import JobReport, Plan, Scope, canonical, local_path, text
from .planner import preview
from .state import JobStore, now, regular


_KEYS = {
    'library.create': ({'name'}, {'description'}),
    'library.list': (set(), set()),
    'library.catalog': (set(), {'after', 'limit', 'title', 'author', 'language'}),
    'library.import': ({'path'}, set()),
    'library.import_folder': ({'path'}, set()),
    'library.search': ({'query'}, {'mode', 'limit', 'title', 'author', 'language'}),
    'library.extract': ({'edition', 'name'}, set()),
    'library.backup': ({'name'}, set()),
    'audio.produce': (set(), {'text', 'script', 'voice_ids', 'title', 'format', 'stems'}),
}


def _check_arguments(step):
    required, optional = _KEYS[step.tool]
    if set(step.arguments) - required - optional or not required <= set(step.arguments):
        raise ValueError(f'Invalid arguments for {step.tool}.')
    for key, value in step.arguments.items():
        if key in ('script',):
            if type(value) is not dict:
                raise ValueError('A structured audio script must be an object.')
        elif key == 'voice_ids':
            if not isinstance(value, (tuple, list)) or not value or len(value) > 100:
                raise ValueError('Choose one or more available voice identities.')
            for voice in value:
                text(voice, 'voice identity', maximum=4096)
        elif key == 'stems':
            if type(value) is not bool:
                raise ValueError('The stems option must be true or false.')
        elif key == 'limit':
            if type(value) is not int or not 1 <= value <= (500 if step.tool == 'library.catalog' else 100):
                raise ValueError('Invalid result page size.')
        else:
            text(value, key, maximum=4 * 1024 * 1024 if key == 'text' else 32768)
    if step.tool == 'audio.produce' and (('text' in step.arguments) == ('script' in step.arguments)):
        raise ValueError('Provide either script text or a structured audio script.')
    if 'mode' in step.arguments and step.arguments['mode'] not in ('word', 'phrase', 'terms', 'thematic', 'semantic'):
        raise ValueError('Unsupported search mode.')


def _hash_file(path, cancelled=lambda: False):
    digest = sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            if cancelled():
                raise InterruptedError('Assistant operation was stopped.')
            digest.update(block)
    return digest.hexdigest()


def _plain(value):
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


class AssistantService:
    def __init__(self, app, *, state_dir=None, production=None):
        self.app = app
        self.store = JobStore(state_dir or (app.root / 'state' / 'assistant'))
        if not self.store.directory.is_relative_to(app.root):
            raise ValueError('Assistant state must remain inside this application data directory.')
        self.production = production

    def preview(self, instruction, scope):
        plan = preview(instruction, scope)
        self.validate(plan)
        return plan

    def validate(self, plan):
        if not isinstance(plan, Plan):
            raise TypeError('A typed plan is required.')
        workspace = plan.scope.workspace_id
        if workspace is not None:
            with self.app.read(workspace):
                pass
        for i, step in enumerate(plan.steps):
            _check_arguments(step)
            if step.tool == 'library.create':
                if i != 0:
                    raise ValueError('Create a new library as the first step.')
                workspace = '<new>'
            if step.tool not in ('library.create', 'library.list', 'library.backup', 'audio.produce') and not workspace:
                raise PermissionError('Choose a library before this operation.')
            if step.tool.startswith('library.import'):
                path = plan.scope.input(step.arguments['path'])
                if step.tool.endswith('_folder') != path.is_dir():
                    raise ValueError('The selected input does not match the import operation.')
            if step.tool in ('library.extract', 'library.backup'):
                target = plan.scope.output(step.arguments['name'])
                if target.exists():
                    raise FileExistsError('The selected output already exists; choose another name.')
            if step.tool == 'library.backup' and not plan.scope.allow_full_backup:
                raise PermissionError('Complete backup includes all libraries; enable that explicit permission first.')
            if step.tool == 'audio.produce':
                if plan.scope.output_root is None:
                    raise PermissionError('Choose the audio output directory.')
                if self.production is None:
                    raise RuntimeError('The audio production runtime is not configured.')
        return plan

    def submit(self, plan):
        self.validate(plan)
        identity = uuid4().hex
        record = {'schema': 'carr-assistant-job/1', 'id': identity, 'data_dir': str(self.app.root),
                  'plan': plan.to_dict(), 'plan_sha256': plan.digest, 'created': now(), 'updated': now(),
                  'status': 'queued', 'message': '', 'workspace': plan.scope.workspace_id,
                  'steps': [{'status': 'pending', 'intent': {}, 'result': None, 'message': ''}
                            for _ in plan.steps]}
        self.store.save(record)
        return identity

    def _load(self, identity):
        record = self.store.load(identity)
        plan = Plan.from_dict(record['plan'])
        if record.get('data_dir') != str(self.app.root):
            raise PermissionError('This job belongs to another data directory; review and submit its plan again.')
        if record.get('plan_sha256') != plan.digest or len(record.get('steps', [])) != len(plan.steps):
            raise ValueError('The approved assistant plan has changed.')
        if record.get('status') not in ('queued', 'running', 'paused', 'cancelled', 'complete', 'attention', 'failed'):
            raise ValueError('Invalid assistant job status.')
        workspace = record.get('workspace')
        if plan.scope.workspace_id is not None and workspace != plan.scope.workspace_id:
            raise PermissionError('Assistant workspace permission changed.')
        if plan.scope.workspace_id is None and workspace is not None:
            expected = uuid5(NAMESPACE_URL, 'carr-assistant:' + identity + ':library').hex
            if not plan.scope.allow_create or workspace != expected:
                raise PermissionError('Assistant cannot select another workspace from a checkpoint.')
        for step, checkpoint in zip(plan.steps, record['steps']):
            _check_arguments(step)
            if type(checkpoint) is not dict or checkpoint.get('status') not in ('pending', 'running', 'complete', 'failed'):
                raise ValueError('Invalid assistant step checkpoint.')
        return record, plan

    @staticmethod
    def _report(record):
        done = sum(s['status'] == 'complete' for s in record['steps'])
        return JobReport(record['id'], record['status'], done, len(record['steps']),
                         tuple(s['result'] for s in record['steps'] if s['status'] == 'complete'),
                         record.get('message', ''))

    def get(self, identity):
        record, _ = self._load(identity)
        return self._report(record)

    def jobs(self):
        return tuple(self.get(identity) for identity in self.store.identities())

    def pause(self, identity):
        self._load(identity)
        self.store.control(identity, 'pause')

    def cancel(self, identity):
        self._load(identity)
        self.store.control(identity, 'cancel')

    def resume(self, identity, **kwargs):
        self._load(identity)
        self.store.control(identity, 'run')
        return self.run(identity, **kwargs)

    def _artifacts(self, result, scope, cancelled):
        raw = result.get('artifacts', []) if isinstance(result, dict) else []
        if not isinstance(raw, (list, tuple)):
            raise ValueError('Tool artifact manifest is invalid.')
        records = []
        total = 0
        for item in raw:
            value = item.get('path') if isinstance(item, dict) else item
            path = local_path(value)
            if scope.output_root is None or not path.is_relative_to(local_path(scope.output_root)):
                raise PermissionError('A tool returned an artifact outside the selected output folder.')
            size = regular(path).st_size
            total += size
            if total > scope.max_bytes:
                raise ValueError('Produced artifacts exceed the approved byte budget.')
            records.append({'path': str(path), 'size': size, 'sha256': _hash_file(path, cancelled)})
        return records

    def _verify_completed(self, result, scope, cancelled):
        if not isinstance(result, dict):
            raise ValueError('Completed tool result is damaged.')
        before = result.get('artifacts', [])
        current = self._artifacts(result, scope, cancelled)
        if current != before:
            raise ValueError('A completed artifact changed; the job was not silently re-rendered.')

    def run(self, identity, *, cancelled=lambda: False, progress=None):
        with self.store.lock(identity):
            record, plan = self._load(identity)
            started = monotonic()
            def stopped():
                return (bool(cancelled()) or self.store.control(identity) != 'run'
                        or monotonic() - started > plan.scope.max_seconds)
            def persist():
                self.store.save(record)
            record['status'] = 'running'
            record['message'] = ''
            persist()
            try:
                for index, (step, checkpoint) in enumerate(zip(plan.steps, record['steps'])):
                    if stopped():
                        raise InterruptedError('Assistant job stopped; completed steps are preserved.')
                    if record['workspace']:
                        with self.app.read(record['workspace']):
                            pass
                    if checkpoint['status'] == 'complete':
                        self._verify_completed(checkpoint['result'], plan.scope, stopped)
                        continue
                    checkpoint['status'] = 'running'
                    checkpoint['message'] = ''
                    persist()
                    if progress:
                        progress(index, len(plan.steps), step.tool)
                    result = self._execute(step, plan.scope, record, checkpoint, persist, stopped, progress, index)
                    result = _plain(result)
                    if not isinstance(result, dict):
                        raise RuntimeError('Tool returned an invalid result.')
                    result['artifacts'] = self._artifacts(result, plan.scope, stopped)
                    checkpoint['result'] = result
                    checkpoint['status'] = 'complete'
                    persist()
                record['status'] = 'complete'
                record['message'] = 'All selected operations completed and returned artifacts were verified.'
            except InterruptedError as exc:
                command = self.store.control(identity)
                record['status'] = 'cancelled' if command == 'cancel' else 'paused'
                record['message'] = str(exc)
            except Exception as exc:
                record['status'] = 'attention' if isinstance(exc, (FileExistsError, PermissionError)) else 'failed'
                # No credentials enter local tools or exceptions; remote adapters redact separately.
                record['message'] = str(exc)[:2000]
                for checkpoint in record['steps']:
                    if checkpoint['status'] == 'running':
                        checkpoint['status'] = 'failed'
                        checkpoint['message'] = record['message']
            persist()
            if progress:
                progress(sum(s['status'] == 'complete' for s in record['steps']), len(plan.steps), record['status'])
            return self._report(record)

    def _input_files(self, path, scope, cancelled):
        accepted = []
        total = 0
        def add(candidate):
            nonlocal total
            candidate = scope.input(candidate)
            if candidate.suffix.lower() not in ('.txt', '.pdf', '.docx', '.epub'):
                return
            size = regular(candidate).st_size
            total += size
            if total > scope.max_bytes or len(accepted) >= scope.max_files:
                raise ValueError('Import exceeds the selected file or byte budget; narrow the folder or raise its explicit budget.')
            accepted.append(candidate)
        if path.is_file():
            add(path)
        else:
            for directory, dirs, files in os.walk(path, followlinks=False):
                if cancelled():
                    raise InterruptedError('Folder scan stopped.')
                scope.input(directory)
                for name in dirs:
                    scope.input(Path(directory) / name)
                for name in sorted(files):
                    if cancelled():
                        raise InterruptedError('Folder scan stopped.')
                    add(Path(directory) / name)
        if not accepted:
            raise ValueError('No supported PDF, DOCX, EPUB or UTF-8 TXT files were found.')
        return tuple(sorted(accepted))

    def _execute(self, step, scope, record, checkpoint, persist, cancelled, progress, index):
        args = step.arguments
        workspace = record['workspace']
        intent = checkpoint['intent']
        if step.tool == 'library.create':
            from ..application import _name
            from ..catalog.membership import _metadata
            identity = uuid5(NAMESPACE_URL, 'carr-assistant:' + record['id'] + ':library').hex
            name = _name(args['name'])
            description = _metadata(args.get('description', '')) or ''
            # Same application-authorized transaction and invariants as create_workspace,
            # with a deterministic identity to close the action/checkpoint crash window.
            with self.app.write() as db:
                row = db.execute('SELECT name,description,archived FROM workspace_meta WHERE workspace=?', (identity,)).fetchone()
                if row is None:
                    db.execute('INSERT INTO workspace(id) VALUES(?)', (identity,))
                    db.execute('INSERT INTO workspace_meta(workspace,name,description,archived) VALUES(?,?,?,0)',
                               (identity, name, description))
                elif tuple(row) != (name, description, 0):
                    raise ValueError('The previously created library changed; review this job.')
            record['workspace'] = identity
            persist()
            return {'workspace': identity, 'name': name}
        if step.tool == 'library.list':
            if scope.workspace_id:
                return {'libraries': [_plain(x) for x in self.app.workspaces() if x.id == scope.workspace_id]}
            return {'libraries': [_plain(x) for x in self.app.workspaces()]}
        if step.tool == 'library.catalog':
            rows = self.app.browse(workspace, **args)
            return {'books': _plain(rows), 'next_after': rows[-1].edition_id if rows else None}
        if step.tool.startswith('library.import'):
            path = scope.input(args['path'])
            if 'batch' not in intent:
                files = self._input_files(path, scope, cancelled)
                batch = self.app.plan_import_batch(workspace, files, recursive=False, cancelled=cancelled)
                intent['batch'] = batch['batch_id']
                persist()
            # A checkpoint may name an existing queue, but it may never enlarge
            # the file/folder authorization of the original reviewed plan.
            saved_batch = self.app.import_batch(workspace, intent['batch'])
            if len(saved_batch['rows']) > scope.max_files:
                raise PermissionError('The resumed queue exceeds this plan\'s file budget.')
            size = 0
            for row in saved_batch['rows']:
                source = scope.input(row['source'])
                if source != path and (not path.is_dir() or not source.is_relative_to(path)):
                    raise PermissionError('The resumed import queue contains a file outside the selected input.')
                manifest = row.get('manifest')
                if manifest:
                    size += manifest['size']
            if size > scope.max_bytes:
                raise PermissionError('The resumed queue exceeds this plan\'s byte budget.')
            batch = self.app.run_import_batch(workspace, intent['batch'], cancelled=cancelled,
                                             timeout=min(120.0, scope.max_seconds), retry_failed=True)
            if batch['state'] != 'complete':
                if cancelled():
                    raise InterruptedError('Import paused; its durable queue will resume.')
                raise RuntimeError('Import requires attention in Import Center; no unreviewed text was approved. Batch ' + intent['batch'])
            return {'batch_id': intent['batch'], 'counts': batch['counts'],
                    'editions': [r['edition_id'] for r in batch['rows'] if r['state'] == 'complete']}
        if step.tool == 'library.search':
            return {'results': _plain(self.app.search(workspace, cancelled=cancelled,
                                                      timeout=min(60.0, scope.max_seconds), **args)),
                    'notice': 'Search excerpts retain source verification status; assistant output does not approve quotations.'}
        if step.tool == 'library.extract':
            from ..reader.reading_desk import ReadingDesk
            desk = ReadingDesk(self.app, workspace, args['edition'], cancelled=cancelled)
            target = scope.output(args['name'])
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            local_path(target.parent)
            if target.exists():
                expected = intent.get('sha256')
                if expected and _hash_file(target, cancelled) == expected:
                    return {'artifacts': [str(target)], 'source_revision': desk.revision, 'reading_copy': True}
                raise FileExistsError('The extraction destination exists and is not this completed job output.')
            with NamedTemporaryFile(dir=target.parent, prefix='.assistant-extract-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    total = 0
                    for i in range(1, len(desk.pages) + 1):
                        raw = desk.page(i).raw_text.encode('utf-8')
                        total += len(raw) + (2 if i > 1 else 0)
                        if total > scope.max_bytes:
                            raise ValueError('Reading copy exceeds the approved output budget.')
                        if i > 1:
                            stream.write(b'\n\n')
                        stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                    intent['sha256'] = _hash_file(temporary, cancelled)
                    persist()
                except BaseException:
                    stream.close()
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                os.link(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return {'artifacts': [str(target)], 'source_revision': desk.revision,
                    'reading_copy': True, 'notice': 'Exact extracted segments joined by blank lines; quotation approval is unchanged.'}
        if step.tool == 'library.backup':
            if not scope.allow_full_backup:
                raise PermissionError('Full-library backup was not authorized.')
            target = scope.output(args['name'])
            if target.exists():
                raise FileExistsError('Backup output already exists. Review its manifest before submitting a new backup.')
            checksum = self.app.backup(target)
            return {'directory': str(target), 'manifest_sha256': checksum,
                    'artifacts': [str(target / 'manifest.json')], 'includes': 'All workspaces in this application data directory'}
        if step.tool == 'audio.produce':
            if self.production is None:
                raise RuntimeError('Audio production is unavailable.')
            settings = dict(args)
            settings['operation_id'] = uuid5(NAMESPACE_URL, 'carr-assistant:' + record['id'] + ':' + str(index)).hex
            def production_progress(message):
                if progress:
                    progress(index, len(record['steps']), message)
            result = self.production(settings, local_path(scope.output_root), cancelled, production_progress)
            if not isinstance(result, dict) or not result.get('artifacts'):
                raise RuntimeError('Audio production did not return completed audio artifacts.')
            return result
        raise ValueError('Unknown assistant tool.')
