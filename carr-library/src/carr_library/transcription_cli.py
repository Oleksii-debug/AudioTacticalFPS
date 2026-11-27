"""Headless routes to the current retained-input supplied-worker service."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import signal
import sys
import threading

from .audio.jobs import read_json, verified_reader
from .audio.model_manifest import local_path
from .backup_files import optional_present
from .installation_lease import installed_entrypoint
from .operations import ResourceCancelled
from .runtime_settings import EngineProfileStore
from .transcription_receipt import DEFAULT_SETTINGS, validate_settings
from .transcription_service import TranscriptionService
from .transcription_state import check_cancelled, regular

_HASH = re.compile(r'[0-9a-f]{64}\Z')
_FORMATS = ('txt', 'srt', 'vtt', 'json')


def _write(value, *, stream=None):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2),
          file=sys.stdout if stream is None else stream, flush=True)


@contextmanager
def _cancellation():
    stopped = threading.Event()
    previous = None
    install = threading.current_thread() is threading.main_thread()
    if install:
        previous = signal.signal(signal.SIGINT, lambda number, frame: stopped.set())
    try:
        yield stopped.is_set
    finally:
        if install:
            signal.signal(signal.SIGINT, previous)


def _runtime_arguments(parser):
    selected = parser.add_mutually_exclusive_group(required=True)
    selected.add_argument('--profile', help='Enabled local supplied-worker profile ID')
    selected.add_argument('--manifest', type=Path, help='Explicit reviewed local runtime manifest')
    parser.add_argument('--reviewed-sha256',
        help='Exact manifest SHA-256 from inspect-runtime; required with --manifest')


def _settings_arguments(parser):
    parser.add_argument('--model', help='Exact already installed local model directory name')
    parser.add_argument('--language')
    parser.add_argument('--task', choices=('transcribe', 'translate'),
                        help='Whisper translate produces English, without a remote provider')
    parser.add_argument('--device', choices=('cpu', 'cuda', 'auto'))
    parser.add_argument('--compute-type', choices=('int8', 'auto', 'float16', 'float32'))
    parser.add_argument('--beam-size', type=int)
    parser.add_argument('--parallel-files', type=int)
    parser.add_argument('--output-mode', choices=('separate', 'one_per_folder', 'one_whole_job'))
    parser.add_argument('--format', dest='formats', action='append', choices=_FORMATS)
    parser.add_argument('--multilingual', action='store_true', default=None)
    parser.add_argument('--no-vad', action='store_true', default=None)
    parser.add_argument('--include-headers', action='store_true', default=None)
    parser.add_argument('--no-previous-text', action='store_true', default=None)


def _settings(args, *, base=None, model=None):
    value = dict(DEFAULT_SETTINGS if base is None else base)
    if model is not None:
        value['model'] = model
    for name in ('model', 'language', 'task', 'device', 'compute_type', 'beam_size',
                 'parallel_files', 'output_mode', 'multilingual', 'include_headers'):
        chosen = getattr(args, name, None)
        if chosen is not None:
            value[name] = chosen
    if getattr(args, 'formats', None) is not None:
        if len(set(args.formats)) != len(args.formats):
            raise ValueError('Choose each output format once.')
        for format in _FORMATS:
            value['output_' + format] = format in args.formats
    if getattr(args, 'no_vad', None):
        value['vad_filter'] = False
    if getattr(args, 'no_previous_text', None):
        value['condition_on_previous_text'] = False
    return validate_settings(value)


def _selected_runtime(store, args, cancelled):
    if args.profile is not None:
        return store.admit(args.profile, engine='supplied-whisper', cancelled=cancelled).manifest
    return store.inspect(args.manifest, expected_sha256=args.reviewed_sha256,
                         engine='supplied-whisper', cancelled=cancelled).manifest


def _runtime_report(reviewed):
    manifest = reviewed.manifest
    return dict(engine=manifest.engine, manifest_sha256=manifest.manifest_sha256,
        model_id=manifest.model_id, files=reviewed.files, bytes=reviewed.bytes,
        settings=dict(manifest.settings), executed=False, quality='NOT_QUALIFIED')


def _status(service, identity):
    from .transcription_backup import _execution
    definition = service.definition(identity, verify_inputs=False)
    path = service._job(identity) / 'execution.json'
    execution = None
    if optional_present(path):
        execution = read_json(path, limit=65536)
        _execution(execution)
    return dict(id=identity, title=definition['title'], inputs=len(definition['inputs']),
        settings=definition['settings'], runtime_manifest_sha256=definition['runtime_manifest_sha256'],
        recorded_state='prepared' if execution is None else execution['state'],
        execution=execution, completion_verified=False,
        note='Recorded state is metadata. Use verify or outputs to check retained bytes.')


def _selected_output(service, identity, relative, cancelled):
    summary = service.completed(identity, cancelled=cancelled)
    if relative is None:
        rows = [row for row in summary['outputs'] if row['format'] == 'txt']
        if len(rows) != 1:
            raise ValueError('Choose one exact --output path from the verified outputs list.')
        relative = rows[0]['path']
    if relative not in {row['path'] for row in summary['outputs']}:
        raise ValueError('The selected output is not in the verified completion.')
    return relative


def _edit_text(path, cancelled):
    path = local_path(path)
    if regular(path).st_size > 8 * 1024**2:
        raise ValueError('Choose a UTF-8 reading edit of at most 8 MiB.')
    check_cancelled(cancelled)
    with verified_reader(path) as stream:
        raw = stream.read(8 * 1024**2 + 1)
    if len(raw) > 8 * 1024**2:
        raise ValueError('The reading edit grew beyond its byte budget.')
    check_cancelled(cancelled)
    return raw.decode('utf-8-sig')


def _parser():
    import argparse
    parser = argparse.ArgumentParser(description='Resonance retained local transcription')
    parser.add_argument('--data-dir', required=True, type=Path,
                        help='Existing private library directory; no implicit new library')
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect-runtime', help='Verify a selected local manifest without executing it')
    inspect.add_argument('--manifest', type=Path, required=True)
    register = commands.add_parser('register-runtime', help='Register the exact explicitly reviewed local installation')
    register.add_argument('--manifest', type=Path, required=True)
    register.add_argument('--reviewed-sha256', required=True)
    register.add_argument('--name', required=True)
    commands.add_parser('runtimes', help='List metadata of locally registered supplied-worker profiles')
    commands.add_parser('jobs', help='List retained job metadata without opening original source locations')
    for name, help_text in (
        ('submit', 'Retain explicit input copies; launch only with --run'),
        ('reprepare', 'Create a new job from retained inputs and a newly reviewed runtime')):
        command = commands.add_parser(name, help=help_text)
        if name == 'submit':
            command.add_argument('inputs', nargs='+', type=Path)
            command.add_argument('--recursive', action='store_true',
                                 help='Allow explicitly selected folders to be scanned recursively')
        else:
            command.add_argument('job_id')
        command.add_argument('--title')
        command.add_argument('--run', action='store_true')
        command.add_argument('--timeout', type=float, default=86400)
        _runtime_arguments(command)
        _settings_arguments(command)
    for name in ('run', 'resume'):
        command = commands.add_parser(name, help='Start a fresh controlled attempt from retained inputs')
        command.add_argument('job_id')
        command.add_argument('--timeout', type=float, default=86400)
        _runtime_arguments(command)
    for name in ('status', 'verify', 'outputs', 'pause', 'continue', 'cancel'):
        command = commands.add_parser(name)
        command.add_argument('job_id')
    read = commands.add_parser('read', help='Read one verified saved output without any model installation')
    read.add_argument('job_id')
    read.add_argument('--output', help='Relative output path from outputs; defaults only when exactly one TXT exists')
    read.add_argument('--plain', action='store_true')
    edit = commands.add_parser('save-edit', help='Create a separate current reading project; keep the transcript immutable')
    edit.add_argument('job_id')
    edit.add_argument('--output')
    edit.add_argument('--text-file', type=Path, required=True)
    export = commands.add_parser('export', help='Export exact saved formats to a new directory, with provenance')
    export.add_argument('job_id')
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--format', dest='formats', action='append', choices=_FORMATS)
    export_edit = commands.add_parser('export-edit', help='Export an explicitly selected reading project as UTF-8')
    export_edit.add_argument('project_id')
    export_edit.add_argument('--output', type=Path, required=True)
    return parser


def _validate_arguments(parser, args):
    if hasattr(args, 'timeout') and (not math.isfinite(args.timeout) or not 1 <= args.timeout <= 604800):
        parser.error('--timeout must be between one second and seven days.')
    fingerprint = getattr(args, 'reviewed_sha256', None)
    if fingerprint is not None and _HASH.fullmatch(fingerprint) is None:
        parser.error('--reviewed-sha256 needs exactly 64 lowercase hexadecimal characters.')
    if args.command in ('submit', 'reprepare', 'run', 'resume'):
        if args.manifest is not None and fingerprint is None:
            parser.error('--manifest requires the exact --reviewed-sha256 from inspect-runtime.')
        if args.profile is not None and fingerprint is not None:
            parser.error('A saved --profile already supplies its exact pin; omit --reviewed-sha256.')
    if args.command in ('submit', 'reprepare'):
        try:
            _settings(args)
        except ValueError as error:
            parser.error(str(error))
    if getattr(args, 'formats', None) is not None and len(set(args.formats)) != len(args.formats):
        parser.error('Choose each output format once.')


@installed_entrypoint()
def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    _validate_arguments(parser, args)
    job_id = getattr(args, 'job_id', None)
    try:
        with _cancellation() as cancelled:
            root = local_path(args.data_dir)
            service = TranscriptionService(root)
            store = EngineProfileStore(root)
            result = None
            if args.command == 'inspect-runtime':
                result = _runtime_report(store.inspect(args.manifest, engine='supplied-whisper', cancelled=cancelled))
            elif args.command == 'register-runtime':
                reviewed = store.inspect(args.manifest, expected_sha256=args.reviewed_sha256,
                                         engine='supplied-whisper', cancelled=cancelled)
                profile = store.register(reviewed.manifest.path, name=args.name,
                    expected_sha256=args.reviewed_sha256, cancelled=cancelled)
                result = dict(id=profile.id, name=profile.name, engine=profile.engine,
                              manifest_sha256=profile.manifest_sha256, enabled=profile.enabled, executed=False)
            elif args.command == 'runtimes':
                result = [dict(id=row.id, name=row.name, engine=row.engine,
                    manifest_sha256=row.manifest_sha256, enabled=row.enabled, quality='NOT_QUALIFIED')
                    for row in store.snapshot().profiles if row.engine == 'supplied-whisper']
            elif args.command == 'jobs':
                result = list(service.jobs(cancelled=cancelled))
            elif args.command in ('submit', 'reprepare', 'run', 'resume'):
                manifest = _selected_runtime(store, args, cancelled)
                if args.command in ('submit', 'reprepare'):
                    previous = None
                    if args.command == 'submit':
                        if not args.recursive and any(path.is_dir() for path in args.inputs):
                            raise ValueError('A selected folder requires explicit --recursive.')
                        sources = args.inputs
                    else:
                        previous = service.definition(job_id, cancelled=cancelled)
                        job = service._job(job_id)
                        sources = [job / row['path'] for row in previous['inputs']]
                    settings = _settings(args, base=None if previous is None else previous['settings'],
                                         model=manifest.settings.get('model'))
                    job_id = service.prepare(sources, manifest.path, settings,
                        title=(args.title if args.title is not None else
                               ('Local transcription' if previous is None else previous['title'])),
                        expected_manifest_sha256=manifest.manifest_sha256, cancelled=cancelled)
                    # Acknowledgement is emitted immediately after durable prepare, before any later read/run.
                    result = dict(status='prepared', id=job_id, executed=False)
                    _write(dict(prepared_job=job_id), stream=sys.stderr)
                if args.command in ('run', 'resume') or args.run:
                    definition = service.definition(job_id, cancelled=cancelled)
                    if manifest.manifest_sha256 != definition['runtime_manifest_sha256']:
                        raise ValueError('The selected runtime differs from this immutable job. Use reprepare for a new job.')
                    def progress(done, total, phase):
                        _write(dict(job_id=job_id, done=done, total=total, phase=phase), stream=sys.stderr)
                    result = service.run(job_id, manifest.path, timeout=args.timeout,
                                         cancelled=cancelled, progress=progress)
            elif args.command == 'status':
                result = _status(service, job_id)
            elif args.command in ('verify', 'outputs'):
                summary = service.completed(job_id, cancelled=cancelled)
                result = summary if args.command == 'verify' else dict(id=job_id, outputs=summary['outputs'],
                    completion_sha256=summary['completion_sha256'], historical=True,
                    runtime_current_verified=False)
            elif args.command in ('pause', 'continue', 'cancel'):
                action = {'pause': 'pause', 'continue': 'resume', 'cancel': 'stop'}[args.command]
                service.control(job_id, action)
                result = dict(id=job_id, requested=args.command, acknowledged=False,
                    note='Control requested. The owned worker acknowledges only at its checked boundaries.')
            elif args.command in ('read', 'save-edit'):
                chosen = _selected_output(service, job_id, args.output, cancelled)
                project = service.reading_copy(job_id, chosen, cancelled=cancelled)
                if args.command == 'read':
                    if args.plain:
                        print(project.original_text, end='')
                        return 0
                    result = dict(id=job_id, output=chosen, text=project.original_text, historical=True,
                                  runtime_current_verified=False, citation_authority=False)
                else:
                    from .reader.narration import NarrationStore, revise
                    text = _edit_text(args.text_file, cancelled)
                    project = revise(project, text, label='Explicit transcription reading edit')
                    check_cancelled(cancelled)
                    checksum = NarrationStore(root).save(project)
                    result = dict(status='saved', project_id=project.id, sha256=checksum,
                                  source_job=job_id, source_output=chosen, citation_authority=False)
            elif args.command == 'export':
                from .transcription_exports import export_results
                result = export_results(service, job_id, args.output, formats=args.formats, cancelled=cancelled)
            else:
                from .reader.narration import NarrationStore, protect_original_path
                from .research.export_file import write_export
                project, _ = NarrationStore(root).load(args.project_id)
                protect_original_path(project.original_path, args.output)
                check_cancelled(cancelled)
                receipt = write_export(args.output, project.current_text, private_root=root,
                                       require_absent=True, cancelled=cancelled)
                result = dict(status='complete', path=str(receipt.path), cleanup_warning=receipt.cleanup_warning)
            _write(result)
            return 0
    except (KeyboardInterrupt, InterruptedError, ResourceCancelled):
        _write(dict(status='interrupted', job_id=job_id,
            message='Retained inputs and accepted results remain available.'), stream=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        from .transcription_exports import PublishedExportError
        value = dict(status='error', job_id=job_id, message=str(error))
        if isinstance(error, PublishedExportError):
            value.update(status='published_durability_unconfirmed', path=str(error.destination))
        _write(value, stream=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
