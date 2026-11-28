"""Read shared operation checkpoints or explicitly configure cooperative limits."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from ..durable_state import read_json
from ..installation_lease import installed_entrypoint
from ..studio.cli import data_directory


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description='Resonance saved operations and resource allowances')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--workspace', help='Limit saved operation views to an authorized library')
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--limit', type=int, default=1000)
    for name in ('get', 'pause', 'cancel'):
        operation = commands.add_parser(name)
        operation.add_argument('kind', choices=('studio', 'assistant', 'import', 'translation', 'asr'))
        operation.add_argument('job_id')
    facade = None
    commands.add_parser('resources')
    configure = commands.add_parser('configure-resources')
    configure.add_argument('policy', type=Path, help='JSON object with the exact ResourceLimits fields')
    args = parser.parse_args(argv)
    try:
        from ..application import LibraryApp
        from . import OperationFacade, ResourceBroker, ResourceLimits
        app = LibraryApp(data_directory(args.data_dir))
        broker = ResourceBroker.for_app(app.root)
        facade = OperationFacade.for_app(app, broker=broker, controls=args.command in ('pause', 'cancel'))
        if args.command == 'list':
            result = [asdict(row) for row in facade.list(workspace=args.workspace, limit=args.limit)]
        elif args.command == 'get':
            result = asdict(facade.get(args.kind, args.job_id, workspace=args.workspace))
        elif args.command in ('pause', 'cancel'):
            result = asdict(facade.control(args.kind, args.job_id, args.command,
                workspace=args.workspace))
            result['requested'] = args.command
            result['notice'] = 'Request recorded; the owning workflow acknowledges stopping. Resume remains explicit.'
        else:
            if args.workspace:
                parser.error('--workspace is only used with saved-job views and controls.')
            if args.command == 'configure-resources':
                broker.configure(ResourceLimits.from_dict(read_json(args.policy)))
            result = broker.snapshot()
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print('Resonance operations: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        if facade is not None:
            facade.close()


if __name__ == '__main__':
    raise SystemExit(main())
