"""Explicit headless book reviews over the existing Studio jobs and receipts."""
import argparse
from contextlib import ExitStack
from dataclasses import asdict
import json
from pathlib import Path
import sys
import threading

from ..durable_state import atomic_json, encode, local_path, read_json, regular
from .book_voice import select_voice
from .book_audio import BookAudioService, BookPlan, SplitOptions, capture_plan
from .book_batch import BookBatch, BookBatchService, MAX_BOOKS, select_books, validate_plans
from .project import Role


def capture_request(books, request, *, topic=None, cancelled=lambda: False):
    """Resolve explicit selections once, then retain their immutable settings."""
    if (type(request) is not dict or set(request) != {'schema', 'title', 'books'}
            or request['schema'] != 'carr-book-batch-request/1'
            or type(request['books']) is not list or not 1 <= len(request['books']) <= MAX_BOOKS):
        raise ValueError('Use a bounded carr-book-batch-request/1 with title and books.')
    registry = books.discover_voices(cancelled=cancelled)
    voices = tuple(registry.voices())
    plans, outputs = [], []
    for item in request['books']:
        required = {'edition', 'voice', 'output_parent'}
        allowed = required | {'revision', 'first', 'last', 'chapter', 'options', 'format', 'sample_rate', 'bookmark_ids'}
        if type(item) is not dict or not required <= set(item) <= allowed:
            raise ValueError('Each book needs its library edition, explicit voice choice and output parent.')
        choice = item['voice']
        selected = select_voice(voices, choice)
        role = Role('narrator', 'Narrator', **selected)
        options = item.get('options', {})
        if type(options) is not dict:
            raise ValueError('Book split options must be an object.')
        try:
            options = SplitOptions(**options)
        except TypeError as exc:
            raise ValueError('Unknown book split option.') from exc
        plan = capture_plan(books.library, books.workspace, item['edition'], topic=topic,
            revision=item.get('revision'), first=item.get('first'), last=item.get('last'),
            chapter=item.get('chapter'), options=options, role=role,
            format=item.get('format', 'wav'), sample_rate=item.get('sample_rate', 24000),
            bookmark_ids=item.get('bookmark_ids'),
            cancelled=cancelled)
        if sum(len(existing.source['text']) for existing in plans) + len(plan.source['text']) > 8_000_000:
            raise ValueError('The selected books exceed the aggregate text review budget.')
        plans.append(plan)
        outputs.append(item['output_parent'])
    batch = select_books(plans, title=request['title'])
    return batch, tuple(plans), tuple(outputs)


def reviewed_document(batch, plans, outputs, review):
    validate_plans(batch, plans)
    return {'schema': 'carr-book-batch-review/1', 'batch': batch.to_dict(),
            'plans': [plan.to_dict() for plan in plans], 'outputs': list(outputs),
            'review_sha256': review.fingerprint}


def read_review(path, workspace, topic):
    value = read_json(path)
    if (type(value) is not dict or set(value) != {'schema', 'batch', 'plans', 'outputs', 'review_sha256'}
            or value['schema'] != 'carr-book-batch-review/1'
            or type(value['plans']) is not list or type(value['outputs']) is not list):
        raise ValueError('Use the exact saved book-batch preview document.')
    batch = BookBatch.from_dict(value['batch'])
    if (batch.workspace, batch.topic) != (workspace, topic):
        raise ValueError('The saved preview belongs to another library/topic scope.')
    plans = validate_plans(batch, tuple(BookPlan.from_dict(plan) for plan in value['plans']))
    if len(value['outputs']) != len(plans):
        raise ValueError('The saved preview needs one explicit output parent per book.')
    return batch, plans, tuple(value['outputs']), value['review_sha256']


def chosen_outputs(batch, declarations):
    """Every invocation names its plan/output mapping; restored paths are inert."""
    outputs = {}
    for declaration in declarations:
        identity, separator, destination = declaration.partition('=')
        if not separator or not destination or identity in outputs:
            raise ValueError('Repeat --output PLAN_ID=EXISTING_DIRECTORY once per selected book.')
        outputs[identity] = destination
    if set(outputs) != {item.plan_id for item in batch.items}:
        raise ValueError('Explicitly review an output parent for every book; no stored path was assumed.')
    return tuple(outputs[item.plan_id] for item in batch.items)


def review_summary(batch, plans, review):
    return {'batch': batch.id, 'title': batch.title, 'prepared': review.prepared,
            'approval': review.fingerprint, 'notice': 'Run requires a separate explicit approval; no automatic speech.',
            'books': [{'plan': plan.id, 'title': plan.source['title'],
                'source_anchor': plan.source['source_anchor'],
                'source_metadata': plan.source['source_metadata'], 'warnings': plan.source['warnings'],
                'voice': plan.role.to_dict(), 'format': plan.format, 'sample_rate': plan.sample_rate,
                'split': asdict(plan.options), 'parts': [asdict(part) for part in plan.parts],
                **({'landmarks': plan.landmarks} if plan.landmarks_json else {}),
                **asdict(book)} for plan, book in zip(plans, review.books)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reviewed multi-book audio using existing local Studio jobs')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--workspace', required=True, help='Existing library identity')
    parser.add_argument('--topic', help='Exact existing topic scope, if selected')
    operations = parser.add_subparsers(dest='operation', required=True)
    for operation in ('duration-review', 'duration-publish'):
        measured = operations.add_parser(operation, help='Review/publish exact frames of an already completed Book part')
        measured.add_argument('plan_id')
        measured.add_argument('--part', type=int, required=True, help='Exact completed Book part index')
        measured.add_argument('--output', type=Path, required=True, help='New PCM-parts .zip outside private library data')
        measured.add_argument('--milliseconds', type=int, required=True, help='Maximum measured PCM interval; never a text estimate')
        measured.add_argument('--name-template', default='{index:04d} - {book} - {part}')
        measured.add_argument('--timeout', type=float, default=3600)
        measured.add_argument('--max-bytes', type=int, default=32 * 1024**3)
        if operation == 'duration-publish':
            measured.add_argument('--approval', required=True, help='Exact current duration-review SHA-256')
    bookmarks = operations.add_parser('bookmarks', help='List existing reading bookmark IDs; no jobs or speech')
    bookmarks.add_argument('edition', help='Existing library edition identity')
    bookmarks.add_argument('--revision', help='Exact source revision to review')
    preview = operations.add_parser('preview', help='Capture sources and names; no jobs or speech')
    preview.add_argument('--request', type=Path, required=True)
    preview.add_argument('--review', type=Path, required=True, help='New review JSON outside private library data')
    prepare = operations.add_parser('prepare', help='Create ordinary Studio jobs; no speech')
    selection = prepare.add_mutually_exclusive_group(required=True)
    selection.add_argument('--review', type=Path, help='Exact initial preview JSON')
    selection.add_argument('--batch', help='Existing saved selection, including after restore')
    prepare.add_argument('--output', action='append', default=[], metavar='PLAN_ID=DIRECTORY',
                         help='Explicit output parents when preparing a saved --batch')
    prepare.add_argument('--approval', required=True, help='Exact SHA-256 displayed by preview')
    fresh = operations.add_parser('fresh-review',
        help='Review new job identities after restore; historical jobs stay unchanged')
    fresh.add_argument('batch_id')
    fresh.add_argument('--output', action='append', required=True, metavar='OLD_PLAN_ID=DIRECTORY')
    fresh.add_argument('--review', type=Path, required=True, help='New review JSON outside private library data')
    operations.add_parser('list', help='List saved immutable selections; no source/engine activation')
    for operation in ('status', 'pause', 'cancel', 'review', 'run'):
        command = operations.add_parser(operation)
        command.add_argument('batch_id')
        if operation in ('review', 'run'):
            command.add_argument('--output', action='append', required=True, metavar='PLAN_ID=DIRECTORY')
        if operation == 'run':
            command.add_argument('--approval', required=True, help='Exact current prepared review SHA-256')
    args = parser.parse_args(argv)
    books = None
    runtime_context = ExitStack()
    cancelled = threading.Event()
    try:
        from ..application import LibraryApp
        from .cli import data_directory
        app = LibraryApp(data_directory(args.data_dir))
        if args.operation in ('run', 'duration-review', 'duration-publish'):
            from ..audio.local_runtime import open_local_audio_runtime
            runtime_context.enter_context(open_local_audio_runtime(
                app.root, strict=True, cancelled=cancelled.is_set))
        books = BookAudioService(app, args.workspace)
        groups = BookBatchService(books)
        if args.operation in ('duration-review', 'duration-publish'):
            from ..audio import AudioBudget
            selected = books.load(args.plan_id)
            if selected.source['source_topic'] != args.topic:
                raise ValueError('Choose the completed Book plan in its exact library/topic scope.')
            options = dict(maximum_ms=args.milliseconds, name_template=args.name_template,
                budget=AudioBudget(timeout=args.timeout, max_output_bytes=args.max_bytes),
                cancelled=cancelled.is_set,
                progress=lambda message: print(message, file=sys.stderr, flush=True))
            if args.operation == 'duration-review':
                result = books.review_duration(selected.id, args.part, args.output, **options)
            else:
                result = books.publish_duration(selected.id, args.part, args.output, approval=args.approval, **options)
        elif args.operation == 'bookmarks':
            from .book_landmarks import available_bookmarks
            result = [{'bookmark': asdict(row['bookmark']), 'current_revision': row['current_revision']}
                      for row in available_bookmarks(app, args.workspace, args.edition,
                          topic=args.topic, revision=args.revision, cancelled=cancelled.is_set)]
        elif args.operation == 'preview':
            if regular(args.request).st_size > 1024 * 1024:
                raise ValueError('The book selection request exceeds one MiB.')
            destination = local_path(args.review)
            local_path(destination.parent, exists=True)
            if destination.is_relative_to(app.root):
                raise ValueError('Save the explicit review document outside private library data.')
            if destination.exists():
                raise FileExistsError('The review file already exists and will not be overwritten.')
            request = read_json(args.request)
            if len(encode(request)) > 1024 * 1024:
                raise ValueError('The book selection request exceeds one MiB.')
            batch, plans, outputs = capture_request(books, request, topic=args.topic,
                                                    cancelled=cancelled.is_set)
            review = groups.review(batch, outputs, plans=plans, cancelled=cancelled.is_set)
            atomic_json(destination, reviewed_document(batch, plans, outputs, review))
            result = review_summary(batch, plans, review)
            result['review_file'] = str(destination)
        elif args.operation == 'fresh-review':
            previous = groups.load(args.batch_id)
            if previous.topic != args.topic:
                raise ValueError('Choose this saved batch in its exact library/topic scope.')
            outputs = chosen_outputs(previous, args.output)
            destination = local_path(args.review)
            local_path(destination.parent, exists=True)
            if destination.is_relative_to(app.root):
                raise ValueError('Save the explicit review document outside private library data.')
            if destination.exists():
                raise FileExistsError('The review file already exists and will not be overwritten.')
            batch, plans = groups.fresh(previous.id, cancelled=cancelled.is_set)
            review = groups.review(batch, outputs, plans=plans, cancelled=cancelled.is_set)
            atomic_json(destination, reviewed_document(batch, plans, outputs, review))
            result = review_summary(batch, plans, review)
            result['review_file'] = str(destination)
        elif args.operation == 'prepare':
            if args.review is not None:
                if args.output:
                    raise ValueError('An initial preview uses its exact reviewed output parents; capture a new preview to change them.')
                batch, plans, outputs, original_approval = read_review(args.review, args.workspace, args.topic)
                if args.approval != original_approval:
                    raise ValueError('Approve the exact saved preview fingerprint before preparing jobs.')
            else:
                batch = groups.load(args.batch)
                if batch.topic != args.topic:
                    raise ValueError('Choose this saved batch in its exact library/topic scope.')
                plans = groups.plans(batch)
                outputs = chosen_outputs(batch, args.output)
            review = groups.prepare(batch, outputs, plans=plans, approval=args.approval,
                                    cancelled=cancelled.is_set)
            result = review_summary(batch, plans, review)
        elif args.operation == 'list':
            result = [{'batch': batch.id, 'title': batch.title, 'books': len(batch.items)}
                      for batch in groups.saved() if batch.topic == args.topic]
        else:
            batch = groups.load(args.batch_id)
            if batch.topic != args.topic:
                raise ValueError('Choose this saved batch in its exact library/topic scope.')
            if args.operation == 'status':
                result = groups.status(batch.id)
            elif args.operation in ('pause', 'cancel'):
                result = groups.request_stop(batch.id, args.operation)
            else:
                outputs = chosen_outputs(batch, args.output)
                if args.operation == 'review':
                    plans = groups.plans(batch)
                    result = review_summary(batch, plans, groups.review(batch, outputs, cancelled=cancelled.is_set))
                else:
                    result = groups.run(batch.id, outputs, approval=args.approval, cancelled=cancelled.is_set,
                        progress=lambda message: print(message, file=sys.stderr, flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        cancelled.set()
        print('Book audio interrupted. Existing Studio items remain saved; review before explicitly resuming.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Book audio: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        try:
            if books is not None:
                books.close()
        finally:
            runtime_context.close()


if __name__ == '__main__':
    raise SystemExit(main())
