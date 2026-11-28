"""Explicit streamed TXT command using the normal import supervisor."""
import argparse
from carr_library.installation_lease import installed_entrypoint
import json
import math
from pathlib import Path
import re
import sys


@installed_entrypoint
def main(arguments=None):
    parser = argparse.ArgumentParser(prog='carr_library import-text',
        description='Import a selected UTF-8 text in bounded logical fragments; keep original bytes.')
    parser.add_argument('--data-dir', required=True, type=Path, help='Existing private library directory')
    parser.add_argument('--workspace', required=True, help='Existing selected library ID')
    selected = parser.add_mutually_exclusive_group(required=True)
    selected.add_argument('--file', type=Path, help='Selected local UTF-8 .txt original')
    selected.add_argument('--resume-request', help='Saved streamed-text request ID; uses its frozen profile')
    parser.add_argument('--max-mib', type=int, help='Explicit source allowance, 1 through 128 MiB (default 128)')
    parser.add_argument('--budget-file', type=Path,
        help='Explicit local carr-import-budget/1 JSON for a new TXT; cannot replace a saved policy')
    parser.add_argument('--title', help='Explicit title for a new import')
    parser.add_argument('--timeout', type=float,
                        help='Optional worker deadline; legacy default180/maximum600, explicit budget keeps its selected maximum')
    args = parser.parse_args(arguments)
    if not args.workspace.strip() or len(args.workspace) > 200:
        parser.error('Choose an existing library ID.')
    if args.timeout is not None and (not math.isfinite(args.timeout) or not 0.1 <= args.timeout <= 86400):
        parser.error('Choose a finite worker deadline within the selected source policy.')
    if args.budget_file is not None and args.max_mib is not None:
        parser.error('Choose the legacy byte limit or one explicit budget file, not both.')
    if args.resume_request is None and args.budget_file is None and args.timeout is not None and args.timeout > 600:
        parser.error('Choose a worker timeout between 0.1 and 600 seconds.')
    if args.max_mib is not None and not 1 <= args.max_mib <= 128:
        parser.error('Choose a byte allowance between 1 and 128 MiB.')
    if args.resume_request is not None:
        if not re.fullmatch('[0-9a-f]{32}', args.resume_request):
            parser.error('Choose a saved streamed-text request ID.')
        if args.max_mib is not None or args.title is not None or args.budget_file is not None:
            parser.error('A resumed request keeps its original profile and title.')
    elif args.file.suffix.casefold() != '.txt':
        parser.error('This explicit profile accepts UTF-8 .txt originals only.')
    if not (args.data_dir / 'library.sqlite').is_file():
        parser.error('Select an existing library directory; this command does not create one.')
    from .application import LibraryApp
    from .ingest.streamed_text import TextProfile
    from .ingest.worker import load_request, request_profile
    from .audio.cleanup import drain_pending_cleanup
    operation_error = None
    try:
        app = LibraryApp(args.data_dir)
        with app.read(args.workspace):
            if args.resume_request is not None:
                request = load_request(app.root, args.resume_request)
                if request['version'] != 2 or request['workspace'] != args.workspace:
                    raise ValueError('The saved request is not streamed text in the selected library.')
        if args.resume_request is not None:
            profile = request_profile(request)
        elif args.budget_file is not None:
            from .durable_state import read_json_snapshot
            from .ingest.resource_budget import ImportBudget
            value, _ = read_json_snapshot(args.budget_file, max_bytes=64_000)
            chosen = ImportBudget.from_dict(value)
            profile = TextProfile(chosen.source_bytes, chosen)
        else:
            profile = TextProfile((args.max_mib or 128) * 1024**2)
        timeout = args.timeout
        if timeout is None:
            timeout = 180 if profile.import_budget is None else profile.import_budget.seconds
        if timeout > profile.maximum_timeout:
            raise ValueError('The worker deadline exceeds the immutable source policy.')
        if args.resume_request is not None:
            result = app.resume_import(args.workspace, args.resume_request, timeout=timeout)
        else:
            result = app.import_file(args.workspace, args.file, title=args.title,
                source_profile=profile, timeout=timeout)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get('state') == 'complete' else 2
    except (OSError, ValueError, RuntimeError) as exc:
        operation_error = exc
        print(json.dumps(dict(state='failed', message=str(exc)), ensure_ascii=False, sort_keys=True))
        return 2
    finally:
        error = operation_error if operation_error is not None else sys.exc_info()[1]
        interrupted = drain_pending_cleanup(error)
        if interrupted is not None:
            raise interrupted


if __name__ == '__main__':
    raise SystemExit(main())
