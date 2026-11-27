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


def _clip_controls(parser):
    """Expose existing typed Cue controls; omission preserves saved settings."""
    for name, flag in (
            ('start', '--start'), ('trim_start', '--trim-start'),
            ('gain_db', '--gain-db'), ('pan', '--pan'),
            ('fade_in', '--fade-in'), ('fade_out', '--fade-out')):
        parser.add_argument(flag, dest=name, type=float, default=argparse.SUPPRESS)
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument('--duration', type=float, default=argparse.SUPPRESS)
    duration.add_argument('--remaining', action='store_true',
                          help='Use the remaining retained original after its selected start')
    parser.add_argument('--role', choices=('sfx', 'ambience', 'music', 'microphone'),
                        default=argparse.SUPPRESS)
    parser.add_argument('--clip-title', dest='clip_title', default=argparse.SUPPRESS)


def _clip_settings(args):
    settings = {name: getattr(args, name) for name in
                ('start', 'trim_start', 'duration', 'gain_db', 'pan',
                 'fade_in', 'fade_out', 'role') if hasattr(args, name)}
    if getattr(args, 'remaining', False):
        settings['duration'] = None
    if hasattr(args, 'clip_title'):
        settings['title'] = args.clip_title
    return settings


from ..installation_lease import installed_entrypoint


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description='Local reading and audio production; no remote fallback')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--input-root', action='append', type=Path, default=[], help='Explicit local cue folder; repeat if needed')
    operations = parser.add_subparsers(dest='operation', required=True)
    batch = operations.add_parser('convert-batch', help='Review and explicitly approve an ordered audio conversion batch')
    from ..audio.conversion_batch_cli import add_arguments as batch_arguments
    batch_arguments(batch)
    operations.add_parser('voices', help='List actually installed local voices')
    operations.add_parser('jobs', help='List saved resumable production jobs')
    operations.add_parser('projects', help='List saved projects')
    create_clips = operations.add_parser('clip-create', help='Create a portable audio-clip project; no microphone or voice starts')
    create_clips.add_argument('--title', default='Audio clips')
    for name, help_text in (
            ('clip-add', 'Explicitly retain a selected local file or verified saved recording'),
            ('clip-edit', 'Edit clip timing, gain, pan, fades or track role without changing the original'),
            ('clip-remove', 'Remove a clip instruction; keep its retained original'),
            ('clip-move', 'Move a clip to another scene in this project'),
            ('clip-duration', 'Set the selected scene minimum duration'),
            ('clip-preview', 'Render a new local preview of the saved clip; do not start playback')):
        operation = operations.add_parser(name, help=help_text)
        operation.add_argument('project_id')
        operation.add_argument('scene_id')
        operation.add_argument('--revision', type=int,
                               help='Require this exact current project revision before acting')
        if name in ('clip-edit', 'clip-remove', 'clip-move', 'clip-preview'):
            operation.add_argument('clip_id')
        if name == 'clip-add':
            selected = operation.add_mutually_exclusive_group(required=True)
            selected.add_argument('--file', type=Path, help='Explicit original audio file to retain')
            selected.add_argument('--recording', help='Verified recording ID from this selected library')
        if name in ('clip-add', 'clip-edit'):
            _clip_controls(operation)
        if name == 'clip-move':
            operation.add_argument('target_scene_id')
        if name == 'clip-duration':
            operation.add_argument('seconds', type=float)
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
    operations.add_parser('capabilities', help='Inspect the actual installed audio runtime')
    render = operations.add_parser('render', help='Produce actual WAV master, chapters, stems and subtitles')
    source = render.add_mutually_exclusive_group(required=True)
    source.add_argument('--script', type=Path, help='UTF-8 script file')
    source.add_argument('--project', type=Path, help='Versioned JSON project with chosen voices')
    source.add_argument('--project-id', help='Saved project ID in this selected library')
    render.add_argument('--title', default='Audio production')
    render.add_argument('--voice', action='append', default=[], help='Installed voice ID; repeated voices rotate through roles')
    render.add_argument('--output', type=Path, required=True, help='Existing parent folder; a new project subfolder is created')
    render.add_argument('--format', default='wav', choices=('wav','flac','mp3','ogg','opus','m4a','m4b','aiff','wma'))
    render.add_argument('--no-stems', action='store_true')
    resume = operations.add_parser('resume', help='Resume a saved interrupted job, retaining successful chunks')
    resume.add_argument('job_id')
    resume.add_argument('--output', type=Path, help='Explicit new output parent required after a portable project is restored')
    args = parser.parse_args(argv)
    try:
        from ..application import LibraryApp
        app = LibraryApp(data_directory(args.data_dir))
        if args.operation == 'convert-batch':
            from ..audio.conversion_batch_cli import execute as execute_batch
            return execute_batch(args, app)
        service = ProductionService(app.root / 'state' / 'studio', input_roots=args.input_root)
        if args.operation == 'voices':
            result = [json_safe(asdict(voice)) for voice in service.voices.voices()]
        elif args.operation == 'projects':
            result = [project.to_dict() for project in service.projects.list()]
        elif args.operation == 'jobs':
            result = [{'id': job['id'], 'title': job['project']['title'], 'status': job['status'],
                       'progress': job['progress'], 'destination': job['destination'],
                       'requires_output_directory': bool(job.get('recovery', {}).get('requires_output_directory'))}
                      for job in service.jobs()]
        elif args.operation.startswith('clip-'):
            from .clips import ClipEditor
            editor = ClipEditor(app.root, service=service)
            if args.operation == 'clip-create':
                result = editor.create(args.title).to_dict()
            else:
                project = service.projects.load(args.project_id)
                if args.revision is not None and project.revision != args.revision:
                    raise ValueError('The project revision changed. Review the current project before editing clips.')
                if args.operation == 'clip-add':
                    settings = _clip_settings(args)
                    if args.recording is not None:
                        from ..audio.recording import RecordingService
                        record = RecordingService(app.root).get(args.recording)
                        settings.setdefault('role', 'microphone')
                        settings.setdefault('title', record['title'])
                        project = editor.add(project, args.scene_id, record['path'],
                                             expected_sha256=record['audio']['sha256'], **settings)
                    else:
                        project = editor.add(project, args.scene_id, args.file, **settings)
                elif args.operation == 'clip-edit':
                    project = editor.edit(project, args.scene_id, args.clip_id, **_clip_settings(args))
                elif args.operation == 'clip-remove':
                    project = editor.remove(project, args.scene_id, args.clip_id)
                elif args.operation == 'clip-move':
                    project = editor.move(project, args.scene_id, args.clip_id, args.target_scene_id)
                elif args.operation == 'clip-duration':
                    project = editor.scene_duration(project, args.scene_id, args.seconds)
                else:
                    output = editor.preview(project, args.scene_id, args.clip_id)
                    result = {'project_id': project.id, 'revision': project.revision,
                              'scene_id': args.scene_id, 'clip_id': args.clip_id,
                              'path': str(output), 'playback': 'not-started'}
                if args.operation != 'clip-preview':
                    result = project.to_dict()
        elif args.operation in ('publications', 'publish-epub', 'publish-daisy', 'verify-publication'):
            from ..audio.models import AudioBudget
            from .publishing import PublicationService
            if args.operation == 'publications':
                result = PublicationService(service, private_root=app.root).publications()
            else:
                publisher = PublicationService(service, budget=AudioBudget(
                    max_output_bytes=args.max_bytes, timeout=args.timeout), private_root=app.root)
                if args.operation == 'publish-epub':
                    result = publisher.export_epub(args.job_id, args.output, language=args.language,
                        creator=args.creator, progress=lambda text: print(text, file=sys.stderr, flush=True))
                elif args.operation == 'publish-daisy':
                    result = publisher.export_daisy(args.job_id, args.output, language=args.language,
                        creator=args.creator, publisher=args.publisher,
                        progress=lambda text: print(text, file=sys.stderr, flush=True))
                else:
                    result = publisher.verify(args.publication_id, output=args.file)
        elif args.operation == 'capabilities':
            runtime = service._runtime()
            result = {'audio': runtime.capabilities(), 'music': runtime.music_capabilities()}
        else:
            if args.operation == 'render':
                if args.project_id:
                    project = service.projects.load(args.project_id)
                elif args.project:
                    project = Project.from_dict(read_json(args.project))
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
            print('Production job: ' + identity, file=sys.stderr, flush=True)
            job = service.run(identity, progress=lambda text: print(text, file=sys.stderr, flush=True))
            result = {'id': job['id'], 'status': job['status'], 'destination': job['destination'],
                      'artifacts': job['artifacts'], 'result': job['result']}
        # JSON preserves Unicode values even when a Windows pipe uses an older code page.
        print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print('Production interrupted. Resume its saved job ID to reuse completed chunks.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Audio Studio: ' + str(exc), file=sys.stderr)
        return 1


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
