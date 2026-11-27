"""Metadata-only operation views and explicit cooperative resource policies."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description='Resonance tasks and resource allowances')
    parser.add_argument('--data-dir', type=Path, required=True,
                        help='Explicit local library directory')
    parser.add_argument('--workspace', help='An existing workspace identity for the list command')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list')
    commands.add_parser('resources')
    configure = commands.add_parser('configure-resources')
    configure.add_argument('policy', type=Path, help='JSON object with the exact ResourceLimits fields')
    args = parser.parse_args(argv)
    if args.workspace is not None and args.command != 'list':
        parser.error('--workspace is only used with list.')
    try:
        from .application import LibraryApp
        from .audio.jobs import read_json
        from .operations import OperationFacade, ResourceBroker, ResourceLimits
        app = LibraryApp(args.data_dir)
        # Read-only job listing does not create a broker or rewrite job stores.
        if args.command == 'list':
            result = [asdict(row) for row in OperationFacade(app).list(workspace=args.workspace)]
        else:
            broker = ResourceBroker.for_app(app.root)
            if args.command == 'configure-resources':
                broker.configure(ResourceLimits.from_dict(read_json(args.policy, limit=65536)))
            result = OperationFacade(app, broker=broker).resources()
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, TypeError, OSError, RuntimeError, KeyError):
        # Stored metadata may contain private paths or provider diagnostics.
        print('Resonance operations: request could not complete; review the selected library and policy.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
