"""Finite explicit CLI arming of local backup and watched-folder rules."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import signal
import sqlite3
import sys
from threading import Event
import time

from .durable_state import local_path, regular
from .local_schedules import ScheduleSession, ScheduleStore


def _emit(value, stream=None):
    stream = sys.stdout if stream is None else stream
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if hasattr(stream, 'buffer'):
        stream.buffer.write(raw)
        stream.buffer.flush()
    else:
        stream.write(raw.decode('utf-8'))
        stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m carr_library local-schedules',
        description='Save inactive local rules; explicitly arm a finite foreground session.')
    parser.add_argument('--data-dir', type=Path, required=True,
                        help='Existing library directory; never initialized by this command.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='Read saved declarations; does not inspect external folders or arm anything.')
    for kind in ('backup', 'watch'):
        command = commands.add_parser('create-' + kind, help='Save an inactive, explicitly chosen ' + kind + ' rule.')
        command.add_argument('--title', required=True)
        command.add_argument('--target', type=Path, required=True,
                             help='Existing initialized Vault, or existing explicitly chosen source folder.')
        command.add_argument('--at', required=True, help='First start as a complete UTC timestamp.')
        command.add_argument('--interval-seconds', type=int, default=3600)
        command.add_argument('--lateness-seconds', type=int, default=3600)
        if kind == 'backup':
            command.add_argument('--parent-sha256', help='Separately retained current token; empty repository if omitted.')
            command.add_argument('--include-generated', action='store_true')
        else:
            command.add_argument('--workspace', required=True)
            command.add_argument('--collection', action='append', default=[])
            command.add_argument('--no-recursive', action='store_true')
            command.add_argument('--ocr-languages', default='eng')
    for name in ('disable', 'review', 'run'):
        command = commands.add_parser(name)
        command.add_argument('--rule', required=True)
        command.add_argument('--sha256', required=True, help='Exact saved rule byte hash from the current review.')
        if name == 'review':
            command.add_argument('--at', required=True)
            command.add_argument('--parent-sha256',
                help='For backups, the independently checked current token; prior attempts are not reclassified.')
        elif name == 'run':
            command.add_argument('--run', action='store_true',
                help='Explicitly arm this rule in this process. Required; no implicit startup task.')
            command.add_argument('--wait-seconds', type=float, default=0,
                                 help='Finite foreground lifetime, zero to one day.')
            command.add_argument('--max-occurrences', type=int, default=1,
                                 help='Stop after at most 1..32 returned attempts.')
    args = parser.parse_args(argv)
    if args.command == 'run':
        import math
        if not args.run:
            parser.error('Use --run to explicitly arm the reviewed rule.')
        if not math.isfinite(args.wait_seconds) or not 0 <= args.wait_seconds <= 86400:
            parser.error('--wait-seconds must be between zero and one day.')
        if not 1 <= args.max_occurrences <= 32:
            parser.error('--max-occurrences must be 1..32.')
    stopped = Event()
    session = None
    previous = None
    signal_set = False
    native = None
    try:
        try:
            previous = signal.signal(signal.SIGINT, lambda *_: stopped.set())
            signal_set = True
        except ValueError:
            pass
        from .native_runtime import activate_bundled_runtime
        native = activate_bundled_runtime()
        root = local_path(args.data_dir, exists=True)
        regular(root, directory=True)
        regular(root / 'library.sqlite')
        from .application import LibraryApp
        store = ScheduleStore(LibraryApp(root))
        if args.command == 'list':
            result = {'armed': False, 'rules': [asdict(view) for view in store.list()],
                      'notice': 'Historical declarations only. Restart and restore never arm rules.'}
        elif args.command.startswith('create-'):
            kind = args.command[7:]
            options = dict(first_due=args.at, interval_seconds=args.interval_seconds,
                           lateness_seconds=args.lateness_seconds, cancelled=stopped.is_set)
            if kind == 'backup':
                options.update(parent=args.parent_sha256, include_generated=args.include_generated)
            else:
                from .extract.page_ocr import Config
                options.update(workspace=args.workspace, collections=args.collection,
                    recursive=not args.no_recursive, config=Config(languages=args.ocr_languages))
            view = store.create(kind, args.title, args.target, **options)
            result = {'armed': False, 'saved': asdict(view)}
        elif args.command == 'disable':
            result = {'armed': False, 'saved': asdict(store.disable(args.rule, expected=args.sha256))}
        elif args.command == 'review':
            result = {'armed': False, 'saved': asdict(store.acknowledge(args.rule, expected=args.sha256,
                next_due=args.at, parent=args.parent_sha256, cancelled=stopped.is_set))}
        else:
            session = ScheduleSession(store)
            session.arm(args.rule, expected=args.sha256, cancelled=stopped.is_set)
            deadline = time.monotonic() + args.wait_seconds
            outcomes = []
            while not stopped.is_set():
                outcome = session.tick(cancelled=stopped.is_set)
                if outcome is not None:
                    outcomes.append(outcome)
                    if len(outcomes) >= args.max_occurrences or not session.armed:
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                stopped.wait(min(.2, remaining))
            complete = (len(outcomes) == args.max_occurrences and all(
                row['status'] == 'complete' and row['recorded'] and not row['message'] for row in outcomes))
            status = ('complete' if complete else 'cancelled' if stopped.is_set()
                      else 'attention' if outcomes else 'not-due')
            session.close()
            _emit({'armed': False, 'status': status, 'outcomes': outcomes})
            return 0 if complete else 130 if status == 'cancelled' else 3
        _emit(result)
        return 0
    except InterruptedError as error:
        _emit({'status': 'cancelled', 'armed': False, 'message': str(error)}, sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        _emit({'status': 'failed', 'armed': False, 'message': str(error)}, sys.stderr)
        return 1
    finally:
        if session is not None:
            session.close()
        if signal_set:
            signal.signal(signal.SIGINT, previous)
        if native is not None:
            native.close()


if __name__ == '__main__':
    raise SystemExit(main())
