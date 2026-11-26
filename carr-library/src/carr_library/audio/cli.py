"""Accessible text interface to the same local studio used by the desktop."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

from .contracts import EXPORT_FORMATS, SAMPLE_RATES, MusicCue, parse_script
from .project import ProjectStore, contained_file, local_directory, read_json, restore_project


def _print(value, *, stream=None):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), file=stream or sys.stdout)


def _engines(args):
    from .ffmpeg import AudioBackend
    from .voices import discover_local_providers
    backend = AudioBackend(args.ffmpeg, getattr(args, "ffprobe", None),
                           sample_rate=getattr(args, "sample_rate", 24000))
    return backend, discover_local_providers(ffmpeg=backend.ffmpeg)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="research-library audio",
        description="Private, local audio projects. No remote calls or automatic model downloads.")
    commands = parser.add_subparsers(dest="command", required=True)

    def runtime(command, *, rate=False):
        command.add_argument("--ffmpeg", type=Path, help="Explicit trusted local FFmpeg executable")
        command.add_argument("--ffprobe", type=Path, help="Matching trusted local ffprobe executable")
        if rate:
            command.add_argument("--sample-rate", type=int, choices=SAMPLE_RATES, default=24000)

    voices = commands.add_parser("voices", help="List actual local voices and unavailable optional engines")
    runtime(voices)
    create = commands.add_parser("create", help="Create a new project from an independent author script")
    runtime(create, rate=True)
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument("--script", type=Path, help="UTF-8 script, with Speaker: text and # Chapter headings")
    source.add_argument("--text", help="Independent narration text")
    create.add_argument("--directory", type=Path, required=True, help="New project directory; existing folders are preserved")
    create.add_argument("--title", default="Audio project")
    create.add_argument("--voice", action="append", help="Actual provider:voice ID, repeated to assign successive roles")
    create.add_argument("--pan", action="append", default=[], metavar="SPEAKER=VALUE",
                        help="Speaker placement from -1 (left) to 1 (right)")
    create.add_argument("--music", choices=("none", "calm", "tense", "bright"), default="none")
    create.add_argument("--format", choices=EXPORT_FORMATS, default="wav")
    create.add_argument("--no-stems", action="store_true")
    render = commands.add_parser("render", help="Render or resume a saved local project")
    render.add_argument("--project", type=Path, required=True)
    render.add_argument("--progress", action="store_true", help="Write readable operation progress to stderr")
    runtime(render)
    for name in ("show", "status"):
        command = commands.add_parser(name, help="Show the saved plan" if name == "show" else "Show durable job status")
        command.add_argument("--project", type=Path, required=True)
    backup = commands.add_parser("backup", help="Create a verified portable project/checkpoint backup")
    backup.add_argument("--project", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--without-audio", action="store_true")
    restore = commands.add_parser("restore", help="Verify a backup and restore to a new folder")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--directory", type=Path, required=True)
    restore.add_argument("--manifest-sha256", required=True)
    verify = commands.add_parser("verify", help="Verify checksums, source mapping and actual audio frame dimensions")
    verify.add_argument("--production", type=Path, required=True)
    runtime(verify)
    convert = commands.add_parser("convert", help="Render an audio derivative without replacing the input")
    runtime(convert, rate=True)
    convert.add_argument("--input", type=Path, required=True)
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--format", choices=(*EXPORT_FORMATS, "opus", "aac", "m4b", "aiff", "wma"), required=True)
    convert.add_argument("--effects-json", type=Path, help="List of typed effect objects; arbitrary filter strings are rejected")
    capabilities = commands.add_parser("capabilities", help="Inspect the actual local codecs and supported numeric effects")
    runtime(capabilities)
    args = parser.parse_args(argv)
    try:
        if args.command == "voices":
            from .voices import discover_local_providers
            from .music import engines
            registry = discover_local_providers(ffmpeg=args.ffmpeg)
            _print({"local_only": True, "voices": [asdict(v) for v in registry.list_voices()],
                    "providers": [asdict(v) for v in registry.availability()], "music": engines()})
        elif args.command == "capabilities":
            backend, registry = _engines(args)
            from .music import engines
            _print({"local_only": True, "voices": [asdict(v) for v in registry.list_voices()],
                    "providers": [asdict(v) for v in registry.availability()],
                    "music": engines(), "audio": backend.capabilities(),
                    "effects": backend.effect_schema(), "runtime": backend.identity})
        elif args.command == "create":
            backend, registry = _engines(args)
            if args.script:
                script = contained_file(local_directory(args.script.absolute().parent), args.script.name)
                with script.open("rb") as stream:
                    raw = stream.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError("Script exceeds the configured parser resource budget.")
                text = raw.decode("utf-8-sig")
            else:
                text = args.text
            available = registry.list_voices()
            if args.voice:
                by_id = {f"{v.provider}:{v.id}": v for v in available}
                if any(identity not in by_id for identity in args.voice):
                    raise ValueError("A selected local voice is unavailable; use the voices command.")
                available = tuple(by_id[identity] for identity in args.voice)
            cue = None if args.music == "none" else MusicCue(mood=args.music)
            project = parse_script(text, available, title=args.title, music=cue,
                                   sample_rate=args.sample_rate, export_format=args.format, stems=not args.no_stems)
            pans = {}
            for item in args.pan:
                name, separator, value = item.rpartition("=")
                if not separator or not name:
                    raise ValueError("Use --pan SPEAKER=VALUE with a value from -1 to 1.")
                pans[name] = float(value)
            if set(pans) - {role.name for role in project.roles}:
                raise ValueError("A pan setting names a speaker absent from the script.")
            project = replace(project, roles=tuple(replace(role, pan=pans.get(role.name, role.pan)) for role in project.roles)).validate()
            store = ProjectStore.create(args.directory, project)
            _print({"status": "saved", "project": str(store.path), "project_sha256": project.sha256,
                    "speakers": [asdict(role) for role in project.roles], "chapters": len(project.scenes),
                    "next": "Render this project to produce and verify actual audio."})
        elif args.command == "render":
            from .director import render_project
            from .ffmpeg import AudioBackend
            from .voices import discover_local_providers
            store = ProjectStore(args.project)
            project = store.load().project
            backend = AudioBackend(args.ffmpeg, args.ffprobe, sample_rate=project.sample_rate)
            registry = discover_local_providers(ffmpeg=backend.ffmpeg)
            progress = (lambda row: print(row["message"], file=sys.stderr, flush=True)) if args.progress else None
            result = render_project(store, backend=backend, registry=registry, progress=progress)
            _print({"status": "complete", "manifest": str(result.manifest), "master": str(result.master),
                    "frames": result.frames, "sample_rate": result.sample_rate,
                    "duration_seconds": result.duration_seconds, "created_checkpoints": result.created,
                    "reused_checkpoints": result.reused})
        elif args.command == "show":
            _print(ProjectStore(args.project).load().project.to_dict())
        elif args.command == "status":
            store = ProjectStore(args.project)
            state = store.root / "render-state.json"
            _print(read_json(state) if state.exists() else {"status": "ready", "message": "No render has started."})
        elif args.command == "backup":
            checksum = ProjectStore(args.project).backup(args.output, include_audio=not args.without_audio)
            _print({"status": "complete", "backup": str(args.output), "manifest_sha256": checksum})
        elif args.command == "restore":
            store = restore_project(args.backup, args.directory, manifest_sha256=args.manifest_sha256)
            _print({"status": "complete", "project": str(store.path)})
        elif args.command == "verify":
            from .director import verify_production
            from .ffmpeg import AudioBackend
            _print(verify_production(args.production, backend=AudioBackend(args.ffmpeg, args.ffprobe)))
        elif args.command == "convert":
            from .ffmpeg import Effect
            backend, _ = _engines(args)
            effects = read_json(args.effects_json) if args.effects_json else []
            if not isinstance(effects, list) or any(not isinstance(item, dict) or set(item) - {"name", "parameters"} or "name" not in item for item in effects):
                raise ValueError("Effects must be a list of name/parameters objects.")
            result = backend.convert(args.input, args.output, format=args.format,
                                     effects=tuple(Effect(**item) for item in effects))
            _print({"status": "complete", "output": str(result.path), "sample_rate": result.sample_rate,
                    "frames": result.frames, "exact_frames": result.exact_frames,
                    "measurements": backend.measure(result.path)})
        return 0
    except KeyboardInterrupt:
        print("Audio interrupted. Completed checkpoints are retained; render the same project to resume.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
        print(f"Audio Studio: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
