"""Bounded, durable execution of approved typed local assistant plans."""
from dataclasses import asdict, is_dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic
from uuid import NAMESPACE_URL, uuid4, uuid5

from .models import JobReport, Plan, Scope, canonical, local_path, text
from .planner import preview
from .state import JobStore, now, regular
from .backup_receipt import capture_backup_receipt, verify_backup_receipt


from .validation import validate_arguments as _check_arguments


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
        managed = app.root / 'state' / 'assistant'
        if state_dir is not None and local_path(state_dir, must_exist=False) != managed:
            raise ValueError('Assistant state must use this library\'s managed state/assistant directory.')
        self.store = JobStore(managed, writer_root=app.root)
        self.production = production

    def preview(self, instruction, scope):
        plan = preview(instruction, scope)
        self.validate(plan)
        return plan

    def validate(self, plan):
        if not isinstance(plan, Plan):
            raise TypeError('A typed plan is required.')
        workspace = plan.scope.workspace_id
        if plan.scope.output_root is not None and plan.scope.output_root.is_relative_to(self.app.root):
            raise PermissionError('Choose an output folder outside the private library.')
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
                if callable(getattr(self.production, 'plan', None)):
                    self.production.plan(step.arguments, max_bytes=plan.scope.max_bytes)
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
        if record.get('requires_reapproval') or record.get('data_dir') != str(self.app.root):
            raise PermissionError('This job belongs to another data directory; review and submit its plan again.')
        plan = Plan.from_dict(record['plan'])
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
        record = self.store.load(identity)
        if record.get('requires_reapproval') or record.get('data_dir') != str(self.app.root):
            return JobReport(identity, 'attention', 0, len(record['steps']), (),
                'Restored plan requires review and a new approval; its original paths were preserved.')
        try:
            record, _ = self._load(identity)
        except (OSError, ValueError) as exc:
            # Listing does not grant execution authority. Keep a structurally
            # validated checkpoint visible when its saved paths disappear or
            # become unsafe; run/resume still require the unchanged _load.
            return self._report({**record, 'status': 'attention', 'message': (
                'Saved paths are unavailable or no longer permitted. Restore the original '
                'folders or review and submit a new plan. ' + str(exc))[:2000]})
        return self._report(record)

    def jobs(self):
        reports = []
        for identity in self.store.identities():
            try:
                reports.append(self.get(identity))
            except (ValueError, OSError) as exc:
                # One damaged checkpoint must not hide the other saved jobs.
                reports.append(JobReport(identity, 'attention', 0, 0, (),
                    'Saved job is damaged or unavailable; it was not run. ' + str(exc)[:1000]))
        return tuple(reports)

    def review_instruction(self, identity):
        """Copy saved steps into an unapproved draft, without transferring scope.

        The caller must select current input/output/library permissions and run
        the normal preview/submit flow. Original path arguments remain literal;
        this does not edit a checkpoint or authorize resuming it elsewhere.
        """
        saved = self.store.load(identity)['plan']
        return json.dumps({key: saved[key] for key in ('schema', 'steps', 'summary')},
                          ensure_ascii=False, indent=2)

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
        from ..audio.jobs import regular_file
        raw = result.get('artifacts', []) if isinstance(result, dict) else []
        if not isinstance(raw, (list, tuple)) or len(raw) > scope.max_files:
            raise ValueError('Tool artifact manifest is invalid.')
        records = []
        total = 0
        for item in raw:
            value = item.get('path') if isinstance(item, dict) else item
            path = local_path(value)
            if scope.output_root is None or not path.is_relative_to(local_path(scope.output_root)):
                raise PermissionError('A tool returned an artifact outside the selected output folder.')
            # Read-only render/cache artifacts may be intentional hard links.
            # Private assistant checkpoint files retain their stricter rule.
            size = regular_file(path).st_size
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
        if 'backup_receipt' in result:
            directory = local_path(result.get('directory', ''))
            if scope.output_root is None or not directory.is_relative_to(local_path(scope.output_root)):
                raise PermissionError('The completed backup is outside the approved output directory.')
            verify_backup_receipt(directory, result['backup_receipt'], max_bytes=scope.max_bytes,
                                  max_files=scope.max_files, cancelled=cancelled)

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
                        try:
                            self._verify_completed(checkpoint['result'], plan.scope, stopped)
                        except Exception:
                            checkpoint['status'] = 'failed'
                            raise
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
        # The shared intake registry includes additional document formats when
        # that component is installed. There is no independent suffix catalogue.
        from ..ingest.retained_assets import SUFFIXES
        suffixes = set(SUFFIXES.values())
        try:
            from ..extract.document_formats import FORMAT_BY_SUFFIX
        except ModuleNotFoundError as exc:
            if exc.name != 'carr_library.extract.document_formats':
                raise
        else:
            suffixes.update(FORMAT_BY_SUFFIX)
        def add(candidate):
            nonlocal total
            candidate = scope.input(candidate)
            if candidate.suffix.lower() not in suffixes:
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
            raise ValueError('No supported local document files were found in the selected scope.')
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
            page = self.app.search_page(workspace, cancelled=cancelled,
                timeout=min(60.0, scope.max_seconds), **args)
            return {'results': _plain(page.results), 'next_cursor': page.next_cursor,
                    'page_number': page.page_number, 'ordering': page.ordering,
                    'source_pages_examined': page.source_pages_examined,
                    'total_source_pages': page.total_source_pages,
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
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            local_path(target.parent)
            # The existing complete backup service owns the database, reader
            # state and future sidecars. Stage its full result and verify every
            # byte before publishing inside the approved output directory.
            with TemporaryDirectory(prefix='.assistant-backup-', dir=target.parent) as temporary:
                staged = Path(temporary) / 'backup'
                if cancelled():
                    raise InterruptedError('Backup cancelled before creation.')
                checksum = self.app.backup(staged, cancelled=cancelled)
                receipt = capture_backup_receipt(staged, checksum, max_bytes=scope.max_bytes,
                    max_files=scope.max_files, cancelled=cancelled)
                if cancelled():
                    raise InterruptedError('Backup cancelled before publication.')
                from ..library_backup import _sync_directory
                target.mkdir(mode=0o700)
                # Exclusive mkdir reserves the new directory; manifest last
                # remains the existing backup service's completion boundary.
                for child in sorted(staged.iterdir()):
                    if child.name != 'manifest.json':
                        child.rename(target / child.name)
                _sync_directory(target)
                (staged / 'manifest.json').rename(target / 'manifest.json')
                _sync_directory(target)
                _sync_directory(target.parent)
            return {'directory': str(target), 'manifest_sha256': checksum,
                    'backup_receipt': receipt.to_dict(),
                    'artifacts': [str(target / 'manifest.json')], 'includes': 'All workspaces in this application data directory'}
        if step.tool == 'audio.produce':
            if self.production is None:
                raise RuntimeError('Audio production is unavailable.')
            settings = dict(args)
            settings['operation_id'] = uuid5(NAMESPACE_URL, 'carr-assistant:' + record['id'] + ':' + str(index)).hex
            settings['_artifact_budget_bytes'] = scope.max_bytes
            def production_progress(message):
                if progress:
                    progress(index, len(record['steps']), message)
            result = self.production(settings, local_path(scope.output_root), cancelled, production_progress)
            if not isinstance(result, dict) or not result.get('artifacts'):
                raise RuntimeError('Audio production did not return completed audio artifacts.')
            return result
        raise ValueError('Unknown assistant tool.')
