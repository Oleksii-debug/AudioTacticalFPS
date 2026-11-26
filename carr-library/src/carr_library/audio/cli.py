"""Headless controls for the same persistent studio used by the native desktop."""
from dataclasses import asdict
import json
from pathlib import Path

from .projects import MAX_PROJECT_BYTES, ProjectStore, parse_script, plain


def add_arguments(parser):
    commands = parser.add_argument_group('Local Audio Studio')
    commands.add_argument('--voices', action='store_true', help='List actually installed local voices and capabilities')
    commands.add_argument('--voice-config', type=Path, help='Validate and save an explicit local voice configuration JSON; no downloads')
    commands.add_argument('--voice-status', action='store_true', help='Show saved local voice registration status without running an engine')
    commands.add_argument('--voice-revalidate', action='store_true', help='Explicitly revalidate saved/restored local voice paths and current bytes')
    commands.add_argument('--voice-reset', action='store_true', help='Detach optional local voice registrations; keep all installed assets')
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
    commands.add_argument('--music-config', type=Path, help='Verify and save an explicit local MusicGen/FluidSynth settings JSON; no acquisition')
    commands.add_argument('--music-status', action='store_true', help='Show saved local music registrations without claiming tested inference')
    commands.add_argument('--music-sample', type=Path, help='Generate a real three-second sample into a new WAV file')
    commands.add_argument('--music-engine', choices=('procedural','fluidsynth','musicgen'), default='procedural')


def requested(args):
    return any((args.voices, args.studio_list, args.studio_script, args.studio_import,
                args.studio_project, args.studio_jobs, args.studio_resume, args.studio_status, args.audio_output,
                args.music_config, args.music_status, args.music_sample,
                args.voice_config, args.voice_status, args.voice_revalidate, args.voice_reset))


def execute(args, root, *, printer=print, progress=None):
    music_commands = (args.music_config, args.music_status, args.music_sample)
    voice_commands = (args.voice_config, args.voice_status, args.voice_revalidate, args.voice_reset)
    if any(voice_commands):
        if sum(bool(value) for value in voice_commands) != 1 or any((*music_commands, args.voices,
                args.studio_list, args.studio_script, args.studio_import, args.studio_project,
                args.studio_jobs, args.studio_resume, args.studio_status, args.audio_output)):
            raise ValueError('Choose one voice setup command separately from studio production.')
        from .voice_settings import VoiceSettingsStore, voice_configuration_status
        store = VoiceSettingsStore(root)
        if args.voice_config:
            store.import_config(args.voice_config)
        elif args.voice_revalidate:
            store.revalidate(expected_revision=store.read()['revision'])
        elif args.voice_reset:
            store.reset(expected_revision=store.read()['revision'])
        printer(json.dumps(voice_configuration_status(root), ensure_ascii=False, indent=2))
        return 0
    if any(music_commands):
        if sum(bool(value) for value in music_commands) != 1 or any((args.voices, args.studio_list,
                args.studio_script,args.studio_import,args.studio_project,args.studio_jobs,args.studio_resume,
                args.studio_status,args.audio_output)):
            raise ValueError('Choose one music setup/sample command separately from studio production.')
        from .music_settings import MusicSettingsStore, music_configuration_status, load_music_options
        if args.music_config:
            MusicSettingsStore(root).import_registration(args.music_config)
            result = music_configuration_status(root)
        elif args.music_status:
            result = music_configuration_status(root)
        else:
            from .music import generate_music, MusicSpec
            if args.music_sample.suffix.lower() != '.wav':
                raise ValueError('A local engine sample must use a new .wav filename.')
            options = {} if args.music_engine == 'procedural' else load_music_options(root)
            result = generate_music(MusicSpec(3,engine=args.music_engine),args.music_sample,**options).as_dict()
        printer(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    from .voices import VoiceRegistry
    from .director import ProductionDirector
    from .projects import Settings
    registry = VoiceRegistry.for_library(root)
    if args.voices:
        if any((args.studio_script,args.studio_import,args.studio_project,args.audio_output,args.studio_resume)):
            raise ValueError('List voices separately from a production command.')
        result = {'mode':'LOCAL_ONLY','voices':[asdict(voice) for voice in registry.list_voices()],
                  'engines':registry.engine_status()}
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
                selected = args.voice_id or voices
                if not selected or args.voice_id and any(voice not in available for voice in args.voice_id):
                    raise ValueError('Select an installed local voice; use --voices to list identities.')
                if args.voice_id:
                    for voice_id in args.voice_id:
                        registry.choose_voice(args.audio_language, preferred=voice_id)
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
