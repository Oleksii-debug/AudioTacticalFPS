"""Explicit headless book selection over existing Studio jobs and receipts."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys
import threading

from ..durable_state import encode, read_json
from ..installation_lease import installed_entrypoint
from ..language_qualification.catalog import canonical_language_tag
from ..research.export_file import capture_export_destination, write_binary_export
from .book_audio import BookAudioService, BookPlan, SplitOptions, capture_plan
from .book_batch import (BookBatch, BookBatchService, BookBatchSelectionCommitted,
                         MAX_BOOKS, select_books, validate_plans)
from .project import Role
from .publishing import MeasuredPublicationCommitted

# A failed provider close must not lose its last owner when this entry point is
# called from another Python host. No worker, retry thread or second queue exists.
_pending_cleanup = []
_cleanup_lock = threading.RLock()


def retry_pending_cleanup():
    """Explicitly retry retained owners on the caller's existing worker."""
    with _cleanup_lock:
        failures = []
        for owner in tuple(_pending_cleanup):
            try:
                owner.close()
            except Exception:
                failures.append(owner)
            else:
                _pending_cleanup.remove(owner)
        return len(failures)


def capture_request(books, request, *, topic=None, cancelled=lambda: False):
    """Freeze an explicitly installed voice and its supported controls per book."""
    if (type(request) is not dict or set(request) != {'schema', 'title', 'books'}
            or request['schema'] != 'carr-book-batch-request/1'
            or type(request['books']) is not list or not 1 <= len(request['books']) <= MAX_BOOKS):
        raise ValueError('Use a bounded carr-book-batch-request/1 with title and books.')
    books._check_scope()
    selections = []
    for item in request['books']:
        required = {'edition', 'voice', 'output_parent'}
        allowed = required | {'revision', 'first', 'last', 'chapter', 'options', 'format', 'sample_rate'}
        if type(item) is not dict or not required <= set(item) <= allowed:
            raise ValueError('Each book needs its library edition, explicit voice choice and output parent.')
        choice = item['voice']
        if (type(choice) is not dict or not {'language', 'voice_id'} <= set(choice)
                or not set(choice) <= {'language', 'voice_id', 'rate', 'volume', 'effects'}
                or type(choice['voice_id']) is not str or not choice['voice_id']):
            raise ValueError('Choose an exact installed voice ID, language and supported controls.')
        language = canonical_language_tag(choice['language'])
        if language != choice['language']:
            raise ValueError('Use the exact canonical language tag of the selected installed voice.')
        from ..audio.effect_presets import read_effect
        effects = choice.get('effects', [])
        if type(effects) is not list or len(effects) > 32:
            raise ValueError('Choose an ordered list of at most 32 typed role effects.')
        role = Role('narrator', 'Narrator', choice['voice_id'], language,
                    rate=choice.get('rate', 0), volume=choice.get('volume', 100),
                    effects=tuple(read_effect(effect) for effect in effects))
        if type(item['output_parent']) is not str or not item['output_parent']:
            raise ValueError('Choose an explicit output parent for every book.')
        options = item.get('options', {})
        if type(options) is not dict:
            raise ValueError('Book split options must be an object.')
        try:
            options = SplitOptions(**options)
        except TypeError as exc:
            raise ValueError('Unknown book split option.') from exc
        selections.append((item, role, options))
    if cancelled():
        raise InterruptedError('Book selection cancelled before voice discovery.')
    registry = books.production.discover_voices(cancelled=cancelled)
    voices = tuple(registry.voices())
    books._check_scope()
    plans, outputs = [], []
    for item, role, options in selections:
        voice = next((voice for voice in voices if voice.id == role.voice_id), None)
        if voice is None or canonical_language_tag(voice.language, adapter_underscores=True) != role.language:
            raise ValueError('The exact installed voice and language are unavailable; no fallback was selected.')
        capabilities = getattr(voice, 'capabilities', ())
        if (role.rate != 0 and 'rate' not in capabilities
                or role.volume != 100 and 'volume' not in capabilities):
            raise ValueError('The selected installed voice does not support these controls.')
        plan = capture_plan(books.library, books.workspace, item['edition'], topic=topic,
            revision=item.get('revision'), first=item.get('first'), last=item.get('last'),
            chapter=item.get('chapter'), options=options, role=role,
            format=item.get('format', 'wav'), sample_rate=item.get('sample_rate', 24000),
            cancelled=cancelled, guard=books._check_scope)
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
    # Use the same bounded JSON contract as encode() in _publish_review.
    value = read_json(path)
    if (type(value) is not dict or set(value) != {'schema', 'batch', 'plans', 'outputs', 'review_sha256'}
            or value['schema'] != 'carr-book-batch-review/1'
            or type(value['plans']) is not list or not 1 <= len(value['plans']) <= MAX_BOOKS
            or type(value['outputs']) is not list
            or type(value['review_sha256']) is not str
            or re.fullmatch('[0-9a-f]{64}', value['review_sha256']) is None):
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
                **asdict(book)} for plan, book in zip(plans, review.books)]}


def _publish_review(books, groups, batch, plans, outputs, review, path, cancelled):
    def checked():
        if cancelled():
            return True
        books._check_scope()
        for plan in plans:
            books._validate(plan, cancelled, books._check_scope)
        if groups.review(batch, outputs, plans=plans, cancelled=cancelled).fingerprint != review.fingerprint:
            raise ValueError('The exact book review changed before its file was published.')
        return bool(cancelled())

    approval = capture_export_destination(path, private_root=books.library.root, cancelled=checked)
    if not approval.require_absent:
        raise FileExistsError('The review file already exists and will not be overwritten.')
    raw = encode(reviewed_document(batch, plans, outputs, review))
    return write_binary_export(path, raw, private_root=books.library.root,
        cancelled=checked, require_absent=True, approval=approval)


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description='Reviewed multi-book audio using existing local Studio jobs')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--workspace', required=True, help='Existing library identity')
    parser.add_argument('--topic', help='Exact existing topic scope, if selected')
    operations = parser.add_subparsers(dest='operation', required=True)
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
    operations.add_parser('list', help='List saved immutable selections; no engine activation')
    for operation in ('status', 'pause', 'cancel', 'review', 'run'):
        command = operations.add_parser(operation)
        command.add_argument('batch_id')
        if operation in ('review', 'run'):
            command.add_argument('--output', action='append', required=True, metavar='PLAN_ID=DIRECTORY')
        if operation == 'run':
            command.add_argument('--approval', required=True, help='Exact current prepared review SHA-256')
    for operation in ('duration-review', 'duration-publish'):
        command = operations.add_parser(operation, help='Review or publish measured frames from a completed Book part')
        command.add_argument('plan_id')
        command.add_argument('--part', required=True, type=int)
        command.add_argument('--output', required=True, type=Path, help='New ZIP outside library and completed job')
        command.add_argument('--maximum-ms', required=True, type=int, help='Maximum actual PCM duration per part')
        command.add_argument('--name-template', default='{index:04d} - {book} - {part}')
        command.add_argument('--timeout', type=float, default=3600)
        command.add_argument('--max-output-bytes', type=int, default=32 * 1024**3)
        command.add_argument('--reserve-disk-bytes', type=int, default=32 * 1024**2)
        if operation == 'duration-publish':
            command.add_argument('--approval', required=True, help='Exact SHA-256 from duration-review')
    args = parser.parse_args(argv)
    books, result, code = None, None, 0
    cancelled = threading.Event()
    try:
        with _cleanup_lock:
            if _pending_cleanup:
                raise RuntimeError('An earlier Book audio owner still needs explicit cleanup; no new operation started.')
        from ..application import LibraryApp
        from .cli import data_directory
        app = LibraryApp(data_directory(args.data_dir))
        books = BookAudioService(app, args.workspace)
        groups = BookBatchService(books)
        if args.operation == 'preview':
            request = read_json(args.request, maximum=1024 * 1024)
            batch, plans, outputs = capture_request(books, request, topic=args.topic,
                                                    cancelled=cancelled.is_set)
            review = groups.review(batch, outputs, plans=plans, cancelled=cancelled.is_set)
            published = _publish_review(books, groups, batch, plans, outputs, review,
                                        args.review, cancelled.is_set)
            result = review_summary(batch, plans, review)
            result['review_file'] = str(published.path)
            if published.cleanup_warning:
                result['cleanup_notice'] = published.cleanup_warning
                code = 3
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
        elif args.operation in ('duration-review', 'duration-publish'):
            from ..audio.models import AudioBudget
            plan = books.load(args.plan_id)
            if plan.source['source_topic'] != args.topic:
                raise ValueError('Choose the saved Book plan in its exact library/topic scope.')
            options = dict(maximum_ms=args.maximum_ms, name_template=args.name_template,
                budget=AudioBudget(timeout=args.timeout, max_output_bytes=args.max_output_bytes,
                                   reserve_disk_bytes=args.reserve_disk_bytes),
                cancelled=cancelled.is_set, guard=books._check_scope,
                progress=lambda message: print(message, file=sys.stderr, flush=True))
            if args.operation == 'duration-review':
                result = books.review_duration(plan.id, args.part, args.output, **options)
            else:
                result = books.publish_duration(plan.id, args.part, args.output,
                                                approval=args.approval, **options)
        elif args.operation == 'list':
            result = [{'batch': batch.id, 'title': batch.title, 'books': len(batch.items)}
                      for batch in groups.saved(cancelled=cancelled.is_set) if batch.topic == args.topic]
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
                    if result.get('control_cleanup_pending') or result.get('checkpoint_read_pending'):
                        code = 3
    except KeyboardInterrupt as exc:
        cancelled.set()
        result = getattr(exc, 'publication_receipt', getattr(exc, 'book_batch_result', None))
        print('Book audio interrupted. Existing Studio items remain saved; review before explicitly resuming.', file=sys.stderr)
        code = 130
    except BookBatchSelectionCommitted as exc:
        result = {'batch': exc.batch, 'selection_saved': True, 'durable': False,
                  'notice': 'Selection is visible but storage confirmation needs review. No new speech was started.'}
        code = 3
    except MeasuredPublicationCommitted as exc:
        result = {'published': True, 'receipt': exc.receipt,
                  'notice': str(exc), 'requires_explicit_verification': True}
        print(str(exc), file=sys.stderr)
        code = 3
    except (ValueError, OSError, RuntimeError) as exc:
        result = getattr(exc, 'book_batch_result', None)
        print('Book audio: ' + str(exc), file=sys.stderr)
        code = 1
    finally:
        if books is not None:
            try:
                books.close()
            except Exception:
                with _cleanup_lock:
                    if books not in _pending_cleanup:
                        _pending_cleanup.append(books)
                notice = 'Saved results remain available. The local voice owner needs explicit cleanup.'
                if isinstance(result, dict):
                    result = dict(result, cleanup_pending=True, cleanup_notice=notice)
                elif result is not None:
                    result = {'result': result, 'cleanup_pending': True, 'cleanup_notice': notice}
                print(notice, file=sys.stderr)
                if code == 0:
                    code = 3
    if result is not None:
        # Unicode values survive isolated Windows pipes with older code pages.
        print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
