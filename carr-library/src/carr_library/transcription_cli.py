"""Headless entrypoint for the same library transcription service as the UI."""
import argparse
import json
from pathlib import Path
import sys

from .transcription_host import data_directory
from .transcription import PROTOCOLS, RECEIPTS_PROTOCOL


def main(argv=None):
    parser = argparse.ArgumentParser(description='Supplied local Whisper transcription; no downloads')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--workspace', required=True, help='Active library ID from --list-libraries')
    commands = parser.add_subparsers(dest='operation', required=True)
    configure = commands.add_parser('configure', help='Bind an explicitly selected local worker/runtime/model cache')
    configure.add_argument('--python', type=Path, required=True)
    configure.add_argument('--worker', type=Path, required=True)
    configure.add_argument('--models-dir', type=Path, required=True)
    configure.add_argument('--protocol', choices=PROTOCOLS, default=RECEIPTS_PROTOCOL,
        help='Explicit worker contract; use whisper-v5-manifest-v1 for the supplied repaired V5 worker and its two helpers')
    commands.add_parser('jobs', help='List saved library transcription jobs')
    create = commands.add_parser('create', help='Retain original media and save a transcription job')
    create.add_argument('files', type=Path, nargs='+')
    folder = commands.add_parser('create-folder', help='Retain supported media from a local folder and its subfolders')
    folder.add_argument('folder', type=Path)
    for command in (create, folder):
        command.add_argument('--title', default='Transcription')
        command.add_argument('--model', default='small')
        command.add_argument('--model-revision')
        command.add_argument('--language', default='auto')
        command.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
        command.add_argument('--compute-type', default='int8')
        command.add_argument('--multilingual', action='store_true')
        command.add_argument('--no-word-timestamps', action='store_true')
        command.add_argument('--protocol', choices=PROTOCOLS,
            help='Persist this worker contract in the job; defaults to the configured profile')
        command.add_argument('--max-attempts', type=int,
            help='V5 requires 3 (its fixed, deduplicated same-language retry policy); receipts-v1 defaults to 1')
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
            result = service.configure(args.python, args.worker, args.models_dir, protocol=args.protocol)
        elif args.operation == 'jobs':
            result = [{'id': job['id'], 'title': job['title'], 'status': job['status'],
                       'protocol': job['protocol'], 'done': job['done'], 'total': len(job['sources'])} for job in service.list()]
        elif args.operation in ('create', 'create-folder'):
            options = {
                'model': args.model, 'model_revision': args.model_revision,
                'language': args.language, 'device': args.device, 'compute_type': args.compute_type,
                'multilingual': args.multilingual, 'word_timestamps': not args.no_word_timestamps}
            if args.max_attempts is not None:
                options['max_attempts'] = args.max_attempts
            result = (service.submit_folder(args.folder, title=args.title, options=options, protocol=args.protocol)
                      if args.operation == 'create-folder' else
                      {'id': service.submit(args.files, title=args.title, options=options, protocol=args.protocol)})
        elif args.operation == 'run':
            result = service.run(args.job, timeout=args.timeout,
                progress=lambda done, total, state: print(f'{done}/{total}: {state}', file=sys.stderr, flush=True))
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
