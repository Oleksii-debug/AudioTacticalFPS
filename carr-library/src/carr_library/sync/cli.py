"""Explicit local snapshot commands; secrets never appear in argv or JSON."""
import argparse
import getpass
import json
from pathlib import Path
import sys


def _password(args, parser):
    if getattr(args, 'password_stdin', False):
        # A bounded single line is a deliberate secret-input channel. Do not echo
        # it, accept environment variables, or include it in exception output.
        value = sys.stdin.readline(1026)
        if not value.endswith('\n') and len(value) == 1026:
            parser.error('Password input exceeds the supported size.')
        value = value.removesuffix('\n').removesuffix('\r')
    elif getattr(args, 'password_prompt', False):
        if not sys.stdin.isatty():
            parser.error('A hidden password prompt requires a terminal; use --password-stdin for an explicit pipe.')
        value = getpass.getpass('Snapshot passphrase: ')
        if args.command == 'publish' and value != getpass.getpass('Repeat snapshot passphrase: '):
            parser.error('Passphrases did not match.')
    else:
        return None
    try:
        valid = 12 <= len(value) and len(value.encode('utf-8')) <= 1024
    except UnicodeError:
        valid = False
    if not valid:
        parser.error('Use a passphrase of at least 12 characters and at most 1024 UTF-8 bytes.')
    return value


from ..installation_lease import installed_entrypoint


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(prog='research-library sync',
        description='Checked local portable snapshots of every library in a data folder. No network service is used.')
    parser.add_argument('--data-dir', required=True, type=Path,
                        help='Existing local library data folder; never a shared live SQLite directory')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('list', 'publish', 'inspect', 'restore'):
        command = commands.add_parser(name)
        command.add_argument('--folder', required=True, type=Path, help='Separate local portable snapshot folder')
        if name == 'publish':
            command.add_argument('--label', default='Portable checkpoint')
            parent = command.add_mutually_exclusive_group(required=True)
            parent.add_argument('--empty', action='store_true', help='Require an empty portable history')
            parent.add_argument('--parent', help='Require this previously reviewed current snapshot ID')
        if name in ('inspect', 'restore'):
            command.add_argument('--token', required=True, help='Independent 64-character transfer code from publishing device')
        if name == 'restore':
            command.add_argument('--destination', required=True, type=Path, help='New, nonexistent local data directory')
        if name != 'list':
            secret = command.add_mutually_exclusive_group(required=name == 'publish')
            secret.add_argument('--plain', action='store_true', help='Explicitly use an unencrypted snapshot')
            secret.add_argument('--password-prompt', action='store_true', help='Read a hidden passphrase from the terminal')
            secret.add_argument('--password-stdin', action='store_true', help='Read one passphrase line from standard input without echo')
    args = parser.parse_args(argv)
    if not args.data_dir.is_dir() or not (args.data_dir / 'library.sqlite').is_file():
        parser.error('--data-dir must name an existing library. Open a new local library in the desktop first if needed.')
    password = _password(args, parser)
    from .service import SyncService, SyncPublishedError, SyncRestoredError
    handle = None
    try:
        from ..native_runtime import activate_bundled_runtime
        from ..application import LibraryApp
        handle = activate_bundled_runtime()
        service = SyncService(LibraryApp(args.data_dir))
        if args.command == 'list':
            result = {'snapshots': service.list(args.folder), 'trusted': False,
                      'notice': 'Listed IDs do not replace an independently retained transfer code.'}
        elif args.command == 'publish':
            result = service.publish(args.folder, args.label, args.parent, password)
        elif args.command == 'inspect':
            result = service.inspect(args.folder, args.token, password)
        else:
            result = service.restore(args.folder, args.token, args.destination, password)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if result.get('durability_confirmed') is False else 0
    except SyncPublishedError as exc:
        print(json.dumps({'status': 'PUBLISHED_WITH_HISTORY_ERROR', 'receipt': exc.receipt,
            'message': 'Keep the transfer code. Restore into a new folder before continuing this history.'}), file=sys.stderr)
        return 3
    except SyncRestoredError as exc:
        print(json.dumps({'status': 'RESTORED_WITH_FINALIZATION_ERROR', 'destination': exc.destination,
            'token': exc.token, 'durability_confirmed': exc.durability_confirmed,
            'message': 'The restored folder exists. Check it before retrying.'}), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print('Snapshot operation interrupted. Check history and destination before retrying.', file=sys.stderr)
        return 130
    except Exception as exc:
        # Keep credential-bearing exception text and tracebacks out of output.
        from ..ui.portable_sync import _failure
        print(_failure(exc), file=sys.stderr)
        return 2
    finally:
        password = None
        if handle is not None:
            handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
