"""Explicit document/batch translation CLI on the current provider and job owners."""
from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
import signal
import sys
from threading import Event

from .assistant.models import canonical, local_path
from .assistant.translation import TranslationService, language_approval, translation_approval
from .assistant.translation_structure import capture_text, capture_library, source_text, structure_hash
from .assistant.validation import decode_state_json
from .audio.jobs import regular_file, verified_reader
from .installation_lease import installed_entrypoint


def _json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _read(path, maximum):
    source = local_path(path)
    info = regular_file(source)
    if not 0 < info.st_size <= maximum:
        raise ValueError('The selected input exceeds its explicit bounded file allowance.')
    with verified_reader(source) as stream:
        raw = stream.read(maximum + 1)
        if len(raw) != info.st_size or len(raw) > maximum or stream.read(1):
            raise ValueError('The selected input changed while it was read.')
    return raw


def _profile(service, identity):
    value = next((row for row in service.available_profiles() if row.id == identity), None)
    if value is None:
        raise ValueError('Choose an existing connected Basic v2 or Advanced v3 profile.')
    service._current_profile(value)
    return value


def _document(arguments, service, *, cancelled=lambda: False):
    if arguments.input is not None:
        raw = _read(arguments.input, 8 * 1024**2)
        return capture_text(raw.decode('utf-8'), title=arguments.title,
                            markdown=arguments.markdown)
    from .application import open_existing_library
    regular_file(service.root / 'library.sqlite')
    selections = decode_state_json(_read(arguments.selection, 128 * 1024))
    return capture_library(open_existing_library(service.root, cancelled=cancelled), selections,
                           markdown=arguments.markdown, cancelled=cancelled)


def review(service, profile, document, *, source, target, budget_micros, title, rate='', glossary=''):
    if type(budget_micros) is not int or not 0 <= budget_micros <= 10**12:
        raise ValueError('Choose an explicit bounded integer USD reservation.')
    value = source_text(document)
    preview = service.preview(profile, value, source, target, rate_per_million=rate or None,
                              glossary=glossary, structure=document)
    if preview['estimated_cost'] is not None:
        from decimal import Decimal
        if Decimal(preview['estimated_cost']) * 1000000 > budget_micros:
            raise ValueError('The entered estimate exceeds the selected reservation.')
    record = {'schema': 'carr-translation-review/1', 'profile_fingerprint': profile.fingerprint,
              'profile_id': profile.id, 'source': source, 'target': target, 'title': title,
              'source_sha256': preview['source_sha256'], 'structure_sha256': structure_hash(document),
              'budget_micros': budget_micros, 'currency': 'USD', 'rate': rate, 'glossary': glossary,
              'language_catalog_sha256': preview['language_catalog_sha256']}
    return {**preview, **record,
            'review_sha256': sha256(canonical(record).encode('utf-8')).hexdigest(),
            'text_transferred': False, 'network_performed': False}


def parser():
    result = ArgumentParser(description=__doc__)
    result.add_argument('--data-dir', required=True, help='Existing local library directory.')
    commands = result.add_subparsers(dest='command', required=True)
    commands.add_parser('profiles')
    languages = commands.add_parser('languages')
    languages.add_argument('--profile', required=True)
    languages.add_argument('--refresh', action='store_true')
    languages.add_argument('--allow-network', action='store_true')
    languages.add_argument('--approve-profile')
    for name in ('preview', 'queue'):
        command = commands.add_parser(name)
        command.add_argument('--profile', required=True)
        choice = command.add_mutually_exclusive_group(required=True)
        choice.add_argument('--input', help='Explicit bounded UTF-8 TXT or Markdown text file.')
        choice.add_argument('--selection', help='JSON array of explicit retained document/chapter selections.')
        command.add_argument('--markdown', action='store_true',
                             help='Preserve tested ATX headings and fenced code; other layout is unqualified.')
        command.add_argument('--source', default='auto')
        command.add_argument('--target', required=True)
        command.add_argument('--budget-micros', type=int, required=True,
                             help='Total approved integer USD micro-units; no provider spending-cap claim.')
        command.add_argument('--title', default='Translation')
        command.add_argument('--rate', default='')
        command.add_argument('--glossary', default='')
        if name == 'queue':
            command.add_argument('--approve-profile', required=True)
            command.add_argument('--approve-preview', required=True)
            command.add_argument('--approve-transfer', action='store_true',
                                 help='Approve these exact source bytes and requests for a later explicit run.')
    commands.add_parser('list')
    for name in ('inspect', 'run', 'read', 'export'):
        command = commands.add_parser(name)
        command.add_argument('--job', required=True)
        if name == 'run':
            command.add_argument('--allow-network', action='store_true')
            command.add_argument('--approve-job-sha256', required=True)
        elif name == 'read':
            command.add_argument('--part', choices=('original', 'translation', 'structure'), default='translation')
        elif name == 'export':
            command.add_argument('--destination', required=True)
    return result


@installed_entrypoint()
def main(argv=None, *, service_factory=TranslationService):
    arguments = parser().parse_args(argv)
    cancelled = Event()
    previous = None
    try:
        try:
            previous = signal.signal(signal.SIGINT, lambda signum, frame: cancelled.set())
        except ValueError:
            pass  # Non-main callers keep their existing signal owner.
        # The service opens its current typed state/profile/ledger. It does not
        # fetch capabilities or send selected text merely because CLI starts.
        service = service_factory(arguments.data_dir)
        command = arguments.command
        if command == 'profiles':
            _json([{'id': p.id, 'name': p.name, 'provider': p.provider, 'endpoint': p.endpoint,
                    'model': p.model, 'fingerprint': p.fingerprint,
                    'credential_configured': bool(p.credential_ref)} for p in service.available_profiles()])
        elif command == 'languages':
            profile = _profile(service, arguments.profile)
            if arguments.refresh:
                if not arguments.allow_network or arguments.approve_profile != profile.fingerprint:
                    raise PermissionError('Approve this exact profile and network language discovery explicitly.')
                rows = service.refresh_languages(profile, approval=language_approval(profile),
                                                  cancelled=cancelled.is_set)
            else:
                rows = service.languages(profile)
            _json({'languages': list(rows), 'refreshed': arguments.refresh})
        elif command in ('preview', 'queue'):
            profile = _profile(service, arguments.profile)
            document = _document(arguments, service, cancelled=cancelled.is_set)
            selected = review(service, profile, document, source=arguments.source, target=arguments.target,
                budget_micros=arguments.budget_micros, title=arguments.title, rate=arguments.rate,
                glossary=arguments.glossary)
            if command == 'preview':
                _json(selected)
            else:
                if (not arguments.approve_transfer or arguments.approve_profile != profile.fingerprint
                        or arguments.approve_preview != selected['review_sha256']):
                    raise PermissionError('Preview and approve these exact source bytes, structure, profile and budget.')
                value = source_text(document)
                permit = translation_approval(profile, value, arguments.source, arguments.target,
                    arguments.budget_micros, glossary=arguments.glossary, structure=document)
                identity = service.submit(profile, value, arguments.source, arguments.target,
                    approval=permit, title=arguments.title, glossary=arguments.glossary,
                    structure=document, cancelled=cancelled.is_set)
                # Retain the committed ID even if a later inspection fails.
                response = {'status': 'queued', 'id': identity, 'text_transferred': False}
                try:
                    response['job_sha256'] = sha256(canonical(service.jobs.load(identity)).encode('utf-8')).hexdigest()
                except Exception as exc:
                    response.update(status='queued_inspection_pending', warning=str(exc))
                _json(response)
        elif command == 'list':
            _json(list(service.list()))
        elif command == 'inspect':
            job = service.jobs.load(arguments.job)
            _json({**service._summary(job),
                'job_sha256': sha256(canonical(job).encode('utf-8')).hexdigest(),
                'source_sha256': job['source_sha256'],
                'structure_sha256': None if job.get('structure') is None else structure_hash(job['structure']),
                'profile_fingerprint': job['approval']['profile_fingerprint'],
                'budget_micros': job['approval']['budget_micros']})
        elif command == 'run':
            if not arguments.allow_network:
                raise PermissionError('Explicitly allow the reviewed remote translation run.')
            result = service.resume(arguments.job, cancelled=cancelled.is_set,
                expected_job_sha256=arguments.approve_job_sha256)
            _json(result)
            if result['status'] == 'paused':
                return 130
            if result['status'] != 'complete':
                return 2
        elif command == 'read':
            job = service.jobs.load(arguments.job)
            if arguments.part == 'original':
                value = job['source_text']
            elif arguments.part == 'translation':
                value = ''.join(row['text'] for row in job['receipts'])
            else:
                value = job.get('structure')
            _json({'id': job['id'], 'status': job['status'], 'part': arguments.part,
                   'quotation_authority': False, 'value': value})
        elif command == 'export':
            _json(service.export(arguments.job, arguments.destination, cancelled=cancelled.is_set))
        return 0
    except (KeyboardInterrupt, InterruptedError):
        _json({'status': 'cancelled', 'message': 'Translation stopped; inspect saved jobs before resuming.'})
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        _json({'status': 'error', 'message': str(exc)})
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


if __name__ == '__main__':
    raise SystemExit(main())
