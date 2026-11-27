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
    resume = operations.add_parser('resume', help='Resume a saved interrupted job, retaining successful chunks')
    resume.add_argument('job_id')
    args = parser.parse_args(argv)
    binding = None
    try:
        from ..application import LibraryApp
        app = LibraryApp(data_directory(args.data_dir))
        if args.operation in ('render', 'resume', 'capabilities'):
            from ..audio.local_runtime import open_local_audio_runtime
            binding = open_local_audio_runtime(app.root, strict=True)
        service = ProductionService(app.root / 'state' / 'studio', input_roots=args.input_root)
        if args.operation == 'voices':
            result = [json_safe(asdict(voice)) for voice in service.voices.voices()]
        elif args.operation == 'projects':
            result = [project.to_dict() for project in service.projects.list()]
        elif args.operation == 'jobs':
            result = [{'id': job['id'], 'title': job['project']['title'], 'status': job['status'],
                       'progress': job['progress'], 'destination': job['destination']} for job in service.jobs()]
        elif args.operation == 'capabilities':
            runtime = service._runtime()
            result = {'audio': runtime.capabilities(), 'music': runtime.music_capabilities()}
        else:
            if args.operation == 'render':
                if args.project:
                    project = Project.from_dict(read_json(args.project))
                else:
                    if args.script.stat().st_size > 16 * 1024**2:
                        raise ValueError('Script exceeds the project text budget; split by chapters.')
                    project = parse_script(args.script.read_text(encoding='utf-8-sig'), title=args.title,
                                           voice_ids=args.voice)
                identity = service.submit(project, args.output, format=args.format, stems=not args.no_stems)
            else:
                identity = args.job_id
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
    finally:
        if binding is not None:
            binding.close()


def assistant_main(argv=None):
    parser = argparse.ArgumentParser(description='Scoped local assistant; preview first, explicit --run executes')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--instructions', type=Path)
    parser.add_argument('--workspace')
    parser.add_argument('--input-root', action='append', type=Path, default=[])
    parser.add_argument('--output-root', type=Path)
    parser.add_argument('--allow-create', action='store_true')
    parser.add_argument('--allow-full-backup', action='store_true')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--resume')
    args = parser.parse_args(argv)
    if bool(args.instructions) == bool(args.resume):
        parser.error('Choose an instruction file or a saved job ID to resume.')
    try:
        from ..application import LibraryApp
        from ..assistant import AssistantService, Scope
        app = LibraryApp(data_directory(args.data_dir))
        production = ProductionService(app.root / 'state' / 'studio', input_roots=args.input_root)
        def produce(arguments, output_directory, cancelled, progress):
            # Pure previews, backups and search do not need an audio runtime.
            from ..audio.local_runtime import open_local_audio_runtime
            with open_local_audio_runtime(app.root, strict=True, cancelled=cancelled):
                return production.assistant_callback(arguments, output_directory, cancelled, progress)
        service = AssistantService(app, production=produce)
        if args.resume:
            if not args.run:
                result = asdict(service.get(args.resume))
            else:
                result = asdict(service.resume(args.resume))
        else:
            if args.instructions.stat().st_size > 4 * 1024**2:
                raise ValueError('Instruction file exceeds its budget.')
            scope = Scope(args.workspace, tuple(args.input_root), args.output_root,
                          allow_create=args.allow_create, allow_full_backup=args.allow_full_backup)
            plan = service.preview(args.instructions.read_text(encoding='utf-8-sig'), scope)
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
