"""Explicit large TXT/PDF originals through the existing owned import worker."""
import argparse
import json
import math
from pathlib import Path
import re
import sys


def main(arguments=None):
    parser = argparse.ArgumentParser(prog='carr_library import-large',
        description='Preserve one large original and use the current bounded isolated TXT/PDF pipeline.')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--workspace', required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--file', type=Path)
    selection.add_argument('--resume-request')
    parser.add_argument('--profile', choices=('txt', 'pdf'))
    parser.add_argument('--max-mib', type=int, help='Required explicit original allowance, 1 through 4096 MiB')
    parser.add_argument('--title')
    parser.add_argument('--timeout', type=float, default=600,
                        help='Owned worker wall-time allowance, 0.1 through 7200 seconds')
    parser.add_argument('--ocr-language', default=None)
    parser.add_argument('--ocr-orientation', action='store_true')
    args = parser.parse_args(arguments)
    if (not args.workspace.strip() or len(args.workspace) > 200
            or not math.isfinite(args.timeout) or not 0.1 <= args.timeout <= 7200):
        parser.error('Choose an existing library and a finite 0.1–7200 second worker allowance.')
    if args.resume_request is not None:
        if (re.fullmatch('[0-9a-f]{32}', args.resume_request) is None
                or any(value is not None for value in (args.profile, args.max_mib, args.title, args.ocr_language))
                or args.ocr_orientation):
            parser.error('A saved large-source request retains its exact profile, metadata and OCR settings.')
    elif (args.profile is None or args.max_mib is None or not 1 <= args.max_mib <= 4096
            or args.file.suffix.casefold() != '.' + args.profile):
        parser.error('Choose --profile txt|pdf, matching --file and --max-mib 1 through 4096.')
    if not (args.data_dir / 'library.sqlite').is_file():
        parser.error('Choose an existing library directory; this command does not create one.')
    from .application import open_existing_library
    from .extract.page_ocr import Config
    from .ingest.source_profiles import LargeTextProfile, PdfProfile
    from .ingest.worker import prepare_request, load_request, run_request
    try:
        app = open_existing_library(args.data_dir)
        with app.read(args.workspace):
            if args.resume_request is not None:
                record = load_request(app.root, args.resume_request)
                if record['version'] != 3 or record['workspace'] != args.workspace:
                    raise ValueError('The saved request is not a large-source request in this library.')
        if args.resume_request is None:
            profile = (LargeTextProfile if args.profile == 'txt' else PdfProfile)(args.max_mib * 1024**2)
            config = Config(languages=args.ocr_language or 'eng', orientation=args.ocr_orientation)
            metadata = dict(title=args.title, authors=[], language=None, edition=None, volume=None,
                            collections=[], work_id=None, edition_id=None)
            record = prepare_request(app.root, args.workspace, args.file, metadata, config,
                                     source_profile=profile)
        # A later failure/interrupt never hides the already prepared request.
        print(json.dumps(dict(state='prepared', request_id=record['request_id']),
                         ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        result = run_request(app.root, args.workspace, record['source'], {}, None,
                             request_id=record['request_id'], timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get('state') == 'complete' else 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps(dict(state='failed', message=str(exc)), ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
