"""Reviewed plaintext translation commands using the existing jobs and gateway.

No command accepts a secret on argv. Configure credentials in the native account
editor; preview, language-cache reads and job listing never make provider calls.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import signal
import sys
import threading
from time import time
from uuid import uuid4

from .models import canonical, local_path
from .profiles import ProfileStore
from .providers import Authorization, Gateway, money
from .translation import TranslationService, translation_approval
from .translation_batch import TranslationBatchService, MAX_ITEMS, MAX_CHARACTERS
from ..application import LibraryApp
from ..installation_lease import installed_entrypoint


def _input(path):
    source = local_path(path)
    if not source.is_file() or source.stat().st_size > 8_000_000:
        raise ValueError('Choose a UTF-8 plain-text file of at most 8 MB.')
    with source.open('rb') as stream:
        raw = stream.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError('The selected text exceeds the byte budget.')
    # This exact immutable text is used for review/approval/submission. A later
    # file-path read cannot replace the content sent to the shared service.
    return raw.decode('utf-8', errors='strict')


def _review(service, profile, args):
    context = {}
    if args.edition is not None:
        if not args.workspace:
            raise ValueError('Choose the existing source workspace with --workspace.')
        selected = service.select_source(args.workspace, args.edition, topic=args.topic,
            chapter=args.chapter, first=args.first, last=args.last)
        value = selected['text']
        context = {key: selected[key] for key in ('source_anchor', 'source_metadata', 'source_topic')}
    else:
        if any(item is not None for item in (args.workspace, args.topic, args.chapter, args.first, args.last)):
            raise ValueError('Library source selectors require --edition; file inputs remain plain text.')
        value = _input(args.input)
    preview = service.preview(profile, value, args.source, args.target,
                              glossary=args.glossary, rate_per_million=args.rate_per_million, **context)
    bound = str(money(args.budget))
    if preview['estimated_cost'] is not None and money(preview['estimated_cost']) > money(bound):
        raise ValueError('The entered rate estimate exceeds the approved total budget.')
    review = {**preview, 'profile': profile.to_dict(), 'budget': bound, 'title': args.title}
    return value, {**review, 'review_sha256': sha256(canonical(review).encode('utf-8')).hexdigest()}


def _batch_input(service, store, path, *, cancelled=lambda: False):
    """Resolve only explicitly listed files or existing library selections."""
    selected = local_path(path)
    if not selected.is_file() or selected.stat().st_size > 131072:
        raise ValueError('Choose a translation batch JSON description of at most 128 KiB.')
    with selected.open('rb') as stream:
        raw = stream.read(131073)
    if len(raw) > 131072:
        raise ValueError('The batch description exceeds its byte budget.')
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('A batch description contains duplicate JSON keys.')
            result[key] = value
        return result
    description = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=pairs)
    if (type(description) is not dict or set(description) != {'schema', 'title', 'items'}
            or description['schema'] != 'carr-translation-batch-input/1'
            or type(description['items']) is not list
            or not 1 <= len(description['items']) <= MAX_ITEMS):
        raise ValueError('Use a typed batch description with 1–32 explicitly selected documents.')
    required = {'profile', 'target', 'budget'}
    optional = {'input', 'workspace', 'edition', 'topic', 'chapter', 'first', 'last',
                'source', 'glossary', 'rate_per_million', 'title'}
    items, count = [], 0
    for row in description['items']:
        if cancelled():
            raise InterruptedError('Translation batch input review cancelled.')
        if type(row) is not dict or not required <= set(row) or set(row) - required - optional:
            raise ValueError('Unknown or missing translation batch item fields.')
        strings = {'profile', 'source', 'target', 'budget', 'input', 'workspace', 'edition', 'title'}
        if (any(key in row and type(row[key]) is not str for key in strings)
                or any(key in row and row[key] is not None and type(row[key]) is not str
                       for key in ('topic', 'glossary', 'rate_per_million'))
                or any(key in row and row[key] is not None and type(row[key]) is not int
                       for key in ('chapter', 'first', 'last'))):
            raise ValueError('Use explicit text settings and integer source positions in each batch item.')
        if ('input' in row) == ('edition' in row):
            raise ValueError('Each batch item selects one UTF-8 file or one existing library edition.')
        profile = store.get(row['profile'])
        context = {}
        if 'edition' in row:
            if 'workspace' not in row:
                raise ValueError('A library edition requires its explicit workspace.')
            record = service.select_source(row['workspace'], row['edition'], topic=row.get('topic'),
                chapter=row.get('chapter'), first=row.get('first'), last=row.get('last'), cancelled=cancelled)
            value, default_title = record['text'], record['title']
            context = {key: record[key] for key in ('source_anchor', 'source_metadata', 'source_topic')}
        else:
            if any(key in row for key in ('workspace', 'topic', 'chapter', 'first', 'last')):
                raise ValueError('Library selectors cannot be attached to a plain-text file.')
            if type(row['input']) is not str or not row['input']:
                raise ValueError('Choose an explicit UTF-8 input pathname.')
            source = Path(row['input'])
            if not source.is_absolute():
                source = selected.parent / source
            value, default_title = _input(source), source.name
        count += len(value)
        if count > MAX_CHARACTERS:
            raise ValueError('The selected batch exceeds two million characters.')
        items.append({'profile': profile.to_dict(), 'text': value,
            'title': row.get('title', default_title), 'source': row.get('source', 'auto'),
            'target': row['target'], 'budget': row['budget'], 'glossary': row.get('glossary'),
            'rate_per_million': row.get('rate_per_million'), **context})
    if cancelled():
        raise InterruptedError('Translation batch input review cancelled.')
    return description['title'], items


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description='Reviewed Google Basic v2 / Advanced v3 plain-text translation')
    parser.add_argument('--data-dir', type=Path, required=True, help='Existing private Resonance library folder')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('profiles', help='List non-secret configured translation accounts')
    languages = sub.add_parser('languages', help='Read the cache, or explicitly fetch current provider declarations')
    languages.add_argument('--profile', required=True)
    languages.add_argument('--approve-remote', action='store_true', help='Approve one official language-list request; no text is sent')
    for name in ('preview', 'submit'):
        action = sub.add_parser(name, help='Review exact text/settings' if name == 'preview' else 'Approve the unchanged preview and save a paused-resumable job')
        action.add_argument('--profile', required=True)
        selected = action.add_mutually_exclusive_group(required=True)
        selected.add_argument('--input', type=Path, help='Selected UTF-8 plain text; no document formatting claim')
        selected.add_argument('--edition', help='Existing library edition; default selects its entire bounded document')
        action.add_argument('--workspace', help='Existing workspace ID for --edition')
        action.add_argument('--topic', help='Optional existing topic scope, rechecked before transfer and publication')
        positions = action.add_mutually_exclusive_group()
        positions.add_argument('--chapter', type=int, help='One existing EPUB chapter ordinal')
        positions.add_argument('--first', type=int, help='First source reading-position ordinal; requires --last')
        action.add_argument('--last', type=int, help='Last source reading-position ordinal')
        action.add_argument('--source', default='auto')
        action.add_argument('--target', required=True)
        action.add_argument('--glossary', help='Existing Advanced v3 glossary resource in the same project/non-global region')
        action.add_argument('--budget', required=True, help='Explicit total USD reservation, not provider-enforced billing')
        action.add_argument('--rate-per-million', help='Optional owner-entered current contract rate; no live price lookup')
        action.add_argument('--title', default='Translation')
        if name == 'submit':
            action.add_argument('--approve-remote', action='store_true', help='Approve external transfer of this reviewed text')
            action.add_argument('--review-sha256', required=True, help='Exact value returned by preview for unchanged text/settings')
    sub.add_parser('jobs', help='List saved statuses without source-text disclosure')
    resume = sub.add_parser('resume', help='Run an already approved job; Ctrl+C pauses before the next bounded request')
    resume.add_argument('job')
    export = sub.add_parser('export', help='Publish a completed derivative into a fresh directory')
    export.add_argument('job')
    export.add_argument('--output', type=Path, required=True)
    for name in ('batch-preview', 'batch-submit'):
        action = sub.add_parser(name, help='Review or prepare explicitly listed documents; sends no text')
        action.add_argument('--batch-file', type=Path, required=True, help='Bounded carr-translation-batch-input/1 JSON')
        if name == 'batch-submit':
            action.add_argument('--approve-remote', action='store_true')
            action.add_argument('--review-sha256', required=True)
    sub.add_parser('batch-jobs', help='List existing child checkpoints without sending text')
    for name in ('batch-review', 'batch-prepare', 'batch-resume', 'batch-export'):
        action = sub.add_parser(name, help='Use the exact saved batch and its existing child approvals')
        action.add_argument('batch')
        if name == 'batch-export':
            action.add_argument('--output', type=Path, required=True, help='New explicit output directory')
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = None
    try:
        root = local_path(args.data_dir)
        if not (root / 'library.sqlite').is_file():
            raise ValueError('Choose an existing Resonance library with library.sqlite.')
        store = ProfileStore(root / 'state' / 'providers')
        service = TranslationService(root / 'state' / 'translation', library=LibraryApp(root))
        if args.command.startswith('batch-'):
            batches = TranslationBatchService(service)
            if args.command not in ('batch-jobs', 'batch-review'):
                previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
            if args.command in ('batch-preview', 'batch-submit'):
                title, items = _batch_input(service, store, args.batch_file, cancelled=stop.is_set)
                if args.command == 'batch-preview':
                    result = batches.preview(items, title=title, cancelled=stop.is_set)
                else:
                    identity = batches.submit(items, title=title, review_sha256=args.review_sha256,
                        approve_remote=args.approve_remote, cancelled=stop.is_set)
                    result = {**batches.get(identity), 'network_access': False,
                              'next': 'batch-resume this ID to use its exact existing document approvals'}
            elif args.command == 'batch-jobs':
                result = list(batches.list())
            elif args.command == 'batch-review':
                result = {'review': batches.get_review(args.batch), 'checkpoints': batches.get(args.batch)}
            elif args.command == 'batch-prepare':
                result = batches.prepare(args.batch, cancelled=stop.is_set)
            elif args.command == 'batch-resume':
                service.gateway = Gateway(service.directory / 'provider', mode='APPROVED_REMOTE')
                result = batches.resume(args.batch, cancelled=stop.is_set,
                    progress=lambda done, total, phase: print(f'{done}/{total}: {phase}', file=sys.stderr, flush=True))
            else:
                result = batches.export(args.batch, args.output, cancelled=stop.is_set)
        elif args.command == 'profiles':
            result = [{key: p.to_dict()[key] for key in ('id', 'name', 'adapter', 'endpoint', 'model', 'enabled')}
                      for p in store.list() if p.adapter in ('google-translation-v2', 'google-translation-v3')]
        elif args.command == 'languages':
            p = store.get(args.profile)
            if args.approve_remote:
                identity = uuid4().hex
                approval = Authorization(identity, identity, ((p.id, p.endpoint),),
                    ('translation.languages',), ('user-instruction',), '0', 1, time() + 600)
                service.gateway = Gateway(service.directory / 'provider', mode='APPROVED_REMOTE')
                previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
                result = service.refresh_languages(p, authorization=approval, cancelled=stop.is_set)
            else:
                result = service.languages(p)
        elif args.command in ('preview', 'submit'):
            p = store.get(args.profile)
            value, result = _review(service, p, args)
            if args.command == 'submit':
                if not args.approve_remote or args.review_sha256 != result['review_sha256']:
                    raise PermissionError('Explicitly approve the exact current preview hash before saving a remote job.')
                context = {key: result[key] for key in ('source_anchor', 'source_metadata', 'source_topic') if key in result}
                approval, cost = translation_approval(p, value, args.source, args.target, args.budget,
                                                     glossary=args.glossary, **context)
                identity = service.submit(p, value, args.source, args.target, title=args.title,
                    glossary=args.glossary, authorization=approval, per_call_cost=cost, **context)
                result = {'id': identity, 'status': 'queued', 'review_sha256': result['review_sha256'],
                          'network_access': False, 'next': 'resume this ID to run its existing approved envelope'}
        elif args.command == 'jobs':
            result = [{key: job.get(key) for key in ('id', 'title', 'status', 'source_language', 'target_language', 'glossary')}
                      | {'completed': len(job['receipts']), 'total': len(job['chunks'])} for job in service.list()]
        elif args.command == 'resume':
            service.gateway = Gateway(service.directory / 'provider', mode='APPROVED_REMOTE')
            previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
            result = service.resume(args.job, cancelled=stop.is_set,
                progress=lambda done, total, phase: print(f'{done}/{total}: {phase}', file=sys.stderr, flush=True))
        else:
            result = service.export(args.job, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and (result.get('status') == 'paused'
            or args.command == 'batch-resume' and result.get('status') != 'complete') else 0
    except (ValueError, RuntimeError, OSError) as error:
        print('Translation: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


if __name__ == '__main__':
    raise SystemExit(main())
