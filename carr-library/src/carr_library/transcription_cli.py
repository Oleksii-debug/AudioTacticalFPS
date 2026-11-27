"""Headless entrypoint for the same library transcription service as the UI."""
import argparse
import json
from pathlib import Path
import sys

from .studio.cli import data_directory


def main(argv=None):
    parser = argparse.ArgumentParser(description='Supplied local Whisper transcription; no downloads')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--workspace', required=True, help='Active library ID from --list-libraries')
    commands = parser.add_subparsers(dest='operation', required=True)
    configure = commands.add_parser('configure', help='Bind an explicitly selected local worker/runtime/model cache')
    configure.add_argument('--python', type=Path, required=True)
    configure.add_argument('--worker', type=Path, required=True)
    configure.add_argument('--models-dir', type=Path, required=True)
    commands.add_parser('jobs', help='List saved library transcription jobs')
    create = commands.add_parser('create', help='Retain original media and save a transcription job')
    create.add_argument('files', type=Path, nargs='+')
    create.add_argument('--title', default='Transcription')
    create.add_argument('--model', default='small')
    create.add_argument('--model-revision')
    create.add_argument('--language', default='auto')
    create.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    create.add_argument('--compute-type', default='int8')
    create.add_argument('--multilingual', action='store_true')
    create.add_argument('--no-word-timestamps', action='store_true')
    run = commands.add_parser('run', help='Run or resume a job with the configured local worker')
    run.add_argument('job')
    run.add_argument('--timeout', type=float, default=86400)
    show = commands.add_parser('show', help='Inspect the saved job and original source names')
    show.add_argument('job')
    verify = commands.add_parser('verify', help='Recheck original hashes and completed worker receipts')
    verify.add_argument('job')
    edit = commands.add_parser('edit', help='Save a separate transcript revision from a UTF-8 text file')
    edit.add_argument('job')
    edit.add_argument('--source', required=True, help='Retained source filename from show')
    edit.add_argument('--text-file', type=Path, required=True)
    export = commands.add_parser('export', help='Export verified formats and separately labelled edits')
    export.add_argument('job')
    export.add_argument('destination', type=Path, help='New directory outside library data')
    args = parser.parse_args(argv)
    try:
        from .application import LibraryApp
        from .durable_state import local_path, regular
        from .transcription import MAX_TEXT_BYTES
        app = LibraryApp(data_directory(args.data_dir))
        service = app.transcription(args.workspace)
        if args.operation == 'configure':
            result = service.configure(args.python, args.worker, args.models_dir)
        elif args.operation == 'jobs':
            result = [{'id': job['id'], 'title': job['title'], 'status': job['status'],
                       'done': job['done'], 'total': len(job['sources'])} for job in service.list()]
        elif args.operation == 'create':
            result = {'id': service.submit(args.files, title=args.title, options={
                'model': args.model, 'model_revision': args.model_revision,
                'language': args.language, 'device': args.device, 'compute_type': args.compute_type,
                'multilingual': args.multilingual, 'word_timestamps': not args.no_word_timestamps})}
        elif args.operation == 'run':
            def report_progress(done, total, state):
                count = (f'{done}/{total}' if done is not None and total is not None
                         else 'Progress indeterminate')
                print(f'{count}: {state}', file=sys.stderr, flush=True)
            result = service.run(args.job, timeout=args.timeout,
                                 progress=report_progress)
        elif args.operation == 'show':
            result = service.get(args.job)
        elif args.operation == 'verify':
            result = service.verify(args.job)
        elif args.operation == 'edit':
            path = local_path(args.text_file, exists=True)
            if regular(path).st_size > MAX_TEXT_BYTES:
                raise ValueError('Edited text exceeds its size allowance.')
            result = {'revision': service.save_edit(args.job, args.source, path.read_text(encoding='utf-8-sig'))}
        else:
            result = {'exported_to': str(service.export(args.job, args.destination))}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.operation == 'run' and result['status'] != 'completed':
            return 2 if result['status'] == 'stopped' else 1
        return 0
    except KeyboardInterrupt:
        print('Transcription stopped. Resume the saved job to reuse completed files.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Transcription: ' + str(exc), file=sys.stderr)
        return 1
