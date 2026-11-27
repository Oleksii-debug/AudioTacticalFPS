"""Saved book reviews use only the current managed Studio and production engine."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import threading

from ..backup_files import optional_present
from ..reader.book_source import validate_source
from .book_plan import (BookPlan, SplitOptions, capture_plan, encode, _check, _identity)
from .book_store import BookStore, part_plan
from .book_export import output_parent, output_name, export_part
from .jobs import checked_directory, read_json, project_lock
from .managed import ManagedProjects
from .runtime import resolve_ffmpeg


@dataclass(frozen=True)
class BookReview:
    plan_sha256: str
    output_parent: str
    parent_identity: tuple
    destinations: tuple[str, ...]
    prepared: bool

    @property
    def fingerprint(self):
        return sha256(encode(asdict(self))).hexdigest()


class BookAudioService:
    def __init__(self, library, workspace, *, engine=None, ffmpeg=None, renderer=None):
        self.library, self.workspace = library, workspace
        self.store = BookStore(library.root)
        self.managed = ManagedProjects(library.root)
        self.engine, self.ffmpeg, self.renderer = engine, ffmpeg, renderer
        self._closed = threading.Event()
        with library.read(workspace):
            pass

    def close(self):
        self._closed.set()

    def load(self, identity):
        plan = self.store.load(identity)
        if plan.source['source_anchor']['workspace'] != self.workspace:
            raise ValueError('The saved book belongs to another library.')
        return plan

    def saved(self, *, cancelled=lambda: False):
        with self.library.read(self.workspace):
            pass
        return self.store.saved(workspace=self.workspace, cancelled=cancelled)

    def _stopping(self, cancelled):
        return self._closed.is_set() or bool(cancelled())

    def _validate(self, plan, cancelled, guard):
        _check(lambda: self._stopping(cancelled), guard)
        source = plan.source
        if source['source_anchor']['workspace'] != self.workspace:
            raise ValueError('The book source belongs to another workspace.')
        validate_source(self.library, source['text'], source['source_anchor'],
                        source['source_metadata'], source['source_topic'],
                        cancelled=lambda: self._stopping(cancelled), guard=guard)
        if plan.landmarks_json:
            from .book_landmarks import validate_current
            validate_current(self.library, source, plan.landmarks,
                             cancelled=lambda: self._stopping(cancelled), guard=guard)

    def save(self, plan, *, cancelled=lambda: False, guard=lambda: None):
        self._validate(plan, cancelled, guard)
        return self.store.save(plan, cancelled=cancelled,
            guard=lambda: self._validate(plan, cancelled, guard))

    def _document(self, plan, index):
        root, document, audio = self.managed.paths(plan.job_id(index))
        if not optional_present(document):
            return None
        payload = read_json(document, limit=64 * 1024**2)
        found, part = part_plan(self.library.root, payload)
        if found.fingerprint != plan.fingerprint or part != index:
            raise ValueError('The managed Studio part differs from this immutable review.')
        recovery = document.with_name('project.json.recovery.json')
        if optional_present(recovery):
            restored = read_json(recovery, limit=64 * 1024**2)
            if restored != payload:
                raise ValueError('An unsaved edit of this book part needs review in Audio Studio.')
        return payload

    def review(self, plan, destination, *, cancelled=lambda: False, guard=lambda: None):
        if not isinstance(plan, BookPlan):
            raise ValueError('Use a typed reviewed book plan.')
        self._validate(plan, cancelled, guard)
        parent, identity = output_parent(destination, self.library.root)
        prepared, names = True, []
        for index in range(1, len(plan.parts) + 1):
            _check(cancelled, guard)
            prepared = self._document(plan, index) is not None and prepared
            names.append(str(parent / output_name(plan, index)))
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError('The reviewed output names collide on a case-insensitive filesystem.')
        return BookReview(plan.fingerprint, str(parent), identity, tuple(names), prepared)

    def prepare(self, plan, destination, *, approval, cancelled=lambda: False, guard=lambda: None):
        reviewed = self.review(plan, destination, cancelled=cancelled, guard=guard)
        if approval != reviewed.fingerprint:
            raise ValueError('Review the exact split, source, voice and output names before Prepare.')
        self.save(plan, cancelled=cancelled, guard=guard)
        for index in range(1, len(plan.parts) + 1):
            self._validate(plan, cancelled, guard)
            if self._document(plan, index) is None:
                _, document, _ = self.managed.paths(plan.job_id(index))
                value = json.dumps(plan.document(index), ensure_ascii=False, sort_keys=True,
                                   indent=2, allow_nan=False) + '\n'
                self.managed.write(plan.job_id(index), document, value, cancelled=cancelled)
        return self.review(plan, destination, cancelled=cancelled, guard=guard)

    def _engine(self):
        if self.engine is None:
            from ..runtime_settings import configured_voice_registry
            self.engine = configured_voice_registry(self.library.root, ffmpeg=resolve_ffmpeg(self.ffmpeg))
        return self.engine

    def _saved_completion(self, plan, index, audio, *, broker, cancelled, source_guard):
        """Read accepted current files, never discover archived engine paths.

        The caller holds this managed project's native production lock. A
        legacy marker without the typed-plan receipt still needs the ordinary
        live renderer's exact engine/cache validation before it can be upgraded.
        """
        from ..operations import ResourceRequest
        from .models import digest
        from .production import _public_result, _verified_output
        journal = audio / 'render-journal.json'
        if not optional_present(journal):
            return None
        state = read_json(journal, limit=1024**2)
        if type(state) is not dict:
            raise ValueError('The recorded book production state is invalid.')
        if state.get('state') != 'complete':
            return None
        run_key = state.get('run_key')
        expected = plan.project(index)
        if (type(state.get('schema_version')) is not int or state['schema_version'] != 1
                or type(run_key) is not str or len(run_key) != 64
                or any(char not in '0123456789abcdef' for char in run_key)
                or state.get('source_sha256') != expected.source_sha256):
            raise ValueError('The recorded book completion has a different source identity.')
        stored = read_json(audio / ('plan-' + run_key + '.json'), limit=32 * 1024**2)
        plan_sha256 = digest(expected.to_dict())
        if type(stored) is not dict or digest(stored) != plan_sha256:
            raise ValueError('The recorded book completion has a different typed plan.')
        directory = audio / 'renders' / run_key
        if not optional_present(directory):
            return None
        checked_directory(directory)
        marker = directory / 'complete.json'
        if not optional_present(marker):
            return None
        # Read-only completion verification reserves CPU and memory, not new disk.
        request = ResourceRequest(cpu_slots=1, memory_bytes=64 * 1024**2)
        with broker.reserve(request, operation_id='studio-verify:' + plan.job_id(index),
                            cancelled=cancelled, timeout=60) as lease:
            _check(lease.cancelled, source_guard)
            receipt = _verified_output(directory, run_key, lease.cancelled)
            _check(lease.cancelled, source_guard)
            if receipt is None:
                raise ValueError('The recorded completed book audio no longer verifies.')
            if 'plan_sha256' not in receipt:
                return None
            if (receipt['plan_sha256'] != plan_sha256
                    or receipt.get('source_sha256') != expected.source_sha256
                    or receipt.get('input_kind') != expected.input_kind):
                raise ValueError('The recorded completed book receipt differs from the reviewed part.')
            # No executable, model, old absolute output path or engine identity
            # from this historical receipt is opened or activated.
            return _public_result(receipt, directory)

    def run(self, plan_id, destination, *, approval, cancelled=lambda: False,
            guard=lambda: None, progress=None):
        plan = self.load(plan_id)
        review = self.review(plan, destination, cancelled=cancelled, guard=guard)
        if not review.prepared or approval != review.fingerprint:
            raise ValueError('Prepare and review the current book destinations before Run.')
        from ..operations import ResourceBroker
        from .production import render_project
        renderer = self.renderer or render_project
        broker = ResourceBroker.for_app(self.library.root, cancelled=cancelled)
        rows = []
        def notify(message):
            if progress is not None:
                try:
                    progress(str(message))
                except Exception:
                    pass
        for index in range(1, len(plan.parts) + 1):
            def source_guard():
                self._validate(plan, cancelled, guard)
                if self.load(plan.id).fingerprint != plan.fingerprint or self._document(plan, index) is None:
                    raise ValueError('The retained book review changed during production.')
            source_guard()
            _, _, audio = self.managed.paths(plan.job_id(index))
            notify('Part %d/%d: checking the current managed production.' % (index, len(plan.parts)))
            # A completed retained production is data: verify it under the
            # same native guard before considering any live engine discovery.
            # This keeps a restored accepted book exportable offline.
            with project_lock(audio):
                source_guard()
                result = self._saved_completion(plan, index, audio, broker=broker,
                    cancelled=lambda: self._stopping(cancelled), source_guard=source_guard)
                retained_completion = result is not None
                if retained_completion:
                    notify('Retained completion reused; no new synthesis or engine qualification.')
                    exported = export_part(plan, index, result, audio,
                        Path(review.output_parent), review.parent_identity, broker=broker,
                        cancelled=lambda: self._stopping(cancelled), source_guard=source_guard)
            if result is None:
                ffmpeg = resolve_ffmpeg(self.ffmpeg)
                if ffmpeg is None:
                    raise ValueError('Configure the reviewed local FFmpeg runtime before rendering an incomplete part.')
                result = renderer(plan.project(index), audio, engine=self._engine(), ffmpeg=ffmpeg,
                    cancelled=lambda: self._stopping(cancelled), on_status=notify,
                    broker=broker, operation_id='studio:' + plan.job_id(index), source_guard=source_guard)
                # Reacquire the renderer's exact native guard for receipt
                # validation and copy after the live renderer has settled.
                with project_lock(audio):
                    source_guard()
                    exported = export_part(plan, index, result, audio,
                        Path(review.output_parent), review.parent_identity, broker=broker,
                        cancelled=lambda: self._stopping(cancelled), source_guard=source_guard)
            rows.append({'index': index, 'job': plan.job_id(index),
                         'retained_completion': retained_completion, **exported})
            # A completed publication remains acknowledged even if a stop
            # arrives immediately afterwards; only the next part is withdrawn.
            if self._stopping(cancelled) and index < len(plan.parts):
                break
        return tuple(rows)


    def _measured_service(self, plan_id, index, *, cancelled, guard):
        """Bind one current completed managed part; never resolve an engine."""
        from ..operations import ResourceBroker
        from .models import digest
        from .publishing import PublicationService
        plan = self.load(plan_id)
        plan.job_id(index)
        stopping = lambda: self._stopping(cancelled)
        self._validate(plan, stopping, guard)
        if self._document(plan, index) is None:
            raise ValueError('Prepare and complete the selected Book part before measured splitting.')
        _, _, audio = self.managed.paths(plan.job_id(index))
        state = read_json(audio / 'render-journal.json', limit=1024**2)
        run_key = state.get('run_key') if type(state) is dict else None
        if (type(state) is not dict or state.get('schema_version') != 1
                or state.get('state') != 'complete' or type(run_key) is not str
                or len(run_key) != 64 or any(char not in '0123456789abcdef' for char in run_key)
                or state.get('source_sha256') != plan.project(index).source_sha256):
            raise ValueError('Measured splitting requires a recorded complete current Book part.')
        def current():
            self._validate(plan, stopping, guard)
            if (self.load(plan.id).fingerprint != plan.fingerprint or self._document(plan, index) is None
                    or read_json(audio / 'render-journal.json', limit=1024**2) != state):
                raise ValueError('The original Book review or completed production changed.')
        publisher = PublicationService(audio, private_root=self.library.root,
            broker=ResourceBroker.for_app(self.library.root, cancelled=stopping),
            operation_id='publication:' + plan.job_id(index),
            expected_plan_sha256=digest(plan.project(index).to_dict()))
        return plan, run_key, publisher, current, stopping

    def review_duration(self, plan_id, index, output, *, maximum_ms,
                        name_template='{index:04d} - {book} - {part}',
                        cancelled=lambda: False, guard=lambda: None, progress=None,
                        destination_approval=None):
        plan, run_key, publisher, current, stopping = self._measured_service(
            plan_id, index, cancelled=cancelled, guard=guard)
        return publisher.review_pcm_parts(run_key, output, book_plan=plan, part_index=index,
            maximum_ms=maximum_ms, name_template=name_template, source_guard=current,
            cancelled=stopping, progress=progress, approval=destination_approval)

    def publish_duration(self, plan_id, index, output, *, maximum_ms, review_sha256,
                         name_template='{index:04d} - {book} - {part}',
                         cancelled=lambda: False, guard=lambda: None, progress=None,
                         destination_approval=None):
        plan, run_key, publisher, current, stopping = self._measured_service(
            plan_id, index, cancelled=cancelled, guard=guard)
        return publisher.export_pcm_parts(run_key, output, book_plan=plan, part_index=index,
            maximum_ms=maximum_ms, name_template=name_template, review_sha256=review_sha256,
            source_guard=current, cancelled=stopping, progress=progress, approval=destination_approval)

    def duration_publications(self, plan_id, index):
        """Recorded metadata only; explicit verify rehashes the selected archive."""
        from .publishing import PublicationService
        plan = self.load(plan_id)
        _, _, audio = self.managed.paths(plan.job_id(index))
        return tuple(row for row in PublicationService(audio, private_root=self.library.root).publications()
                     if row['format'] == 'pcm-parts')

    def verify_duration(self, plan_id, index, publication_id, *, output=None, cancelled=lambda: False):
        from .publishing import PublicationService
        plan = self.load(plan_id)
        _, _, audio = self.managed.paths(plan.job_id(index))
        return PublicationService(audio, private_root=self.library.root).verify(publication_id,
            output=output, cancelled=lambda: self._stopping(cancelled))
