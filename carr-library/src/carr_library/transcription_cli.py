"""Thin headless entry points for the same native TranscriptionService."""
import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import re
import signal
import sys
import threading

from .durable_state import read_json
from .studio.cli import data_directory
from .transcription import (EngineSpec, MAX_ARTIFACT_BYTES, TranscriptionService,
                            TranscriptionSettings, _bounded_bytes, _relative, _text)
from .transcription_inputs import select_inputs


def _job_id(value):
    if not re.fullmatch(r'[0-9a-f]{32}', value):
        raise argparse.ArgumentTypeError('Use the saved 32-character lowercase hexadecimal job ID.')
    return value


def _deadline(value, maximum):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Choose a positive finite number of seconds.') from exc
    if not 0 < number <= maximum or not math.isfinite(number):
        raise argparse.ArgumentTypeError('Choose a positive time budget no greater than ' + str(maximum) + ' seconds.')
    return number


def _artifact(value):
    try:
        value = _relative(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not value.startswith('results/'):
        raise argparse.ArgumentTypeError('Use a verified job-relative results/... artifact path from status.')
    return value


def _settings(args, parser):
    overrides = {name: getattr(args, name) for name in ('model', 'language', 'device', 'compute_type', 'task')
                 if getattr(args, name) is not None}
    if args.settings is not None:
        if overrides or args.formats:
            parser.error('Choose a settings JSON file or individual settings flags, not both.')
        value = read_json(args.settings)
        if type(value) is not dict:
            raise ValueError('Transcription settings JSON must be an object.')
    else:
        value = overrides
        if args.formats:
            value.update({'output_' + fmt: fmt in args.formats for fmt in ('txt', 'srt', 'vtt', 'json')})
    try:
        return TranscriptionSettings(**value)
    except TypeError as exc:
        raise ValueError('Unknown or malformed transcription settings.') from exc


def _summary(job):
    receipt = job.get('receipt') or {}
    return {'id': job['id'], 'title': job['title'], 'state': job['state'],
            'done': receipt.get('done', 0), 'total': len(job['inputs']),
            'artifacts': receipt.get('artifacts', []), 'revisions': len(job['revisions']),
            'error': job.get('error')}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local reviewed transcription; submit and execution are separate operations')
    parser.add_argument('--data-dir', type=Path, help='Existing application data selection, or an explicit private library root')
    operations = parser.add_subparsers(dest='operation', required=True)
    configure = operations.add_parser('configure', help='Verify and save an explicit reviewed local engine profile')
    configure.add_argument('--python', '--python-path', dest='python_path', required=True, type=Path)
    configure.add_argument('--worker', '--worker-path', dest='worker_path', required=True, type=Path)
    configure.add_argument('--worker-sha256', required=True)
    configure.add_argument('--models-dir', required=True, type=Path)
    operations.add_parser('capability', help='Inspect configured worker availability; does not run or download anything')
    operations.add_parser('jobs', help='List saved jobs without starting them')
    status = operations.add_parser('status', help='Revalidate saved source/output receipts and show one job')
    status.add_argument('job_id', type=_job_id)
    submit = operations.add_parser('submit', help='Snapshot selected audio into a new queued job; never executes the worker')
    submit.add_argument('files', nargs='*', type=Path)
    submit.add_argument('--folder', action='append', type=Path, default=[],
                        help='Explicit local folder to expand now; repeat for more folders. No ongoing watch.')
    submit.add_argument('--recursive', action='store_true',
                        help='Include subfolders of --folder; off by default. Links/reparse points are never followed.')
    submit.add_argument('--title', default='Transcription')
    submit.add_argument('--settings', type=Path, help='Bounded JSON object containing TranscriptionSettings fields')
    submit.add_argument('--model', help='Installed model directory name; tiny.en is English-only')
    submit.add_argument('--language', help='auto or an explicit supported source language code')
    submit.add_argument('--device', choices=('cpu', 'cuda', 'auto'))
    submit.add_argument('--compute-type', choices=('default', 'auto', 'int8', 'int8_float16', 'int8_float32',
        'int8_bfloat16', 'int16', 'float16', 'float32', 'bfloat16'))
    submit.add_argument('--task', choices=('transcribe', 'translate'), help='Whisper translation target is English')
    submit.add_argument('--format', action='append', dest='formats', choices=('txt', 'srt', 'vtt', 'json'),
                        help='Requested output format; repeat as needed. Defaults to all four.')
    run = operations.add_parser('run', help='Explicitly run or resume a saved job with the configured reviewed engine')
    run.add_argument('job_id', type=_job_id)
    run.add_argument('--timeout', type=lambda value: _deadline(value, 7 * 24 * 3600), default=24 * 3600,
                     help='Wall-clock seconds, including pauses; default 86400')
    run.add_argument('--stop-grace', type=lambda value: _deadline(value, 30), default=3,
                     help='Seconds to allow cooperative cancellation before stopping the owned process tree')
    for name in ('pause', 'continue'):
        command = operations.add_parser(name, help='Request ' + name + ' for an already running job')
        command.add_argument('job_id', type=_job_id)
    edit = operations.add_parser('edit', help='Save a bounded UTF-8 TXT derivative as a new revision')
    edit.add_argument('job_id', type=_job_id)
    edit.add_argument('--artifact', required=True, type=_artifact)
    edit.add_argument('--text-file', required=True, type=Path)
    export = operations.add_parser('export', help='Publish one verified original or saved revision to a new file')
    export.add_argument('job_id', type=_job_id)
    export.add_argument('--artifact', required=True, type=_artifact)
    export.add_argument('--output', required=True, type=Path)
    export.add_argument('--revision', type=_job_id, help='Saved TXT revision ID; omit for the original worker output')
    args = parser.parse_args(argv)
    if args.operation == 'submit' and (not args.files and not args.folder or args.recursive and not args.folder):
        parser.error('Choose explicit audio files or --folder; --recursive requires --folder.')
    cancelled = threading.Event()
    previous_signal = None
    current_job = getattr(args, 'job_id', None)
    def interrupt(signum, frame):
        cancelled.set()
    def progress(done, total, phase):
        label = f'{done}/{total}: {phase}' if done is not None and total is not None else phase
        print(label, file=sys.stderr, flush=True)
    try:
        if threading.current_thread() is threading.main_thread():
            previous_signal = signal.signal(signal.SIGINT, interrupt)
        # Validate the explicit profile/settings/text before creating any app
        # state. The durable service still performs its own boundary checks.
        engine = (EngineSpec(str(args.python_path), str(args.worker_path), args.worker_sha256,
                            str(args.models_dir)).validate() if args.operation == 'configure' else None)
        settings = _settings(args, parser) if args.operation == 'submit' else None
        if args.operation == 'submit':
            _text(args.title, 'job name')
            selected = select_inputs(args.files, folders=args.folder, recursive=args.recursive,
                                     cancelled=cancelled.is_set)
            if args.folder:
                print(selected.describe(), file=sys.stderr, flush=True)
        text = (_bounded_bytes(args.text_file, min(MAX_ARTIFACT_BYTES, 8_000_000)).decode('utf-8-sig')
                if args.operation == 'edit' else None)
        if text is not None and (len(text) > 2_000_000 or '\x00' in text):
            raise ValueError('An edited transcript must be bounded plain text.')
        from .application import LibraryApp
        library = LibraryApp(data_directory(args.data_dir))
        service = TranscriptionService(library.root / 'state' / 'transcription')
        code = 0
        if args.operation == 'configure':
            admitted = service.configure_engine(engine)
            result = {'state': 'configured', 'engine': asdict(admitted),
                      'qualification': 'worker digest admitted; runtime, model and acoustic quality are separate'}
        elif args.operation == 'capability':
            result = service.capability()
        elif args.operation == 'jobs':
            result = [_summary(job) for job in service.list()]
        elif args.operation == 'status':
            result = service.get(args.job_id, cancelled=cancelled.is_set)
        elif args.operation == 'submit':
            current_job = service.submit(selected.files, settings, title=args.title,
                                         cancelled=cancelled.is_set, progress=progress)
            result = _summary(service.get(current_job, verify=False))
        elif args.operation == 'run':
            print('Transcription job: ' + args.job_id, file=sys.stderr, flush=True)
            result = service.run(args.job_id, cancelled=cancelled.is_set, progress=progress,
                                 timeout=args.timeout, stop_grace=args.stop_grace)
            code = 0 if result['state'] == 'succeeded' else 1
        elif args.operation in ('pause', 'continue'):
            service.set_paused(args.job_id, args.operation == 'pause')
            result = {'id': args.job_id, 'state': args.operation + '-requested',
                      'message': 'Worker acknowledgement is separate; completed outputs remain retained.'}
        elif args.operation == 'edit':
            result = service.revise(args.job_id, args.artifact, text)
        else:
            destination = service.export(args.job_id, args.artifact, args.output, revision_id=args.revision)
            result = {'id': args.job_id, 'state': 'exported', 'path': str(destination)}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if cancelled.is_set():
            print('Interrupted. Explicitly run the saved job ID to resume: ' + str(current_job or 'none published'),
                  file=sys.stderr, flush=True)
            return 130
        return code
    except (KeyboardInterrupt, InterruptedError):
        print('Transcription interrupted. Saved jobs and completed files remain available; explicitly run the job to resume.'
              + (' Job: ' + current_job if current_job else ''), file=sys.stderr, flush=True)
        return 130
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        print('Local transcription: ' + str(exc), file=sys.stderr, flush=True)
        return 1
    finally:
        if previous_signal is not None:
            signal.signal(signal.SIGINT, previous_signal)


if __name__ == '__main__':
    raise SystemExit(main())
