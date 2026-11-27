"""Ordered immutable book selection using the existing managed renderer."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import re
from uuid import uuid4

from .book_plan import BookPlan, encode, _identity, _short

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
    books: tuple

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
        from .book_audio import BookAudioService
        if not isinstance(books, BookAudioService):
            raise ValueError('Use the current scoped BookAudioService.')
        self.books = books

    def load(self, identity):
        batch = self.books.store.load(identity, 'batches')
        self._scope(batch)
        return batch

    def _scope(self, batch):
        if not isinstance(batch, BookBatch) or batch.workspace != self.books.workspace:
            raise ValueError('This saved selection belongs to another workspace.')
        with self.books.library.read(batch.workspace):
            pass

    def plans(self, batch):
        self._scope(batch)
        return validate_plans(batch, tuple(self.books.load(item.plan_id) for item in batch.items))

    def saved(self, *, cancelled=lambda: False):
        with self.books.library.read(self.books.workspace):
            pass
        return self.books.store.saved(workspace=self.books.workspace, batches=True, cancelled=cancelled)

    def review(self, batch, outputs, *, plans=None, cancelled=lambda: False, guard=lambda: None):
        self._scope(batch)
        plans = self.plans(batch) if plans is None else validate_plans(batch, plans)
        if type(outputs) not in (tuple, list) or len(outputs) != len(plans):
            raise ValueError('Explicitly select one output parent for every book, in order.')
        reviews = tuple(self.books.review(plan, output, cancelled=cancelled, guard=guard)
                        for plan, output in zip(plans, outputs))
        names = [name.casefold() for review in reviews for name in review.destinations]
        if len(set(names)) != len(names):
            raise ValueError('The reviewed batch destinations collide.')
        # A later source capture or output inspection must not hide revocation
        # of an earlier selected source before the user sees the review.
        for plan in plans:
            self.books._validate(plan, cancelled, guard)
        return BookBatchReview(batch.fingerprint, reviews)

    def prepare(self, batch, outputs, *, approval, plans=None, cancelled=lambda: False, guard=lambda: None):
        plans = self.plans(batch) if plans is None else validate_plans(batch, plans)
        review = self.review(batch, outputs, plans=plans, cancelled=cancelled, guard=guard)
        if approval != review.fingerprint:
            raise ValueError('Review the exact ordered books and destinations before Prepare.')
        for plan in plans:
            self.books.save(plan, cancelled=cancelled, guard=guard)
        # All referenced reviews exist before the immutable batch is committed.
        self.books.store.save(batch, cancelled=cancelled, guard=guard)
        for plan, item in zip(plans, review.books):
            self.books.prepare(plan, item.output_parent, approval=item.fingerprint,
                               cancelled=cancelled, guard=guard)
        return self.review(batch, outputs, cancelled=cancelled, guard=guard)

    def status(self, identity, *, cancelled=lambda: False):
        from ..backup_files import optional_present
        from .jobs import read_json
        batch = self.load(identity)
        rows = []
        for plan in self.plans(batch):
            parts = []
            for index in range(1, len(plan.parts) + 1):
                if cancelled():
                    raise InterruptedError('Book status read cancelled.')
                present = self.books._document(plan, index) is not None
                _, document, audio = self.books.managed.paths(plan.job_id(index))
                journal = audio / 'render-journal.json'
                stored = read_json(journal, limit=1024**2) if optional_present(journal) else None
                state = stored.get('state', 'unknown') if type(stored) is dict else 'prepared' if present else 'not-prepared'
                if state not in ('prepared', 'not-prepared', 'complete', 'running', 'cancelled', 'failed', 'unknown'):
                    state = 'unknown'
                parts.append({'index': index, 'project': plan.job_id(index), 'recorded_state': state})
            rows.append({'plan': plan.id, 'title': plan.source['title'], 'parts': parts})
        return {'batch': batch.id, 'books': rows, 'verified_outputs': False,
                'notice': 'Recorded managed render state only. Run rechecks all accepted audio and requires a fresh output review.'}

    def run(self, identity, outputs, *, approval, cancelled=lambda: False,
            cancel_action=lambda: 'pause', guard=lambda: None, progress=None):
        from .jobs import checked_directory, project_lock
        batch = self.load(identity)
        plans = self.plans(batch)
        review = self.review(batch, outputs, cancelled=cancelled, guard=guard)
        if not review.prepared or approval != review.fingerprint:
            raise ValueError('Prepare and explicitly review this exact batch before Run.')
        runtime = checked_directory(self.books.library.root / 'runtime' / 'book-batches' / batch.id, create=True)
        rows = []
        with project_lock(runtime):
            for number, (plan, book_review) in enumerate(zip(plans, review.books), 1):
                if cancelled() or self.books._closed.is_set():
                    break
                def current():
                    guard()
                    if self.load(identity).fingerprint != batch.fingerprint:
                        raise ValueError('The saved ordered selection changed during production.')
                def notify(message):
                    if progress is not None:
                        try:
                            progress('Book %d/%d: %s' % (number, len(plans), message))
                        except Exception:
                            pass
                try:
                    items = self.books.run(plan.id, book_review.output_parent,
                        approval=book_review.fingerprint, cancelled=cancelled, guard=current, progress=notify)
                except InterruptedError:
                    if not (cancelled() or self.books._closed.is_set()):
                        raise
                    break
                rows.append({'plan': plan.id, 'items': list(items)})
                if len(items) != len(plan.parts) or any(item['status'] != 'complete' for item in items):
                    break
        complete = len(rows) == len(plans) and all(
            len(row['items']) == len(plan.parts) and all(item['status'] == 'complete' for item in row['items'])
            for row, plan in zip(rows, plans))
        action = cancel_action()
        if action not in ('pause', 'cancel'):
            raise ValueError('Choose the current invocation pause or cancel action.')
        return {'batch': batch.id, 'status': 'complete' if complete else 'cancelled' if action == 'cancel' else 'paused',
                'books': rows, 'stored': self.status(batch.id)}
