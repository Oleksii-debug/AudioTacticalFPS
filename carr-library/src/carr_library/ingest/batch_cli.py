"""Explicit Import Center pages through the existing guarded application."""
import argparse
import json
from pathlib import Path
import signal
import sqlite3
import sys
from threading import Event

from ..installation_lease import installed_entrypoint


def _profile(args):
    from .resource_budget import ImportBudget, MIB
    fields = ('source_mib', 'segment_mib', 'segments', 'steps', 'seconds',
              'memory_mib', 'library_mib', 'temporary_mib')
    if not args.review_default_budget and all(getattr(args, name) is None for name in fields):
        return None
    values = ImportBudget().to_dict()
    mapping = {'source_mib': 'source_bytes', 'segment_mib': 'segment_bytes',
               'memory_mib': 'memory_bytes', 'library_mib': 'library_bytes',
               'temporary_mib': 'temporary_bytes'}
    for name in fields:
        value = getattr(args, name)
        if value is not None:
            values[mapping.get(name, name)] = value * MIB if name in mapping else value
    return ImportBudget.from_dict(values)


def run(argv):
    from ..application import LibraryApp
    from ..audio.cleanup import AudioCleanupPending, drain_pending_cleanup
    from ..extract.page_ocr import Config, config_dict, config_from_dict
    from .batches import ImportBatchStore

    parser = argparse.ArgumentParser(
        description='Review bounded source pages, then explicitly run their saved current import jobs.')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--workspace', required=True, help='Exact existing library/workspace ID')
    commands = parser.add_subparsers(dest='operation', required=True)
    plan = commands.add_parser('plan', help='Save one source page without importing')
    plan.add_argument('paths', nargs='+', type=Path)
    plan.add_argument('--collection', action='append', default=[])
    plan.add_argument('--no-recursive', action='store_true')
    plan.add_argument('--ocr-languages', default='eng', help='Explicit model pool, such as eng+ukr+slk')
    plan.add_argument('--auto-ocr-languages', action='store_true',
                      help='Enable the pinned offline language detector and reviewed regional candidates')
    plan.add_argument('--review-default-budget', action='store_true',
                      help='Freeze an explicit profile even when all resource values keep their defaults')
    for name, label in (
        ('source-mib', 'Maximum original bytes in MiB; only strict streamed TXT can exceed 64'),
        ('segment-mib', 'Maximum individual TXT record in MiB, at most 64'),
        ('segments', 'Maximum streamed TXT logical fragments'),
        ('steps', 'Checkpoint/publication steps per explicit work slice'),
        ('memory-mib', 'Declared worker memory reservation in MiB'),
        ('library-mib', 'Measured library growth allowance per work slice in MiB'),
        ('temporary-mib', 'Temporary worker reservation in MiB')):
        plan.add_argument('--' + name, type=int, default=None, help=label)
    plan.add_argument('--seconds', type=float, default=None,
                      help='Reviewed native time allowance per explicit work slice, at most 86400')
    continuation = commands.add_parser('continue', help='Review the exact next saved source page')
    continuation.add_argument('batch_id')
    for child in (plan, continuation):
        child.add_argument('--page-files', type=int, default=1000, help='Rows in this scan page, at most 5000')
        child.add_argument('--scan-entries', type=int, default=20000, help='Traversal advances in this page')
        child.add_argument('--scan-seconds', type=float, default=60, help='This scan-page work allowance, at most 600')
        child.add_argument('--page-bytes', type=int, default=4*1024**2, help='Bounded source-row metadata bytes')
    commands.add_parser('list', help='List saved pages and legacy queues without running them')
    show = commands.add_parser('show', help='Read one saved page')
    show.add_argument('batch_id')
    execute = commands.add_parser('run', help='Run one explicit bounded slice of an existing page')
    execute.add_argument('batch_id')
    execute.add_argument('--retry-failed', action='store_true')
    execute.add_argument('--slice-seconds', '--file-timeout', dest='timeout', type=float, default=None,
                         help='Optional shorter work slice; omitted uses the saved profile or legacy default')
    for action in ('include', 'exclude'):
        child = commands.add_parser(action, help='Change one never-started reviewed row')
        child.add_argument('batch_id')
        child.add_argument('row_id')
    args = parser.parse_args(argv)
    stopped = Event()
    previous_signal, operation_error = None, None
    try:
        selected = _profile(args) if args.operation == 'plan' else None
        previous_signal = signal.signal(signal.SIGINT, lambda *unused: stopped.set())
        from ..durable_state import local_path, regular
        try:
            root = local_path(args.data_dir, exists=True)
            regular(root, directory=True)
            regular(root / 'library.sqlite')
        except FileNotFoundError as exc:
            raise ValueError('Choose an existing library containing a regular library.sqlite.') from exc
        app = LibraryApp(root)
        store = ImportBatchStore(app)
        if args.operation in ('plan', 'continue'):
            limits = dict(max_files=args.page_files, max_entries=args.scan_entries,
                          timeout=args.scan_seconds, max_bytes=args.page_bytes)
            if args.operation == 'plan':
                chosen = Config(languages=args.ocr_languages,
                                automatic_languages=args.auto_ocr_languages)
                # Preserve the current omission of an unselected automatic
                # mode so historical extraction fingerprints remain identical.
                chosen = config_from_dict(config_dict(chosen))
                result = app.plan_import_page(args.workspace, args.paths,
                    collections=args.collection, recursive=not args.no_recursive, config=chosen,
                    cancelled=stopped.is_set, import_budget=selected, **limits)
            else:
                result = app.continue_import_scan(args.workspace, args.batch_id,
                                                   cancelled=stopped.is_set, **limits)
        elif args.operation == 'list':
            result = store.list(args.workspace)
        elif args.operation == 'show':
            result = store.get(args.workspace, args.batch_id)
        elif args.operation in ('include', 'exclude'):
            result = store.set_row(args.workspace, args.batch_id, args.row_id,
                                   included=args.operation == 'include')
        else:
            result = app.run_import_batch(args.workspace, args.batch_id,
                timeout=args.timeout, retry_failed=args.retry_failed,
                cancelled=stopped.is_set,
                progress=lambda value: print(
                    f'{value["counts"]["complete"]}/{value["total"]} imported in this saved page',
                    file=sys.stderr, flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if args.operation == 'run' and result['state'] != 'complete' else 0
    except AudioCleanupPending as exc:
        operation_error = exc
        print('Import cleanup remains pending. Waiting for the same native owner; '
              'saved checkpoints and accepted books are retained.', file=sys.stderr, flush=True)
        return 3
    except KeyboardInterrupt as exc:
        operation_error = exc
        print('Import interrupted. Saved pages and accepted books remain available.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        operation_error = exc
        print('Import Center: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        error = operation_error if operation_error is not None else sys.exc_info()[1]
        try:
            if previous_signal is not None:
                signal.signal(signal.SIGINT, previous_signal)
        finally:
            interrupted = drain_pending_cleanup(error)
            if interrupted is not None:
                raise interrupted


@installed_entrypoint
def main(argv=None):
    from ..audio.cleanup import drain_pending_cleanup
    from ..native_runtime import activate_bundled_runtime
    handle, error = None, None
    try:
        handle = activate_bundled_runtime()
        return run(sys.argv[1:] if argv is None else argv)
    except BaseException as exc:
        error = exc
        raise
    finally:
        interrupted = drain_pending_cleanup(error)
        close_error = None
        if handle is not None:
            try:
                handle.close()
            except BaseException as exc:
                close_error = exc
        later = drain_pending_cleanup(close_error)
        if close_error is not None:
            raise close_error
        if interrupted is not None or later is not None:
            raise (interrupted if interrupted is not None else later)


if __name__ == '__main__':
    raise SystemExit(main())
