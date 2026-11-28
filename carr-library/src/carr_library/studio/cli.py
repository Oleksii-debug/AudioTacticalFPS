"""Headless entry points share the exact native project's local production service."""
import argparse
from carr_library.installation_lease import installed_entrypoint
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


@installed_entrypoint
def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ['book']:
        from .book_cli import main as book_main
        return book_main(arguments[1:])
    parser = argparse.ArgumentParser(description='Local reading and audio production; no remote fallback')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--input-root', action='append', type=Path, default=[], help='Explicit local cue folder; repeat if needed')
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser('voices', help='List actually installed local voices')
    operations.add_parser('jobs', help='List saved resumable production jobs')
    operations.add_parser('projects', help='List saved projects')
    operations.add_parser('capabilities', help='Inspect the actual installed audio runtime')
    operations.add_parser('publications', help='List saved publication receipts; use verify-publication to check actual bytes')
    for operation, extension, description in (
            ('publish-epub', '.epub', 'Local EPUB3 with measured line overlays'),
            ('publish-daisy', '.zip', 'DAISY 2.02 file-set ZIP with measured line synchronization')):
        publish = operations.add_parser(operation, help=description + ' from a completed Studio job')
        publish.add_argument('job_id', help='Completed Scene job ID, scene:ID, or director:PROJECT:JOB')
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
    silence = operations.add_parser('inspect-silence',
        help='Measure warning-only long-silence intervals in an existing completed PCM master')
    silence.add_argument('job_id')
    silence.add_argument('--threshold-dbfs', type=float, required=True,
                         help='Explicit amplitude threshold from -192 through 0 dBFS; every channel must meet it')
    silence.add_argument('--minimum-seconds', type=float, required=True,
                         help='Positive minimum duration; rounded up to the exact frame grid')
    resume = operations.add_parser('resume', help='Resume a saved interrupted job, retaining successful chunks')
    resume.add_argument('job_id')
    args = parser.parse_args(arguments)
    binding = service = None
    try:
        if args.operation == 'inspect-silence':
            result = _inspect_silence_command(args)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        from ..application import LibraryApp
        app = LibraryApp(data_directory(args.data_dir))
        if args.operation in ('render', 'resume', 'capabilities', 'publish-epub', 'publish-daisy'):
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
        elif args.operation in ('publish-epub', 'publish-daisy', 'publications', 'verify-publication'):
            from ..audio.models import AudioBudget
            from .publishing import PublicationService
            budget = (AudioBudget(timeout=args.timeout, max_output_bytes=args.max_bytes)
                      if args.operation != 'publications' else None)
            source = service
            job_id = getattr(args, 'job_id', None)
            if job_id is not None and ':' in job_id:
                parts = job_id.split(':')
                if len(parts) == 2 and parts[0] == 'scene':
                    job_id = parts[1]
                elif len(parts) == 3 and parts[0] == 'director':
                    from .publishing_source import DirectorPublicationSource
                    source = DirectorPublicationSource(app.root, parts[1])
                    job_id = parts[2]
                else:
                    raise ValueError('Choose an existing Scene or primary Audio Studio job.')
            publisher = PublicationService(source, budget=budget, private_root=app.root)
            if args.operation == 'publications':
                result = publisher.publications()
            elif args.operation == 'verify-publication':
                result = publisher.verify(args.publication_id, output=args.file)
            elif args.operation == 'publish-daisy':
                result = publisher.export_daisy(job_id, args.output, language=args.language,
                    creator=args.creator, publisher=args.publisher,
                    progress=lambda message: print(message, file=sys.stderr, flush=True))
            else:
                result = publisher.export_epub(job_id, args.output, language=args.language, creator=args.creator,
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
        if args.operation == 'inspect-silence':
            print('Silence inspection interrupted. The saved job and audio are preserved; rerun the inspection when ready.', file=sys.stderr)
        else:
            print('Production interrupted. Resume its saved job ID to reuse completed chunks.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Audio Studio: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        try:
            if service is not None:
                service.close()
        finally:
            if binding is not None:
                binding.close()



def _inspect_silence_command(args):
    """Keep the selected runtime and process alive through actual native cleanup."""
    from contextlib import ExitStack
    from ..application import LibraryApp
    from ..audio.cleanup import _pending_owners, retained_context
    from ..audio.local_runtime import open_local_audio_runtime
    from ..audio.silence import SilenceCriteria

    criteria = SilenceCriteria(args.threshold_dbfs, args.minimum_seconds)
    app = LibraryApp(data_directory(args.data_dir))
    try:
        with retained_context(ExitStack()) as bindings:
            def bind_runtime(*, cancelled):
                bindings.enter_context(open_local_audio_runtime(app.root, strict=True,
                                                               cancelled=cancelled))

            service = ProductionService(app.root / 'state' / 'studio',
                                        input_roots=args.input_root)
            try:
                return service.inspect_silence(args.job_id,
                    threshold_dbfs=criteria.threshold_dbfs,
                    minimum_seconds=criteria.minimum_seconds,
                    runtime_binding=bind_runtime,
                    progress=lambda message: print(message, file=sys.stderr, flush=True))
            finally:
                service.close()
    except BaseException as error:
        # A CLI must not end its interpreter while retained cleanup daemons
        # still own the decoder, source handle, staging, lock or broker lease.
        # Retry only the existing causal owners; no new owner or worker.
        owners = _pending_owners(error)
        interrupted = None
        while owners:
            try:
                waiting = tuple(owner for owner in owners
                                if not owner.quiescent_event.is_set())
                if not waiting:
                    break
                for owner in waiting:
                    owner.close()
                waiting[0].quiescent_event.wait(.25)
            except (KeyboardInterrupt, SystemExit) as signal:
                if interrupted is None:
                    interrupted = signal
        if interrupted is not None:
            raise interrupted from error
        raise


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
    from contextlib import ExitStack, contextmanager
    from ..audio.cleanup import _pending_owners, pending_cleanup
    bindings = ExitStack()
    production = None

    @contextmanager
    def lifetime():
        # Release the producer before its selected runtime. A producer close
        # can itself defer native shutdown, including after work already failed.
        # One existing retry owner retains this ordered release; independent
        # ExitStack callbacks would continue to unbind after a pending close.
        waiting = {}

        def release():
            if any(not owner.quiescent for owner in waiting.values()):
                return False
            try:
                if production is not None:
                    production.close()
            except BaseException as error:
                owners = _pending_owners(error)
                if not owners:
                    raise
                waiting.update((id(owner), owner) for owner in owners)
                return False
            bindings.close()
            return True

        try:
            yield
        finally:
            error = sys.exc_info()[1]
            waiting.update((id(owner), owner) for owner in _pending_owners(error))
            if not release():
                raise pending_cleanup(release, message=(
                    'Assistant audio shutdown is pending; the selected runtime '
                    'remains owned until its producer has closed.'))

    with lifetime():
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
            def execute(identity, **options):
                # Inspect the existing typed checkpoint before granting execution
                # access. Passive proposals/reviews and verified completed results
                # do not inspect or activate a selected media installation.
                record, selected = service._load(identity)
                media = {'audio.produce', 'audio.render', 'audio.transcribe',
                         'audio.inspect', 'audio.effects', 'audio.convert',
                         'book.run', 'book.batch.run'}
                if any(step.tool in media and checkpoint['status'] != 'complete'
                       for step, checkpoint in zip(selected.steps, record['steps'])):
                    from ..audio.local_runtime import open_local_audio_runtime
                    bindings.enter_context(open_local_audio_runtime(app.root, strict=True,
                        cancelled=options.get('cancelled', lambda: False)))
                return service.resume(identity, **options)
            if args.list_local_profiles:
                result = [{'id': profile.id, 'name': profile.name, 'endpoint': profile.endpoint,
                           'model': profile.model} for profile in service.local_planner_profiles()]
            elif args.resume:
                if not args.run:
                    result = asdict(service.get(args.resume))
                else:
                    result = asdict(execute(args.resume))
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
                    result = asdict(execute(identity,
                        progress=lambda complete, total, text: print(f'{complete}/{total}: {text}', file=sys.stderr, flush=True)))
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        except KeyboardInterrupt as exc:
            if _pending_owners(exc):
                raise
            print('Assistant interrupted. Resume the saved job to continue.', file=sys.stderr)
            return 130
        except (ValueError, OSError, RuntimeError) as exc:
            if _pending_owners(exc):
                raise
            print('Local assistant: ' + str(exc), file=sys.stderr)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
