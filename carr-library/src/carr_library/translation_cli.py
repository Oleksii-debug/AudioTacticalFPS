"""Explicit CLI routes over the same translation, provider and source contracts."""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import time
from uuid import uuid4

from .assistant.models import canonical
from .assistant.profiles import ProfileStore
from .assistant.providers import Authorization, Gateway, money
from .assistant.translation import TranslationService, translation_approval
from .assistant.library_translation import LibrarySelection, capture_library_sources, source_summary
from .speech._io import local_file


def _fingerprint(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def _plain_input(path):
    path = local_file(path)
    before = path.stat()
    if not 0 < before.st_size <= 8_000_000:
        raise ValueError('Choose a UTF-8 text file within the two-million-character translation budget.')
    with path.open('rb') as stream:
        raw = stream.read(8_000_001)
        opened = os.fstat(stream.fileno())
    after = path.stat()
    fields = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if len(raw) > 8_000_000 or fields(before) != fields(opened) or fields(before) != fields(after):
        raise ValueError('The selected text file changed while it was being read.')
    return raw.decode('utf-8', errors='strict')


def _source(app, args):
    if args.text_file is not None:
        if args.first is not None or args.last is not None:
            raise ValueError('Reading positions apply only to an explicitly selected library edition.')
        return _plain_input(args.text_file), None
    if not args.workspace:
        raise ValueError('Choose an explicit workspace for library chapter, document or batch input.')
    if (args.first is not None or args.last is not None) and len(args.edition) != 1:
        raise ValueError('A chapter range requires exactly one selected edition.')
    selections = tuple(LibrarySelection(edition, args.first, args.last) for edition in args.edition)
    captured = capture_library_sources(app, args.workspace, selections, topic=args.topic, collection=args.collection)
    return captured.text, captured.binding


def _preview(service, profile, value, binding, args):
    result = service.preview(profile, value, args.source, args.target,
        rate_per_million=args.rate_per_million, source_binding=binding, glossary=args.glossary)
    budget = str(money(args.budget))
    if result['estimated_cost'] is not None and money(result['estimated_cost']) > money(budget):
        raise ValueError('The entered price estimate exceeds the approved total budget.')
    envelope = {'profile': profile.to_dict(), 'source_sha256': result['source_sha256'],
        'source_binding_sha256': binding['sha256'] if binding is not None else None,
        'source_language': args.source, 'target_language': args.target, 'budget': budget, 'glossary': args.glossary,
        'rate_per_million': str(money(args.rate_per_million)) if args.rate_per_million is not None else None}
    return {**result, 'provider_id': profile.id, 'provider_name': profile.name, 'endpoint': profile.endpoint,
            'model': profile.model, 'budget': budget, 'currency': 'USD', 'approval_sha256': _fingerprint(envelope),
            'transmission': 'No request sent by preview. Submit requires this exact digest and --allow-remote.'}


def job_summary(job):
    immutable = {key: job.get(key) for key in (
        'id', 'profile', 'source_sha256', 'source_binding', 'source_language', 'target_language',
        'authorization', 'per_call_cost', 'restore_requires_review', 'glossary')}
    return {'id': job['id'], 'title': job['title'], 'status': job['status'],
        'source_sha256': job['source_sha256'], 'source_language': job['source_language'],
        'target_language': job['target_language'], 'characters': len(job['source_text']),
        'completed': len(job['receipts']), 'total': len(job['chunks']),
        'profile_id': job['profile']['id'], 'profile_name': job['profile']['name'],
        'provider_adapter': job['profile']['adapter'], 'endpoint': job['profile']['endpoint'],
        'model': job['profile']['model'], 'glossary': job.get('glossary'),
        'budget': job['authorization']['budget'], 'currency': job['authorization']['currency'],
        'source': source_summary(job['source_binding']) if job.get('source_binding') is not None else None,
        'restore_requires_review': job.get('restore_requires_review', False),
        'resume_approval_sha256': _fingerprint(immutable)}


def parser():
    root = argparse.ArgumentParser(description='Explicit selected-text and linked library translation.')
    root.add_argument('--data-dir', type=Path, required=True, help='Existing private library directory')
    root.add_argument('--workspace', help='Exact selected library workspace')
    root.add_argument('--topic', help='Optional current topic permission')
    root.add_argument('--collection', help='Optional current collection membership')
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('profiles', help='List nonsecret translation account identities without accessing credentials')
    languages = commands.add_parser('languages', help='Read cached declarations or explicitly fetch the provider language inventory')
    languages.add_argument('--profile', required=True)
    languages.add_argument('--allow-remote', action='store_true')
    languages.add_argument('--cached', action='store_true', help='Read the exact account cache without any external request')
    for name in ('preview', 'submit'):
        command = commands.add_parser(name)
        command.add_argument('--profile', required=True)
        inputs = command.add_mutually_exclusive_group(required=True)
        inputs.add_argument('--text-file', type=Path, help='Exact UTF-8 selected text, read without newline conversion')
        inputs.add_argument('--edition', action='append', help='Selected edition; repeat for at most16 explicit documents')
        command.add_argument('--first', type=int, help='First reading-order position for one chapter/range')
        command.add_argument('--last', type=int, help='Last reading-order position for one chapter/range')
        command.add_argument('--source', default='auto')
        command.add_argument('--target', required=True)
        command.add_argument('--glossary', help='Existing Advanced v3 glossary in the exact project and non-global region')
        command.add_argument('--budget', required=True, help='Explicit total USD reservation bound')
        command.add_argument('--rate-per-million', help='Your current provider contract price; no default quote')
        if name == 'submit':
            command.add_argument('--title', default='Library translation')
            command.add_argument('--allow-remote', action='store_true')
            command.add_argument('--approve-preview', required=True, help='Exact approval_sha256 returned by Preview')
    commands.add_parser('jobs', help='List saved job metadata without original or translated text')
    inspect = commands.add_parser('inspect', help='Validate one saved job and show its exact resume review digest')
    inspect.add_argument('--job', required=True)
    resume = commands.add_parser('resume', help='Resume the existing verified approval; never renew a charge automatically')
    resume.add_argument('--job', required=True)
    resume.add_argument('--allow-remote', action='store_true')
    resume.add_argument('--approve-job', required=True, help='Exact resume_approval_sha256 returned by Inspect')
    export = commands.add_parser('export', help='Publish a new original/translation/bilingual derivative bundle offline')
    export.add_argument('--job', required=True)
    export.add_argument('--output', type=Path, required=True)
    reading = commands.add_parser('reading', help='Save a linked reading project for the existing voice reader')
    reading.add_argument('--job', required=True)
    reading.add_argument('--view', choices=('original', 'translation', 'bilingual'), default='bilingual')
    reading.add_argument('--output', type=Path, required=True)
    return root


def execute(args):
    from .application import LibraryApp
    app = LibraryApp(args.data_dir)
    service = TranslationService(app.root / 'state/translation', library=app, workspace=args.workspace)
    profiles = ProfileStore(app.root / 'state/providers')
    if args.command == 'profiles':
        return {'profiles': [{key: profile.to_dict()[key] for key in
            ('id', 'name', 'adapter', 'endpoint', 'model', 'enabled')}
            for profile in profiles.list()
            if profile.adapter in ('google-translation-v2', 'google-translation-v3')]}
    if args.command == 'languages':
        if args.cached:
            if args.allow_remote:
                raise ValueError('Choose cached language review or an explicit remote refresh, not both.')
            return {'languages': service.languages(profiles.get(args.profile)),
                    'qualification': 'cached provider declarations; no network request or quality qualification'}
        if not args.allow_remote:
            raise PermissionError('Explicit --allow-remote consent is required for provider language discovery.')
        profile = profiles.get(args.profile)
        identity = uuid4().hex
        authorization = Authorization(identity, identity, ((profile.id, profile.endpoint),),
            ('translation.languages',), ('user-instruction',), '0', 1, time() + 600)
        service.gateway = Gateway(service.directory / 'provider', mode='APPROVED_REMOTE')
        return {'languages': service.refresh_languages(profile, authorization=authorization),
                'qualification': 'provider-declared language support; quality is not independently qualified'}
    if args.command in ('preview', 'submit'):
        profile = profiles.get(args.profile)
        value, binding = _source(app, args)
        review = _preview(service, profile, value, binding, args)
        if args.command == 'preview':
            return review
        if not args.allow_remote or args.approve_preview != review['approval_sha256']:
            raise PermissionError('Approve the exact current preview digest and --allow-remote before external transfer.')
        authorization, cost = translation_approval(profile, value, args.source, args.target, args.budget,
                                                   source_binding=binding, glossary=args.glossary)
        service.gateway = Gateway(service.directory / 'provider', mode='APPROVED_REMOTE')
        identity = service.submit(profile, value, args.source, args.target, title=args.title,
            authorization=authorization, per_call_cost=cost, source_binding=binding, glossary=args.glossary)
        return service.resume(identity)
    if args.command == 'jobs':
        return {'jobs': [job_summary(job) for job in service.list()]}
    job = service.get(args.job)
    if args.command == 'inspect':
        return job_summary(job)
    if args.command == 'resume':
        if not args.allow_remote:
            raise PermissionError('Explicit --allow-remote consent is required before resuming external translation calls.')
        if args.approve_job != job_summary(job)['resume_approval_sha256']:
            raise PermissionError('Inspect and explicitly approve the exact saved job before resuming external calls.')
        if profiles.get(job['profile']['id']).to_dict() != job['profile']:
            raise PermissionError('The saved provider account changed. Preview and approve a new translation.')
        service.gateway = Gateway(service.directory / 'provider', mode='APPROVED_REMOTE')
        return service.resume(args.job)
    if args.command == 'export':
        return service.export(args.job, args.output)
    if args.command == 'reading':
        return service.export_reading(args.job, args.output, view=args.view)
    raise ValueError('Unsupported translation command.')


def main(argv=None):
    args = parser().parse_args(argv)
    native_handle = None
    try:
        from .native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
        result = execute(args)
    except (InterruptedError, KeyboardInterrupt):
        print(json.dumps({'status': 'paused', 'message': 'Translation interrupted. Inspect the saved job before resuming.'}),
              file=sys.stderr)
        return 130
    except (OSError, ValueError, PermissionError, RuntimeError) as exc:
        print(json.dumps({'status': 'failed', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if native_handle is not None:
            native_handle.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
