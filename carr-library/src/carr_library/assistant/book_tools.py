"""Typed adapters to saved Book plans and the existing Studio production store.

A proposal can review an existing selection, prepare its deterministic jobs, or
run separately approved prepared destinations. No source/voice choice is inferred.
The original Book service owns source checks, immutable jobs and resume receipts.
"""
from contextlib import contextmanager, closing
from dataclasses import asdict, replace
from hashlib import sha256
import json
from time import monotonic
from pathlib import Path
import re

from .models import canonical, text, local_path
from .state import regular
from ..audio.cleanup import retained_context
from ..durable_state import encode
from .local_tools import _bounded, _check, _workspace

RULES = {
    'book.review': ({'plan', 'plan_sha256', 'name'}, set()),
    'book.prepare': ({'plan', 'plan_sha256', 'name', 'review_sha256'}, set()),
    'book.run': ({'plan', 'plan_sha256', 'name', 'review_sha256'}, set()),
    'book.batch.review': ({'batch', 'batch_sha256', 'names'}, set()),
    'book.batch.prepare': ({'batch', 'batch_sha256', 'names', 'review_sha256'}, set()),
    'book.batch.run': ({'batch', 'batch_sha256', 'names', 'review_sha256'}, set()),
}
PUBLICATION_TOOLS = frozenset({'book.prepare', 'book.run', 'book.batch.prepare', 'book.batch.run'})


def check_arguments(step):
    """Pure checks for plans and passive backup admission; never opens a path."""
    required, _ = RULES[step.tool]
    args = step.arguments
    if set(args) != required:
        raise ValueError('Use the exact saved Book identity, output names and review fields.')
    for key in ('plan', 'batch'):
        if key in args and (type(args[key]) is not str or re.fullmatch('[0-9a-f]{32}', args[key]) is None):
            raise ValueError('Choose an exact saved Book or batch identity.')
    for key in ('plan_sha256', 'batch_sha256', 'review_sha256'):
        if key in args and (type(args[key]) is not str or re.fullmatch('[0-9a-f]{64}', args[key]) is None):
            raise ValueError('Use the exact reviewed Book fingerprint.')
    names = args.get('names', [args.get('name')])
    if type(names) is not list or not 1 <= len(names) <= 32:
        raise ValueError('Choose one explicit output parent per saved book, up to 32 books.')
    for name in names:
        text(name, 'Book output name', maximum=1024)
        if (Path(name).is_absolute() or '..' in Path(name).parts
                or ':' in name or '\\' in name or name in ('', '.')):
            raise ValueError('Book output parents must be children of the selected output folder.')
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError('Choose distinct explicitly named Book output parents.')


def _production(callback, app):
    from ..studio.render import ProductionService
    owner = getattr(callback, '__self__', None)
    if not isinstance(owner, ProductionService) or owner.root != app.root / 'state' / 'studio':
        raise RuntimeError('Book tools require the existing production service for this library.')
    owner._check_open()
    return owner


def _parents(step, scope, app):
    values = step.arguments.get('names', [step.arguments.get('name')])
    parents = tuple(scope.output(value) for value in values)
    for parent in parents:
        if parent.is_relative_to(app.root) or not parent.is_dir():
            raise PermissionError('Choose existing Book output parents outside private library data.')
    return parents


def validate_scope(step, scope, app, production):
    _production(production, app)
    if scope.workspace_id is None:
        raise PermissionError('Select the library that owns the saved Book plans.')
    _parents(step, scope, app)
    if scope.max_files < 1 or scope.max_bytes < 1024**2:
        raise ValueError('Book production needs an explicit artifact budget of at least one MiB.')


class _BorrowedVoices:
    """Lazy forwarding to the already selected Studio owner, never discovery here."""
    def __init__(self, production):
        self.production = production

    def __getattr__(self, name):
        return getattr(self.production.voices, name)


@contextmanager
def _services(app, workspace, scope, callback, cancelled):
    from ..studio.book_audio import BookAudioService
    from ..studio.book_batch import BookBatchService
    from ..studio.render import ProductionService, _retained_bytes

    original = _production(callback, app)
    _check(cancelled)
    # Existing retained cache is not a new grant. Bound additional work by this
    # plan while preserving the producer's stricter total-space policy.
    baseline = _retained_bytes((original.root,), cancelled)
    def runtime(root, **options):
        original._check_open()
        _check(cancelled)
        selected = original._runtime()
        selected.budget = replace(selected.budget,
            timeout=min(selected.budget.timeout, scope.max_seconds),
            max_output_bytes=min(selected.budget.max_output_bytes, scope.max_bytes))
        return selected
    scoped = ProductionService(original.root, voices=_BorrowedVoices(original),
        runtime_factory=runtime, input_roots=original.input_roots,
        total_disk_bytes=min(original.total_disk_bytes, baseline + scope.max_bytes),
        broker=original._broker)
    # Same ProjectStore, cache, job identities and broker; only the invocation's
    # runtime budget is narrowed. Borrowed voice ownership never transfers.
    with retained_context(closing(scoped)):
        books = BookAudioService(app, workspace, production=scoped)
        with retained_context(closing(books)):
            yield books, BookBatchService(books)


def _selection(books, groups, step, scope, app, cancelled):
    args = step.arguments
    parents = _parents(step, scope, app)
    is_batch = step.tool.startswith('book.batch.')
    selected = groups.load(args['batch']) if is_batch else books.load(args['plan'])
    expected = args['batch_sha256'] if is_batch else args['plan_sha256']
    if selected.fingerprint != expected:
        raise PermissionError('The saved Book selection changed after plan approval.')
    plans = groups.plans(selected) if is_batch else (selected,)
    if len(parents) != len(plans) or sum(len(plan.parts) for plan in plans) > scope.max_files:
        raise PermissionError('This saved Book selection exceeds the selected item or output-parent scope.')
    if sum(len(plan.source['text'].encode('utf-8')) for plan in plans) > scope.max_bytes:
        raise PermissionError('The literal Book source exceeds the plan byte budget.')
    def guard():
        _workspace(app, books.workspace)
        if _parents(step, scope, app) != parents:
            raise PermissionError('The selected Book output scope changed.')
        current = groups.load(selected.id) if is_batch else books.load(selected.id)
        if current.fingerprint != selected.fingerprint:
            raise PermissionError('The saved Book selection changed during execution.')
    guard()
    reviewed = (groups.review(selected, parents, cancelled=cancelled, guard=guard)
                if is_batch else books.review(selected, parents[0], cancelled=cancelled, guard=guard))
    return selected, plans, parents, reviewed, guard


def _review_dict(reviewed):
    value = reviewed.to_dict() if hasattr(reviewed, 'to_dict') else asdict(reviewed)
    return json.loads(canonical(value))


def _without_prepared(value):
    if 'books' in value:
        return {**value, 'books': [{key: item for key, item in book.items() if key != 'prepared'}
                                 for book in value['books']]}
    return {key: item for key, item in value.items() if key != 'prepared'}


def _result(selected, reviewed):
    return {'selection': selected.id, 'selection_sha256': selected.fingerprint,
            'review': _review_dict(reviewed), 'review_sha256': reviewed.fingerprint,
            'prepared': reviewed.prepared, 'artifacts': [],
            'notice': 'Saved literal Book plans and existing Studio jobs; a new exact review is required before Run.'}


def _bind(intent, step, reviewed, persist):
    value = {'tool': step.tool, 'arguments_sha256': sha256(canonical(step.arguments).encode('utf-8')).hexdigest(),
             'review': _review_dict(reviewed), 'review_sha256': reviewed.fingerprint}
    saved = intent.get('book_operation')
    if saved is None:
        if step.arguments['review_sha256'] != reviewed.fingerprint:
            raise PermissionError('The reviewed Book destinations changed. Review the current result before this action.')
        intent['book_operation'] = value
        persist()  # Exact source/output intent precedes job preparation or rendering.
    else:
        if (type(saved) is not dict or set(saved) != set(value)
                or saved['tool'] != value['tool']
                or saved['arguments_sha256'] != value['arguments_sha256']
                or saved['review_sha256'] != step.arguments['review_sha256']
                or sha256(encode(saved['review'])).hexdigest()
                   != saved['review_sha256']
                or _without_prepared(saved['review']) != _without_prepared(value['review'])):
            raise PermissionError('The retained Book intent no longer matches this approved plan.')
        # Prepare can recover its own deterministic jobs after an interrupted
        # receipt; it cannot replace source/output identities or clear restore.
        if step.tool.endswith('.run') and saved['review'] != value['review']:
            raise PermissionError('The prepared Book execution review changed.')


def _accepted(books, plans, reviewed, scope, cancelled):
    """Verify completed original receipts without invoking another render."""
    reviews = reviewed.books if hasattr(reviewed, 'books') else (reviewed,)
    results, artifacts = [], []
    total = 0
    for plan, review in zip(plans, reviews):
        for index, destination in enumerate(review.destinations, 1):
            _check(cancelled)
            books._validate(plan, cancelled, lambda: None)
            state = books._job(plan, index)
            if (state is None or state['status'] != 'complete'
                    or state.get('restore_requires_new_output') is True
                    or state['destination'] != destination):
                raise PermissionError('The Book has an incomplete or restored production item; no result was adopted.')
            books.production._verify_artifacts(state, cancelled=cancelled)
            from ..durable_state import encode, read_json
            if encode(read_json(Path(destination) / 'book-source.json')) != encode(plan.provenance(index)):
                raise PermissionError('The completed Book provenance no longer matches the original plan.')
            for row in state['artifacts']:
                if (type(row) is not dict or set(row) != {'path', 'size', 'sha256'}
                        or type(row['size']) is not int or row['size'] < 0):
                    raise ValueError('Invalid Book artifact receipt.')
                target = local_path(row['path'])
                if not target.is_relative_to(scope.output_root):
                    raise PermissionError('A completed Book artifact is outside the selected output folder.')
                total += row['size']
                if total > scope.max_bytes:
                    raise ValueError('The completed Book artifacts exceed the approved byte budget.')
                artifacts.append(dict(row))
            results.append({'plan': plan.id, 'index': index, 'job': state['id'],
                            'destination': destination, 'status': 'complete'})
    return results, artifacts


def execute(app, workspace, scope, step, intent, persist, cancelled, progress, production):
    _check(cancelled)
    with _services(app, workspace, scope, production, cancelled) as (books, groups):
        selected, plans, parents, reviewed, guard = _selection(books, groups, step, scope, app, cancelled)
        if step.tool.endswith('.review'):
            return _bounded(_result(selected, reviewed), scope)
        _bind(intent, step, reviewed, persist)
        if step.tool.endswith('.prepare'):
            reviewed = (groups.prepare(selected, parents, approval=reviewed.fingerprint,
                          cancelled=cancelled, guard=guard) if step.tool.startswith('book.batch.')
                        else books.prepare(selected, parents[0], approval=reviewed.fingerprint,
                          cancelled=cancelled, guard=guard))
            guard()
            result = _result(selected, reviewed)
            result['notice'] = 'Deterministic Studio jobs prepared; no speech was generated. Review this new fingerprint before Run.'
            intent['book_completed'] = True
            persist()
            return _bounded(result, scope)
        if not reviewed.prepared:
            raise PermissionError('Prepare and separately review the exact Book destinations before Run.')
        from ..studio.render import _retained_bytes
        reviews = reviewed.books if hasattr(reviewed, 'books') else (reviewed,)
        outputs = tuple(Path(path) for row in reviews for path in row.destinations)
        checked_at = None
        def execution_cancelled():
            nonlocal checked_at
            if cancelled():
                return True
            now = monotonic()
            if checked_at is None or now - checked_at >= .2:
                guard()
                if _retained_bytes(outputs, cancelled) > scope.max_bytes:
                    raise ValueError('The selected Book outputs exceed this plan’s aggregate byte budget.')
                checked_at = now
            return False
        _check(execution_cancelled)
        if step.tool.startswith('book.batch.'):
            value = groups.run(selected.id, parents, approval=reviewed.fingerprint,
                cancelled=execution_cancelled, guard=guard, progress=progress)
            complete = value['status'] == 'complete'
        else:
            value = books.run(selected.id, parents[0], approval=reviewed.fingerprint,
                cancelled=execution_cancelled, guard=guard, progress=progress)
            complete = bool(value) and all(row['status'] == 'complete' for row in value)
        if not complete:
            if cancelled() or (isinstance(value, dict) and value.get('status') in ('paused', 'cancelled')):
                raise InterruptedError('Book production stopped; exact completed parts remain reusable.')
            raise RuntimeError('Book production is incomplete; review its existing Studio jobs.')
        # Durable producer completion is rechecked independently of a late
        # stop. Subsequent assistant steps still observe the original token.
        guard()
        rows, artifacts = _accepted(books, plans, reviewed, scope, lambda: False)
        result = _result(selected, reviewed)
        result.update(status='complete', jobs=rows, artifacts=artifacts,
            notice='Original Book source and all existing Studio output receipts verified; no historical output was retargeted.')
        intent['book_completed'] = True
        persist()
        return _bounded(result, scope)


def verify_result(app, workspace, scope, step, intent, result, production, cancelled):
    """Completed/recovered plans never renew restored output authority."""
    with _services(app, workspace, scope, production, cancelled) as (books, groups):
        selected, plans, parents, reviewed, guard = _selection(books, groups, step, scope, app, cancelled)
        if (type(result) is not dict or result.get('selection') != selected.id
                or result.get('selection_sha256') != selected.fingerprint):
            raise PermissionError('The saved Book result belongs to another selection.')
        if step.tool.endswith('.run'):
            _bind(intent, step, reviewed, lambda: None)
            rows, artifacts = _accepted(books, plans, reviewed, scope, cancelled)
            if result.get('jobs') != rows or result.get('artifacts') != artifacts:
                raise PermissionError('The completed Book receipts changed; no audio was repeated.')
        elif step.tool.endswith('.prepare'):
            _bind(intent, step, reviewed, lambda: None)
            if not reviewed.prepared or result.get('review_sha256') != reviewed.fingerprint:
                raise PermissionError('The prepared Book result changed; review its current jobs.')
        else:
            if result.get('review_sha256') != reviewed.fingerprint:
                raise PermissionError('This Book review changed; request a fresh review before preparing or running.')
        guard()
