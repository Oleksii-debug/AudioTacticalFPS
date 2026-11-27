"""Headless review and explicit execution through the existing assistant service."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import signal
import sqlite3
import sys
from threading import Event
from uuid import uuid4


def _data_directory(selected):
    if selected is not None:
        return Path(selected)
    from platformdirs import user_data_path
    from ..preferences import selected_directory
    remembered = selected_directory()
    if remembered is not None:
        if not (remembered / 'library.sqlite').is_file():
            raise ValueError('The saved library location is unavailable. Choose --data-dir explicitly.')
        return remembered
    return user_data_path('Universal Research Library', appauthor=False, roaming=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Scoped local assistant: preview first; explicit --run executes')
    parser.add_argument('--data-dir', type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--instructions', type=Path)
    source.add_argument('--resume')
    source.add_argument('--jobs', action='store_true', help='List saved jobs without executing them')
    source.add_argument('--list-local-profiles', action='store_true',
                        help='List configured local chat profiles without contacting them')
    parser.add_argument('--local-profile',
        help='Explicit saved profile ID for one proposal only; review its JSON before a separate --run')
    parser.add_argument('--workspace')
    parser.add_argument('--input-root', action='append', type=Path, default=[])
    parser.add_argument('--output-root', type=Path)
    parser.add_argument('--allow-create', action='store_true')
    parser.add_argument('--allow-full-backup', action='store_true')
    parser.add_argument('--max-bytes', type=int)
    parser.add_argument('--max-files', type=int)
    parser.add_argument('--max-steps', type=int)
    parser.add_argument('--max-seconds', type=float)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    if args.local_profile is not None and (args.instructions is None or args.run):
        parser.error('--local-profile requires --instructions and cannot combine with --run. '
                     'Review the proposal in a separate invocation before execution.')
    if (args.jobs or args.list_local_profiles) and args.run:
        parser.error('Listing saved jobs or profiles never runs an operation.')
    if args.resume and any((args.workspace, args.input_root, args.output_root, args.allow_create,
            args.allow_full_backup, *(value is not None for value in
                (args.max_bytes, args.max_files, args.max_steps, args.max_seconds)))):
        parser.error('Resume uses the original approved scope and budgets. Review a new plan to change permissions.')
    stopped = Event()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        from ..application import LibraryApp
        from ..audio.jobs import verified_reader
        from ..providers import BudgetLedger, JobApproval, ProfileStore, ProviderGateway
        from .models import Scope, text
        from .service import AssistantService
        from .production import LocalProduction
        from .llm import local_plan_request, propose_local_plan
        instruction = None
        if args.instructions is not None:
            maximum = 256 * 1024 if args.local_profile is not None else 4 * 1024**2
            with verified_reader(args.instructions) as stream:
                raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError('Instruction file exceeds its byte budget.')
            instruction = text(raw.decode('utf-8-sig'), 'assistant instruction',
                               maximum=64 * 1024 if args.local_profile is not None else maximum)
        app = LibraryApp(_data_directory(args.data_dir))
        production = LocalProduction(app.root, cancelled=stopped.is_set)
        service = AssistantService(app, production=production)
        if args.list_local_profiles:
            result = [{'id': row.id, 'name': row.name, 'endpoint': row.endpoint, 'model': row.model}
                      for row in ProfileStore(app.root / 'api-connections.json').profiles()
                      if row.local and row.provider == 'openai-compatible' and row.capabilities == ('llm',)]
        elif args.jobs:
            result = [asdict(row) for row in service.jobs()]
        elif args.resume:
            result = asdict(service.resume(args.resume, cancelled=stopped.is_set)
                            if args.run else service.get(args.resume))
        else:
            limits = {name: getattr(args, name) for name in ('max_bytes', 'max_files', 'max_steps', 'max_seconds')
                      if getattr(args, name) is not None}
            scope = Scope(args.workspace, tuple(args.input_root), args.output_root,
                          allow_create=args.allow_create, allow_full_backup=args.allow_full_backup, **limits)
            if args.local_profile is not None:
                store = ProfileStore(app.root / 'api-connections.json')
                profile = next((row for row in store.profiles() if row.id == args.local_profile), None)
                if profile is None:
                    raise ValueError('Choose a configured local profile from --list-local-profiles.')
                request = local_plan_request(instruction)
                approval = JobApproval(uuid4().hex, profile.fingerprint,
                    (request.fingerprint(profile),), allow_loopback=True)
                gateway = ProviderGateway(BudgetLedger(app.root / 'api-operations.sqlite'))
                plan = propose_local_plan(service, gateway, profile, instruction, scope,
                    approval=approval, operation_id=uuid4().hex, cancelled=stopped.is_set, profile_store=store)
                print('Local proposal only. Review its exact scope, then use a separate --run invocation.',
                      file=sys.stderr, flush=True)
            else:
                plan = service.preview(instruction, scope)
            result = plan.to_dict()
            if args.run:
                if stopped.is_set():
                    raise InterruptedError('Assistant stopped before submitting its reviewed plan.')
                identity = service.submit(plan)
                print('Assistant job: ' + identity, file=sys.stderr, flush=True)
                result = asdict(service.run(identity, cancelled=stopped.is_set,
                    progress=lambda done, total, phase: print(f'{done}/{total}: {phase}',
                                                              file=sys.stderr, flush=True)))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.run:
            if result.get('status') in ('paused', 'cancelled'):
                return 130
            if result.get('status') != 'complete':
                return 1
        return 0
    except (KeyboardInterrupt, InterruptedError):
        print('Assistant stopped. Saved job checkpoints remain available for review or resume.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        if stopped.is_set():
            print('Assistant stopped. No new plan was approved.', file=sys.stderr)
            return 130
        print('Local assistant: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == '__main__':
    raise SystemExit(main())
