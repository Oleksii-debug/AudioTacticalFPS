"""Bounded, durable execution of approved typed local assistant plans."""
from dataclasses import asdict, is_dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Event
from time import monotonic
from uuid import NAMESPACE_URL, uuid4, uuid5

from .models import JobReport, Plan, Scope, Step, canonical, local_path, text
from .planner import preview
from .state import JobStore, now, regular
from .backup_receipt import capture_backup_receipt, verify_backup_receipt


from .validation import validate_arguments as _check_arguments


def _hash_file(path, cancelled=lambda: False):
    from ..audio.jobs import regular_file
    path = local_path(path)
    before = regular_file(path)
    def stamp(value):
        return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
                value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    # An unbuffered second pass compares actual content, including same-size
    # writes on filesystems with coarse/deferred modification timestamps.
    # Windows st_ctime is creation time, not a reliable content-change counter.
    with os.fdopen(os.open(path, flags), 'rb', buffering=0) as stream:
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError('Assistant artifact changed while opening.')
        def read_digest():
            remaining = before.st_size
            digest = sha256()
            while True:
                block = stream.read(min(1024 * 1024, remaining + 1))
                if cancelled():
                    raise InterruptedError('Assistant operation was stopped.')
                if not block:
                    break
                remaining -= len(block)
                if remaining < 0:
                    raise ValueError('Assistant artifact grew while verifying.')
                digest.update(block)
            if remaining or stamp(os.fstat(stream.fileno())) != stamp(before):
                raise ValueError('Assistant artifact changed while verifying.')
            return digest.hexdigest()
        checksum = read_digest()
        stream.seek(0)
        if read_digest() != checksum:
            raise ValueError('Assistant artifact content changed while verifying.')
    if stamp(regular_file(path)) != stamp(before):
        raise ValueError('Assistant artifact changed while verifying.')
    return checksum


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
        from ..search.federated import FederatedSearch, _data_identity
        self._bound_app = app
        self._identity = _data_identity(app)
        root = local_path(app.root)
        info = root.stat()
        self._root_identity = (info.st_dev, info.st_ino)
        self.store = JobStore(state_dir or (app.root / 'state' / 'assistant'))
        if not self.store.directory.is_relative_to(app.root):
            raise ValueError('Assistant state must remain inside this application data directory.')
        self.production = production
        self._search = FederatedSearch(app)
        self._approvals = {}

    def _check_binding(self):
        from ..search.federated import _data_identity
        if self.app is not self._bound_app or _data_identity(self.app) != self._identity:
            raise PermissionError('The application data directory changed. Open the assistant again.')
        root = local_path(self.app.root)
        info = root.stat()
        if (info.st_dev, info.st_ino) != self._root_identity:
            raise PermissionError('The application data directory was replaced. Open the assistant again.')

    def _authorize(self, record, expected):
        expected = expected if expected is not None else self._approvals.get(record['id'])
        if (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)
                or expected != record['plan_sha256']):
            raise PermissionError('Review the exact saved plan and approve its fingerprint before resuming.')
        return expected

    def preview(self, instruction, scope):
        plan = preview(instruction, scope)
        # New reviews state the choice explicitly. Saved schema-1 plans keep
        # their original arguments and digest when read or resumed.
        steps = tuple(Step(step.tool, {'include_generated': False, **step.arguments})
                      if step.tool == 'library.backup' else step for step in plan.steps)
        plan = Plan(plan.scope, steps, plan.summary)
        self.validate(plan)
        return plan

    def _output(self, scope, name):
        target = scope.output(name)
        if target.is_relative_to(self.app.root) or self.app.root.is_relative_to(target):
            raise PermissionError('Assistant outputs must remain outside the private library.')
        return target

    def validate(self, plan):
        self._check_binding()
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
                target = self._output(plan.scope, step.arguments['name'])
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

    def submit(self, plan, *, approved_plan_sha256=None):
        if approved_plan_sha256 is not None and plan.digest != approved_plan_sha256:
            raise PermissionError('The previewed assistant plan changed. Preview it again.')
        # Retain no mutable argument dictionaries supplied by a caller.
        plan = Plan.from_dict(plan.to_dict())
        self.validate(plan)
        identity = uuid4().hex
        record = {'schema': 'carr-assistant-job/1', 'id': identity, 'data_dir': str(self.app.root),
                  'plan': plan.to_dict(), 'plan_sha256': plan.digest, 'created': now(), 'updated': now(),
                  'status': 'queued', 'message': '', 'workspace': plan.scope.workspace_id,
                  'steps': [{'status': 'pending', 'intent': {}, 'result': None, 'message': ''}
                            for _ in plan.steps]}
        self.store.save(record)
        self._approvals[identity] = record['plan_sha256']
        return identity

    def _load(self, identity):
        self._check_binding()
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
    def _report(record, *, fresh=False):
        done = sum(s['status'] == 'complete' for s in record['steps'])
        results = []
        for step, checkpoint in zip(record['plan']['steps'], record['steps']):
            if checkpoint['status'] != 'complete':
                continue
            result = json.loads(canonical(checkpoint['result']))
            if step['tool'] == 'library.search' and not fresh:
                result['source_status'] = 'historical_unchecked'
                result['notice'] = 'Saved search report. Run this approved search again to verify the current sources.'
                for hit in result.get('results', []):
                    hit['verified_at_capture'] = hit.get('verified', False)
                    hit['verified'] = False
            results.append(result)
        return JobReport(record['id'], record['status'], done, len(record['steps']),
                         tuple(results),
                         record.get('message', ''))

    def get(self, identity):
        self._check_binding()
        record = self.store.load(identity)
        if record.get('requires_reapproval') or record.get('data_dir') != str(self.app.root):
            return JobReport(identity, 'attention', 0, len(record['steps']), (),
                'Restored plan requires review and a new approval; its original paths were preserved.')
        record, _ = self._load(identity)
        return self._report(record)

    def jobs(self):
        self._check_binding()
        return tuple(self.get(identity) for identity in self.store.identities())

    def review(self, identity):
        """Return the exact original plan for an explicit Run/resume decision."""
        _, plan = self._load(identity)
        return plan

    def pause(self, identity):
        self._load(identity)
        self.store.control(identity, 'pause')

    def cancel(self, identity):
        self._load(identity)
        self.store.control(identity, 'cancel')

    def resume(self, identity, *, approved_plan_sha256=None, **kwargs):
        record, _ = self._load(identity)
        expected = self._authorize(record, approved_plan_sha256)
        self.store.control(identity, 'run')
        return self.run(identity, approved_plan_sha256=expected, **kwargs)

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
            if path.is_relative_to(self.app.root):
                raise PermissionError('A tool returned private library state as an output artifact.')
            # Read-only render/cache artifacts may be intentional hard links.
            # Private assistant checkpoint files retain their stricter rule.
            size = regular_file(path).st_size
            total += size
            if total > scope.max_bytes:
                raise ValueError('Produced artifacts exceed the approved byte budget.')
            records.append({'path': str(path), 'size': size, 'sha256': _hash_file(path, cancelled)})
        return records

    def _verify_completed(self, result, scope, cancelled, *, step=None):
        if not isinstance(result, dict):
            raise ValueError('Completed tool result is damaged.')
        if step is not None and step.tool in ('library.extract', 'library.backup'):
            target = self._output(scope, step.arguments['name'])
            expected = target / 'manifest.json' if step.tool == 'library.backup' else target
            if [item.get('path') for item in result.get('artifacts', [])] != [str(expected)]:
                raise PermissionError('The completed output differs from this exact approved task.')
            if step.tool == 'library.backup' and result.get('directory') != str(target):
                raise PermissionError('The backup destination differs from this exact approved task.')
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
            if step is not None and step.tool == 'library.backup':
                self._check_backup_policy(directory, result['manifest_sha256'],
                                          step.arguments.get('include_generated', False))

    def run(self, identity, *, cancelled=lambda: False, progress=None, approved_plan_sha256=None):
        self._check_binding()
        with self.store.lock(identity):
            record, plan = self._load(identity)
            self._authorize(record, approved_plan_sha256)
            if not callable(cancelled):
                raise TypeError('A cancellation callback is required.')
            started = monotonic()
            halted = Event()
            def stopped():
                if halted.is_set():
                    return True
                # Check again after the caller callback: it may itself switch
                # the host root or invalidate a native parent window.
                requested = bool(cancelled())
                try:
                    self._check_binding()
                except (OSError, ValueError, PermissionError):
                    halted.set()
                    return True
                if (requested or self.store.control(identity) != 'run'
                        or monotonic() - started >= plan.scope.max_seconds):
                    halted.set()
                return halted.is_set()
            def persist():
                self._check_binding()
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
                            if step.tool in ('library.search', 'library.list', 'library.catalog'):
                                # A persisted excerpt is a historical receipt,
                                # never a reusable live search capability.
                                result = self._execute(step, plan.scope, record, checkpoint,
                                                       persist, stopped, progress, index)
                                checkpoint['result'] = {**_plain(result), 'artifacts': []}
                                persist()
                            else:
                                self._verify_completed(checkpoint['result'], plan.scope, stopped, step=step)
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
                    # A backup's complete tree has already been verified before
                    # its atomic publication. Keep its committed receipt even
                    # if cancellation arrives at that final boundary.
                    check = (lambda: False) if step.tool == 'library.backup' else stopped
                    result['artifacts'] = self._artifacts(result, plan.scope, check)
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
            return self._report(record, fresh=record['status'] == 'complete')

    def _import_inventory(self, path, scope, cancelled):
        """Admit the bounded inventory without opening any document bytes.

        Reuse Import Center's walker, traversal bounds and fingerprint. A
        selected folder stays one root, even when it contains over 256 files.
        These counters cover the whole scan, rather than resetting each page.
        """
        from ..ingest.batch_scan import _Page, _scan_paths

        class Refused(Exception):
            pass

        class Admission(_Page):
            files = 0
            size = 0

            def admit(self, candidate, root, *, source=True):
                if source:
                    try:
                        candidate = scope.input(candidate)
                        size = regular(candidate).st_size
                    except (OSError, ValueError) as exc:
                        # The shared walker turns ordinary source errors into
                        # display rows. An authorization refusal must escape it.
                        raise Refused(str(exc)) from exc
                    self.files += 1
                    self.size += size
                    if self.files > scope.max_files or self.size > scope.max_bytes:
                        raise Refused('Import exceeds the selected file or byte budget; '
                                      'narrow the folder or raise its explicit budget.')
                return super().admit(candidate, root, source=source)

        inventory = Admission(inventory_only=True)
        try:
            _scan_paths((path,), recursive=path.is_dir(), cancelled=cancelled, _page=inventory)
        except Refused as exc:
            raise PermissionError(str(exc)) from exc
        if inventory.unscanned:
            raise PermissionError('The selected folder was not completely scanned within the '
                                  'entry/path limits. Select smaller folders in Import Center; '
                                  'no sources were imported by this operation.')
        if not inventory.files:
            raise ValueError('No supported local document files were found in the selected scope.')
        return {'sha256': inventory.digest.hexdigest(), 'files': inventory.files,
                'bytes': inventory.size}

    def _import(self, scope, workspace, path, intent, persist, cancelled):
        from ..ingest.batches import MAX_BATCHES, STATES

        # Old assistant checkpoints may contain one V1 queue or a V2 queue
        # whose roots were individual files. Keep them readable and continue to
        # check every row against the exact approved file/folder below.
        admission = None
        if 'batch' not in intent or 'import_scan' in intent:
            admission = self._import_inventory(path, scope, cancelled)
            if 'import_scan' in intent and intent['import_scan'] != admission:
                raise PermissionError('Source inventory changed. Review a new import plan; '
                                      'previously completed pages were preserved.')
        if 'batch' not in intent:
            batch = self.app.plan_import_batch(workspace, (path,), recursive=path.is_dir(),
                                               cancelled=cancelled)
            if batch['scan']['inventory_sha256'] != admission['sha256']:
                raise PermissionError('Source inventory changed before import. Review a new plan.')
            intent.update(batch=batch['batch_id'], import_scan=admission)
            persist()

        batch_id = intent['batch']
        counts = dict.fromkeys(STATES, 0)
        editions, pages, seen_sources = [], [], set()
        total_files = total_bytes = 0
        previous = None
        while True:
            if cancelled():
                raise InterruptedError('Import paused; its durable pages will resume.')
            if batch_id in pages or len(pages) >= MAX_BATCHES:
                raise PermissionError('The import page chain exceeds its bounded queue capacity.')
            batch = self.app.import_batch(workspace, batch_id)
            scan = batch.get('scan')
            if admission is not None and (scan is None
                    or scan['roots'] != [str(path)] or batch['recursive'] != path.is_dir()
                    or scan['inventory_sha256'] != admission['sha256']):
                raise PermissionError('The import page differs from the selected source inventory.')
            if previous is not None:
                old = previous['scan']
                if (scan is None or scan['previous_batch_id'] != previous['batch_id']
                        or scan['offset'] != old['next_offset']
                        or scan['roots'] != old['roots']
                        or scan['inventory_sha256'] != old['inventory_sha256']
                        or scan['options_sha256'] != old['options_sha256']):
                    raise PermissionError('The import continuation differs from its approved page chain.')
            elif scan is not None and (scan['previous_batch_id'] is not None or scan['offset'] != 0):
                raise PermissionError('An assistant import must start at its original first page.')
            if scan is not None and scan['unscanned']:
                raise PermissionError('The saved scan has unscanned folders. Review smaller scopes '
                                      'in Import Center; this operation is incomplete.')
            for row in batch['rows']:
                if cancelled():
                    raise InterruptedError('Import paused; its durable pages will resume.')
                source = scope.input(row['source'])
                if source != path and (not path.is_dir() or not source.is_relative_to(path)):
                    raise PermissionError('The resumed import queue contains a file outside the selected input.')
                manifest = row.get('manifest')
                if manifest:
                    if source in seen_sources:
                        raise PermissionError('The import page chain repeats a source.')
                    seen_sources.add(source)
                    total_files += 1
                    total_bytes += manifest['size']
            if total_files > scope.max_files or total_bytes > scope.max_bytes:
                raise PermissionError('The resumed queue exceeds this plan\'s file or byte budget.')
            if batch['state'] != 'complete':
                batch = self.app.run_import_batch(workspace, batch_id, cancelled=cancelled,
                    timeout=min(120.0, scope.max_seconds), retry_failed=True)
            if batch['state'] != 'complete':
                if cancelled():
                    raise InterruptedError('Import paused; its durable pages will resume.')
                raise PermissionError('Import requires attention in Import Center; no unreviewed '
                                      'text was approved. Batch ' + batch_id)
            pages.append(batch_id)
            for state, count in batch['counts'].items():
                counts[state] += count
            editions.extend(row['edition_id'] for row in batch['rows'] if row['state'] == 'complete')
            if scan is None or scan['next_offset'] is None:
                break
            if cancelled():
                raise InterruptedError('Import paused; its durable pages will resume.')
            try:
                following = self.app.next_import_scan_page(workspace, batch_id, cancelled=cancelled)
            except ValueError as exc:
                raise PermissionError('Import continuation requires review in Import Center: ' + str(exc)) from exc
            if following['batch_id'] != scan['next_batch_id']:
                raise PermissionError('The saved import continuation identity changed.')
            previous, batch_id = batch, following['batch_id']
        if admission is not None and (total_files != admission['files'] or total_bytes != admission['bytes']):
            raise PermissionError('The completed pages do not cover the whole admitted source inventory.')
        return {'batch_id': intent['batch'], 'batch_ids': pages, 'counts': counts,
                'editions': editions, 'scan_complete': True}

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
                if cancelled():
                    raise InterruptedError('Library creation cancelled before commit.')
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
            if step.tool.endswith('_folder') != path.is_dir():
                raise PermissionError('The selected input no longer matches the approved file/folder operation.')
            return self._import(scope, workspace, path, intent, persist, cancelled)
        if step.tool == 'library.search':
            options = dict(args)
            limit = options.pop('limit', 50)
            try:
                session = self._search.search((workspace,), cancelled=cancelled,
                    timeout=min(60.0, scope.max_seconds), limit_per_library=limit, **options)
                rows = []
                for hit in session.hits:
                    value = _plain(hit.result)
                    value.pop('ref', None)  # A session handle is never a portable citation.
                    if hit.result.verified:
                        context = self._search.context(hit, cancelled=cancelled)
                        page = context.page(0)
                        anchor = page.anchor
                        if (anchor.identity.workspace != workspace
                                or anchor.identity.edition_id != hit.result.edition_id
                                or anchor.identity.revision != hit.result.revision
                                or anchor.quote != hit.result.quote or page.citation != hit.result.citation):
                            raise ValueError('Search citation changed during source verification.')
                        value['source_citation'] = _plain(anchor)
                    rows.append(value)
                if cancelled():
                    raise InterruptedError('Search cancelled before displaying results.')
                return {'results': rows, 'source_status': 'verified_this_run',
                        'notice': 'Each verified excerpt was resolved from its exact source. '
                                  'Saved reports are historical and do not authorize evidence actions.'}
            except TimeoutError as exc:
                # Search shares one bounded exception for cancellation and
                # deadlines. A withdrawn assistant window/scope must retain a
                # resumable paused task, while an actual search timeout still
                # reports failure when no cancellation was requested.
                if cancelled():
                    raise InterruptedError('Search paused before displaying results.') from exc
                raise
            finally:
                self._search.clear()
        if step.tool == 'library.extract':
            from ..reader.reading_desk import ReadingDesk
            desk = ReadingDesk(self.app, workspace, args['edition'], cancelled=cancelled)
            if intent.get('extraction_revision', desk.revision) != desk.revision:
                raise PermissionError('The extraction source revision changed. Review a new plan.')
            target = self._output(scope, args['name'])
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
                    intent['extraction_revision'] = desk.revision
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
            target = self._output(scope, args['name'])
            include_generated = args.get('include_generated', False)
            if target.exists():
                saved = intent.get('backup_receipt')
                if saved is None or intent.get('backup_target') != str(target):
                    raise FileExistsError('Backup output already exists. Review its manifest before submitting a new backup.')
                receipt = verify_backup_receipt(target, saved, max_bytes=scope.max_bytes,
                    max_files=scope.max_files, cancelled=cancelled)
                self._check_backup_policy(target, receipt.manifest_sha256, include_generated)
                return self._backup_result(target, receipt, include_generated=include_generated)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            local_path(target.parent)
            # The existing complete backup service owns the database, reader
            # state and future sidecars. Stage its full result and verify every
            # byte before publishing inside the approved output directory.
            with TemporaryDirectory(prefix='.assistant-backup-', dir=target.parent) as temporary:
                staged = Path(temporary) / 'backup'
                if cancelled():
                    raise InterruptedError('Backup cancelled before creation.')
                checksum = self.app.backup(staged, max_bytes=scope.max_bytes,
                    include_generated=include_generated, cancelled=cancelled)
                receipt = capture_backup_receipt(staged, checksum, max_bytes=scope.max_bytes,
                    max_files=scope.max_files, cancelled=cancelled)
                self._check_backup_policy(staged, checksum, include_generated)
                if cancelled():
                    raise InterruptedError('Backup cancelled before publication.')
                intent.update(backup_receipt=receipt.to_dict(), backup_target=str(target))
                persist()
                if cancelled():
                    raise InterruptedError('Backup cancelled before publication.')
                from ..file_publication import publish_directory_no_replace
                from ..library_backup import _sync_directory
                publish_directory_no_replace(staged, target)
                warnings = []
                try:
                    _sync_directory(target.parent)
                except OSError as exc:
                    warnings.append('Backup was published; directory durability needs attention: ' + type(exc).__name__)
            return self._backup_result(target, receipt, include_generated=include_generated,
                                       warnings=warnings)
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

    @staticmethod
    def _check_backup_policy(directory, checksum, include_generated):
        # The manifest is authenticated against the already captured producer
        # receipt. Reuse the common versioned codec, including legacy readers.
        from ..library_backup import _read_manifest
        from ..backup_sidecars import normalize_manifest
        manifest = _read_manifest(directory, checksum)
        if normalize_manifest(manifest).generated_audio_included != include_generated:
            raise ValueError('The backup manifest differs from the approved generated-audio choice.')

    @staticmethod
    def _backup_result(target, receipt, *, include_generated, warnings=()):
        return {'directory': str(target), 'manifest_sha256': receipt.manifest_sha256,
                'backup_receipt': receipt.to_dict(), 'warnings': list(warnings),
                'generated_audio_included': include_generated,
                'artifacts': [str(target / 'manifest.json')],
                'includes': 'All workspaces in this application data directory'}
