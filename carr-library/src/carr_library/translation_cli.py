"""Review, queue and explicitly run the same selected/scoped translation services."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import signal
import sqlite3
import sys
from threading import Event

from .application import LibraryApp
from .assistant.models import canonical
from .assistant.translation import TranslationService, language_approval, translation_approval, _assemble_translation
from .assistant.translation_scope import TranslationScopeService
from .assistant.translation_scope_state import scope_approval
from .assistant.translation_selection import DocumentSelection, capture_selection
from .assistant.translation_state import MAX_SOURCE_CHARACTERS, split_text, text_hash
from .audio.jobs import verified_reader
from .installation_lease import installed_entrypoint


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument('--data-dir', type=Path, required=True)
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('profiles')
    languages = commands.add_parser('languages')
    languages.add_argument('--profile', required=True)
    languages.add_argument('--refresh', action='store_true')
    languages.add_argument('--allow-network', action='store_true')
    languages.add_argument('--approve-profile')
    for name in ('preview', 'queue'):
        command = commands.add_parser(name)
        command.add_argument('--profile', required=True)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument('--input', type=Path, help='Explicit UTF-8 text; exact bytes retained as a plain-text source.')
        source.add_argument('--workspace', help='Existing library workspace for authenticated retained documents.')
        command.add_argument('--edition', action='append', default=[])
        command.add_argument('--scope-mode', choices=('document', 'chapter', 'batch'), default='document')
        command.add_argument('--chapter', type=int)
        command.add_argument('--source', default='auto')
        command.add_argument('--target', required=True)
        command.add_argument('--budget-micros', type=int, required=True,
                             help='Explicit integer USD reservation; local translation requires zero.')
        command.add_argument('--title', default='Translation')
        command.add_argument('--rate', default='')
        command.add_argument('--glossary', default='')
        if name == 'queue':
            command.add_argument('--approve-profile', required=True)
            command.add_argument('--approve-preview', required=True)
            command.add_argument('--approve-transfer', action='store_true')
    listing = commands.add_parser('list')
    listing.add_argument('--scoped', action='store_true')
    for name in ('inspect', 'run', 'read', 'export'):
        command = commands.add_parser(name)
        command.add_argument('--job', required=True)
        command.add_argument('--scoped', action='store_true')
        if name == 'run':
            command.add_argument('--allow-network', action='store_true',
                                 help='Explicitly permit the reviewed endpoint, including loopback.')
            command.add_argument('--approve-job-sha256', required=True,
                                 help='Exact validated checkpoint hash from queue or inspect.')
        if name == 'export':
            command.add_argument('--destination', type=Path, required=True)
    return root


def _profile(service, identity):
    chosen = next((item for item in service.available_profiles() if item.id == identity), None)
    if chosen is None:
        raise ValueError('Choose an existing translation connection from profiles.')
    return chosen


def _input(args):
    if args.edition or args.chapter is not None or args.scope_mode != 'document':
        raise ValueError('Edition and chapter selectors require --workspace.')
    with verified_reader(args.input) as stream:
        raw = stream.read(MAX_SOURCE_CHARACTERS * 4 + 4)
    if len(raw) > MAX_SOURCE_CHARACTERS * 4 + 3:
        raise ValueError('The selected UTF-8 text exceeds its byte budget.')
    value = raw.decode('utf-8-sig')
    split_text(value)
    return value


def _review(service, library, args, cancelled):
    profile = _profile(service, args.profile)
    if type(args.budget_micros) is not int or not 0 <= args.budget_micros <= 10**12:
        raise ValueError('An explicit bounded integer reservation is required.')
    if profile.local and (args.budget_micros != 0 or args.rate or args.glossary):
        raise ValueError('Local translation requires zero remote reservation, no price and no glossary.')
    if args.workspace is not None:
        if not args.edition or (args.chapter is not None and args.scope_mode != 'chapter'):
            raise ValueError('Select retained editions and a matching document/chapter/batch mode.')
        selection = capture_selection(library, args.workspace,
            [DocumentSelection(identity, chapter=args.chapter) for identity in args.edition],
            mode=args.scope_mode, cancelled=cancelled)
        owner = TranslationScopeService(library, service=service)
        preview = owner.preview(profile, selection, args.source, args.target, args.budget_micros,
                                rate=args.rate, glossary=args.glossary, cancelled=cancelled)
        return profile, owner, selection, dict(preview, scoped=True, network_performed=False,
                                               profile_fingerprint=profile.fingerprint)
    value = _input(args)
    preview = service.preview(profile, value, args.source, args.target,
                              rate_per_million=args.rate or None, glossary=args.glossary)
    if preview['estimated_cost'] is not None and Decimal(preview['estimated_cost']) * 1_000_000 > args.budget_micros:
        raise ValueError('The entered price estimate exceeds the approved reservation.')
    # An exact review includes the declared inventory, immutable text, metadata and budget.
    reviewed = dict(profile_fingerprint=profile.fingerprint, source_sha256=text_hash(value),
        source=args.source, target=args.target, budget_micros=args.budget_micros,
        title=args.title, rate=args.rate, glossary=args.glossary,
        language_catalog_sha256=preview['language_catalog_sha256'])
    return profile, service, value, dict(preview, review_sha256=text_hash(canonical(reviewed)),
        budget_micros=args.budget_micros, profile_fingerprint=profile.fingerprint,
        scoped=False, network_performed=False)


@installed_entrypoint
def main(argv=None, *, service_factory=TranslationService, library_factory=LibraryApp):
    args = parser().parse_args(argv)
    stopped = Event()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        library = library_factory(args.data_dir)
        service = service_factory(library.root)
        if service.root != library.root:
            raise ValueError('Translation must use the explicitly selected library.')
        command = args.command
        if command == 'profiles':
            result = [dict(id=p.id, name=p.name, provider=p.provider, endpoint=p.endpoint,
                model=p.model, fingerprint=p.fingerprint, local=p.local,
                credential_configured=bool(p.credential_ref)) for p in service.available_profiles()]
        elif command == 'languages':
            profile = _profile(service, args.profile)
            if args.refresh:
                if not args.allow_network or args.approve_profile != profile.fingerprint:
                    raise PermissionError('Approve the exact current connection and allow this language-only request.')
                result = service.refresh_languages(profile, approval=language_approval(profile), cancelled=stopped.is_set)
            else:
                if args.allow_network or args.approve_profile:
                    raise ValueError('Network approval applies only to an explicit --refresh.')
                result = service.languages(profile)
        elif command in ('preview', 'queue'):
            profile, owner, value, preview = _review(service, library, args, stopped.is_set)
            result = preview
            if command == 'queue':
                if (not args.approve_transfer or args.approve_profile != profile.fingerprint
                        or args.approve_preview != preview['review_sha256']):
                    raise PermissionError('Review and approve the exact current source, profile, languages and budget.')
                if stopped.is_set():
                    raise InterruptedError('Stopped before publishing a reviewed translation job.')
                if preview['scoped']:
                    permit = scope_approval(profile, value, args.source, args.target, args.budget_micros,
                        language_catalog_sha256=preview['language_catalog_sha256'],
                        glossary=args.glossary, rate=args.rate)
                    identity = owner.submit(profile, value, args.source, args.target, args.budget_micros,
                        approval=permit, glossary=args.glossary, rate=args.rate, cancelled=stopped.is_set)
                    job = owner.plans.load(identity)
                else:
                    permit = translation_approval(profile, value, args.source, args.target,
                                                  args.budget_micros, glossary=args.glossary)
                    identity = owner.submit(profile, value, args.source, args.target, approval=permit,
                                            glossary=args.glossary, title=args.title)
                    job = owner.jobs.load(identity)
                result = dict(id=identity, status=job['status'], scoped=preview['scoped'],
                              job_sha256=text_hash(canonical(job)), network_performed=False)
        else:
            owner = TranslationScopeService(library, service=service) if args.scoped else service
            if command == 'list':
                result = owner.list()
            elif command == 'run':
                if not args.allow_network:
                    raise PermissionError('Explicitly allow the reviewed translation endpoint, including loopback.')
                result = owner.resume(args.job, cancelled=stopped.is_set,
                                      expected_job_sha256=args.approve_job_sha256)
            elif command == 'export':
                result = owner.export(args.job, args.destination, cancelled=stopped.is_set)
            else:
                job = (owner.plans if args.scoped else owner.jobs).load(args.job)
                if command == 'inspect':
                    result = dict(job=job, job_sha256=text_hash(canonical(job)), scoped=args.scoped)
                else:
                    if args.scoped:
                        raise ValueError('Use export for authenticated scoped structure and bilingual derivatives.')
                    if job['status'] != 'complete':
                        raise ValueError('Finish every selected-text chunk before reading its complete derivative.')
                    service._receipt_integrity(job)
                    result = dict(id=args.job, original=job['source_text'],
                                  translated=_assemble_translation(split_text(job['source_text']), job['receipts']))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if command == 'run' and result['status'] != 'complete':
            return 130 if result['status'] == 'paused' else 1
        return 0
    except (InterruptedError, KeyboardInterrupt):
        print('Translation stopped; accepted checkpoints remain saved.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        print('Translation: ' + str(error), file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == '__main__':
    raise SystemExit(main())
