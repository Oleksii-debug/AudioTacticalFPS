"""Reviewed plaintext translation commands using the existing jobs and gateway.

No command accepts a secret on argv. Configure credentials in the native account
editor; preview, language-cache reads and job listing never make provider calls.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import signal
import sys
import threading
from time import time
from uuid import uuid4

from .models import canonical, local_path
from .profiles import ProfileStore, ProviderProfile
from .providers import Authorization, Gateway, money, translation_authorization_type
from .translation import TranslationService, translation_approval
from ..application import LibraryApp


def _require_current_account(store, profile):
    """Saved approval never authorizes a replaced or removed account."""
    try:
        current = store.get(profile.id)
    except (OSError, ValueError):
        current = None
    if current is None or not current.enabled or current.to_dict() != profile.to_dict():
        raise ValueError('The selected account is unavailable, disabled or changed. '
                         'Review AI and API connections before another remote request. '
                         'Completed saved work can still be exported offline.')


def _input(path):
    source = local_path(path)
    if not source.is_file() or source.stat().st_size > 8_000_000:
        raise ValueError('Choose a UTF-8 plain-text file of at most 8 MB.')
    with source.open('rb') as stream:
        raw = stream.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError('The selected text exceeds the byte budget.')
    # This exact immutable text is used for review/approval/submission. A later
    # file-path read cannot replace the content sent to the shared service.
    return raw.decode('utf-8', errors='strict')


def _review(service, profile, args):
    value = _input(args.input)
    preview = service.preview(profile, value, args.source, args.target,
                              glossary=args.glossary, rate_per_million=args.rate_per_million,
                              preserve_paragraphs=args.preserve_paragraphs)
    bound = str(money(args.budget))
    if preview['estimated_cost'] is not None and money(preview['estimated_cost']) > money(bound):
        raise ValueError('The entered rate estimate exceeds the approved total budget.')
    review = {**preview, 'profile': profile.to_dict(), 'budget': bound, 'title': args.title}
    return value, {**review, 'review_sha256': sha256(canonical(review).encode('utf-8')).hexdigest()}


def _approved_destination(args, profile):
    local = bool(getattr(args, 'approve_local', False))
    remote = bool(getattr(args, 'approve_remote', False))
    if (profile.local and (not local or remote)) or (not profile.local and (not remote or local)):
        raise PermissionError('Approve this exact selected destination with --approve-local or --approve-remote.')


def _document_selections(library, path, *, cancelled):
    from .translation_documents import capture_document, MAX_DOCUMENTS, MAX_CHARACTERS
    raw = _input(path)
    if len(raw.encode('utf-8')) > 65536:
        raise ValueError('The explicit document selection exceeds 64 KiB.')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate document selection field.')
            value[key] = item
        return value
    rows = json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Invalid document selection number.')))
    if type(rows) is not list or not 1 <= len(rows) <= MAX_DOCUMENTS:
        raise ValueError('Explicitly select between 1 and 64 retained documents.')
    selected, remaining = [], MAX_CHARACTERS
    for row in rows:
        if (type(row) is not dict or not {'workspace', 'edition'} <= set(row)
                or set(row) - {'workspace', 'edition', 'first', 'last', 'topic'}):
            raise ValueError('Selections accept only explicit retained workspace/edition/range/topic identities.')
        item = capture_document(library, row['workspace'], row['edition'],
            first=row.get('first', 1), last=row.get('last'), topic=row.get('topic'),
            cancelled=cancelled, max_characters=remaining)
        selected.append(item)
        remaining -= len(item.document.source_text)
    return tuple(selected)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reviewed Google or explicit local translation; retained-document paragraph batches')
    parser.add_argument('--data-dir', type=Path, required=True, help='Existing private library folder')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('profiles', help='List non-secret configured translation accounts')
    languages = sub.add_parser('languages', help='Read the cache, or explicitly fetch current provider declarations')
    languages.add_argument('--profile', required=True)
    languages.add_argument('--approve-remote', action='store_true', help='Approve one official language-list request; no text is sent')
    languages.add_argument('--approve-local', action='store_true', help='Approve one request to the explicitly configured loopback translator')
    for name in ('preview', 'submit'):
        action = sub.add_parser(name, help='Review exact text/settings' if name == 'preview' else 'Approve the unchanged preview and save a paused-resumable job')
        action.add_argument('--profile', required=True)
        action.add_argument('--input', type=Path, required=True, help='Selected UTF-8 plain text; no document formatting claim')
        action.add_argument('--source', default='auto')
        action.add_argument('--target', required=True)
        action.add_argument('--glossary', help='Existing Advanced v3 glossary resource in the same project/non-global region')
        action.add_argument('--budget', required=True, help='Explicit total USD reservation, not provider-enforced billing')
        action.add_argument('--rate-per-million', help='Optional owner-entered current contract rate; no live price lookup')
        action.add_argument('--title', default='Translation')
        action.add_argument('--preserve-paragraphs', action='store_true', help='Retain explicit blank-line paragraph boundaries, without source-file layout')
        if name == 'submit':
            action.add_argument('--approve-remote', action='store_true', help='Approve external transfer of this reviewed text')
            action.add_argument('--approve-local', action='store_true', help='Approve sending only this reviewed text to the selected loopback service')
            action.add_argument('--review-sha256', required=True, help='Exact value returned by preview for unchanged text/settings')
    sub.add_parser('jobs', help='List saved statuses without source-text disclosure')
    resume = sub.add_parser('resume', help='Run an already approved job; Ctrl+C pauses before the next bounded request')
    resume.add_argument('job')
    export = sub.add_parser('export', help='Publish a completed derivative into a fresh directory')
    export.add_argument('job')
    export.add_argument('--output', type=Path, required=True)
    for name in ('documents-preview', 'documents-submit'):
        action = sub.add_parser(name, help='Review or save explicit retained-source paragraph jobs; no request is sent')
        action.add_argument('--profile', required=True)
        action.add_argument('--selections', type=Path, required=True, help='UTF-8 JSON array of explicit workspace/edition/first/last/topic selections')
        action.add_argument('--source', default='auto')
        action.add_argument('--target', required=True)
        action.add_argument('--budget', required=True)
        action.add_argument('--rate-per-million')
        action.add_argument('--glossary', help='Existing Advanced v3 glossary; explicit source language required')
        if name == 'documents-submit':
            action.add_argument('--review-sha256', required=True)
            action.add_argument('--approve-remote', action='store_true')
            action.add_argument('--approve-local', action='store_true')
    action = sub.add_parser('documents-run', help='Run or resume only explicitly named existing source jobs')
    action.add_argument('jobs', nargs='+')
    action = sub.add_parser('documents-export', help='Export explicit completed source jobs into new child directories')
    action.add_argument('jobs', nargs='+')
    action.add_argument('--parent', type=Path, required=True)
    for name in ('docx-preview', 'docx-submit', 'docx-run', 'docx-export',
                 'epub-preview', 'epub-submit', 'epub-run', 'epub-export'):
        action = sub.add_parser(name,
            help='Retained DOCX or EPUB text/layout mapping through existing jobs; explicit rich-format limits')
        action.add_argument('--workspace', required=True)
        action.add_argument('--edition', required=True)
        action.add_argument('--topic')
        if name.endswith(('-preview', '-submit')):
            action.add_argument('--profile', required=True)
            action.add_argument('--source', default='auto')
            action.add_argument('--target', required=True)
            action.add_argument('--budget', required=True)
            action.add_argument('--rate-per-million')
            action.add_argument('--glossary')
        else:
            action.add_argument('jobs', nargs='+', help='Explicit existing paragraph job IDs; no automatic substitution')
        if name.endswith('-submit'):
            action.add_argument('--review-sha256', required=True)
            action.add_argument('--approve-remote', action='store_true')
            action.add_argument('--approve-local', action='store_true')
        if name.endswith('-export'):
            action.add_argument('--output', type=Path, required=True,
                                help='NEW .' + name.split('-')[0] + ' file outside the private library')
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = None
    try:
        root = local_path(args.data_dir)
        if not (root / 'library.sqlite').is_file():
            raise ValueError('Choose an existing library with library.sqlite.')
        library = LibraryApp(root)
        store = ProfileStore(root / 'state' / 'providers')
        service = TranslationService(root / 'state' / 'translation', library=library)

        def remote_cancelled(profile):
            if stop.is_set():
                return True
            _require_current_account(store, profile)
            return False
        if args.command == 'profiles':
            result = [{key: p.to_dict()[key] for key in ('id', 'name', 'adapter', 'endpoint', 'model', 'enabled')}
                      for p in store.list() if p.adapter in ('google-translation-v2', 'google-translation-v3', 'libretranslate-local')]
        elif args.command == 'languages':
            p = store.get(args.profile)
            if args.approve_remote or args.approve_local:
                _approved_destination(args, p)
                _require_current_account(store, p)
                identity = uuid4().hex
                approval = translation_authorization_type(p)(identity, identity, ((p.id, p.endpoint),),
                    ('translation.languages',), ('user-instruction',), '0', 1, time() + 600)
                service.gateway = Gateway(service.directory / 'provider', mode='LOCAL_ONLY' if p.local else 'APPROVED_REMOTE')
                previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
                result = service.refresh_languages(p, authorization=approval,
                    cancelled=lambda: remote_cancelled(p))
            else:
                result = service.languages(p)
        elif args.command in ('preview', 'submit'):
            p = store.get(args.profile)
            value, result = _review(service, p, args)
            if args.command == 'submit':
                _approved_destination(args, p)
                if args.review_sha256 != result['review_sha256']:
                    raise PermissionError('Explicitly approve the exact current preview hash before saving a translation job.')
                _require_current_account(store, p)
                approval, cost = translation_approval(p, value, args.source, args.target, args.budget,
                    glossary=args.glossary, preserve_paragraphs=args.preserve_paragraphs)
                identity = service.submit(p, value, args.source, args.target, title=args.title,
                    glossary=args.glossary, authorization=approval, per_call_cost=cost,
                    preserve_paragraphs=args.preserve_paragraphs,
                    cancelled=lambda: remote_cancelled(p), guard=lambda: _require_current_account(store, p))
                result = {'id': identity, 'status': 'queued', 'review_sha256': result['review_sha256'],
                          'network_access': False, 'next': 'resume this ID to run its existing approved envelope'}
        elif args.command == 'jobs':
            result = [{key: job.get(key) for key in ('id', 'title', 'status', 'source_language', 'target_language', 'glossary')}
                      | {'completed': len(job['receipts']), 'total': len(job['chunks'])} for job in service.list()]
        elif args.command == 'resume':
            saved = service.get(args.job)
            p = ProviderProfile(**saved['profile'])
            _require_current_account(store, p)
            service.gateway = Gateway(service.directory / 'provider', mode='LOCAL_ONLY' if p.local else 'APPROVED_REMOTE')
            previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
            result = service.resume(args.job, cancelled=lambda: remote_cancelled(p),
                guard=lambda: _require_current_account(store, p),
                progress=lambda done, total, phase: print(f'{done}/{total}: {phase}', file=sys.stderr, flush=True))
        elif args.command in ('documents-preview', 'documents-submit'):
            from .translation_documents import review_documents, prepare_documents
            p = store.get(args.profile)
            previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
            selected = _document_selections(library, args.selections, cancelled=stop.is_set)
            def current():
                _require_current_account(store, p)
            result = review_documents(service, p, selected, args.source, args.target, args.budget,
                glossary=args.glossary, rate_per_million=args.rate_per_million, cancelled=stop.is_set, guard=current)
            if args.command == 'documents-submit':
                _approved_destination(args, p)
                result = prepare_documents(service, p, selected, args.source, args.target, args.budget,
                    approved_review=args.review_sha256, glossary=args.glossary, rate_per_million=args.rate_per_million,
                    cancelled=stop.is_set, guard=current)
        elif args.command.startswith(('docx-', 'epub-')):
            document_format, action = args.command.split('-', 1)
            if document_format == 'docx':
                from .translation_layout import (
                    capture_layout, review_layout, prepare_layout, order_layout_jobs, export_layout)
            else:
                from .translation_epub_layout import (
                    capture_layout, review_layout, prepare_layout, order_layout_jobs, export_layout)
            previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
            selected = capture_layout(library, args.workspace, args.edition, topic=args.topic,
                                      cancelled=stop.is_set)
            if action in ('preview', 'submit'):
                p = store.get(args.profile)
                def current():
                    _require_current_account(store, p)
                if action == 'preview':
                    result = review_layout(service, p, selected, args.source, args.target, args.budget,
                        glossary=args.glossary, rate_per_million=args.rate_per_million,
                        cancelled=stop.is_set, guard=current)
                else:
                    _approved_destination(args, p)
                    result = prepare_layout(service, p, selected, args.source, args.target, args.budget,
                        approved_review=args.review_sha256, glossary=args.glossary,
                        rate_per_million=args.rate_per_million, cancelled=stop.is_set, guard=current)
            else:
                identities = order_layout_jobs(service, selected, args.jobs, cancelled=stop.is_set)
                if action == 'run':
                    from .translation_documents import run_documents
                    result = run_documents(service, identities, cancelled=stop.is_set,
                        progress=lambda done, total, phase: print(phase, file=sys.stderr, flush=True))
                else:
                    from ..research.export_file import capture_export_destination
                    approval = capture_export_destination(args.output, private_root=library.root,
                                                          cancelled=stop.is_set)
                    if approval.state is not None:
                        raise FileExistsError('Choose a new ' + document_format.upper() + ' filename; existing files are not replaced.')
                    result = export_layout(service, selected, identities, args.output,
                                           approval=approval, cancelled=stop.is_set)
        elif args.command in ('documents-run', 'documents-export'):
            from .translation_documents import run_documents, export_documents
            previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
            if args.command == 'documents-run':
                result = run_documents(service, args.jobs, cancelled=stop.is_set,
                    progress=lambda done, total, phase: print(phase, file=sys.stderr, flush=True))
            else:
                result = export_documents(service, args.jobs, args.parent, cancelled=stop.is_set)
        else:
            result = service.export(args.job, args.output)
        # JSON Unicode escapes preserve values even when a Windows pipe uses a
        # legacy console encoding. No interpreter/environment UTF-8 assumption.
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 2 if isinstance(result, dict) and result.get('status') == 'paused' else 0
    except (ValueError, RuntimeError, OSError) as error:
        receipt = getattr(error, 'receipt', None)
        if receipt is not None:
            print(json.dumps({'receipt': receipt, 'warning': str(error)}, ensure_ascii=True, indent=2))
            return 2
        print('Translation: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


if __name__ == '__main__':
    raise SystemExit(main())
