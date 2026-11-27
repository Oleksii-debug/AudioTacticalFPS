"""Headless entry points share the exact native project's local production service."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from ..durable_state import read_json
from .project import Project, parse_script
from .render import ProductionService, json_safe


def data_directory(selected):
    if selected:
        return Path(selected)
    from platformdirs import user_data_path
    from ..preferences import selected_directory
    return selected_directory() or user_data_path('Universal Research Library', appauthor=False, roaming=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local reading and audio production; no remote fallback')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--input-root', action='append', type=Path, default=[], help='Explicit local cue folder; repeat if needed')
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser('voices', help='List actually installed local voices')
    operations.add_parser('jobs', help='List saved resumable production jobs')
    operations.add_parser('projects', help='List saved projects')
    operations.add_parser('publications', help='List saved EPUB publication receipts; explicit verification checks bytes')
    publish = operations.add_parser('publish-epub', help='Publish a completed Studio job with measured line synchronization')
    publish.add_argument('job_id')
    publish.add_argument('--output', type=Path, required=True)
    publish.add_argument('--language', default='und')
    publish.add_argument('--creator', default='')
    publish.add_argument('--max-bytes', type=int, default=32 * 1024**3)
    publish.add_argument('--timeout', type=float, default=3600)
    verify_publication = operations.add_parser('verify-publication', help='Verify an existing EPUB against its exact receipt')
    verify_publication.add_argument('publication_id')
    verify_publication.add_argument('--file', type=Path, help='Explicit existing EPUB after relocation')
    verify_publication.add_argument('--max-bytes', type=int, default=32 * 1024**3)
    verify_publication.add_argument('--timeout', type=float, default=3600)
    operations.add_parser('capabilities', help='Inspect the actual installed audio runtime')
    render = operations.add_parser('render', help='Produce actual WAV master, chapters, stems and subtitles')
    source = render.add_mutually_exclusive_group(required=True)
    source.add_argument('--script', type=Path, help='UTF-8 script file')
    source.add_argument('--project', type=Path, help='Versioned JSON project with chosen voices')
    render.add_argument('--title', default='Audio production')
    render.add_argument('--voice', action='append', default=[], help='Installed voice ID; repeated voices rotate through roles')
    render.add_argument('--output', type=Path, required=True, help='Existing parent folder; a new project subfolder is created')
    render.add_argument('--format', default='wav', choices=('wav','flac','mp3','ogg','opus','m4a','m4b','aiff','wma'))
    render.add_argument('--no-stems', action='store_true')
    render.add_argument('--approve-input', action='append', type=Path, default=[],
                        help='Explicit exact local input to approve after reviewing an imported project; repeat as needed')
    resume = operations.add_parser('resume', help='Resume a saved interrupted job, retaining successful chunks')
    resume.add_argument('job_id')
    resume.add_argument('--output', type=Path, help='Explicit new output parent for a restored job')
    resume.add_argument('--approve-input', action='append', type=Path, default=[],
                        help='Explicit exact local input to approve for this restored job; repeat as needed')
    args = parser.parse_args(argv)
    service = None
    try:
        from ..application import LibraryApp
        app = LibraryApp(data_directory(args.data_dir))
        service = ProductionService(app.root / 'state' / 'studio', input_roots=args.input_root)
        if args.operation == 'voices':
            result = [json_safe(asdict(voice)) for voice in service.voices.voices()]
        elif args.operation == 'projects':
            result = [project.to_dict() for project in service.projects.list()]
        elif args.operation == 'jobs':
            result = [{'id': job['id'], 'title': job['project']['title'], 'status': job['status'],
                       'progress': job['progress'], 'destination': job['destination']} for job in service.jobs()]
        elif args.operation in ('publications', 'publish-epub', 'verify-publication'):
            from ..audio.models import AudioBudget
            from .publishing import PublicationService
            if args.operation == 'publications':
                result = PublicationService(service).publications()
            else:
                publisher = PublicationService(service, budget=AudioBudget(
                    max_output_bytes=args.max_bytes, timeout=args.timeout))
                if args.operation == 'publish-epub':
                    result = publisher.export_epub(args.job_id, args.output, language=args.language,
                        creator=args.creator, progress=lambda text: print(text, file=sys.stderr, flush=True))
                else:
                    result = publisher.verify(args.publication_id, output=args.file)
        elif args.operation == 'capabilities':
            runtime = service._runtime()
            result = {'audio': runtime.capabilities(), 'music': runtime.music_capabilities()}
        else:
            if args.operation == 'render':
                if args.project:
                    project = Project.from_dict(read_json(args.project))
                    service.register_project_inputs(project, imported=True)
                else:
                    if args.script.stat().st_size > 16 * 1024**2:
                        raise ValueError('Script exceeds the project text budget; split by chapters.')
                    project = parse_script(args.script.read_text(encoding='utf-8-sig'), title=args.title,
                                           voice_ids=args.voice)
                identity = service.submit(project, args.output, format=args.format, stems=not args.no_stems)
            else:
                identity = args.job_id
                if args.output is not None:
                    service.relocate_restored_job(identity, args.output)
            if args.approve_input:
                pending = set(service.restored_inputs_to_review(identity))
                selected = tuple(str(path) for path in args.approve_input)
                if not set(selected) <= pending:
                    raise ValueError('Approve only exact inputs listed for this selected production job.')
                service.approve_restored_inputs(selected)
            print('Production job: ' + identity, file=sys.stderr, flush=True)
            job = service.run(identity, progress=lambda text: print(text, file=sys.stderr, flush=True))
            result = {'id': job['id'], 'status': job['status'], 'destination': job['destination'],
                      'artifacts': job['artifacts'], 'result': job['result']}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print('Production interrupted. Resume its saved job ID to reuse completed chunks.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Audio Studio: ' + str(exc), file=sys.stderr)
        return 1


def assistant_main(argv=None):
    parser = argparse.ArgumentParser(description='Scoped local assistant; preview first, explicit --run executes')
    parser.add_argument('--data-dir', type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--instructions', type=Path)
    source.add_argument('--resume')
    source.add_argument('--list-local-profiles', action='store_true',
                        help='List enabled local chat profiles without contacting them')
    parser.add_argument('--local-profile',
                        help='Explicit profile ID for a proposal only; save and review its JSON before a separate --run')
    parser.add_argument('--workspace')
    parser.add_argument('--input-root', action='append', type=Path, default=[])
    parser.add_argument('--output-root', type=Path)
    parser.add_argument('--allow-create', action='store_true')
    parser.add_argument('--allow-full-backup', action='store_true')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    if args.local_profile is not None and (not args.instructions or args.run):
        parser.error('--local-profile requires --instructions and cannot be combined with --run. '
                     'Save and review the proposed JSON, then use it as --instructions in a separate invocation.')
    if args.list_local_profiles and args.run:
        parser.error('--list-local-profiles does not execute a plan.')
    try:
        from ..application import LibraryApp
        from ..assistant import AssistantService, Scope
        from ..assistant.credentials import validate_profile_id
        from ..assistant.models import local_path, text
        if args.local_profile is not None:
            validate_profile_id(args.local_profile)
        instruction = None
        if args.instructions:
            path = local_path(args.instructions)
            maximum = 512 * 1024 if args.local_profile is not None else 4 * 1024**2
            with path.open('rb') as stream:
                raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError('Instruction file exceeds its budget.')
            instruction = text(raw.decode('utf-8-sig'), 'assistant instruction', maximum=maximum)
        app = LibraryApp(data_directory(args.data_dir))
        production = ProductionService(app.root / 'state' / 'studio', input_roots=args.input_root)
        service = AssistantService(app, production=production.assistant_callback)
        if args.list_local_profiles:
            result = [{'id': profile.id, 'name': profile.name, 'endpoint': profile.endpoint,
                       'model': profile.model} for profile in service.local_planner_profiles()]
        elif args.resume:
            if not args.run:
                result = asdict(service.get(args.resume))
            else:
                result = asdict(service.resume(args.resume))
        else:
            scope = Scope(args.workspace, tuple(args.input_root), args.output_root,
                          allow_create=args.allow_create, allow_full_backup=args.allow_full_backup)
            if args.local_profile is not None:
                plan = service.propose_local(instruction, scope, profile_id=args.local_profile)
                print('Local proposal only. Review this JSON and its exact scope before running it in a separate invocation.',
                      file=sys.stderr, flush=True)
            else:
                plan = service.preview(instruction, scope)
            result = plan.to_dict()
            if args.run:
                identity = service.submit(plan)
                print('Assistant job: ' + identity, file=sys.stderr, flush=True)
                result = asdict(service.run(identity,
                    progress=lambda complete, total, text: print(f'{complete}/{total}: {text}', file=sys.stderr, flush=True)))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print('Assistant interrupted. Resume the saved job to continue.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Local assistant: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
