"""Headless entrypoint for the same workspace-bound transcription service as wx."""
import argparse
import json
from pathlib import Path
import sys

from .studio.cli import data_directory
from .transcription import PROTOCOLS, RECEIPTS_PROTOCOL
from .installation_lease import installed_entrypoint


def _runtime_arguments(command):
    command.add_argument('--python', type=Path, required=True)
    command.add_argument('--worker', type=Path, required=True)
    command.add_argument('--models-dir', '--model-directory', type=Path, required=True)
    command.add_argument('--protocol', choices=PROTOCOLS, default=RECEIPTS_PROTOCOL,
        help='Explicit reviewed worker contract; V5 requires worker.py and its two helpers')


def _recognition_arguments(command):
    command.add_argument('--title', default='Transcription')
    command.add_argument('--profile', help='Exact explicitly registered local runtime profile')
    command.add_argument('--model', default='small')
    command.add_argument('--model-revision')
    command.add_argument('--language', default='auto')
    command.add_argument('--task', choices=('transcribe',), default='transcribe',
        help='Source-language transcription only; speech translation is not supported by this bridge')
    command.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    command.add_argument('--compute-type', choices=(
        'int8', 'float32', 'float16', 'int8_float16', 'int8_float32', 'auto'), default='int8')
    command.add_argument('--beam-size', type=int, default=1)
    command.add_argument('--parallel-files', type=int, default=1)
    command.add_argument('--multilingual', action='store_true')
    command.add_argument('--no-vad', action='store_true')
    command.add_argument('--no-context', action='store_true')
    command.add_argument('--no-word-timestamps', action='store_true')
    command.add_argument('--include-headers', action='store_true',
        help='Only when supported by the selected worker profile')
    command.add_argument('--protocol', choices=PROTOCOLS,
        help='Persist this exact contract; defaults to the selected runtime profile')
    command.add_argument('--max-attempts', type=int,
        help='V5 requires 3; receipts-v1 defaults to 1')


def _options(args):
    value = dict(model=args.model, model_revision=args.model_revision, task=args.task,
        language=args.language, device=args.device, compute_type=args.compute_type,
        beam_size=args.beam_size, parallel_files=args.parallel_files,
        multilingual=args.multilingual, vad_filter=not args.no_vad,
        condition_on_previous_text=not args.no_context,
        word_timestamps=not args.no_word_timestamps, include_headers=args.include_headers)
    if args.max_attempts is not None:
        value['max_attempts'] = args.max_attempts
    return value


def _read_result(service, identity, source, *, edited):
    job = service.get(identity)
    if source is None:
        if len(job['sources']) != 1:
            raise ValueError('Choose the exact retained --source filename from show for this multi-file job.')
        source = job['sources'][0]['filename']
    original = service.text(identity, source)
    latest = [row for row in service.edits(identity) if row['source'] == source] if edited else []
    if edited and not latest:
        raise FileNotFoundError('No separately saved edit exists for this source.')
    row = latest[-1] if latest else None
    return {'job': identity, 'source': source, 'text': row['text'] if row else original,
            'edited': row is not None, 'revision': row['id'] if row else None,
            'notice': 'Unverified transcription derivative; original media and timed recognition remain unchanged.'}


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description='Supplied local Whisper transcription; no downloads or uploads')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--workspace', required=True, help='Active library ID from --list-libraries')
    commands = parser.add_subparsers(dest='operation', required=True)
    configure = commands.add_parser('configure', help='Explicitly bind the existing legacy/current local runtime')
    _runtime_arguments(configure)
    inspect = commands.add_parser('inspect-runtime', help='Inspect selected local files without executing them')
    _runtime_arguments(inspect)
    register = commands.add_parser('register-runtime', help='Register an exact inspected local runtime profile')
    _runtime_arguments(register)
    register.add_argument('--name', required=True)
    register.add_argument('--trust-runtime', action='store_true',
        help='Explicitly trust these reviewed runtime, worker and model files')
    register.add_argument('--fingerprint', help='Exact fingerprint from inspect-runtime')
    commands.add_parser('runtimes', help='List registered profiles; never start or silently select one')
    commands.add_parser('jobs', help='List saved library transcription jobs')
    create = commands.add_parser('create', help='Retain selected media files and save a job without inference')
    create.add_argument('files', type=Path, nargs='+')
    folder = commands.add_parser('create-folder', help='Retain supported media from one folder and its subfolders')
    folder.add_argument('folder', type=Path)
    submit = commands.add_parser('submit', help='Retain selected media files/folders in one saved job')
    submit.add_argument('inputs', type=Path, nargs='+')
    submit.add_argument('--recursive', action='store_true')
    submit.add_argument('--run', action='store_true', help='Explicitly run after retaining and publishing the job')
    submit.add_argument('--timeout', type=float, default=86400)
    for command in (create, folder, submit):
        _recognition_arguments(command)
    for name in ('run', 'resume'):
        command = commands.add_parser(name, help='Run the saved immutable choices with their reviewed runtime')
        command.add_argument('job')
        command.add_argument('--timeout', type=float, default=86400)
    for name in ('show', 'status', 'verify', 'pause', 'cancel', 'continue'):
        command = commands.add_parser(name)
        command.add_argument('job')
    rebind = commands.add_parser('rebind-runtime', help='Explicitly review a compatible runtime after restore')
    rebind.add_argument('job')
    rebind.add_argument('--profile', required=True)
    copy = commands.add_parser('copy-job', help='New ready job from verified retained originals; no inference')
    copy.add_argument('job')
    copy.add_argument('--profile', required=True, help='Explicit trusted profile with the saved worker protocol')
    copy.add_argument('--title')
    interrupted = commands.add_parser('acknowledge-interruption',
        help='Confirm that the previous external worker has stopped before reuse')
    interrupted.add_argument('job')
    interrupted.add_argument('--worker-stopped', action='store_true')
    read = commands.add_parser('read', help='Read verified original recognition or a separately saved edit')
    read.add_argument('job')
    read.add_argument('--source', help='Exact retained source filename; optional only for a single-file job')
    read.add_argument('--edited', action='store_true')
    for name in ('edit', 'save-edit'):
        edit = commands.add_parser(name, help='Save a separate revision from a UTF-8 text file')
        edit.add_argument('job')
        edit.add_argument('--source', required=True, help='Retained source filename from show')
        edit.add_argument('--text-file', type=Path, required=True)
    export = commands.add_parser('export', help='Export selected verified formats and separately labelled edits')
    export.add_argument('job')
    export.add_argument('destination', nargs='?', type=Path, help='New directory outside library data')
    export.add_argument('--output', type=Path, help='Alternative explicit new export directory')
    export.add_argument('--format', action='append', choices=('txt', 'srt', 'vtt', 'json'), dest='formats')
    args = parser.parse_args(argv)
    if args.operation == 'register-runtime' and args.trust_runtime and not args.fingerprint:
        parser.error('--trust-runtime requires the exact --fingerprint from inspect-runtime.')
    if args.operation == 'export' and (args.destination is None) == (args.output is None):
        parser.error('Choose exactly one new export directory: destination or --output.')
    job_id = getattr(args, 'job', None)

    def progress(*value):
        if len(value) == 1 and isinstance(value[0], dict):
            print(json.dumps(value[0], ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)
        elif len(value) == 3:
            print(f'{value[0]}/{value[1]}: {value[2]}', file=sys.stderr, flush=True)

    try:
        from .application import LibraryApp
        from .durable_state import local_path, regular
        from .transcription import MAX_TEXT_BYTES
        app = LibraryApp(data_directory(args.data_dir))
        service = app.transcription(args.workspace)
        if args.operation == 'configure':
            result = service.configure(args.python, args.worker, args.models_dir, protocol=args.protocol)
        elif args.operation == 'inspect-runtime':
            result = service.inspect_runtime(args.python, args.worker, args.models_dir,
                                              protocol=args.protocol, progress=progress)
        elif args.operation == 'register-runtime':
            result = service.register_runtime(args.name, args.python, args.worker, args.models_dir,
                protocol=args.protocol, expected_fingerprint=args.fingerprint,
                owner_trusted=args.trust_runtime, progress=progress)
        elif args.operation == 'runtimes':
            result = service.list_runtimes()
        elif args.operation == 'jobs':
            result = [{'id': job['id'], 'title': job['title'], 'status': job['status'],
                       'protocol': job['protocol'], 'done': job['done'], 'total': len(job['sources'])}
                      for job in service.list()]
        elif args.operation in ('create', 'create-folder', 'submit'):
            options = _options(args)
            selected = {'profile_id': args.profile} if args.profile is not None else {}
            if args.operation == 'create':
                result = {'id': service.submit(args.files, title=args.title, options=options,
                                               protocol=args.protocol, **selected)}
            elif args.operation == 'create-folder':
                result = service.submit_folder(args.folder, title=args.title, options=options,
                                                protocol=args.protocol, **selected)
            else:
                result = service.submit_sources(args.inputs, title=args.title, options=options,
                    protocol=args.protocol, recursive=args.recursive, **selected)
            job_id = result['id']
            if args.operation == 'submit' and args.run:
                print('Saved transcription job: ' + job_id, file=sys.stderr, flush=True)
                result = service.run(job_id, timeout=args.timeout, progress=progress)
        elif args.operation in ('run', 'resume'):
            result = getattr(service, args.operation)(args.job, timeout=args.timeout, progress=progress)
        elif args.operation in ('show', 'status'):
            result = service.get(args.job)
        elif args.operation == 'verify':
            result = service.verify(args.job)
        elif args.operation in ('pause', 'cancel', 'continue'):
            method = 'continue_job' if args.operation == 'continue' else args.operation
            result = getattr(service, method)(args.job)
        elif args.operation == 'rebind-runtime':
            result = service.rebind_runtime(args.job, args.profile)
        elif args.operation == 'copy-job':
            result = service.copy_job(args.job, profile_id=args.profile, title=args.title)
        elif args.operation == 'acknowledge-interruption':
            result = service.acknowledge_interruption(args.job, worker_stopped=args.worker_stopped)
        elif args.operation == 'read':
            result = _read_result(service, args.job, args.source, edited=args.edited)
        elif args.operation in ('edit', 'save-edit'):
            path = local_path(args.text_file, exists=True)
            if regular(path).st_size > MAX_TEXT_BYTES:
                raise ValueError('Edited text exceeds its size allowance.')
            # Bound the actual read too: a selected file can grow after stat.
            from .durable_state import _open_json_read
            import os
            import stat
            before = regular(path)
            with _open_json_read(path) as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or getattr(opened, 'st_file_attributes', 0) & 0x400):
                    raise ValueError('Choose one regular local edited-text file.')
                raw = stream.read(MAX_TEXT_BYTES + 1)
                after = os.fstat(stream.fileno())
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            named = regular(path)
            if any(getattr(before, key) != getattr(value, key)
                   for value in (opened, after, named) for key in fields):
                raise ValueError('Edited text changed during its bounded read.')
            if len(raw) > MAX_TEXT_BYTES:
                raise ValueError('Edited text exceeds its size allowance.')
            result = {'revision': service.save_edit(args.job, args.source, raw.decode('utf-8-sig'))}
        else:
            result = {'exported_to': str(service.export(args.job, args.destination or args.output,
                formats=tuple(dict.fromkeys(args.formats)) if args.formats else None))}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        executed = args.operation in ('run', 'resume') or args.operation == 'submit' and args.run
        if executed and result['status'] != 'completed':
            return 2 if result['status'] in ('stopped', 'paused', 'cancelled') else 1
        return 0
    except KeyboardInterrupt:
        print('Transcription stopped. Completed work remains saved.' +
              (f' Saved job: {job_id}' if job_id else ''), file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Transcription: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
