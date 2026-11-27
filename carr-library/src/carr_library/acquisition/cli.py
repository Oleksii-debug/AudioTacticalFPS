"""Headless DOI and ISBN operations using the same scoped native services."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re


def add_arguments(parser, operations):
    operations.add_argument('--lookup-doi', metavar='DOI',
        help='Preview metadata for one explicitly approved public DOI (no source text is uploaded)')
    operations.add_argument('--lookup-isbn', metavar='ISBN',
        help='Preview book metadata for one ISBN from Open Library; no document is downloaded')
    parser.add_argument('--allow-online-doi', action='store_true',
        help='Approve this DOI request to the selected providers; offline is the default')
    parser.add_argument('--doi-provider', action='append', choices=('crossref', 'europe_pmc', 'unpaywall', 'openalex'),
        help='Provider to query; repeat for another provider (default: crossref)')
    parser.add_argument('--allow-online-isbn', action='store_true',
        help='Approve this ISBN-only request to Open Library; offline is the default')
    parser.add_argument('--unpaywall-email-env', metavar='VARIABLE',
        help='Read the Unpaywall contact email from this named environment variable for this request only')
    parser.add_argument('--save-doi-reference', action='store_true',
        help='Save the preview as a bibliographic reference, deduplicated by exact DOI')
    parser.add_argument('--save-isbn-reference', action='store_true',
        help='Save the ISBN preview as a bibliographic reference, reusing an equivalent ISBN record')
    parser.add_argument('--download-source-url', metavar='HTTPS_URL',
        help='Download this exact provider-reported open-access PDF URL from the preview')
    parser.add_argument('--acquisition-folder', type=Path,
        help='New local folder for the acquired PDF and provenance receipt; never overwritten')
    parser.add_argument('--confirm-source-rights', action='store_true',
        help='Confirm you may obtain the selected source; does not confer redistribution rights')


def validate_arguments(parser, args):
    doi_options = (args.allow_online_doi, args.doi_provider is not None,
        args.unpaywall_email_env is not None, args.save_doi_reference,
        args.download_source_url is not None, args.acquisition_folder is not None, args.confirm_source_rights)
    isbn_options = (getattr(args, 'allow_online_isbn', False), getattr(args, 'save_isbn_reference', False))
    if getattr(args, 'lookup_isbn', None) is not None:
        if any(doi_options):
            parser.error('--lookup-isbn cannot use DOI consent, providers, email, save or download options. '
                         'Use --allow-online-isbn and optionally --save-isbn-reference.')
        if not args.allow_online_isbn:
            parser.error('--lookup-isbn requires explicit --allow-online-isbn; online access is disabled by default.')
        if not args.workspaces or len(args.workspaces) != 1:
            parser.error('--lookup-isbn requires exactly one --workspace.')
        return
    if any(isbn_options):
        parser.error('ISBN options require --lookup-isbn.')
    if args.lookup_doi is None:
        if any(doi_options):
            parser.error('DOI options require --lookup-doi.')
        return
    if not args.allow_online_doi:
        parser.error('--lookup-doi requires explicit --allow-online-doi; online access is disabled by default.')
    if not args.workspaces or len(args.workspaces) != 1:
        parser.error('--lookup-doi requires exactly one --workspace.')
    if args.download_source_url is not None:
        if not args.download_source_url.strip():
            parser.error('--download-source-url requires a nonempty provider-reported PDF URL.')
        if not args.acquisition_folder or not args.confirm_source_rights:
            parser.error('Downloading requires --acquisition-folder and --confirm-source-rights.')
    elif args.acquisition_folder or args.confirm_source_rights:
        parser.error('--acquisition-folder and --confirm-source-rights require --download-source-url.')
    if args.unpaywall_email_env is not None and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.unpaywall_email_env):
        parser.error('Use an environment variable name for the Unpaywall contact email.')


def preview_json(preview):
    def metadata(record):
        return {key: value for key, value in asdict(record).items() if key != 'raw_json'}
    return dict(doi=preview.doi, identifier_kind=getattr(preview, 'identifier_kind', 'doi'),
        identifier=getattr(preview, 'identifier', '') or preview.doi,
        workspace=preview.workspace, fetched_utc=preview.fetched_utc,
        cached=preview.cached, metadata=metadata(preview.metadata),
        provider_records=[metadata(record) for record in preview.provider_records],
        sources=[asdict(source) for source in preview.sources], warnings=preview.warnings)


def run(app, args):
    workspace = args.workspaces[0]
    isbn = getattr(args, 'lookup_isbn', None)
    if isbn is not None:
        preview = app.lookup_isbn(workspace, isbn, online_allowed=args.allow_online_isbn)
    else:
        email = os.environ.get(args.unpaywall_email_env, '') if args.unpaywall_email_env else ''
        preview = app.lookup_doi(workspace, args.lookup_doi, online_allowed=args.allow_online_doi,
            providers=tuple(args.doi_provider or ('crossref',)), unpaywall_email=email)
    output = {'preview': preview_json(preview)}
    if isbn is None and args.download_source_url:
        matches = [i for i, source in enumerate(preview.sources) if source.url == args.download_source_url
                   and source.reported_open_access and source.format.casefold() == 'pdf']
        if len(matches) != 1:
            raise ValueError('The exact selected open-access PDF URL is missing or ambiguous in the fresh preview. '
                             'Review a new --lookup-doi result before downloading.')
        acquired = app.acquire_doi_source(workspace, preview, matches[0], args.acquisition_folder,
            online_allowed=args.allow_online_doi, rights_confirmed=args.confirm_source_rights)
        output['acquisition'] = {**asdict(acquired), 'path': str(acquired.path),
                                 'receipt_path': str(acquired.receipt_path)}
    elif ((isbn is not None and getattr(args, 'save_isbn_reference', False)) or
          (isbn is None and args.save_doi_reference)):
        output['reference'] = asdict(app.save_doi_reference(workspace, preview))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    # A retained download is a recoverable partial result, not successful import.
    return 2 if output.get('acquisition', {}).get('state') not in (None, 'complete', 'duplicate') else 0
