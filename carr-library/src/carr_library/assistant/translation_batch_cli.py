"""Explicit document-batch translation commands over the existing service."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading

from .models import local_path
from .profiles import ProfileStore
from .providers import Gateway
from .translation import TranslationService
from .translation_batch import TranslationBatchService, MAX_ITEMS, MAX_CHARACTERS
from .translation_source import capture_source, read_text_file
from ..application import LibraryApp
from ..installation_lease import installed_entrypoint

def _batch_input(service, store, path, *, cancelled=lambda: False):
    """Resolve only explicitly listed files or existing library selections."""
    from ..backup_sidecars import read_regular
    if cancelled():
        raise InterruptedError('Translation batch input review cancelled.')
    selected = local_path(path)
    raw = read_regular(selected, 131072)
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
            record = capture_source(service.library, row['workspace'], row['edition'], topic=row.get('topic'),
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
            value, default_title = read_text_file(source, cancelled=cancelled), source.name
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
    parser = argparse.ArgumentParser(
        description='Explicit reviewed document batches; plain-text derivatives, no layout preservation')
    parser.add_argument('--data-dir', type=Path, required=True, help='Existing private Resonance library')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('preview', 'submit'):
        action = sub.add_parser(name, help='Review or prepare an explicit batch; no provider text request')
        action.add_argument('--batch-file', type=Path, required=True,
                            help='carr-translation-batch-input/1 JSON with explicit files or retained editions')
        if name == 'submit':
            action.add_argument('--approve-remote', action='store_true')
            action.add_argument('--review-sha256', required=True)
    sub.add_parser('jobs', help='List saved child-derived statuses without sending text')
    for name in ('review', 'prepare', 'resume', 'pause', 'export'):
        action = sub.add_parser(name)
        action.add_argument('batch')
        if name == 'export':
            action.add_argument('--output', type=Path, required=True, help='A NEW .zip plain-text translation package')
    args = parser.parse_args(argv)
    stop, previous = threading.Event(), None
    try:
        root = local_path(args.data_dir)
        if not (root / 'library.sqlite').is_file():
            raise ValueError('Choose an existing Resonance library with library.sqlite.')
        app = LibraryApp(root)
        store = ProfileStore(root / 'state' / 'providers')
        service = TranslationService(root / 'state' / 'translation', library=app)
        batches = TranslationBatchService(service)
        root_info = root.stat()
        original_root = root_info.st_dev, root_info.st_ino
        def guard():
            found = root.stat()
            if (found.st_dev, found.st_ino) != original_root:
                raise PermissionError('The selected library directory changed.')
        def accounts(rows):
            guard()
            for row in rows:
                profile = store.get(row['profile']['id'])
                if not profile.enabled or profile.to_dict() != row['profile']:
                    raise PermissionError('A reviewed account changed or was disabled. Review a new batch.')
        previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
        if args.command in ('preview', 'submit'):
            title, items = _batch_input(service, store, args.batch_file, cancelled=stop.is_set)
            accounts(items)
            result = batches.preview(items, title=title, cancelled=stop.is_set, guard=guard)
            if args.command == 'submit':
                identity = batches.submit(items, title=title, review_sha256=args.review_sha256,
                    approve_remote=args.approve_remote, cancelled=stop.is_set, guard=lambda: accounts(items))
                result = {**batches.get(identity), 'network_access': False,
                          'next': 'Explicit resume uses only these retained child approvals.'}
        elif args.command == 'jobs':
            result = batches.list(cancelled=stop.is_set)
        elif args.command == 'review':
            result = {'review': batches.get_review(args.batch), 'status': batches.get(args.batch)}
        elif args.command == 'prepare':
            result = batches.prepare(args.batch, cancelled=stop.is_set, guard=guard)
        elif args.command == 'pause':
            batches.pause(args.batch)
            result = batches.get(args.batch)
        elif args.command == 'resume':
            review = batches.get_review(args.batch)
            accounts(review['items'])
            service.gateway = Gateway(service.directory / 'provider', mode='APPROVED_REMOTE')
            accounts(review['items'])
            result = batches.resume(args.batch, cancelled=stop.is_set,
                guard=lambda: accounts(review['items']),
                progress=lambda done, total, phase: print(f'{done}/{total}: {phase}', file=sys.stderr, flush=True))
        else:
            result = batches.export(args.batch, args.output, cancelled=stop.is_set, guard=guard)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get('status') == 'paused' else 0
    except (ValueError, RuntimeError, OSError, InterruptedError) as error:
        print('Translation batch: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


if __name__ == '__main__':
    raise SystemExit(main())
