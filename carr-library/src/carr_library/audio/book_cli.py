"""Explicit headless book reviews over the existing managed audio services."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import signal
import sys
import threading

from ..application import open_existing_library
from ..installation_lease import installed_entrypoint
from ..operations import ResourceBroker, ResourceCancelled, ResourceRequest
from .book_audio import BookAudioService
from .book_batch import (BookBatchService, MAX_BOOKS, MAX_TOTAL_CHARACTERS, MAX_TOTAL_PARTS,
                         select_books)
from .book_plan import SplitOptions, capture_plan
from .jobs import read_json
from .models import Role


def _write(value, *, stream=None):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2),
          file=sys.stdout if stream is None else stream, flush=True)


@contextmanager
def _cancellation():
    stopped, previous = threading.Event(), None
    install = threading.current_thread() is threading.main_thread()
    if install:
        previous = signal.signal(signal.SIGINT, lambda number, frame: stopped.set())
    try:
        yield stopped.is_set
    finally:
        if install:
            signal.signal(signal.SIGINT, previous)


def _id(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise ValueError('Choose an exact saved Book or batch identity.')
    return value


def _hash(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('Pass the exact current review SHA-256.')
    return value


def _selection(value):
    if (type(value) is not dict or set(value) != {'schema', 'title', 'books'}
            or value['schema'] != 'carr-book-capture-request/1'
            or type(value['title']) is not str or not 0 < len(value['title'].strip()) <= 512
            or type(value['books']) is not list or not 1 <= len(value['books']) <= MAX_BOOKS):
        raise ValueError('Use a bounded carr-book-capture-request/1 with 1 to 32 explicit retained books.')
    result = []
    required = {'edition', 'options', 'role'}
    allowed = required | {'format', 'sample_rate', 'topic', 'first', 'last', 'chapter', 'revision', 'bookmark_ids'}
    for row in value['books']:
        if type(row) is not dict or not required <= set(row) <= allowed:
            raise ValueError('Each selected retained book requires edition, options and an explicit role.')
        if (type(row['edition']) is not str or not 0 < len(row['edition']) <= 1024
                or type(row['options']) is not dict or type(row['role']) is not dict):
            raise ValueError('Invalid typed retained source selection.')
        for key in ('topic', 'revision'):
            if row.get(key) is not None and (type(row[key]) is not str or not 0 < len(row[key]) <= 1024):
                raise ValueError('Invalid explicit source scope.')
        for key in ('first', 'last', 'chapter'):
            if row.get(key) is not None and (type(row[key]) is not int or not 1 <= row[key] <= 2**31 - 1):
                raise ValueError('Choose positive exact source positions.')
        bookmarks = row.get('bookmark_ids')
        if bookmarks is not None and (type(bookmarks) is not list or not 1 <= len(bookmarks) <= 10000
                or any(type(item) is not str or not 0 < len(item) <= 1024 for item in bookmarks)):
            raise ValueError('Choose a bounded exact bookmark selection.')
        try:
            selected = dict(row)
            selected['options'], selected['role'] = SplitOptions(**row['options']), Role.from_dict(row['role'])
        except (TypeError, KeyError) as exc:
            raise ValueError('Invalid explicit split or current voice parameters.') from exc
        # Runtime availability is deliberately not asserted by this inert
        # source review. The existing renderer admits the live voice at Run.
        result.append(selected)
    return value['title'], tuple(result)


def _summary(plan):
    source = plan.source
    return {'id': plan.id, 'sha256': plan.fingerprint, 'title': source['title'],
        'workspace': source['source_anchor']['workspace'], 'edition': source['source_anchor']['edition'],
        'revision': source['source_anchor']['revision'], 'topic': source['source_topic'],
        'voice': plan.role.voice, 'language': plan.role.language, 'format': plan.format,
        'sample_rate': plan.sample_rate, 'characters': len(source['text']), 'split': plan.options.mode,
        'parts': [{'index': i, 'start': part.start, 'end': part.end, 'label': part.label,
                   'project': plan.job_id(i)} for i, part in enumerate(plan.parts, 1)],
        'warnings': source['warnings'], 'quotation_authority': False}


def _review(batch, result):
    return dict(result.to_dict(), batch=batch.id, approval_sha256=result.fingerprint,
                prepared=result.prepared, executed=False)


def _parser():
    import argparse
    parser = argparse.ArgumentParser(description='Reviewed retained books through the current managed audio producer')
    parser.add_argument('--data-dir', type=Path, required=True, help='Existing private library; never implicitly created')
    parser.add_argument('--workspace', required=True, help='Exact existing library workspace ID')
    parser.add_argument('--ffmpeg', type=Path, help='Explicit local executable for an incomplete live render only')
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('capture', help='Capture and save an ordered inert review; no audio or output grant')
    capture.add_argument('--selection', type=Path, required=True, help='Bounded UTF-8 carr-book-capture-request/1 JSON')
    batch = commands.add_parser('batch', help='Save an exact ordered selection of existing reviewed plans')
    batch.add_argument('--plan', dest='plans', action='append', required=True)
    batch.add_argument('--title', default='Book audio batch')
    listing = commands.add_parser('list', help='List bounded passive saved plan or batch metadata')
    listing.add_argument('--batches', action='store_true')
    show = commands.add_parser('show', help='Show one retained plan without emitting all source text')
    show.add_argument('identity')
    status = commands.add_parser('status', help='Recorded batch state; not verified current completion')
    status.add_argument('identity')
    for name in ('review', 'prepare', 'run'):
        command = commands.add_parser(name, help={
            'review': 'Review exact current output parents and all names',
            'prepare': 'Retain ordinary Studio parts for this exact review',
            'run': 'Render or reuse verified completed parts; Ctrl+C pauses this invocation'}[name])
        command.add_argument('identity', help='Saved batch identity')
        command.add_argument('--output-parent', dest='outputs', action='append', type=Path, required=True,
                             help='One explicit existing external directory per book in saved order')
        if name != 'review':
            command.add_argument('--reviewed-sha256', required=True)
        if name == 'run':
            command.add_argument('--cancel-on-interrupt', action='store_true',
                                 help='Report this stopped invocation as cancelled instead of paused')
    for name in ('duration-review', 'duration-publish'):
        command = commands.add_parser(name, help='Exact frame partitions of one already completed Book part')
        command.add_argument('identity', help='Saved Book plan identity')
        command.add_argument('--part', type=int, required=True)
        command.add_argument('--maximum-ms', type=int, required=True)
        command.add_argument('--name-template', default='{index:04d} - {book} - {part}')
        command.add_argument('--output', type=Path, required=True, help='New .zip file; no overwrite')
        if name == 'duration-publish':
            command.add_argument('--reviewed-sha256', required=True)
    records = commands.add_parser('publications', help='Recorded measured-publication receipts only')
    records.add_argument('identity')
    records.add_argument('--part', type=int, required=True)
    verify = commands.add_parser('duration-verify', help='Verify retained archive bytes offline using an existing receipt')
    verify.add_argument('identity')
    verify.add_argument('--part', type=int, required=True)
    verify.add_argument('--receipt', required=True)
    verify.add_argument('--output', type=Path, required=True, help='Explicit current existing ZIP path, including after restore')
    return parser


@installed_entrypoint()
def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if not args.workspace or len(args.workspace) > 1024:
            raise ValueError('Choose an exact existing workspace ID.')
        if hasattr(args, 'identity'):
            _id(args.identity)
        if hasattr(args, 'reviewed_sha256'):
            _hash(args.reviewed_sha256)
        if hasattr(args, 'receipt'):
            _id(args.receipt)
        if hasattr(args, 'plans'):
            if not 1 <= len(args.plans) <= MAX_BOOKS:
                raise ValueError('Choose 1 to 32 saved Book plans.')
            tuple(_id(identity) for identity in args.plans)
        if hasattr(args, 'outputs') and not 1 <= len(args.outputs) <= MAX_BOOKS:
            raise ValueError('Choose one explicit output parent per book, at most 32.')
        if hasattr(args, 'part') and not 1 <= args.part <= MAX_TOTAL_PARTS:
            raise ValueError('Choose a completed Book part between 1 and 256.')
        if hasattr(args, 'maximum_ms'):
            from .measured_audio import options
            options(args.maximum_ms, args.name_template)
    except (ValueError, TypeError) as error:
        parser.error(str(error))
    committed_plans, committed_batch, books = [], None, None
    candidate_plans, candidate_batch = [], None
    try:
        with _cancellation() as cancelled:
            library = open_existing_library(args.data_dir, cancelled=cancelled)
            books = BookAudioService(library, args.workspace, ffmpeg=args.ffmpeg)
            groups = BookBatchService(books)
            def check():
                if cancelled():
                    raise InterruptedError('The Book command was interrupted.')
            def progress(message):
                _write({'progress': str(message)}, stream=sys.stderr)
            if args.command == 'capture':
                title, selected = _selection(read_json(args.selection, limit=128 * 1024))
                broker = ResourceBroker.for_app(library.root, cancelled=cancelled)
                with broker.reserve(ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2),
                        operation_id='document:' + __import__('uuid').uuid4().hex,
                        cancelled=cancelled, timeout=60, run_timeout=300) as lease:
                    plans, characters, parts = [], 0, 0
                    for row in selected:
                        check()
                        selection = dict(row)
                        edition = selection.pop('edition')
                        plan = capture_plan(library, args.workspace, edition, **selection, cancelled=lease.cancelled)
                        characters += len(plan.source['text'])
                        parts += len(plan.parts)
                        if characters > MAX_TOTAL_CHARACTERS or parts > MAX_TOTAL_PARTS:
                            raise ValueError('The exact selected books exceed eight million characters or 256 parts.')
                        plans.append(plan)
                    batch = select_books(tuple(plans), title=title)
                    candidate_plans, candidate_batch = [plan.id for plan in plans], batch.id
                    # All current sources are checked before the first durable
                    # review. Each source is checked again by the existing save.
                    for plan in plans:
                        books._validate(plan, lease.cancelled, check)
                    for plan in plans:
                        books.save(plan, cancelled=lease.cancelled, guard=check)
                        committed_plans.append(plan.id)
                        _write({'saved_plan': plan.id}, stream=sys.stderr)
                    books.store.save(batch, cancelled=lease.cancelled,
                        guard=lambda: [books._validate(plan, lease.cancelled, check) for plan in plans])
                    committed_batch = batch.id
                result = {'status': 'captured', 'batch': batch.id, 'batch_sha256': batch.fingerprint,
                          'books': [_summary(plan) for plan in plans], 'executed': False}
            elif args.command == 'batch':
                plans = tuple(books.load(identity) for identity in args.plans)
                batch = select_books(plans, title=args.title)
                candidate_batch = batch.id
                for plan in plans:
                    books._validate(plan, cancelled, check)
                books.store.save(batch, cancelled=cancelled,
                    guard=lambda: [books._validate(plan, cancelled, check) for plan in plans])
                committed_batch = batch.id
                result = {'status': 'saved', 'batch': batch.id, 'batch_sha256': batch.fingerprint,
                          'books': [_summary(plan) for plan in plans], 'executed': False}
            elif args.command == 'list':
                values = groups.saved(cancelled=cancelled) if args.batches else books.saved(cancelled=cancelled)
                result = {'batches' if args.batches else 'plans':
                    [{'id': item.id, 'sha256': item.fingerprint, 'title': item.title,
                      'plans': [row.plan_id for row in item.items]} for item in values]
                    if args.batches else [_summary(item) for item in values], 'executed': False}
            elif args.command == 'show':
                result = _summary(books.load(args.identity))
            elif args.command == 'status':
                result = groups.status(args.identity, cancelled=cancelled)
            elif args.command in ('review', 'prepare', 'run'):
                batch = groups.load(args.identity)
                if args.command == 'review':
                    result = _review(batch, groups.review(batch, tuple(args.outputs), cancelled=cancelled))
                elif args.command == 'prepare':
                    result = _review(batch, groups.prepare(batch, tuple(args.outputs),
                        approval=args.reviewed_sha256, cancelled=cancelled))
                else:
                    result = groups.run(batch.id, tuple(args.outputs), approval=args.reviewed_sha256,
                        cancelled=cancelled, cancel_action=lambda: 'cancel' if args.cancel_on_interrupt else 'pause',
                        progress=progress)
            elif args.command in ('duration-review', 'duration-publish'):
                options = dict(maximum_ms=args.maximum_ms, name_template=args.name_template,
                               cancelled=cancelled, progress=progress)
                if args.command == 'duration-review':
                    result = books.review_duration(args.identity, args.part, args.output, **options)
                else:
                    result = books.publish_duration(args.identity, args.part, args.output,
                                                    review_sha256=args.reviewed_sha256, **options)
            elif args.command == 'publications':
                result = {'receipts': list(books.duration_publications(args.identity, args.part)),
                          'verified_outputs': False}
            else:
                result = books.verify_duration(args.identity, args.part, args.receipt,
                                               output=args.output, cancelled=cancelled)
            _write(result)
            return 130 if result.get('status') in ('paused', 'cancelled') else 0
    except (KeyboardInterrupt, InterruptedError, ResourceCancelled):
        _write({'status': 'interrupted', 'saved_plans': committed_plans, 'saved_batch': committed_batch,
                'candidate_plans': candidate_plans, 'candidate_batch': candidate_batch,
                'notice': 'Existing source reviews, managed checkpoints and accepted publications remain retained.'},
               stream=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        from .book_export import BookExportPublished
        result = {'status': 'error', 'message': str(error),
                  'saved_plans': committed_plans, 'saved_batch': committed_batch,
                  'candidate_plans': candidate_plans, 'candidate_batch': candidate_batch}
        if isinstance(error, BookExportPublished):
            result.update(status='published_durability_unconfirmed', destination=str(error.destination))
        _write(result, stream=sys.stderr)
        return 1
    finally:
        if books is not None:
            books.close()


if __name__ == '__main__':
    raise SystemExit(main())
