"""Headless DOI operations; the same scoped service as the native dialog."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re


def add_arguments(parser, operations):
    operations.add_argument('--lookup-doi', metavar='DOI',
        help='Preview metadata for one explicitly approved public DOI (no source text is uploaded)')
    parser.add_argument('--allow-online-doi', action='store_true',
        help='Approve this DOI request to the selected providers; shared HYBRID policy must also be enabled')
    parser.add_argument('--doi-provider', action='append', choices=('crossref', 'europe_pmc', 'unpaywall'),
        help='Provider to query; repeat for another provider (default: crossref)')
    parser.add_argument('--unpaywall-email-env', metavar='VARIABLE',
        help='Read the Unpaywall contact email from this named environment variable for this request only')
    parser.add_argument('--save-doi-reference', action='store_true',
        help='Save the preview as a bibliographic reference, deduplicated by exact DOI')
    parser.add_argument('--download-source-url', metavar='HTTPS_URL',
        help='Download this exact provider-reported open-access PDF URL from the preview')
    parser.add_argument('--acquisition-folder', type=Path,
        help='New local folder for the acquired PDF and provenance receipt; never overwritten')
    parser.add_argument('--confirm-source-rights', action='store_true',
        help='Confirm you may obtain the selected source; does not confer redistribution rights')


def validate_arguments(parser, args):
    online_options = (args.allow_online_doi, args.doi_provider, args.unpaywall_email_env,
        args.save_doi_reference, args.download_source_url, args.acquisition_folder, args.confirm_source_rights)
    if args.lookup_doi is None:
        if any(online_options):
            parser.error('DOI options require --lookup-doi.')
        return
    if not args.allow_online_doi:
        parser.error('--lookup-doi requires explicit --allow-online-doi; online access is disabled by default.')
    if not args.workspaces or len(args.workspaces) != 1:
        parser.error('--lookup-doi requires exactly one --workspace.')
    if args.download_source_url:
        if not args.acquisition_folder or not args.confirm_source_rights:
            parser.error('Downloading requires --acquisition-folder and --confirm-source-rights.')
    elif args.acquisition_folder or args.confirm_source_rights:
        parser.error('--acquisition-folder and --confirm-source-rights require --download-source-url.')
    if args.unpaywall_email_env and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.unpaywall_email_env):
        parser.error('Use an environment variable name for the Unpaywall contact email.')


def preview_json(preview):
    def metadata(record):
        return {key: value for key, value in asdict(record).items() if key != 'raw_json'}
    return dict(doi=preview.doi, workspace=preview.workspace, fetched_utc=preview.fetched_utc,
        cached=preview.cached, metadata=metadata(preview.metadata),
        provider_records=[metadata(record) for record in preview.provider_records],
        sources=[asdict(source) for source in preview.sources], warnings=preview.warnings)


def run(app, args):
    workspace = args.workspaces[0]
    email = os.environ.get(args.unpaywall_email_env, '') if args.unpaywall_email_env else ''
    preview = app.lookup_doi(workspace, args.lookup_doi, online_allowed=args.allow_online_doi,
        providers=tuple(args.doi_provider or ('crossref',)), unpaywall_email=email)
    output = {'preview': preview_json(preview)}
    if args.download_source_url:
        matches = [i for i, source in enumerate(preview.sources) if source.url == args.download_source_url
                   and source.reported_open_access and source.format.casefold() == 'pdf']
        if len(matches) != 1:
            raise ValueError('The exact selected open-access PDF URL is missing or ambiguous in the fresh preview. '
                             'Review a new --lookup-doi result before downloading.')
        acquired = app.acquire_doi_source(workspace, preview, matches[0], args.acquisition_folder,
            online_allowed=args.allow_online_doi, rights_confirmed=args.confirm_source_rights)
        output['acquisition'] = {**asdict(acquired), 'path': str(acquired.path),
                                 'receipt_path': str(acquired.receipt_path)}
    elif args.save_doi_reference:
        output['reference'] = asdict(app.save_doi_reference(workspace, preview))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    # A retained download is a recoverable partial result, not successful import.
    return 2 if output.get('acquisition', {}).get('state') not in (None, 'complete', 'duplicate') else 0
