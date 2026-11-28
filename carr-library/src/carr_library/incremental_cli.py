"""Explicit local snapshot commands. No scheduler, deletion or automatic restore."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .application import LibraryApp
from .incremental_backup import (
    create_snapshot, list_snapshots, verify_snapshot, restore_snapshot,
    MAX_FILES, DEFAULT_MAX_BYTES,
)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='resonance snapshots',
        description='Store complete library backups with shared immutable content files.')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('create', 'list', 'verify', 'restore'):
        command = commands.add_parser(name)
        command.add_argument('--repository', required=True, type=Path)
        command.add_argument('--max-files', type=int, default=MAX_FILES)
        command.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES)
        if name == 'create':
            command.add_argument('--data-dir', required=True, type=Path)
        if name in ('verify', 'restore'):
            command.add_argument('--snapshot-sha256', required=True,
                help='The checksum retained separately when the snapshot was created.')
        if name == 'restore':
            command.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args(argv)
    options = {'max_files': args.max_files, 'max_bytes': args.max_bytes}
    try:
        if args.command == 'create':
            # Do not manufacture an empty library for a misspelled source.
            from .backup_files import local_path, regular
            source = local_path(args.data_dir, exists=True)
            regular(source, directory=True)
            regular(source / 'library.sqlite')
            result = create_snapshot(LibraryApp(source), args.repository, **options)
            payload = {'status': 'complete', 'operation': 'create', **asdict(result)}
            payload['notice'] = ('Keep snapshot.snapshot_sha256 separately. Storage reuse '
                                 'does not eliminate complete capture and verification.')
        elif args.command == 'list':
            result = list_snapshots(args.repository, **options)
            payload = {'status': 'complete', 'operation': 'list',
                       'object_verification': 'NOT_RUN',
                       'snapshots': [asdict(item) for item in result]}
        elif args.command == 'verify':
            result = verify_snapshot(args.repository, args.snapshot_sha256, **options)
            payload = {'status': 'complete', 'operation': 'verify', 'snapshot': asdict(result)}
        else:
            result = restore_snapshot(args.repository, args.snapshot_sha256,
                                      args.destination, **options)
            payload = {'status': 'complete', 'operation': 'restore', **asdict(result)}
            payload['path'] = str(result.path)
            payload['notice'] = 'Review restored pending jobs before resuming them.'
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({'status': 'failed', 'operation': args.command,
                          'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
