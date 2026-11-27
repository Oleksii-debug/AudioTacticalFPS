"""Explicit headless capture, review and resume over the native document batch service."""
import argparse
import json
from pathlib import Path
import re
import signal
import sys
from threading import Event

from ..durable_state import local_path, read_json, regular
from ..research.export_file import write_export
from .batches import BatchPreview, DocumentBatches
from .planning import MODES, SplitOptions

REVIEW_SCHEMA = 'carr-document-batch-review/1'


def _write_utf8(stream, payload):
    binary = getattr(stream, 'buffer', None)
    if binary is None:
        stream.write(payload)
        stream.flush()
    else:
        # Redirected Windows console encodings must not corrupt source names.
        binary.write(payload.encode('utf-8'))
        binary.flush()


def _print_json(value):
    _write_utf8(sys.stdout, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def _progress(message):
    _write_utf8(sys.stderr, str(message) + '\n')



def _cleanup_notice(message):
    try:
        _progress(message)
    except (Exception, KeyboardInterrupt):
        pass  # A cleanup notification cannot revoke a returned operation status.


def _summary(record):
    review = record.get('review')
    return {'id': record['id'], 'status': record['status'], 'message': record['message'],
            'source_files': len(record['sources']), 'prepared_files': sum(row['prepared'] is not None for row in record['sources']),
            'completed_outputs': len(record['outputs']),
            'reviewed_outputs': len(review['outputs']) if review else 0,
            'review_sha256': record.get('review_sha256'),
            'destination': review['destination'] if review else None,
            'verification': 'Stored checkpoint metadata. Run explicitly to reverify completed output bytes.'}


def _load_review(path, expected):
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('Supply the exact review SHA256 printed by preview.')
    value = read_json(local_path(path, exists=True))
    if (type(value) is not dict or set(value) != {'schema', 'preview', 'review_sha256'}
            or value['schema'] != REVIEW_SCHEMA or value['review_sha256'] != expected):
        raise ValueError('The saved review does not match the explicitly supplied review SHA256.')
    preview = BatchPreview.from_dict(value['preview'])
    if preview.fingerprint != expected:
        raise ValueError('The reviewed plan changed. Preview it again before approval.')
    return preview


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reviewed document extraction, split/naming and Studio batches; no remote fallback')
    parser.add_argument('--data-dir', type=Path, required=True, help='Existing Resonance library folder')
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('capture', help='Retain explicit files/folders; no extraction, export or speech starts')
    capture.add_argument('paths', type=Path, nargs='+')
    capture.add_argument('--reading-copies', action='store_true', help='Read existing versioned reading-copy JSON files instead of raw documents')
    capture.add_argument('--ocr-language', action='append', default=[], help='Explicit local OCR language for raw image documents; repeat if needed')
    commands.add_parser('jobs', help='List stored bounded batch metadata; does not execute a worker')
    for name, help_text in (
            ('prepare', 'Extract retained inputs with existing local converters; completed items are reverified'),
            ('show', 'Show stored status, item receipts and review metadata; no execution'),
            ('run', 'Run an already approved plan or verify/reuse its completed outputs'),
            ('pause', 'Request pause at the existing worker cancellation boundary'),
            ('cancel', 'Request cancellation while retaining completed receipts')):
        command = commands.add_parser(name, help=help_text)
        command.add_argument('job')
    preview = commands.add_parser('preview', help='Review exact boundaries, output names and collisions; publishes no batch outputs')
    preview.add_argument('job')
    preview.add_argument('--destination', type=Path, required=True, help='Existing output folder outside the private library')
    preview.add_argument('--mode', choices=MODES, default='whole')
    preview.add_argument('--size', type=int, default=5000)
    preview.add_argument('--marker', default='', help='Literal text or bounded regular expression, according to --mode')
    preview.add_argument('--ignore-case', action='store_true')
    preview.add_argument('--seconds', type=float, default=300, help='Estimated-duration target; not measured audio timing')
    preview.add_argument('--words-per-minute', type=int, default=150, help='Explicit whitespace-word estimate for estimated-duration mode')
    preview.add_argument('--template', default='{index:04d}-{source}-{part:03d}')
    preview.add_argument('--combine', action='store_true', help='Explicitly combine prepared copies with two newline separators')
    preview.add_argument('--action', choices=('text', 'studio'), default='text')
    preview.add_argument('--voice-id', default='', help='Exact installed local voice ID for Studio admission')
    preview.add_argument('--language', default='')
    preview.add_argument('--format', choices=('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma'), default='wav')
    preview.add_argument('--output', type=Path, help='New JSON review file; an existing file is never overwritten')
    approve = commands.add_parser('approve', help='Persist an unchanged reviewed plan; run remains a separate explicit command')
    approve.add_argument('--review', type=Path, required=True)
    approve.add_argument('--review-sha256', required=True, help='Exact fingerprint printed by preview')
    args = parser.parse_args(argv)
    if args.command == 'capture' and args.reading_copies and args.ocr_language:
        parser.error('--ocr-language applies to raw document capture, not existing reading copies.')

    stopped, previous, native = Event(), None, None
    service = None
    identity = getattr(args, 'job', None)
    try:
        root = local_path(args.data_dir, exists=True)
        regular(root, directory=True)
        regular(root / 'library.sqlite')
        # Validate the existing catalog schema through the normal application.
        # This command never invents a library in an arbitrary selected folder.
        from ..application import LibraryApp
        library = LibraryApp(root)
        from ..native_runtime import activate_bundled_runtime
        native = activate_bundled_runtime()
        service = DocumentBatches(library.root)
        if args.command in ('capture', 'prepare', 'preview', 'approve', 'run'):
            previous = signal.signal(signal.SIGINT, lambda *unused: stopped.set())
        progress = _progress
        if args.command == 'capture':
            identity = (service.create_reading_copies(args.paths, cancelled=stopped.is_set) if args.reading_copies else
                        service.create(args.paths, ocr_languages=tuple(args.ocr_language), cancelled=stopped.is_set))
            result = _summary(service.get(identity))
        elif args.command == 'prepare':
            result = _summary(service.prepare(identity, cancelled=stopped.is_set, progress=progress))
        elif args.command == 'preview':
            options = SplitOptions(args.mode, args.size, args.marker, args.ignore_case, args.seconds, args.words_per_minute)
            plan = service.preview(identity, args.destination, options, template=args.template, combined=args.combine,
                action=args.action, voice_id=args.voice_id, language=args.language, format=args.format, cancelled=stopped.is_set)
            result = {'schema': REVIEW_SCHEMA, 'preview': plan.to_dict(), 'review_sha256': plan.fingerprint}
            if args.output:
                receipt = write_export(args.output, json.dumps(result, ensure_ascii=False, indent=2) + '\n',
                                       private_root=library.root, require_absent=True, cancelled=stopped.is_set)
                if receipt.cleanup_warning:
                    _cleanup_notice(receipt.cleanup_warning)
        elif args.command == 'approve':
            plan = _load_review(args.review, args.review_sha256)
            identity = plan.job_id
            result = _summary(service.approve(plan, cancelled=stopped.is_set))
        elif args.command == 'run':
            result = _summary(service.run(identity, cancelled=stopped.is_set, progress=progress))
        elif args.command in ('pause', 'cancel'):
            getattr(service, args.command)(identity)
            result = {**_summary(service.get(identity)), 'control_requested': args.command,
                      'control_status': 'Requested; the active worker acknowledges it at its cancellation boundary.'}
        elif args.command == 'jobs':
            result = service.jobs()
        else:
            result = service.get(identity)
            result = {**result, 'verification': 'Stored checkpoint metadata; no current-output verification was requested.'}
        _print_json(result)
        return 0
    except (InterruptedError, KeyboardInterrupt):
        if service is not None and identity is not None:
            try:
                _print_json(_summary(service.get(identity)))
            except (OSError, ValueError, RuntimeError):
                pass
        _progress('Document batch interrupted. Completed receipts are retained; resume with an explicit prepare or run.')
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        _progress('Document batch: ' + str(exc))
        return 1
    finally:
        if service is not None and not service.close():
            _cleanup_notice(service.cleanup_notice)
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        if native is not None:
            try:
                native.close()
            except (Exception, KeyboardInterrupt):
                _cleanup_notice('Document output status is unchanged, but closing the bundled runtime handle could not be confirmed.')


if __name__ == '__main__':
    raise SystemExit(main())
