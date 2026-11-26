"""Headless controls for the same persistent studio used by the native desktop."""
from dataclasses import asdict
import json
from pathlib import Path


def add_arguments(parser):
    commands = parser.add_argument_group('Local Audio Studio')
    commands.add_argument('--voices', action='store_true', help='List actually installed local voices and capabilities')
    commands.add_argument('--studio-list', action='store_true', help='List saved audio projects as JSON')
    commands.add_argument('--studio-script', type=Path, help='Create a saved production from a UTF-8 authored script')
    commands.add_argument('--studio-import', type=Path, help='Import a versioned project JSON as a new project')
    commands.add_argument('--studio-project', help='Saved project identity for rendering or job inspection')
    commands.add_argument('--studio-jobs', action='store_true', help='List persistent jobs for --studio-project')
    commands.add_argument('--studio-resume', help='Resume the exact saved local job identity')
    commands.add_argument('--studio-status', help='Inspect a saved local job without running it')
    commands.add_argument('--audio-output', type=Path, help='Render into a new output directory; never overwrite')
    commands.add_argument('--audio-title', default='Audio project', help='Name for a new production')
    commands.add_argument('--audio-language', default='en', help='Explicit narration language code, e.g. en, uk, sk')
    commands.add_argument('--voice-id', action='append', help='Installed voice ID; repeat to assign successive roles')
    commands.add_argument('--plain-narration', action='store_true', help='Speak imported text as data; do not interpret script directives')
    commands.add_argument('--audio-format', default='wav', choices=('wav','flac','mp3','ogg','opus','m4a','m4b','aiff'))
    commands.add_argument('--audio-no-stems', action='store_true', help='Skip independent per-role stem exports')


def requested(args):
    return any((args.voices, args.studio_list, args.studio_script, args.studio_import,
                args.studio_project, args.studio_jobs, args.studio_resume, args.studio_status, args.audio_output))


def execute(args, root, *, printer=print, progress=None):
    from .voices import VoiceRegistry
    from .director import ProductionDirector
    from .projects import MAX_PROJECT_BYTES, ProjectStore, Settings, parse_script, plain
    registry = VoiceRegistry()
    if args.voices:
        if any((args.studio_script,args.studio_import,args.studio_project,args.audio_output,args.studio_resume)):
            raise ValueError('List voices separately from a production command.')
        result = {'mode':'LOCAL_ONLY','voices':[asdict(voice) for voice in registry.list_voices()]}
    else:
        store = ProjectStore(root)
        director = ProductionDirector(root, registry=registry)
        if args.studio_list:
            result = {'projects':store.list_projects()}
        else:
            if args.studio_script and args.studio_import:
                raise ValueError('Choose a script or project JSON, not both.')
            if (args.studio_script or args.studio_import) and args.studio_project:
                raise ValueError('Create a project separately from an existing project identity.')
            project_id = args.studio_project
            if args.studio_script:
                info = plain(args.studio_script)
                if info.st_size > MAX_PROJECT_BYTES:
                    raise ValueError('Script exceeds the configured project byte budget.')
                script = args.studio_script.read_text(encoding='utf-8')
                voices = registry.list_voices()
                available = {voice.id for voice in voices}
                selected = args.voice_id or tuple(voice.id for voice in voices)
                if not selected or any(voice not in available for voice in selected):
                    raise ValueError('Select an installed local voice; use --voices to list identities.')
                project = parse_script(script, selected, title=args.audio_title,
                    language=args.audio_language, directives=not args.plain_narration,
                    settings=Settings(format=args.audio_format, stems=not args.audio_no_stems))
                store.create(project)
                project_id = project.id
            elif args.studio_import:
                project_id = store.import_project(args.studio_import).id
            if not project_id:
                raise ValueError('Choose --studio-project, --studio-script or --studio-import.')
            if args.studio_status:
                result = director.status(project_id, args.studio_status)
            elif args.studio_jobs:
                result = {'project_id':project_id,'jobs':director.jobs(project_id)}
            elif args.audio_output or args.studio_resume:
                result = director.render(project_id, args.audio_output, job_id=args.studio_resume, progress=progress)
            else:
                result = store.load(project_id).to_dict()
    printer(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0
