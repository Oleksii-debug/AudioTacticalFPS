"""Explicit incremental backup commands over the canonical library snapshot APIs."""
import argparse
from carr_library.installation_lease import installed_entrypoint
import json
from pathlib import Path
import sqlite3
import sys

from .backup_options import add_resource_arguments, resources_from_arguments
from .backup_budget import arguments as resource_arguments

from .backup_incremental import (BackupReference, DEFAULT_MAX_BYTES,
    create_incremental_backup, verify_incremental_backup, restore_incremental_backup)


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(prog='carr-library backup-incremental',
        description='Create, verify or restore incremental library backups with explicitly selected bases.')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--data-dir', type=Path, required=True)
    create.add_argument('--output', type=Path, required=True)
    create.add_argument('--base', nargs=2, metavar=('FOLDER', 'SHA256'), required=True)
    create.add_argument('--ancestor', nargs=2, metavar=('FOLDER', 'SHA256'), action='append', default=[])
    create.add_argument('--include-generated', action='store_true')
    for name in ('verify', 'restore'):
        command = commands.add_parser(name)
        command.add_argument('--source', type=Path, required=True)
        command.add_argument('--sha256', required=True, help='Independently retained top-level manifest checksum')
        command.add_argument('--base', nargs=2, metavar=('FOLDER', 'SHA256'), action='append', default=[])
        if name == 'restore':
            command.add_argument('--output', type=Path, required=True)
    for command in (create, commands.choices['verify'], commands.choices['restore']):
        add_resource_arguments(command)
        command.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES,
                             help='Logical snapshot byte budget; default 64 GiB')
    args = parser.parse_args(argv)
    try:
        kwargs = resource_arguments(resources_from_arguments(args))
        if args.command == 'create':
            from .application import LibraryApp
            if not (args.data_dir / 'library.sqlite').is_file():
                raise ValueError('Select an existing library data folder to back up.')
            result = create_incremental_backup(LibraryApp(args.data_dir), args.output,
                base=BackupReference(Path(args.base[0]), args.base[1]),
                ancestors=tuple(BackupReference(Path(path), checksum) for path, checksum in args.ancestor),
                include_generated=args.include_generated, max_bytes=args.max_bytes, **kwargs).to_dict()
        else:
            bases = tuple(BackupReference(Path(path), checksum) for path, checksum in args.base)
            if args.command == 'verify':
                result = verify_incremental_backup(args.source, args.sha256, bases=bases, max_bytes=args.max_bytes, **kwargs)
            else:
                root = restore_incremental_backup(args.source, args.output, args.sha256,
                                                 bases=bases, max_bytes=args.max_bytes, **kwargs)
                result = {'restored_root': str(root), 'manifest_sha256': args.sha256,
                          'external_execution_review_required': True}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print('Incremental backup: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
