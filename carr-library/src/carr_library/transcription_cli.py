"""Headless entrypoint for the same library transcription service as the UI."""
import argparse
import json
from pathlib import Path
import sys

from .transcription_host import data_directory
from .transcription import PROTOCOLS, RECEIPTS_PROTOCOL
from .transcription_receipt import DEFAULT_SETTINGS as SUPPLIED_SETTINGS, PROTOCOL as SUPPLIED_PROTOCOL


def _supplied_settings_arguments(command):
    choices = {'output_mode': ('separate', 'one_per_folder', 'one_whole_job'),
               'task': ('transcribe', 'translate'), 'translation_target': ('en',),
               'device': ('cpu', 'cuda', 'auto'),
               'compute_type': ('int8', 'auto', 'float16', 'float32')}
    for name, default in SUPPLIED_SETTINGS.items():
        options = {'default': default}
        if type(default) is bool:
            options['action'] = argparse.BooleanOptionalAction
        elif type(default) is int:
            options['type'] = int
        if name in choices:
            options['choices'] = choices[name]
        command.add_argument('--' + name.replace('_', '-'), **options)


def _supplied_command(root, args):
    """Use the existing library-wide service without reclassifying workspace jobs."""
    from .transcription_service import TranscriptionService
    service = TranscriptionService(root)
    if args.operation == 'prepare':
        settings = {name: getattr(args, name) for name in SUPPLIED_SETTINGS}
        identity = service.prepare(args.files, args.manifest, settings, title=args.title)
        return {'id': identity, 'scope': 'library', 'protocol': SUPPLIED_PROTOCOL}
    if args.operation == 'jobs':
        return [{'scope': 'library', 'protocol': SUPPLIED_PROTOCOL, **row} for row in service.jobs()]
    if args.operation == 'show':
        return service.definition(args.job)
    if args.operation == 'run':
        return service.run(args.job, args.manifest, timeout=args.timeout,
            progress=lambda done, total, state: print(f'{done}/{total}: {state}', file=sys.stderr, flush=True))
    if args.operation == 'verify':
        return service.completed(args.job)
    if args.operation in ('pause', 'resume', 'stop'):
        service.control(args.job, args.operation)
        return {'id': args.job, 'control_requested': args.operation, 'scope': 'library'}
    if args.operation == 'reading-copy':
        from .reader.narration import NarrationStore
        project = service.reading_copy(args.job, args.output)
        digest = NarrationStore(root).save(project)
        return {'id': project.id, 'saved_sha256': digest,
                'original_sha256': project.original_sha256, 'source_job': args.job,
                'source_output': args.output}
    raise ValueError('This operation is not supported by the selected transcription profile.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Supplied local Whisper transcription; no downloads')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--workspace', help='Required for workspace profiles; omit for library-wide supplied jobs')
    parser.add_argument('--profile', choices=(*PROTOCOLS, SUPPLIED_PROTOCOL),
        help='Explicit contract and scope; supplied-whisper-v43-hardened-1 uses library-wide jobs')
    commands = parser.add_subparsers(dest='operation', required=True)
    configure = commands.add_parser('configure', help='Bind an explicitly selected local worker/runtime/model cache')
    configure.add_argument('--python', type=Path, required=True)
    configure.add_argument('--worker', type=Path, required=True)
    configure.add_argument('--models-dir', type=Path, required=True)
    configure.add_argument('--protocol', choices=PROTOCOLS,
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
    run.add_argument('--manifest', type=Path, help='Required to re-admit the library-wide supplied runtime before Start')
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
    prepare = commands.add_parser('prepare', help='Prepare library-wide supplied inputs with seventeen frozen settings')
    prepare.add_argument('files', type=Path, nargs='+', help='Explicit local files or recursive folders')
    prepare.add_argument('--manifest', type=Path, required=True)
    prepare.add_argument('--title', default='Local transcription')
    _supplied_settings_arguments(prepare)
    for action in ('pause', 'resume', 'stop'):
        commands.add_parser(action, help='Request this control for a running library-wide job').add_argument('job')
    reading = commands.add_parser('reading-copy', help='Save a separate reading copy of one verified supplied output')
    reading.add_argument('job')
    reading.add_argument('--output', required=True, help='Exact relative output path from verify')
    args = parser.parse_args(argv)
    supplied = args.profile == SUPPLIED_PROTOCOL
    if supplied:
        if args.workspace:
            parser.error('The supplied profile uses library-wide jobs; omit --workspace. Existing workspace jobs are unchanged.')
        if args.operation not in ('prepare', 'jobs', 'show', 'run', 'verify', 'pause', 'resume', 'stop', 'reading-copy'):
            parser.error('Use prepare/run/verify or a control command for the library-wide supplied profile.')
        if args.operation == 'run' and args.manifest is None:
            parser.error('Start requires an explicitly selected --manifest for the supplied runtime.')
    else:
        if not args.workspace:
            parser.error('Workspace profiles require --workspace; library-wide jobs require --profile ' + SUPPLIED_PROTOCOL + '.')
        if args.operation in ('prepare', 'pause', 'resume', 'stop', 'reading-copy'):
            parser.error('This command requires --profile ' + SUPPLIED_PROTOCOL + ' and no --workspace.')
        if args.operation == 'run' and args.manifest is not None:
            parser.error('Workspace profiles use their configured runtime; --manifest belongs to the supplied profile.')
        selected_protocol = getattr(args, 'protocol', None)
        if args.profile is not None and selected_protocol is not None and args.profile != selected_protocol:
            parser.error('The selected --profile and --protocol must agree.')
        if args.operation in ('configure', 'create', 'create-folder') and args.profile is not None:
            args.protocol = args.profile
        if args.operation == 'configure' and args.protocol is None:
            args.protocol = RECEIPTS_PROTOCOL
    try:
        from .application import LibraryApp
        from .durable_state import local_path, regular
        from .transcription import MAX_TEXT_BYTES
        app = LibraryApp(data_directory(args.data_dir))
        if supplied:
            result = _supplied_command(app.root, args)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
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
    except InterruptedError:
        print('Transcription stopped. Retained originals and verified histories remain available.', file=sys.stderr)
        return 2
    except (ValueError, OSError, RuntimeError) as exc:
        print('Transcription: ' + str(exc), file=sys.stderr)
        return 1
