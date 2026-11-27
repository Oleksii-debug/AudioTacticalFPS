"""Text-accessible commands using the canonical supplied-worker ASR service."""
import argparse
import json
from pathlib import Path
import sys

from .audio.transcription import (FORMATS, SuppliedWhisperRuntime,
                                  TranscriptionService, TranscriptionSettings)
from .studio.cli import data_directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=(
        'Local transcription through the reviewed supplied Whisper worker. '
        'No model or runtime is downloaded.'))
    parser.add_argument('--data-dir', type=Path)
    actions = parser.add_subparsers(dest='action', required=True)
    draft = actions.add_parser('draft-runtime', help='Inspect selected local files into an unapproved runtime manifest')
    for name in ('output', 'python', 'worker', 'runtime-directory', 'models-directory'):
        draft.add_argument('--' + name, type=Path, required=True)
    draft.add_argument('--model', required=True)
    execute = actions.add_parser('run', help='Transcribe selected local files or resume a saved job')
    execute.add_argument('--runtime-manifest', type=Path, required=True)
    selected = execute.add_mutually_exclusive_group(required=True)
    selected.add_argument('--input', action='append', type=Path, help='Selected local audio/video file; repeat for a batch')
    selected.add_argument('--resume', help='Saved transcription job identity')
    execute.add_argument('--language', default='auto')
    execute.add_argument('--task', default='transcribe', choices=('transcribe', 'translate'),
                         help='Whisper translation targets English only')
    execute.add_argument('--device', default='cpu', choices=('cpu', 'cuda'))
    execute.add_argument('--compute-type', default='int8', choices=('int8','int8_float16','int8_float32','float16','float32'))
    execute.add_argument('--beam-size', type=int, default=5)
    execute.add_argument('--format', action='append', choices=FORMATS)
    execute.add_argument('--no-vad', action='store_true')
    execute.add_argument('--multilingual', action='store_true', help='Require actual multilingual support; never silently downgrade')
    execute.add_argument('--queue-only', action='store_true', help='Retain inputs and create a durable job without starting inference')
    actions.add_parser('jobs', help='List saved job identities and states without requiring an engine')
    for name in ('status', 'pause', 'cancel'):
        command = actions.add_parser(name, help=name.capitalize() + ' a saved transcription job')
        command.add_argument('job_id')
    args = parser.parse_args(argv)
    try:
        if args.action == 'draft-runtime':
            from .audio.supplied_runtime_manifest import write_draft_manifest
            result = write_draft_manifest(args.output, python=args.python, worker=args.worker,
                runtime_directory=args.runtime_directory, models_directory=args.models_directory,
                model=args.model)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        from .application import LibraryApp
        app = LibraryApp(data_directory(args.data_dir))
        runtime = SuppliedWhisperRuntime(args.runtime_manifest) if args.action == 'run' else None
        service = TranscriptionService(app.root/'state', runtime)
        if args.action == 'jobs':
            import re
            result = [{'id':p.name, 'status':service.load(p.name)['status']}
                      for p in sorted(service.root.iterdir())
                      if p.is_dir() and re.fullmatch('[0-9a-f]{64}', p.name)]
        elif args.action == 'status':
            result = service.load(args.job_id)
        elif args.action in ('pause', 'cancel'):
            service.control(args.job_id, args.action)
            result = {'id':args.job_id, 'control_requested':args.action,
                      'status':service.load(args.job_id)['status']}
        else:
            if args.resume:
                job_id = args.resume
                service.control(job_id, 'run')
            else:
                settings = TranscriptionSettings(language=args.language, task=args.task,
                    beam_size=args.beam_size, vad_filter=not args.no_vad,
                    multilingual=args.multilingual, device=args.device, compute_type=args.compute_type)
                job_id = service.create(args.input, settings=settings, formats=args.format or FORMATS)
            print('Transcription job: ' + job_id, file=sys.stderr, flush=True)
            result = service.load(job_id) if args.queue_only else service.run(job_id,
                on_status=lambda text: print(text, file=sys.stderr, flush=True))
            result = {**result, 'output_directory':str(service.root/job_id/'outputs')}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        if args.action == 'draft-runtime':
            print('Manifest inspection interrupted. Any completed draft remains unapproved; inspect the selected output path.',
                  file=sys.stderr)
        else:
            print('Transcription interrupted. Resume its saved job to keep verified completed files.', file=sys.stderr)
        return 130
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        print('Transcription: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
