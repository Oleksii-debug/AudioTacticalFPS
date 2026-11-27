"""Import Center CLI helper reached through the existing guarded public main."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys


def run(argv):
    from ..application import LibraryApp
    from ..extract.page_ocr import Config
    from .batches import ImportBatchStore

    parser = argparse.ArgumentParser(description='Review bounded source pages; explicit run imports one saved page.')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--workspace', required=True, help='Exact existing library/workspace ID')
    commands = parser.add_subparsers(dest='operation', required=True)
    plan = commands.add_parser('plan', help='Save the first page without importing sources')
    plan.add_argument('paths', nargs='+', type=Path)
    plan.add_argument('--collection', action='append', default=[])
    plan.add_argument('--no-recursive', action='store_true')
    plan.add_argument('--ocr-languages', default='eng', help='Explicit manual/bootstrap model pool, such as eng+ukr+slk')
    plan.add_argument('--auto-ocr-languages', action='store_true',
                      help='Use the pinned offline detector and bounded regional second-pass candidates')
    continuation = commands.add_parser('continue', help='Append or reopen the exact next saved page')
    continuation.add_argument('batch_id')
    for child in (plan, continuation):
        child.add_argument('--page-files', type=int, default=1000, help='Reviewed rows in one page, at most 5000')
        child.add_argument('--scan-entries', type=int, default=20000, help='Traversal advances in one page, at most 20000')
        child.add_argument('--scan-seconds', type=float, default=60, help='Explicit page work budget, at most 600 seconds')
        child.add_argument('--page-bytes', type=int, default=4 * 1024**2, help='Source-row metadata budget, at most 8 MiB')
    commands.add_parser('list', help='List ordinary saved import pages and legacy queues')
    show = commands.add_parser('show', help='Read one saved queue, without imports')
    show.add_argument('batch_id')
    execute = commands.add_parser('run', help='Explicitly run queued files in one selected page')
    execute.add_argument('batch_id')
    execute.add_argument('--retry-failed', action='store_true')
    execute.add_argument('--file-timeout', type=float, default=120)
    for action in ('include', 'exclude'):
        child = commands.add_parser(action, help='Change one never-started selected row')
        child.add_argument('batch_id')
        child.add_argument('row_id')
    args = parser.parse_args(argv)
    try:
        app = LibraryApp(args.data_dir)
        store = ImportBatchStore(app)
        if args.operation in ('plan', 'continue'):
            limits = dict(max_files=args.page_files, max_entries=args.scan_entries,
                          timeout=args.scan_seconds, max_bytes=args.page_bytes)
            if args.operation == 'plan':
                result = store.plan_page(args.workspace, args.paths, collections=args.collection,
                    recursive=not args.no_recursive, config=Config(languages=args.ocr_languages,
                        automatic_languages=args.auto_ocr_languages), **limits)
            else:
                result = store.continue_plan(args.workspace, args.batch_id, **limits)
        elif args.operation == 'list':
            result = store.list(args.workspace)
        elif args.operation == 'show':
            result = store.get(args.workspace, args.batch_id)
        elif args.operation in ('include', 'exclude'):
            result = store.set_row(args.workspace, args.batch_id, args.row_id,
                                   included=args.operation == 'include')
        else:
            result = store.run(args.workspace, args.batch_id, timeout=args.file_timeout,
                retry_failed=args.retry_failed,
                progress=lambda value: print(f'{value["counts"]["complete"]}/{value["total"]} imported',
                                               file=sys.stderr, flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if args.operation == 'run' and result['state'] not in ('complete',) else 0
    except KeyboardInterrupt:
        print('Import operation interrupted. Saved pages and accepted imports remain available.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print('Import Center: ' + str(exc), file=sys.stderr)
        return 1
