"""Ordered reviews of existing Book plans; Studio remains the only job store.

A saved selection contains no output permission, progress checkpoint or audio
receipt. Every part runs through BookAudioService and its ProductionService.
Runtime stop signals withdraw a single invocation and are never backed up.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
from uuid import uuid4

from ..durable_state import atomic_json, encode, local_path, read_json, regular, state_lock, sync_directory
from ..audio.cleanup import _pending_owners, retained_state_lock
from .book_audio import (BookAudioService, BookPlan, BookReview, MAX_REVIEW_BYTES,
                         _check, _identity, _short)

MAX_BOOKS = 32
MAX_TOTAL_PARTS = 256
MAX_TOTAL_CHARACTERS = 8_000_000


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
                or value['schema'] != 'carr-book-batch/1' or type(value['items']) is not list):
            raise ValueError('Invalid saved book batch selection.')
        try:
            return cls(value['id'], value['workspace'], value['topic'], value['title'],
                       tuple(BookBatchItem(**item) for item in value['items']))
        except (TypeError, KeyError) as exc:
            raise ValueError('Invalid saved book batch fields.') from exc

    @property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()


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
    plans = tuple(plans)
    if not plans or not all(isinstance(plan, BookPlan) for plan in plans):
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
        self.root = books.root / 'book-batches' / sha256(books.workspace.encode('utf-8')).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        local_path(self.root, exists=True)

    def _scope(self, batch):
        self.books._check_root()
        if not isinstance(batch, BookBatch) or batch.workspace != self.books.workspace:
            raise ValueError('This book batch belongs to another library.')
        with self.books.library.read(batch.workspace) as db:
            if batch.topic is not None:
                from ..research.topic_tree import TopicTree
                TopicTree(db, batch.workspace)._visible(batch.topic)

    def load(self, batch_id):
        path = self.root / (_identity(batch_id) + '.json')
        if regular(path).st_size > 128 * 1024:
            raise ValueError('Saved book batch exceeds its bounded selection size.')
        value = read_json(path)
        if type(value) is not dict or set(value) != {'batch', 'fingerprint'}:
            raise ValueError('Invalid saved book batch review.')
        batch = BookBatch.from_dict(value['batch'])
        if batch.id != batch_id or batch.fingerprint != value['fingerprint']:
            raise ValueError('Saved book batch identity changed.')
        self._scope(batch)
        return batch

    def plans(self, batch):
        self._scope(batch)
        paths = [self.books.plans / (item.plan_id + '.json') for item in batch.items]
        if sum(regular(path).st_size for path in paths) > MAX_REVIEW_BYTES:
            raise ValueError('The selected book review files exceed the aggregate byte budget.')
        plans, characters = [], 0
        for item in batch.items:
            plan = self.books.load(item.plan_id)
            characters += len(plan.source['text'])
            if characters > MAX_TOTAL_CHARACTERS:
                raise ValueError('The selected book text exceeds the aggregate review budget.')
            plans.append(plan)
        return validate_plans(batch, tuple(plans))

    def saved(self):
        with self.books.library.read(self.books.workspace):
            pass
        paths = sorted(self.root.glob('*.json'))
        if len(paths) > 4096:
            raise ValueError('Too many saved selections; use an exact batch identity.')
        return tuple(self.load(path.stem) for path in paths)

    def review(self, batch, outputs, *, plans=None, cancelled=lambda: False, guard=lambda: None):
        self._scope(batch)
        plans = self.plans(batch) if plans is None else validate_plans(batch, plans)
        if type(outputs) not in (tuple, list) or len(outputs) != len(plans):
            raise ValueError('Explicitly choose one output parent for each reviewed book.')
        path = self.root / (batch.id + '.json')
        if os.path.lexists(path) and self.load(batch.id).fingerprint != batch.fingerprint:
            raise ValueError('This batch identity belongs to another immutable selection.')
        reviews = []
        for plan, output in zip(plans, outputs):
            _check(cancelled, guard)
            reviews.append(self.books.review(plan, output, cancelled=cancelled, guard=guard))
        names = [os.path.normcase(str(Path(name))).casefold()
                 for review in reviews for name in review.destinations]
        if len(set(names)) != len(names):
            raise ValueError('The selected book output paths collide.')
        return BookBatchReview(batch.fingerprint, tuple(reviews))

    def prepare(self, batch, outputs, *, approval, plans=None, cancelled=lambda: False, guard=lambda: None):
        self._scope(batch)
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
            path = self.root / (batch.id + '.json')
            if not os.path.lexists(path):
                atomic_json(path, {'batch': batch.to_dict(), 'fingerprint': batch.fingerprint})
            for plan, review in zip(plans, reviewed.books):
                self.books.prepare(plan, review.output_parent, approval=review.fingerprint,
                                   cancelled=cancelled, guard=guard)
            return self.review(batch, outputs, cancelled=cancelled, guard=guard)

    def fresh(self, batch_id, *, cancelled=lambda: False, guard=lambda: None):
        """Return an unsaved selection with fresh per-part execution identities."""
        previous = self.load(batch_id)
        plans = tuple(self.books.fresh(plan, cancelled=cancelled, guard=guard)
                      for plan in self.plans(previous))
        return select_books(plans, title=previous.title), plans

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
                        state.get('restore_requires_new_output') is True})
            parents = {str(Path(item['destination']).parent) for item in items if item['destination']}
            parent = (next(iter(parents)) if len(parents) == 1
                      and not any(item['requires_output_directory'] for item in items) else None)
            rows.append({'plan': plan.id, 'title': plan.source['title'], 'voice': plan.role.to_dict(),
                         'format': plan.format, 'output_parent': parent, 'items': items})
        return {'batch': batch.id, 'fingerprint': batch.fingerprint, 'title': batch.title,
                'notice': 'Stored Studio checkpoints; completed artifacts are verified on explicit Run.',
                'books': rows}

    def _controls(self, batch):
        root = self.books.library.root / 'runtime' / 'book-batches' / sha256(batch.workspace.encode()).hexdigest()
        root.mkdir(parents=True, exist_ok=True)
        return local_path(root, exists=True), root / (batch.id + '.active.json')

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

    def _signal_jobs(self, plans, action):
        for plan in plans:
            for index in range(1, len(plan.parts) + 1):
                state = self.books._job(plan, index)
                if state is not None and state['status'] != 'complete':
                    callback = self.books.production.cancel if action == 'cancel' else self.books.production.request_pause
                    callback(plan.job_id(index))

    def request_stop(self, batch_id, action):
        if action not in ('pause', 'cancel'):
            raise ValueError('Choose Pause or Cancel for the active batch.')
        batch = self.load(batch_id)
        self.plans(batch)
        root, path = self._controls(batch)
        if not os.path.lexists(path):
            raise ValueError('This batch is not currently running. Saved items remain available.')
        current = self._control(read_json(path), batch)
        # The invocation token is part of the signal filename. A late writer
        # from a finished invocation cannot stop or overwrite a newer Run.
        signal = root / (batch.id + '.' + current['token'] + '.control.json')
        atomic_json(signal, dict(current, action=action), replace=True)
        return {'batch': batch.id, 'requested': action, 'notice': 'Waiting for existing native worker cleanup.'}

    def run(self, batch_id, outputs, *, approval, cancelled=lambda: False, cancel_action=lambda: 'pause',
            guard=lambda: None, progress=None):
        batch = self.load(batch_id)
        with retained_state_lock(self.root, 'batch-' + batch.id):
            plans = self.plans(batch)
            reviewed = self.review(batch, outputs, cancelled=cancelled, guard=guard)
            if not reviewed.prepared or approval != reviewed.fingerprint:
                raise ValueError('Prepare and explicitly review all saved book destinations before Run.')
            root, path = self._controls(batch)
            token = uuid4().hex
            for previous in root.glob(batch.id + '.*.control.json'):
                if re.fullmatch(batch.id + r'\.[0-9a-f]{32}\.control\.json', previous.name):
                    regular(previous)
                    previous.unlink()
            active = {'schema': 'carr-book-batch-control/1', 'batch': batch.id,
                      'fingerprint': batch.fingerprint, 'token': token, 'action': 'run'}
            signal = root / (batch.id + '.' + token + '.control.json')
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
                self._control(read_json(path), batch, token)
                action = (self._control(read_json(signal), batch, token)['action']
                          if os.path.lexists(signal) else 'run')
                if self.books._closed.is_set() or cancelled():
                    action = cancel_action()
                    if action not in ('pause', 'cancel'):
                        raise ValueError('Invalid book batch cancellation action.')
                if action != 'run':
                    reason = action
                    self._signal_jobs(plans, action)
                    return True
                return False

            def current_scope():
                guard()
                if self.load(batch.id).fingerprint != batch.fingerprint:
                    raise ValueError('The saved batch selection changed during rendering.')

            try:
                completed_parts = 0
                total_parts = sum(len(plan.parts) for plan in plans)
                for book_index, (plan, book_review) in enumerate(zip(plans, reviewed.books), 1):
                    _check(stopped, current_scope)
                    prefix = f'Book {book_index}/{len(plans)}: {plan.source["title"][:120]}. '
                    notify(prefix + 'Checking saved items.')
                    items = self.books.run(plan.id, book_review.output_parent,
                        approval=book_review.fingerprint, cancelled=stopped, guard=current_scope,
                        progress=lambda message: notify(prefix + str(message)))
                    rows.append({'plan': plan.id, 'items': list(items)})
                    completed_parts += sum(item['status'] == 'complete' for item in items)
                    notify(f'Verified {completed_parts}/{total_parts} saved parts; '
                           'elapsed audio timing is available in each completed Studio receipt.')
                    if not items or any(item['status'] != 'complete' for item in items):
                        break
            except InterruptedError as exc:
                if _pending_owners(exc) or not stopped():
                    raise
            finally:
                self._control(read_json(path), batch, token)
                path.unlink()
                if os.path.lexists(signal):
                    regular(signal)
                    signal.unlink()
                sync_directory(root)
            statuses = [item['status'] for row in rows for item in row['items']]
            state = ('cancelled' if reason == 'cancel' else 'paused' if reason == 'pause'
                     else 'complete' if len(rows) == len(plans) and statuses and all(s == 'complete' for s in statuses)
                     else next((s for s in statuses if s != 'complete'), 'paused'))
            return {'batch': batch.id, 'status': state, 'books': rows, 'stored': self.status(batch.id)}

