"""Headless entry points share the exact native project's local production service."""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import signal
import sys
import threading

from ..durable_state import read_json
from .project import Project, parse_script
from .render import ProductionBudget, ProductionService, json_safe


def _read_utf8(path, maximum):
    """Use the shared stable reader and preserve every UTF-8 source character."""
    from ..speech._io import local_file
    from ..state_backup import read_state_bytes
    return read_state_bytes(local_file(path), maximum).decode('utf-8')


@contextmanager
def _interrupts():
    """Let workers stop and persist their checkpoint before the CLI exits."""
    stopped = threading.Event()
    previous = {}

    def request_stop(number, frame):
        if stopped.is_set():
            raise KeyboardInterrupt
        stopped.set()

    try:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.signal(number, request_stop)
        yield stopped.is_set
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _run_interruptible(operation, *args, **kwargs):
    with _interrupts() as cancelled:
        result = operation(*args, cancelled=cancelled, **kwargs)
        status = result.get('status') if isinstance(result, dict) else getattr(result, 'status', None)
        if cancelled() and status != 'complete':
            raise InterruptedError('The operation was stopped after saving its checkpoint.')
        return result


def _budget(args):
    values = {}
    for option, field in (('max_cache_mib', 'max_cache_bytes'),
                          ('max_export_mib', 'max_export_bytes'),
                          ('reserve_disk_mib', 'reserve_disk_bytes')):
        value = getattr(args, option, None)
        if value is not None:
            values[field] = value * 1024**2
    if args.timeout is not None:
        values['timeout'] = args.timeout
    return ProductionBudget(**values) if values else None


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
    parser.add_argument('--max-cache-mib', type=int, help='Maximum retained production state and cache in MiB')
    parser.add_argument('--max-export-mib', type=int, help='Maximum output bytes per job in MiB')
    parser.add_argument('--reserve-disk-mib', type=int, help='Keep this much free disk space for recovery')
    parser.add_argument('--timeout', type=float, help='Maximum seconds for one production run; completed chunks remain resumable')
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
    render.add_argument('--mode', choices=('faithful', 'authored'), default='faithful',
                        help='Faithful preserves ordinary prose; authored interprets speaker labels')
    render.add_argument('--voice', action='append', default=[], help='Installed voice ID; repeated voices rotate through roles')
    render.add_argument('--output', type=Path, required=True, help='Existing parent folder; a new project subfolder is created')
    render.add_argument('--format', default='wav', choices=('wav','flac','mp3','ogg','opus','m4a','m4b','aiff','wma'))
    render.add_argument('--no-stems', action='store_true')
    resume = operations.add_parser('resume', help='Resume a saved interrupted job, retaining successful chunks')
    resume.add_argument('job_id')
    reexport = operations.add_parser('reexport', help='Export a saved plan and verified cached audio into a new folder')
    reexport.add_argument('job_id')
    reexport.add_argument('--output', type=Path, required=True, help='Existing output parent; prior exports are retained')
    for name in ('pause', 'cancel'):
        control = operations.add_parser(name, help=f'Request {name} for a saved production job')
        control.add_argument('job_id')
    args = parser.parse_args(argv)
    try:
        from ..application import LibraryApp
        app = LibraryApp(data_directory(args.data_dir))
        service = ProductionService(app.root / 'state' / 'studio', input_roots=args.input_root,
                                    budget=_budget(args))
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
        elif args.operation in ('pause', 'cancel'):
            (service.request_pause if args.operation == 'pause' else service.cancel)(args.job_id)
            result = {'id': args.job_id, 'requested': args.operation}
        else:
            if args.operation == 'render':
                if args.project:
                    project = Project.from_dict(read_json(args.project))
                else:
                    project = parse_script(_read_utf8(args.script, 16 * 1024**2), title=args.title,
                                           voice_ids=args.voice, mode=args.mode)
                identity = service.submit(project, args.output, format=args.format, stems=not args.no_stems)
            elif args.operation == 'reexport':
                identity = service.submit_reexport(args.job_id, args.output)
            else:
                identity = args.job_id
            print('Production job: ' + identity, file=sys.stderr, flush=True)
            job = _run_interruptible(service.run, identity,
                                     progress=lambda text: print(text, file=sys.stderr, flush=True))
            result = {'id': job['id'], 'status': job['status'], 'destination': job['destination'],
                      'artifacts': job['artifacts'], 'result': job['result']}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (KeyboardInterrupt, InterruptedError):
        print('Production interrupted. Resume its saved job ID to reuse completed chunks.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Audio Studio: ' + str(exc), file=sys.stderr)
        return 1


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
        service = AssistantService(app, production=production.assistant_callback)
        if args.resume:
            if not args.run:
                result = asdict(service.get(args.resume))
            else:
                result = asdict(_run_interruptible(service.resume, args.resume))
        else:
            scope = Scope(args.workspace, tuple(args.input_root), args.output_root,
                          allow_create=args.allow_create, allow_full_backup=args.allow_full_backup)
            plan = service.preview(_read_utf8(args.instructions, 4 * 1024**2), scope)
            result = plan.to_dict()
            if args.run:
                identity = service.submit(plan)
                print('Assistant job: ' + identity, file=sys.stderr, flush=True)
                result = asdict(_run_interruptible(service.run, identity,
                    progress=lambda complete, total, text: print(f'{complete}/{total}: {text}', file=sys.stderr, flush=True)))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (KeyboardInterrupt, InterruptedError):
        print('Assistant interrupted. Resume the saved job to continue.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Local assistant: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
