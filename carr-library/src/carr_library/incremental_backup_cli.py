"""Explicit local incremental-storage commands for canonical library backups."""
import argparse
import json
from pathlib import Path
import signal
import sqlite3
import sys
from threading import Event

from . import incremental_backup as repository
from .backup_files import local_path, regular


def _emit(value, stream=None):
    stream = sys.stdout if stream is None else stream
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    if hasattr(stream, 'buffer'):
        stream.buffer.write(text.encode('utf-8'))
        stream.buffer.flush()
    else:
        stream.write(text)
        stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m carr_library backup-repository',
        description='Readable local incremental storage; every snapshot restores through the complete-library validator. Password-encrypted copies use the separate sync command.')
    commands = parser.add_subparsers(dest='command', required=True)
    for name, help_text in (
        ('init', 'Explicitly create a new repository directory.'),
        ('create', 'Snapshot an existing library, reusing verified immutable payloads.'),
        ('list', 'List committed descriptor identities; does not verify payloads.'),
        ('verify', 'Verify every payload and the complete canonical library backup.'),
        ('materialize', 'Export an ordinary complete backup to a new directory.'),
        ('restore', 'Restore through the canonical service to a new data directory.'),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--repository', type=Path, required=True)
        if name == 'create':
            command.add_argument('--data-dir', type=Path, required=True,
                                 help='Existing private library data directory; never created implicitly.')
            command.add_argument('--parent-sha256',
                                 help='Optional independently saved predecessor identity; verified before writing.')
            command.add_argument('--max-bytes', type=int,
                                 help='Optional total stored snapshot byte allowance; existing typed backup bounds also apply.')
        if name in ('verify', 'materialize', 'restore'):
            command.add_argument('--snapshot-sha256', required=True,
                                 help='Independently retained SHA256, not an automatically trusted latest entry.')
        if name in ('materialize', 'restore'):
            command.add_argument('--output', type=Path, required=True,
                                 help='New destination; existing data is never overwritten.')
    args = parser.parse_args(argv)
    stopped = Event()
    previous = None
    installed_signal = False
    native_handle = None
    try:
        try:
            previous = signal.signal(signal.SIGINT, lambda *_: stopped.set())
            installed_signal = True
        except ValueError:
            pass  # An embedding host may call main outside its signal-owning thread.
        from .native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
        if args.command == 'init':
            result = repository.initialize_repository(args.repository, cancelled=stopped.is_set)
        elif args.command == 'create':
            root = local_path(args.data_dir, exists=True)
            regular(root, directory=True)
            regular(root / repository.library_backup.DATABASE)
            from .application import LibraryApp
            app = LibraryApp(root)
            result = repository.create_snapshot(app, args.repository,
                parent=args.parent_sha256, max_bytes=args.max_bytes, cancelled=stopped.is_set)
        elif args.command == 'list':
            result = {'snapshots': repository.list_snapshots(args.repository, cancelled=stopped.is_set),
                      'payloads_verified': False,
                      'notice': 'Listing is not verification. Keep each snapshot SHA256 independently.'}
        elif args.command == 'verify':
            result = repository.verify_snapshot(args.repository, args.snapshot_sha256,
                                                cancelled=stopped.is_set)
        elif args.command == 'materialize':
            result = repository.materialize_snapshot(args.repository, args.snapshot_sha256,
                args.output, cancelled=stopped.is_set)
        else:
            result = repository.restore_snapshot(args.repository, args.snapshot_sha256,
                args.output, cancelled=stopped.is_set)
        _emit(result)
        return 0
    except InterruptedError as exc:
        _emit({'status': 'cancelled', 'message': str(exc)}, sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        _emit({'status': 'failed', 'message': str(exc)}, sys.stderr)
        return 1
    finally:
        if installed_signal:
            signal.signal(signal.SIGINT, previous)
        if native_handle is not None:
            native_handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
