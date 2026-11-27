"""Ordered reviews of existing Book plans; Studio remains the only job store.

A saved selection contains no output permission, progress checkpoint or audio
receipt. Every part runs through BookAudioService and its ProductionService.
Runtime stop signals withdraw a single invocation and are never backed up.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
from itertools import islice
import os
from pathlib import Path
import re
from uuid import uuid4

from ..durable_state import atomic_json, encode, local_path, read_json, regular, state_lock, sync_directory
from ..audio.models import AudioCancelled
from .book_audio import BookAudioService, BookPlan, BookReview, _check, _identity, _short

MAX_BOOKS = 32
MAX_TOTAL_PARTS = 256
MAX_TOTAL_CHARACTERS = 8_000_000
MAX_BATCH_BYTES = 128 * 1024
MAX_SAVED_BATCHES = 4096
MAX_COLLECTION_BYTES = 64 * 1024**2


@dataclass(frozen=True)
class BookBatchItem:
    plan_id: str
    plan_sha256: str

    def __post_init__(self):
        _identity(self.plan_id)
        if type(self.plan_sha256) is not str or re.fullmatch('[0-9a-f]{64}', self.plan_sha256) is None:
            raise ValueError('Invalid book plan fingerprint.')


@dataclass(frozen=True)
class BookBatch:
    id: str
    workspace: str
    topic: str | None
    title: str
    items: tuple[BookBatchItem, ...]

    def __post_init__(self):
        _identity(self.id)
        _short(self.workspace, 'batch library identity', 1024)
        _short(self.title, 'batch title', 512)
        if self.topic is not None:
            _short(self.topic, 'batch topic', 1024)
        if (type(self.items) is not tuple or not 1 <= len(self.items) <= MAX_BOOKS
                or not all(isinstance(item, BookBatchItem) for item in self.items)
                or len({item.plan_id for item in self.items}) != len(self.items)):
            raise ValueError('Choose between 1 and 32 distinct reviewed book plans.')

    def to_dict(self):
        return {'schema': 'carr-book-batch/1', 'id': self.id, 'workspace': self.workspace,
                'topic': self.topic, 'title': self.title, 'items': [asdict(item) for item in self.items]}

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {'schema', 'id', 'workspace', 'topic', 'title', 'items'}
                or value['schema'] != 'carr-book-batch/1' or type(value['items']) is not list
                or not 1 <= len(value['items']) <= MAX_BOOKS):
            raise ValueError('Invalid saved book batch selection.')
        try:
            return cls(value['id'], value['workspace'], value['topic'], value['title'],
                       tuple(BookBatchItem(**item) for item in value['items']))
        except (TypeError, KeyError) as exc:
            raise ValueError('Invalid saved book batch fields.') from exc

    @property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()

def validate_saved_batch(value, *, namespace=None, identity=None):
    """Pure metadata validation; no stored selection grants output authority."""
    if (type(value) is not dict or set(value) != {'batch', 'fingerprint'}
            or len(encode(value)) > MAX_BATCH_BYTES):
        raise ValueError('Invalid bounded saved book batch selection.')
    batch = BookBatch.from_dict(value['batch'])
    if (value['fingerprint'] != batch.fingerprint
            or identity is not None and batch.id != identity
            or namespace is not None and
                sha256(batch.workspace.encode('utf-8')).hexdigest() != namespace):
        raise ValueError('Saved book batch identity changed.')
    return batch


class BookBatchSelectionCommitted(OSError):
    """The immutable selection is visible; its directory sync needs review."""
    def __init__(self, batch):
        self.batch = batch.id
        self.fingerprint = batch.fingerprint
        self.selection_saved = True
        self.durable = False
        super().__init__('The batch selection was saved, but durable confirmation failed. '
                         'Reopen this saved selection and review before Prepare; no speech started.')


def validate_plans(batch, plans):
    """Pure exact membership validation, also used by the passive backup reader."""
    if not isinstance(batch, BookBatch) or type(plans) not in (tuple, list) or len(plans) != len(batch.items):
        raise ValueError('The ordered book plans differ from the reviewed batch.')
    characters = parts = 0
    for item, plan in zip(batch.items, plans):
        if not isinstance(plan, BookPlan) or (plan.id, plan.fingerprint) != (item.plan_id, item.plan_sha256):
            raise ValueError('A selected book plan or its order changed.')
        source = plan.source
        if source['source_anchor']['workspace'] != batch.workspace or source['source_topic'] != batch.topic:
            raise ValueError('Every selected book must belong to the same reviewed library/topic scope.')
        characters += len(source['text'])
        parts += len(plan.parts)
    if characters > MAX_TOTAL_CHARACTERS or parts > MAX_TOTAL_PARTS:
        raise ValueError('A batch is bounded to eight million characters and 256 existing Studio parts.')
    return tuple(plans)


def select_books(plans, *, title='Book audio batch'):
    plans = tuple(islice(plans, MAX_BOOKS + 1))
    if not 1 <= len(plans) <= MAX_BOOKS or not all(isinstance(plan, BookPlan) for plan in plans):
        raise ValueError('Choose existing typed book plans.')
    source = plans[0].source
    batch = BookBatch(uuid4().hex, source['source_anchor']['workspace'], source['source_topic'],
        title, tuple(BookBatchItem(plan.id, plan.fingerprint) for plan in plans))
    validate_plans(batch, plans)
    return batch


@dataclass(frozen=True)
class BookBatchReview:
    batch_sha256: str
    books: tuple[BookReview, ...]

    @property
    def prepared(self):
        return bool(self.books) and all(book.prepared for book in self.books)

    def to_dict(self):
        return {'batch_sha256': self.batch_sha256, 'books': [asdict(book) for book in self.books]}

    @property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()


class BookBatchService:
    def __init__(self, books):
        if not isinstance(books, BookAudioService):
            raise ValueError('Use the existing scoped Book audio service.')
        self.books = books
        books._check_scope()
        with books.library.read(books.workspace):
            pass
        self.root = local_path(books.root / 'book-batches' /
                               sha256(books.workspace.encode('utf-8')).hexdigest())
        self.root.mkdir(parents=True, exist_ok=True)
        regular(local_path(self.root, exists=True), directory=True)

    def _scope(self, batch):
        self.books._check_scope()
        if not isinstance(batch, BookBatch) or batch.workspace != self.books.workspace:
            raise ValueError('This book batch belongs to another library.')
        with self.books.library.read(batch.workspace):
            pass

    def _guard(self, batch, guard):
        self._scope(batch)
        guard()

    def _stopped(self, cancelled):
        return (self.books._closed.is_set() or self.books.production._closed
                or bool(cancelled()))

    def load(self, batch_id):
        self.books._check_scope()
        path = local_path(self.root / (_identity(batch_id) + '.json'), exists=True)
        batch = validate_saved_batch(read_json(path, maximum=MAX_BATCH_BYTES),
                                     namespace=self.root.name, identity=batch_id)
        self._scope(batch)
        return batch

    def plans(self, batch):
        self._scope(batch)
        plans, characters, parts = [], 0, 0
        for item in batch.items:
            self._scope(batch)
            plan = self.books.load(item.plan_id)
            if (plan.id, plan.fingerprint) != (item.plan_id, item.plan_sha256):
                raise ValueError('A selected book plan or its order changed.')
            characters += len(plan.source['text'])
            parts += len(plan.parts)
            if characters > MAX_TOTAL_CHARACTERS or parts > MAX_TOTAL_PARTS:
                raise ValueError('A batch is bounded to eight million characters and 256 existing Studio parts.')
            plans.append(plan)
        return validate_plans(batch, tuple(plans))

    def saved(self, *, cancelled=lambda: False):
        self.books._check_scope()
        with self.books.library.read(self.books.workspace):
            pass
        paths, total, count = [], 0, 0
        with os.scandir(local_path(self.root, exists=True)) as entries:
            for entry in entries:
                _check(cancelled, self.books._check_scope)
                count += 1
                if count > MAX_SAVED_BATCHES * 2:
                    raise ValueError('Too many saved batch entries; use an exact identity.')
                path = local_path(entry.path, exists=True)
                info = regular(path)
                if (re.fullmatch(r'\.batch-[0-9a-f]{32}\.lock', entry.name)
                        or re.fullmatch(r'\.pending-[A-Za-z0-9_]{8}', entry.name)):
                    continue
                if re.fullmatch(r'[0-9a-f]{32}\.json', entry.name) is None:
                    raise ValueError('Unknown saved batch metadata needs review.')
                total += info.st_size
                paths.append(path)
                if (len(paths) > MAX_SAVED_BATCHES or not 1 <= info.st_size <= MAX_BATCH_BYTES
                        or total > MAX_COLLECTION_BYTES):
                    raise ValueError('Saved book batches exceed their bounded metadata budget.')
        result = []
        for path in sorted(paths):
            _check(cancelled, self.books._check_scope)
            result.append(self.load(path.stem))
        return tuple(result)

    def _save_selection(self, batch):
        value = {'batch': batch.to_dict(), 'fingerprint': batch.fingerprint}
        validate_saved_batch(value, namespace=self.root.name, identity=batch.id)
        self._scope(batch)
        path = local_path(self.root / (batch.id + '.json'))
        if os.path.lexists(path):
            if self.load(batch.id).fingerprint != batch.fingerprint:
                raise ValueError('This batch identity belongs to another immutable selection.')
            try:
                sync_directory(self.root)
            except OSError as exc:
                raise BookBatchSelectionCommitted(batch) from exc
            return
        try:
            atomic_json(path, value)
        except FileExistsError:
            # Identical competing bytes are still the other writer's commit.
            raise
        except OSError as exc:
            try:
                visible = self.load(batch.id)
            except (OSError, ValueError, RuntimeError):
                raise exc
            if visible.fingerprint != batch.fingerprint:
                raise exc
            raise BookBatchSelectionCommitted(batch) from exc

    def review(self, batch, outputs, *, plans=None, cancelled=lambda: False, guard=lambda: None):
        _check(lambda: self._stopped(cancelled), lambda: self._guard(batch, guard))
        plans = self.plans(batch) if plans is None else validate_plans(batch, plans)
        if type(outputs) not in (tuple, list) or len(outputs) != len(plans):
            raise ValueError('Explicitly choose one output parent for each reviewed book.')
        path = self.root / (batch.id + '.json')
        if os.path.lexists(path) and self.load(batch.id).fingerprint != batch.fingerprint:
            raise ValueError('This batch identity belongs to another immutable selection.')
        reviews = []
        for plan, output in zip(plans, outputs):
            _check(lambda: self._stopped(cancelled), lambda: self._guard(batch, guard))
            reviews.append(self.books.review(plan, output, cancelled=cancelled, guard=guard))
        names = [os.path.normcase(str(Path(name))).casefold()
                 for review in reviews for name in review.destinations]
        if len(set(names)) != len(names):
            raise ValueError('The selected book output paths collide.')
        _check(lambda: self._stopped(cancelled), lambda: self._guard(batch, guard))
        return BookBatchReview(batch.fingerprint, tuple(reviews))

    def prepare(self, batch, outputs, *, approval, plans=None, cancelled=lambda: False, guard=lambda: None):
        _check(lambda: self._stopped(cancelled), lambda: self._guard(batch, guard))
        plans = self.plans(batch) if plans is None else validate_plans(batch, plans)
        with state_lock(self.root, 'batch-' + batch.id):
            reviewed = self.review(batch, outputs, plans=plans, cancelled=cancelled, guard=guard)
            if approval != reviewed.fingerprint:
                raise ValueError('Review the exact ordered books, settings and output paths before Prepare.')
            # All source reviews are checked before preparing any new job.
            # Retain every referenced plan before publishing the selection, so
            # interruption never leaves a dangling batch in a complete backup.
            for plan in plans:
                self.books.save(plan, cancelled=cancelled, guard=guard)
            self.review(batch, outputs, plans=plans, cancelled=cancelled, guard=guard)
            _check(lambda: self._stopped(cancelled), lambda: self._guard(batch, guard))
            self._save_selection(batch)
            for plan, review in zip(plans, reviewed.books):
                self.books.prepare(plan, review.output_parent, approval=review.fingerprint,
                                   cancelled=cancelled, guard=guard)
            return self.review(batch, outputs, cancelled=cancelled, guard=guard)

    def status(self, batch_id):
        """Stored checkpoints only: this does not verify or render audio."""
        batch = self.load(batch_id)
        rows = []
        for plan in self.plans(batch):
            items = []
            for index in range(1, len(plan.parts) + 1):
                state = self.books._job(plan, index)
                items.append({'index': index, 'job': plan.job_id(index),
                    'status': state['status'] if state is not None else 'not-prepared',
                    'destination': state['destination'] if state is not None else '',
                    'requires_output_directory': state is None or
                        state.get('recovery', {}).get('requires_output_directory') is True})
            parents = {str(Path(item['destination']).parent) for item in items if item['destination']}
            parent = (next(iter(parents)) if len(parents) == 1
                      and not any(item['requires_output_directory'] for item in items) else None)
            rows.append({'plan': plan.id, 'title': plan.source['title'], 'voice': plan.role.to_dict(),
                         'format': plan.format, 'output_parent': parent, 'items': items})
        return {'batch': batch.id, 'fingerprint': batch.fingerprint, 'title': batch.title,
                'notice': 'Stored Studio checkpoints; completed artifacts are verified on explicit Run.',
                'books': rows}

    def _controls(self, batch):
        self._scope(batch)
        root = local_path(self.books.library_root / 'runtime' / 'book-batches' /
                          sha256(batch.workspace.encode('utf-8')).hexdigest())
        root.mkdir(parents=True, exist_ok=True)
        regular(local_path(root, exists=True), directory=True)
        return root, root / (batch.id + '.active.json')

    @staticmethod
    def _control(value, batch, token=None):
        if (type(value) is not dict or set(value) != {'schema', 'batch', 'fingerprint', 'token', 'action'}
                or value['schema'] != 'carr-book-batch-control/1'
                or value['batch'] != batch.id or value['fingerprint'] != batch.fingerprint
                or type(value['token']) is not str or re.fullmatch('[0-9a-f]{32}', value['token']) is None
                or token is not None and value['token'] != token
                or value['action'] not in ('run', 'pause', 'cancel')):
            raise ValueError('The active book batch control identity changed.')
        return value

    def request_stop(self, batch_id, action):
        if action not in ('pause', 'cancel'):
            raise ValueError('Choose Pause or Cancel for the active batch.')
        batch = self.load(batch_id)
        self.plans(batch)
        root, path = self._controls(batch)
        if not os.path.lexists(path):
            raise ValueError('This batch is not currently running. Saved items remain available.')
        current = self._control(read_json(path, maximum=4096), batch)
        # The invocation token is part of the signal filename. A late writer
        # from a finished invocation cannot stop or overwrite a newer Run.
        signal = root / (batch.id + '.' + current['token'] + '.control.json')
        self._scope(batch)
        atomic_json(signal, dict(current, action=action), replace=True)
        return {'batch': batch.id, 'requested': action, 'notice': 'Waiting for existing native worker cleanup.'}

    def _cleanup_control(self, batch, root, path, signal, token):
        """Remove only this invocation after the existing native owner returns."""
        self._scope(batch)
        self._control(read_json(path, maximum=4096), batch, token)
        path.unlink()
        if os.path.lexists(signal):
            self._control(read_json(signal, maximum=4096), batch, token)
            signal.unlink()
        sync_directory(root)

    def run(self, batch_id, outputs, *, approval, cancelled=lambda: False, cancel_action=lambda: 'pause',
            guard=lambda: None, progress=None):
        batch = self.load(batch_id)
        _check(lambda: self._stopped(cancelled), lambda: self._guard(batch, guard))
        with state_lock(self.root, 'batch-' + batch.id):
            plans = self.plans(batch)
            reviewed = self.review(batch, outputs, cancelled=cancelled, guard=guard)
            if not reviewed.prepared or approval != reviewed.fingerprint:
                raise ValueError('Prepare and explicitly review all saved book destinations before Run.')
            root, path = self._controls(batch)
            token = uuid4().hex
            # Enumerate only this bounded runtime directory; unreadable or
            # linked state fails before any audio and cannot disappear silently.
            with os.scandir(root) as entries:
                count = 0
                for entry in entries:
                    _check(lambda: self._stopped(cancelled), lambda: self._guard(batch, guard))
                    count += 1
                    if count > MAX_SAVED_BATCHES * 2:
                        raise ValueError('Too many batch runtime controls; review their existing state.')
                    previous = local_path(entry.path, exists=True)
                    regular(previous)
                    if re.fullmatch(batch.id + r'\.[0-9a-f]{32}\.control\.json', entry.name):
                        previous.unlink()
            if os.path.lexists(path):
                self._control(read_json(path, maximum=4096), batch)
            active = {'schema': 'carr-book-batch-control/1', 'batch': batch.id,
                      'fingerprint': batch.fingerprint, 'token': token, 'action': 'run'}
            signal = root / (batch.id + '.' + token + '.control.json')
            _check(lambda: self._stopped(cancelled), lambda: self._guard(batch, guard))
            atomic_json(path, active, replace=True)
            reason, rows, observer = None, [], progress

            def notify(message):
                nonlocal observer
                if observer is not None:
                    try:
                        observer(message)
                    except Exception:
                        observer = None

            def stopped():
                nonlocal reason
                if reason is not None:
                    return True
                self.books._check_scope()
                self._control(read_json(path, maximum=4096), batch, token)
                action = (self._control(read_json(signal, maximum=4096), batch, token)['action']
                          if os.path.lexists(signal) else 'run')
                if self._stopped(cancelled):
                    action = cancel_action()
                    if action not in ('pause', 'cancel'):
                        raise ValueError('Invalid book batch cancellation action.')
                if action != 'run':
                    reason = action
                    return True
                return False

            def current_scope():
                self._guard(batch, guard)
                if self.load(batch.id).fingerprint != batch.fingerprint:
                    raise ValueError('The saved batch selection changed during rendering.')

            failure = cleanup_error = None
            try:
                completed_parts = 0
                total_parts = sum(len(plan.parts) for plan in plans)
                for book_index, (plan, book_review) in enumerate(zip(plans, reviewed.books), 1):
                    _check(stopped, current_scope)
                    prefix = f'Book {book_index}/{len(plans)}: {plan.source["title"][:120]}. '
                    notify(prefix + 'Checking saved items.')
                    try:
                        items = self.books.run(plan.id, book_review.output_parent,
                            approval=book_review.fingerprint, cancelled=stopped, guard=current_scope,
                            cancel_action=lambda: reason or 'pause',
                            progress=lambda message: notify(prefix + str(message)))
                    except BaseException as exc:
                        partial = getattr(exc, 'book_audio_result', None)
                        if partial is not None and partial['plan'] == plan.id:
                            rows.append({'plan': plan.id, 'items': list(partial['items'])})
                        raise
                    rows.append({'plan': plan.id, 'items': list(items)})
                    completed_parts += sum(item['status'] == 'complete' for item in items)
                    notify(f'Verified {completed_parts}/{total_parts} saved parts; '
                           'elapsed audio timing is available in each completed Studio receipt.')
                    if not items or any(item['status'] != 'complete' for item in items):
                        break
            except (InterruptedError, AudioCancelled) as exc:
                try:
                    acknowledged = stopped()
                except Exception:
                    acknowledged = False
                if not acknowledged:
                    failure = exc
            except BaseException as exc:
                failure = exc
            finally:
                try:
                    self._cleanup_control(batch, root, path, signal, token)
                except Exception as exc:
                    cleanup_error = exc
            statuses = [item['status'] for row in rows for item in row['items']]
            state = ('failed' if failure is not None
                     else 'cancelled' if reason == 'cancel' else 'paused' if reason == 'pause'
                     else 'complete' if len(rows) == len(plans) and statuses and all(s == 'complete' for s in statuses)
                     else next((s for s in statuses if s != 'complete'), 'paused'))
            result = {'batch': batch.id, 'status': state, 'books': rows}
            if cleanup_error is not None:
                # These are control files, not native resources or output
                # authority. Never relabel or discard returned Studio receipts.
                result['control_cleanup_pending'] = True
                result['notice'] = ('Studio results are retained. The batch control cleanup '
                                    'needs attention before another explicit Run.')
            if failure is not None:
                failure.book_batch_result = result
                if cleanup_error is not None:
                    failure.add_note('Batch control cleanup also needs attention; existing Studio results are retained.')
                raise failure.with_traceback(failure.__traceback__)
            try:
                result['stored'] = self.status(batch.id)
            except (OSError, ValueError, RuntimeError):
                result['stored'] = None
                result['checkpoint_read_pending'] = True
                result['notice'] = ('Returned Studio results are retained. Reopen the original library '
                                    'to inspect its saved checkpoints; no new operation was started.')
            return result
