"""Headless access to the same offline studio used by the native desktop."""
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sys


def add_audio_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--audio-voices', action='store_true', help='List available local speech voices and compatibility notices')
    group.add_argument('--audio-script', type=Path, help='Read a local UTF-8 production script')
    group.add_argument('--audio-plan', type=Path, help='Read a validated versioned production-plan JSON file')
    parser.add_argument('--audio-preview', action='store_true', help='Print the validated production plan without generating audio')
    parser.add_argument('--audio-output', type=Path, help='Local project output/cache folder; interrupted renders can resume here')
    parser.add_argument('--voice', help='Default local voice ID from --audio-voices; role directives may override it')
    parser.add_argument('--ffmpeg', type=Path, help='Explicit installed FFmpeg executable; no download is performed')
    parser.add_argument('--dordiai-manifest', type=Path, help='Reviewed offline dordiai-voices runtime manifest')
    parser.add_argument('--piper-executable', type=Path, help='User-installed local Piper executable')
    parser.add_argument('--piper-manifest', type=Path, help='Reviewed offline Piper voice model manifest')


def audio_requested(args):
    return bool(args.audio_voices or args.audio_script or args.audio_plan)


def validate_audio_arguments(args, parser):
    requested = audio_requested(args)
    if requested and (args.self_test or args.backup or args.restore or args.list_libraries or args.search is not None):
        parser.error('Choose one library or audio operation per command.')
    if args.audio_preview and not (args.audio_script or args.audio_plan):
        parser.error('--audio-preview requires --audio-script or --audio-plan.')
    if (args.audio_script or args.audio_plan) and not args.audio_preview and not args.audio_output:
        parser.error('Rendering requires --audio-output; use --audio-preview to inspect a plan first.')
    if not requested and any((args.audio_output, args.voice, args.ffmpeg, args.dordiai_manifest,
                              args.piper_executable, args.piper_manifest)):
        parser.error('Audio options require --audio-voices, --audio-script or --audio-plan.')
    if bool(args.piper_executable) != bool(args.piper_manifest):
        parser.error('Supply both --piper-executable and --piper-manifest.')


def run_audio(args):
    from .audio.engines import default_registry
    from .audio.models import ProductionPlan
    from .audio.production import parse_script, render_project
    from .reader.narration_text import decode_json, read_document
    registry = default_registry(dordiai_manifest=args.dordiai_manifest,
        piper_executable=args.piper_executable, piper_manifest=args.piper_manifest)
    if args.audio_voices:
        print(json.dumps({'voices': [asdict(v) for v in registry.voices()],
            'capabilities': registry.capabilities()}, ensure_ascii=False, indent=2))
        return 0
    if args.audio_plan:
        plan = ProductionPlan.from_dict(decode_json(read_document(args.audio_plan)))
    else:
        voice = args.voice
        if not voice and not args.audio_preview:
            available = registry.voices()
            if not available:
                raise RuntimeError('No local speech voice is available. Run --audio-voices for compatibility details.')
            voice = available[0].id
        plan = parse_script(read_document(args.audio_script).decode('utf-8-sig'), title=args.audio_script.stem,
                            voice=voice or 'sapi5:default', language=args.language or 'en')
    if args.audio_preview:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0
    ffmpeg = args.ffmpeg or shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('Select an installed local FFmpeg executable with --ffmpeg.')
    result = render_project(plan, args.audio_output, engine=registry, ffmpeg=Path(ffmpeg),
                            on_status=lambda text: print(text, file=sys.stderr, flush=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
