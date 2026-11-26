"""Preview or run a typed local tool plan with explicit scope and approval."""
import argparse
import json
from pathlib import Path
import sys

from .runtime import LocalAgent, load_plan


def main(argv=None):
    parser = argparse.ArgumentParser(prog='research-library agent', description='Reviewable local library and audio workflows')
    parser.add_argument('command', choices=('preview', 'run'))
    parser.add_argument('plan', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--approve', help='Exact approval_sha256 printed by preview')
    parser.add_argument('--read-root', action='append', type=Path, default=[])
    parser.add_argument('--output-root', action='append', type=Path, default=[])
    parser.add_argument('--workspace', action='append', default=[])
    parser.add_argument('--allow-create', action='store_true')
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan)
        if args.command == 'preview':
            result = plan.preview()
        else:
            if not args.data_dir or not args.approve:
                parser.error('run needs --data-dir and --approve from the reviewed plan.')
            from ..application import LibraryApp
            library = LibraryApp(args.data_dir)
            agent = LocalAgent(library, library.root / 'agent-jobs', read_roots=args.read_root,
                output_roots=args.output_root, workspaces=args.workspace, allow_create=args.allow_create)
            result = agent.run(plan, approval_sha256=args.approve,
                progress=lambda value: print(json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Local Assistant: {exc}', file=sys.stderr)
        return 1
