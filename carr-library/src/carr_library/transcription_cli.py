"""Headless access to the same retained-input transcription service as wx."""
import argparse
import json
from pathlib import Path
import sys

from .durable_state import local_path, regular
from .studio.cli import data_directory


def _runtime_arguments(parser):
    parser.add_argument('--python', type=Path, required=True, help='Existing isolated Python executable')
    parser.add_argument('--worker', type=Path, required=True, help='Reviewed local V4.3 worker file')
    parser.add_argument('--model', type=Path, required=True, help='Existing local model directory; never downloaded')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Resonance local transcription with retained sources and verified results')
    parser.add_argument('--data-dir', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect-runtime', help='Hash the selected local installation without executing it')
    _runtime_arguments(inspect)
    register = commands.add_parser('register-runtime', help='Register a reviewed existing installation')
    _runtime_arguments(register)
    register.add_argument('--name', required=True)
    register.add_argument('--trust-runtime', action='store_true', help='Explicitly trust the exact inspected local code and model')
    register.add_argument('--fingerprint', help='Exact fingerprint from inspect-runtime; required with --trust-runtime')
    commands.add_parser('runtimes', help='List registered local installations and their trust status')
    commands.add_parser('jobs', help='List saved transcription jobs')
    submit = commands.add_parser('submit', help='Retain audio inputs and create a resumable job')
    submit.add_argument('inputs', nargs='+', type=Path)
    submit.add_argument('--profile', required=True)
    submit.add_argument('--title', default='Transcription')
    submit.add_argument('--recursive', action='store_true')
    submit.add_argument('--language', default='auto')
    submit.add_argument('--task', choices=('transcribe', 'translate'), default='transcribe',
                        help='translate is speech translation into English only')
    submit.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    submit.add_argument('--compute-type', choices=('int8','int8_float16','int8_float32','float16','float32'), default='int8')
    submit.add_argument('--beam-size', type=int, default=1)
    submit.add_argument('--parallel-files', type=int, default=1)
    submit.add_argument('--multilingual', action='store_true')
    submit.add_argument('--no-vad', action='store_true')
    submit.add_argument('--format', action='append', choices=('txt','srt','vtt','json'), dest='formats')
    submit.add_argument('--run', action='store_true', help='Explicitly start the saved job after retaining its inputs')
    for name in ('status', 'run', 'resume', 'pause', 'cancel', 'continue'):
        command = commands.add_parser(name)
        command.add_argument('job_id')
    rebind = commands.add_parser('rebind-runtime', help='Choose a newly reviewed local installation after restore')
    rebind.add_argument('job_id')
    rebind.add_argument('--profile', required=True)
    interrupted = commands.add_parser('acknowledge-interruption', help='Confirm the previous external worker has stopped')
    interrupted.add_argument('job_id')
    interrupted.add_argument('--worker-stopped', action='store_true')
    read = commands.add_parser('read', help='Read a verified transcript or its separately saved edit')
    read.add_argument('job_id')
    read.add_argument('--source')
    read.add_argument('--edited', action='store_true')
    edit = commands.add_parser('save-edit', help='Save a separate UTF-8 transcript edit, preserving recognition and source')
    edit.add_argument('job_id')
    edit.add_argument('--source', required=True)
    edit.add_argument('--text-file', type=Path, required=True)
    export = commands.add_parser('export', help='Write new checked TXT/SRT/VTT/JSON files with provenance')
    export.add_argument('job_id')
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--format', action='append', choices=('txt','srt','vtt','json'), dest='formats')
    export.add_argument('--without-edits', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'register-runtime' and args.trust_runtime and not args.fingerprint:
        parser.error('--trust-runtime requires the exact --fingerprint printed by inspect-runtime.')
    def progress(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)
    job_id = None
    try:
        from .application import LibraryApp
        from .operations import ResourceBroker
        from .transcription import TranscriptionService
        app = LibraryApp(data_directory(args.data_dir))
        service = TranscriptionService(app.root / 'state' / 'transcription', broker=ResourceBroker.for_app(app.root))
        if args.command == 'inspect-runtime':
            result = service.inspect_runtime(args.python, args.worker, args.model, progress=progress)
        elif args.command == 'register-runtime':
            result = service.register_runtime(args.name, args.python, args.worker, args.model,
                owner_trusted=args.trust_runtime, expected_fingerprint=args.fingerprint, progress=progress)
        elif args.command == 'runtimes':
            result = service.list_runtimes()
        elif args.command == 'jobs':
            result = service.list_jobs()
        elif args.command == 'submit':
            formats = set(args.formats or ('txt','srt','vtt','json'))
            options = dict(language=args.language, task=args.task, device=args.device,
                compute_type=args.compute_type, beam_size=args.beam_size, parallel_files=args.parallel_files,
                multilingual=args.multilingual, vad_filter=not args.no_vad,
                **{'output_' + name: name in formats for name in ('txt','srt','vtt','json')})
            result = service.create_job(args.profile, args.inputs, settings=options,
                title=args.title, recursive=args.recursive, progress=progress)
            job_id = result['id']
            print('Saved transcription job: ' + job_id, file=sys.stderr, flush=True)
            if args.run:
                result = service.run(job_id, progress=progress)
        else:
            job_id = args.job_id
            if args.command == 'status':
                result = service.get_job(job_id)
            elif args.command in ('run','resume'):
                result = getattr(service, args.command)(job_id, progress=progress)
            elif args.command in ('pause','cancel','continue'):
                result = getattr(service, 'continue_job' if args.command == 'continue' else args.command)(job_id)
            elif args.command == 'rebind-runtime':
                result = service.rebind_runtime(job_id, args.profile)
            elif args.command == 'acknowledge-interruption':
                result = service.acknowledge_interruption(job_id, worker_stopped=args.worker_stopped)
            elif args.command == 'read':
                result = service.read_transcript(job_id, args.source, edited=args.edited)
            elif args.command == 'save-edit':
                path = local_path(args.text_file, exists=True)
                if regular(path).st_size > 16 * 1024**2:
                    raise ValueError('The transcript edit exceeds its 16 MiB text budget.')
                result = service.save_edit(job_id, args.source, path.read_text(encoding='utf-8-sig'))
            else:
                result = service.export(job_id, args.output,
                    formats=tuple(dict.fromkeys(args.formats)) if args.formats else None,
                    include_edits=not args.without_edits)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        executed = args.command in ('run', 'resume') or args.command == 'submit' and args.run
        if executed and result.get('status') != 'complete':
            return 1
        return 0
    except KeyboardInterrupt:
        print('Transcription interrupted. Retained inputs and accepted results remain available.' +
              (f' Saved job: {job_id}' if job_id else ''), file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Resonance transcription: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
