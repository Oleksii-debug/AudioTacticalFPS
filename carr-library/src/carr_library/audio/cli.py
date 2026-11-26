"""Explicit headless local narration commands; no desktop or library creation."""
from contextlib import contextmanager
from dataclasses import asdict
import json
import math
from pathlib import Path
import signal
import sys
from threading import Event, current_thread, main_thread


def add_arguments(parser):
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument('--list-voices', action='store_true',
                          help='List enabled installed SAPI5 voices as JSON; Windows only')
    commands.add_argument('--narrate-text', type=Path, metavar='UTF8_FILE',
                          help='Render an unchanged UTF-8 text file with one installed voice')
    commands.add_argument('--narration-script', type=Path, metavar='UTF8_FILE',
                          help='Render explicit Role: text lines, chapter headings and pauses')
    commands.add_argument('--render-project', type=Path, metavar='DIRECTORY',
                          help='Reopen and resume a saved local narration project')
    parser.add_argument('--audio-project', type=Path,
                        help='Project directory for --narrate-text or --narration-script')
    parser.add_argument('--audio-title', default='Narration', help='New narration project title')
    parser.add_argument('--voice', help='Exact installed voice ID for --narrate-text')
    parser.add_argument('--role-voices', type=Path, metavar='JSON_FILE',
                        help='Explicit role to installed voice ID/settings map for --narration-script')
    parser.add_argument('--speech-rate', type=int, default=0, help='Native SAPI rate, -10 to 10')
    parser.add_argument('--speech-volume', type=int, default=100, help='Native SAPI volume, 0 to 100')
    parser.add_argument('--ffmpeg', type=Path, help='Absolute path of a trusted local FFmpeg executable')
    parser.add_argument('--plan-only', action='store_true', help='Save the validated narration plan without speech generation')
    parser.add_argument('--max-audio-gib', type=float, default=64,
                        help='Output disk budget in GiB, including retained mixes')
    parser.add_argument('--max-input-gib', type=float, default=64,
                        help='Prepared speech and cue disk budget in GiB')
    parser.add_argument('--audio-time-hours', type=float, default=24,
                        help='Wall-clock budget for this render attempt; completed chunks are resumable')


def selected(args):
    return (args.list_voices or args.narrate_text is not None
            or args.narration_script is not None or args.render_project is not None)


def validate(args, parser):
    if not selected(args):
        if any((args.audio_project, args.voice, args.role_voices, args.ffmpeg, args.plan_only)):
            parser.error('Audio options require --list-voices, --narrate-text, --narration-script or --render-project.')
        return
    if args.list_voices:
        if any((args.audio_project, args.voice, args.role_voices, args.ffmpeg, args.plan_only)):
            parser.error('--list-voices does not take project or rendering options.')
        return
    if args.render_project:
        if any((args.audio_project, args.voice, args.role_voices, args.plan_only)):
            parser.error('--render-project uses the saved plan; do not supply replacement text or voice options.')
    else:
        if args.audio_project is None:
            parser.error('New narration requires --audio-project.')
        if args.narrate_text and (not args.voice or args.role_voices):
            parser.error('--narrate-text requires --voice and does not take --role-voices.')
        if args.narration_script and (not args.role_voices or args.voice):
            parser.error('--narration-script requires --role-voices and does not take --voice.')
    if not args.plan_only and (args.ffmpeg is None or not args.ffmpeg.is_absolute()):
        parser.error('Rendering requires --ffmpeg with the absolute path of a trusted local executable.')
    if not -10 <= args.speech_rate <= 10 or not 0 <= args.speech_volume <= 100:
        parser.error('--speech-rate must be -10 to 10; --speech-volume must be 0 to 100.')
    for name in ('max_audio_gib', 'max_input_gib', 'audio_time_hours'):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f'--{name.replace("_", "-")} must be a positive finite budget.')


def _read_text(path, maximum):
    # Use the same strict local-file guards as durable projects. Read bytes once
    # and preserve CRLF/BOM/Unicode exactly when decoding, without newline folding.
    from .director import _directory, _open_file
    path = Path(path).absolute()
    _directory(path.parent)
    with _open_file(path) as (source, info):
        if info.st_size > maximum:
            raise ValueError('The selected text file exceeds the configured project text budget.')
        raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError('The selected text file exceeds the configured project text budget.')
    return raw.decode('utf-8')


def _roles(path, args):
    from .director import VoiceRole

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Role voice JSON contains a duplicate field.')
            result[key] = value
        return result

    rows = json.loads(_read_text(path, 1024 * 1024), object_pairs_hook=unique)
    if type(rows) is not dict or not 1 <= len(rows) <= 64:
        raise ValueError('Role voice JSON must map between one and 64 role names to voice settings.')
    result = {}
    allowed = {'voice_id', 'rate', 'volume', 'pan', 'gain', 'cache_revision'}
    for name, value in rows.items():
        settings = {'voice_id': value} if type(value) is str else value
        if type(settings) is not dict or not set(settings) <= allowed or 'voice_id' not in settings:
            raise ValueError('Each role needs an installed voice ID and supported numeric settings only.')
        effective = dict(rate=args.speech_rate, volume=args.speech_volume)
        effective.update(settings)
        result[name] = VoiceRole(name=name, **effective)
    return result


@contextmanager
def _interrupts():
    stopped = Event()
    previous = None
    if current_thread() is main_thread():
        previous = signal.getsignal(signal.SIGINT)

        def stop(_number, _frame):
            stopped.set()

        signal.signal(signal.SIGINT, stop)
    try:
        yield stopped.is_set
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def run(args):
    from . import director, voices
    from .studio import RenderLimits
    with _interrupts() as cancelled:
        if args.list_voices:
            print(json.dumps([asdict(voice) for voice in voices.list_voices(cancelled=cancelled)],
                             ensure_ascii=False, indent=2))
            return 0
        if args.render_project:
            project = args.render_project
            plan = director.load_plan(project)
        else:
            project = args.audio_project
            text = _read_text(args.narrate_text or args.narration_script, director.MAX_SOURCE_BYTES)
            if args.narrate_text:
                plan = director.plan_narration(text, args.voice, title=args.audio_title,
                                              rate=args.speech_rate, volume=args.speech_volume)
            else:
                plan = director.parse_script(text, _roles(args.role_voices, args), title=args.audio_title)
        if args.plan_only:
            path = director.save_plan(plan, project)
            print(json.dumps({'status':'saved', 'project':str(path), 'mode':plan.mode,
                              'source_sha256':plan.source_sha256,
                              'scenes':len(plan.scenes), 'speech_generated':False}, ensure_ascii=False))
            return 0
        limits = RenderLimits(max_output_bytes=int(args.max_audio_gib * 1024**3),
                              max_input_bytes=int(args.max_input_gib * 1024**3),
                              max_wall_seconds=args.audio_time_hours * 3600)
        result = director.render_narration(plan, project, ffmpeg=args.ffmpeg, limits=limits,
            cancelled=cancelled, progress=lambda event: print(json.dumps(event, ensure_ascii=False),
                                                              file=sys.stderr, flush=True))
        print(json.dumps({'status':'complete', **asdict(result)}, ensure_ascii=False, default=str, indent=2))
        return 0
