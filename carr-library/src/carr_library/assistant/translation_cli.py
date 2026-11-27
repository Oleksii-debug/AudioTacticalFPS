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
from .providers import Authorization, Gateway, money
from .translation import TranslationService, translation_approval


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
                              glossary=args.glossary, rate_per_million=args.rate_per_million)
    bound = str(money(args.budget))
    if preview['estimated_cost'] is not None and money(preview['estimated_cost']) > money(bound):
        raise ValueError('The entered rate estimate exceeds the approved total budget.')
    review = {**preview, 'profile': profile.to_dict(), 'budget': bound, 'title': args.title}
    return value, {**review, 'review_sha256': sha256(canonical(review).encode('utf-8')).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reviewed Google Basic v2 / Advanced v3 plain-text translation')
    parser.add_argument('--data-dir', type=Path, required=True, help='Existing private library folder')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('profiles', help='List non-secret configured translation accounts')
    languages = sub.add_parser('languages', help='Read the cache, or explicitly fetch current provider declarations')
    languages.add_argument('--profile', required=True)
    languages.add_argument('--approve-remote', action='store_true', help='Approve one official language-list request; no text is sent')
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
        if name == 'submit':
            action.add_argument('--approve-remote', action='store_true', help='Approve external transfer of this reviewed text')
            action.add_argument('--review-sha256', required=True, help='Exact value returned by preview for unchanged text/settings')
    sub.add_parser('jobs', help='List saved statuses without source-text disclosure')
    resume = sub.add_parser('resume', help='Run an already approved job; Ctrl+C pauses before the next bounded request')
    resume.add_argument('job')
    export = sub.add_parser('export', help='Publish a completed derivative into a fresh directory')
    export.add_argument('job')
    export.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = None
    try:
        root = local_path(args.data_dir)
        if not (root / 'library.sqlite').is_file():
            raise ValueError('Choose an existing library with library.sqlite.')
        from ..application import LibraryApp
        library = LibraryApp(root)
        store = ProfileStore(root / 'state' / 'providers')
        service = TranslationService(root / 'state' / 'translation', library=library)
        def current_account(expected):
            try:
                current = store.get(expected.id)
            except (OSError, ValueError):
                current = None
            if current is None or not current.enabled or current.to_dict() != expected.to_dict():
                raise ValueError('The approved account is unavailable, disabled or changed. '
                                 'Review its current settings before further remote requests; '
                                 'completed work remains available for offline export.')
        def account_stop(expected):
            if stop.is_set():
                return True
            current_account(expected)
            return False
        if args.command == 'profiles':
            result = [{key: p.to_dict()[key] for key in ('id', 'name', 'adapter', 'endpoint', 'model', 'enabled')}
                      for p in store.list() if p.adapter in ('google-translation-v2', 'google-translation-v3')]
        elif args.command == 'languages':
            p = store.get(args.profile)
            if args.approve_remote:
                current_account(p)
                identity = uuid4().hex
                approval = Authorization(identity, identity, ((p.id, p.endpoint),),
                    ('translation.languages',), ('user-instruction',), '0', 1, time() + 600)
                service.gateway = Gateway(service.directory / 'provider', mode='APPROVED_REMOTE')
                previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
                result = service.refresh_languages(p, authorization=approval,
                                                   cancelled=lambda: account_stop(p))
            else:
                result = service.languages(p)
        elif args.command in ('preview', 'submit'):
            p = store.get(args.profile)
            value, result = _review(service, p, args)
            if args.command == 'submit':
                if not args.approve_remote or args.review_sha256 != result['review_sha256']:
                    raise PermissionError('Explicitly approve the exact current preview hash before saving a remote job.')
                current_account(p)
                approval, cost = translation_approval(p, value, args.source, args.target, args.budget, glossary=args.glossary)
                identity = service.submit(p, value, args.source, args.target, title=args.title,
                    glossary=args.glossary, authorization=approval, per_call_cost=cost,
                    cancelled=stop.is_set, guard=lambda: current_account(p))
                result = {'id': identity, 'status': 'queued', 'review_sha256': result['review_sha256'],
                          'network_access': False, 'next': 'resume this ID to run its existing approved envelope'}
        elif args.command == 'jobs':
            result = [{key: job.get(key) for key in ('id', 'title', 'status', 'source_language', 'target_language', 'glossary')}
                      | {'completed': len(job['receipts']), 'total': len(job['chunks'])} for job in service.list()]
        elif args.command == 'resume':
            job = service.get(args.job)
            account = ProviderProfile(**job['profile'])
            pending = len(job['receipts']) < len(job['chunks'])
            if pending:
                current_account(account)
                service.gateway = Gateway(service.directory / 'provider', mode='APPROVED_REMOTE')
            previous = signal.signal(signal.SIGINT, lambda *unused: stop.set())
            result = service.resume(args.job, cancelled=stop.is_set,
                guard=lambda: current_account(account) if pending else None,
                progress=lambda done, total, phase: print(f'{done}/{total}: {phase}', file=sys.stderr, flush=True))
        else:
            result = service.export(args.job, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get('status') == 'paused' else 0
    except (ValueError, RuntimeError, OSError) as error:
        print('Translation: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


if __name__ == '__main__':
    raise SystemExit(main())
