"""Read shared operation checkpoints or explicitly configure cooperative limits."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .durable_state import read_json
from .studio.cli import data_directory


from carr_library.installation_lease import installed_entrypoint


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description='Resonance saved operations and resource allowances')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--workspace', help='Limit saved operation views to an authorized library')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list')
    commands.add_parser('resources')
    configure = commands.add_parser('configure-resources')
    configure.add_argument('policy', type=Path, help='JSON object with the exact ResourceLimits fields')
    args = parser.parse_args(argv)
    try:
        from .application import LibraryApp
        from .operations import OperationFacade, ResourceBroker, ResourceLimits
        app = LibraryApp(data_directory(args.data_dir))
        broker = ResourceBroker.for_app(app.root)
        if args.command == 'list':
            result = [asdict(row) for row in OperationFacade(app, broker=broker).list(workspace=args.workspace)]
        else:
            if args.workspace:
                parser.error('--workspace is only used with list.')
            if args.command == 'configure-resources':
                broker.configure(ResourceLimits.from_dict(read_json(args.policy)))
            result = broker.snapshot()
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print('Resonance operations: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
