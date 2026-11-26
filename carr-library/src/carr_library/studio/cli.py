"""Keyboard and automation entrypoints for the same local desktop studio."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .models import load_project, narration_project
from .store import ProjectStore


def main(argv=None):
    parser = argparse.ArgumentParser(prog='research-library studio', description='Local speech, durable audio projects and resumable rendering')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('voices', help='List actually installed local voices as JSON')
    commands.add_parser('engines', help='Inspect optional local music runtime availability')
    new = commands.add_parser('new', help='Save a faithful narration project from a UTF-8 text file')
    new.add_argument('--text-file', required=True, type=Path)
    new.add_argument('--voice', required=True)
    new.add_argument('--title', default='Narration')
    new.add_argument('--language')
    new.add_argument('--project-dir', required=True, type=Path)
    render = commands.add_parser('render', help='Render or resume the saved project; optional --plan loads a new revision')
    render.add_argument('--project-dir', required=True, type=Path)
    render.add_argument('--plan', type=Path)
    render.add_argument('--asset-root', type=Path)
    render.add_argument('--allow-audio-root', action='append', type=Path, default=[])
    status = commands.add_parser('status', help='Read durable render job states')
    status.add_argument('--project-dir', required=True, type=Path)
    show = commands.add_parser('plan', help='Print the current saved project and exact revision')
    show.add_argument('--project-dir', required=True, type=Path)
    backup = commands.add_parser('backup', help='Create a verified, consistent project snapshot in a new folder')
    backup.add_argument('--project-dir', required=True, type=Path)
    backup.add_argument('--output', required=True, type=Path)
    backup.add_argument('--without-audio', action='store_true', help='Preserve scripts and history; regenerate audio after restoration')
    restore = commands.add_parser('restore', help='Verify a backup and restore into a new project folder')
    restore.add_argument('--backup-dir', required=True, type=Path)
    restore.add_argument('--project-dir', required=True, type=Path)
    restore.add_argument('--manifest-sha256', required=True, help='Independent digest printed when the backup was created')
    convert = commands.add_parser('convert', help='Convert a selected local audio file into a new output')
    convert.add_argument('source', type=Path)
    convert.add_argument('output', type=Path)
    convert.add_argument('--sample-rate', type=int, default=24000)
    convert.add_argument('--channels', type=int, choices=(1, 2), default=2)
    args = parser.parse_args(argv)
    try:
        if args.command == 'voices':
            from ..speech import discover_voices
            result = [asdict(v) for v in discover_voices()]
        elif args.command == 'engines':
            from ..audio.music import available_music_engines
            result = available_music_engines()
        elif args.command == 'convert':
            from ..audio import FFmpegAudio
            engine = FFmpegAudio(allowed_roots=(args.source.parent, args.output.parent))
            result = asdict(engine.convert(args.source, args.output, sample_rate=args.sample_rate, channels=args.channels))
        elif args.command == 'restore':
            from .backup import restore_project
            store = restore_project(args.backup_dir, args.project_dir,
                                    expected_manifest_sha256=args.manifest_sha256)
            result = {'project_dir': str(store.root), 'revision': store.load().revision,
                      'verified_manifest_sha256': args.manifest_sha256}
        else:
            store = ProjectStore(args.project_dir)
            if args.command == 'new':
                if args.text_file.stat().st_size > 64 * 1024**2:
                    raise ValueError('Use chaptered JSON for source text over the configured 64 MiB editor budget.')
                project = narration_project(args.text_file.read_text(encoding='utf-8-sig'), args.voice,
                    title=args.title, language=args.language)
                result = {'revision': store.save(project), 'project_dir': str(store.root)}
            elif args.command == 'status':
                result = store.jobs()
            elif args.command == 'plan':
                project = store.load()
                result = {'revision': project.revision, 'project': project.to_dict()}
            elif args.command == 'backup':
                from .backup import backup_project
                digest = backup_project(store, args.output, include_audio=not args.without_audio)
                result = {'backup_dir': str(args.output.absolute()), 'manifest_sha256': digest,
                          'include_audio': not args.without_audio,
                          'restore_note': 'Keep this digest separately. Restore requires it before accepting any backup bytes.'}
            else:
                from .director import Director
                project = load_project(args.plan) if args.plan else store.load()
                result = Director(store).render(project,
                    asset_root=args.asset_root or (args.plan.parent if args.plan else store.root),
                    allowed_audio_roots=args.allow_audio_root,
                    progress=lambda value: print(value['message'], file=sys.stderr, flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except KeyboardInterrupt:
        print('Studio paused. Completed chunks are retained; rerun render to resume.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Audio Studio: {exc}', file=sys.stderr)
        return 1
