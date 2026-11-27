"""Headless entry points share the exact native project's local production service."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from ..durable_state import read_json
from ..installation_lease import installed_entrypoint
from .project import Project, parse_script
from .render import ProductionService, json_safe


def data_directory(selected):
    if selected:
        return Path(selected)
    from platformdirs import user_data_path
    from ..preferences import selected_directory
    return selected_directory() or user_data_path('Universal Research Library', appauthor=False, roaming=False)


@installed_entrypoint
def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == 'book':
        from .book_cli import main as book_main
        return book_main(arguments[1:])
    parser = argparse.ArgumentParser(description='Local reading and audio production; no remote fallback',
        epilog='Reviewed multi-book batches: carr-studio book --help (or python -m carr_library studio book --help).')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--input-root', action='append', type=Path, default=[], help='Explicit local cue folder; repeat if needed')
    operations = parser.add_subparsers(dest='operation', required=True)
    audit = operations.add_parser('voice-corpus', help='Audit a reviewed local VoiceWorkshop single-speaker corpus')
    audit.add_argument('review', type=Path, help='VoiceWorkshop review.json containing reviewed clips')
    audit.add_argument('--audio-root', type=Path, required=True, help='Local directory containing the reviewed PCM WAV clips')
    audit.add_argument('--speaker', required=True, help='Expected speaker label in the local review')
    operations.add_parser('voices', help='List actually installed local voices')
    operations.add_parser('jobs', help='List saved resumable production jobs')
    operations.add_parser('projects', help='List saved projects')
    operations.add_parser('capabilities', help='Inspect the actual installed audio runtime')
    operations.add_parser('publications', help='List saved publication receipts; use verify-publication to check actual bytes')
    for operation, extension, description in (
            ('publish-epub', '.epub', 'Local EPUB3 with measured line overlays'),
            ('publish-daisy', '.zip', 'DAISY 2.02 file-set ZIP with measured line synchronization')):
        publish = operations.add_parser(operation, help=description + ' from a completed Studio job')
        publish.add_argument('job_id')
        publish.add_argument('--output', type=Path, required=True, help=f'New {extension} file in an existing directory; never overwritten')
        publish.add_argument('--language', default='und', help='Publication language, such as uk, en or und')
        publish.add_argument('--creator', default='')
        publish.add_argument('--max-bytes', type=int, default=32 * 1024**3, help='Source graph and publication staging byte limit')
        publish.add_argument('--timeout', type=float, default=3600, help='Total export time budget in seconds')
        if operation == 'publish-daisy':
            publish.add_argument('--publisher', required=True, help='Publisher metadata explicitly supplied for this publication')
    verify = operations.add_parser('verify-publication', help='Verify a saved receipt or recover completed publication after interruption')
    verify.add_argument('publication_id')
    verify.add_argument('--file', type=Path, help='Explicit existing publication after relocation; must match the exact saved receipt')
    verify.add_argument('--max-bytes', type=int, default=32 * 1024**3)
    verify.add_argument('--timeout', type=float, default=3600)
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
    convert = operations.add_parser('convert-batch', aliases=['convert'],
        help='Convert explicitly selected local audio/video files into a reviewed batch folder')
    convert.add_argument('sources', nargs='+', type=Path, help='Explicit local media files; at most 64')
    convert.add_argument('--output', type=Path, required=True,
                         help='New batch folder in an existing parent; existing files are never overwritten')
    convert.add_argument('--format', choices=('wav', 'flac', 'mp3', 'm4a', 'm4b', 'ogg', 'opus', 'aac', 'aiff', 'wma'), default='wav')
    convert.add_argument('--resume', action='store_true',
                         help='Review the same originals/settings and keep exact matching saved outputs')
    convert.add_argument('--preview', action='store_true', help='Print the reviewed plan without creating any output')
    convert.add_argument('--start', type=float, default=0)
    convert.add_argument('--duration', type=float)
    convert.add_argument('--sample-rate', type=int, default=24000)
    convert.add_argument('--channels', type=int, choices=(1, 2), default=2)
    convert.add_argument('--bit-depth', type=int, choices=(16, 24, 32), default=16)
    convert.add_argument('--bitrate', type=int, default=192)
    convert.add_argument('--gain-db', type=float, default=0)
    convert.add_argument('--tempo', type=float, default=1)
    convert.add_argument('--loudness', type=float)
    convert.add_argument('--denoise', action='store_true')
    convert.add_argument('--preset', help='Existing reviewed local effect-preset ID; cannot combine with basic effects')
    convert.add_argument('--timeout', type=float, default=3600)
    convert.add_argument('--max-bytes', type=int, default=32 * 1024**3)
    args = parser.parse_args(arguments)
    service = None
    try:
        if args.operation == 'voice-corpus':
            from ..integrations.voiceworkshop_review import audit_voiceworkshop_review
            result = audit_voiceworkshop_review(args.review, args.audio_root, args.speaker)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        from ..application import LibraryApp
        app = LibraryApp(data_directory(args.data_dir))
        if args.operation in ('convert', 'convert-batch'):
            result = _convert_batch(app, args)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            if args.preview or result['status'] == 'completed':
                return 0
            return 2 if result['status'] in ('cancelled', 'timed_out', 'partial', 'attention') else 1
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
        elif args.operation in ('publish-epub', 'publish-daisy', 'publications', 'verify-publication'):
            from ..audio.models import AudioBudget
            from .publishing import PublicationService
            budget = (AudioBudget(timeout=args.timeout, max_output_bytes=args.max_bytes)
                      if args.operation != 'publications' else None)
            publisher = PublicationService(service, budget=budget)
            if args.operation == 'publications':
                result = publisher.publications()
            elif args.operation == 'verify-publication':
                result = publisher.verify(args.publication_id, output=args.file)
            elif args.operation == 'publish-daisy':
                result = publisher.export_daisy(args.job_id, args.output, language=args.language,
                    creator=args.creator, publisher=args.publisher,
                    progress=lambda message: print(message, file=sys.stderr, flush=True))
            else:
                result = publisher.export_epub(args.job_id, args.output, language=args.language, creator=args.creator,
                    progress=lambda message: print(message, file=sys.stderr, flush=True))
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
        print('Operation interrupted. Completed files are retained. Resume a production job, review the same audio batch with --resume, or verify its prepared publication receipt.',
              file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Audio Studio: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close()


def _convert_batch(app, args):
    from ..audio import AudioBudget, Effect
    from ..audio.conversion_batch import AudioConversionBatch
    from ..audio.effect_presets import EffectPresetStore
    from ..durable_state import local_path, regular
    from ..runtime_settings import audio_runtime
    destination = local_path(args.output)
    regular(local_path(destination.parent, exists=True), directory=True)
    selected = tuple(local_path(path, exists=True) for path in args.sources)
    basic = (args.gain_db != 0 or args.tempo != 1 or args.loudness is not None or args.denoise)
    if args.preset is not None:
        if basic:
            raise ValueError('A reviewed preset cannot be silently combined with different basic effect controls.')
        effects = EffectPresetStore(app.root).get(args.preset).effects
    else:
        effects = [Effect('gain', gain_db=args.gain_db), Effect('tempo', factor=args.tempo)]
        if args.denoise:
            effects.append(Effect('denoise'))
        if args.loudness is not None:
            effects.append(Effect('loudness', target_lufs=args.loudness, peak_db=-1))
        effects = tuple(effects)
    runtime = audio_runtime(destination.parent, state_root=app.root / 'state',
        input_roots=tuple(dict.fromkeys(path.parent for path in selected)),
        budget=AudioBudget(timeout=args.timeout, max_output_bytes=args.max_bytes))
    service = AudioConversionBatch(runtime, library_root=app.root)
    progress = lambda done, total, phase: print(f'{done}/{total}: {phase}', file=sys.stderr, flush=True)
    plan = service.prepare(selected, destination, format=args.format, resume=args.resume,
        effects=effects, sample_rate=args.sample_rate, channels=args.channels,
        bit_depth=args.bit_depth, bitrate=args.bitrate, start=args.start, duration=args.duration,
        progress=progress)
    if args.preview:
        return {'status': 'ready', 'plan': plan}
    print('Reviewed output folder: ' + plan['directory'], file=sys.stderr, flush=True)
    return service.run(plan, resume=args.resume, progress=progress)


@installed_entrypoint
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
    production = None
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
            from ..durable_state import regular
            regular(path)
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
    finally:
        if production is not None:
            production.close()


if __name__ == '__main__':
    raise SystemExit(main())
