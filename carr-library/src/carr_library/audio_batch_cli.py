"""Headless access to the same durable local audio queue as native Studio."""
import json
from pathlib import Path
import signal
import sys
from threading import Event


def add_arguments(parser, group):
    group.add_argument('--audio-batch-plan', type=Path,
                       help='Create a durable queue from explicit per-item local input/preset JSON; never auto-run it')
    group.add_argument('--audio-batch', metavar='ID',
                       help='Inspect or explicitly control an existing saved audio queue')
    group.add_argument('--audio-batches', action='store_true', help='List durable audio queues in --data-dir')
    parser.add_argument('--audio-batch-action', choices=('create', 'show', 'run', 'resume', 'retry', 'pause', 'cancel', 'export'))
    parser.add_argument('--audio-batch-title', default='Local audio queue', help='Name for a new saved audio queue')
    parser.add_argument('--audio-batch-item', action='append', default=[], help='Exact failed item ID to retry, or completed item ID to export')
    parser.add_argument('--audio-batch-file', help='Exact verified output basename to export')
    parser.add_argument('--audio-batch-export', type=Path, help='Explicit new local export filename; existing files are never replaced')


def requested(args):
    return bool(getattr(args, 'audio_batch_plan', None) or getattr(args, 'audio_batch', None)
                or getattr(args, 'audio_batches', False))


def validate(args, parser):
    if not args.data_dir:
        parser.error('Audio queues require an explicit --data-dir for their retained originals and checkpoints.')
    if args.audio_output or args.voice:
        parser.error('Audio queues use managed outputs and each saved book project voice. Use per-item presets and explicit export filenames.')
    if bool(args.piper_executable) != bool(args.piper_manifest):
        parser.error('Supply both --piper-executable and --piper-manifest.')
    action = args.audio_batch_action or ('create' if args.audio_batch_plan else 'show')
    if args.audio_batch_plan and action != 'create':
        parser.error('--audio-batch-plan creates a reviewable queue; control it separately by its returned ID.')
    if args.audio_batches and (args.audio_batch_action not in (None, 'show')
                               or args.audio_batch_item or args.audio_batch_export or args.audio_batch_file):
        parser.error('--audio-batches lists queues only.')
    if args.audio_batch and action == 'create':
        parser.error('Queue creation requires --audio-batch-plan.')
    if args.audio_preview and args.audio_batch_plan:
        parser.error('Create the retained plan first, then use --audio-batch ID --audio-preview to inspect its exact names.')
    if args.audio_preview and action != 'show':
        parser.error('--audio-preview is read-only; choose the show action.')
    if action in ('create', 'run', 'resume', 'retry') and args.ffmpeg is None:
        parser.error('Select the existing local FFmpeg executable explicitly with --ffmpeg.')
    if args.audio_batch_item and action not in ('retry', 'export'):
        parser.error('--audio-batch-item is used only for explicit retry or export.')
    if action == 'export':
        if len(args.audio_batch_item) != 1 or not args.audio_batch_file or not args.audio_batch_export:
            parser.error('Export requires one --audio-batch-item, its --audio-batch-file and a new --audio-batch-export filename.')
    elif args.audio_batch_export or args.audio_batch_file:
        parser.error('Output export options require --audio-batch-action export.')


def run(args, *, trust_controller=None):
    from .audio.batches import AudioBatchService
    from .audio.batch_models import Selection, exact
    from .audio.jobs import RenderCancelled, read_json
    from .operations import ResourceCancelled
    from .reader.narration_text import read_document, decode_json
    service = AudioBatchService(args.data_dir, ffmpeg=args.ffmpeg, trust_controller=trust_controller)
    if args.dordiai_manifest or args.piper_manifest:
        from .audio.engines import default_registry
        service.registry = default_registry(ffmpeg=args.ffmpeg, dordiai_manifest=args.dordiai_manifest,
                                            piper_executable=args.piper_executable, piper_manifest=args.piper_manifest)
    action = args.audio_batch_action or ('create' if args.audio_batch_plan else 'show')
    if args.audio_batches:
        print(json.dumps([{'id': value.id, 'title': value.title, 'status': value.status,
                           'items': len(value.items), 'plan_sha256': value.plan_sha256}
                          for value in service.list()], ensure_ascii=False, indent=2))
        return 0
    stopped = Event()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        if args.audio_batch_plan:
            raw = decode_json(read_document(args.audio_batch_plan, byte_budget=16 * 1024**2))
            exact(raw, ('schema', 'items'), 'explicit audio queue selection')
            if raw['schema'] != 'carr-audio-batch-selection/1' or type(raw['items']) is not list:
                raise ValueError('Unsupported local audio queue selection.')
            state = service.create(tuple(Selection.from_dict(row) for row in raw['items']),
                                   title=args.audio_batch_title, cancelled=stopped.is_set)
            value = service.describe(state.id)
        elif action == 'show':
            value = service.describe(args.audio_batch)
        elif action in ('pause', 'cancel'):
            value = service.control(args.audio_batch, action)
        elif action == 'export':
            from .research.export_file import capture_export_destination
            approval = capture_export_destination(args.audio_batch_export, private_root=service.library)
            value = service.export_file(args.audio_batch, args.audio_batch_item[0], args.audio_batch_file,
                                        args.audio_batch_export, approval=approval, cancelled=stopped.is_set)
        else:
            state = service.run(args.audio_batch, action=action, retry_items=args.audio_batch_item,
                                cancelled=stopped.is_set,
                                on_status=lambda value: print(value, file=sys.stderr, flush=True))
            value = service.describe(state.id)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        if value.get('status') in ('cancelled', 'paused'):
            return 130
        return 1 if value.get('status') == 'failed' else 0
    except (RenderCancelled, ResourceCancelled, InterruptedError) as error:
        print(json.dumps({'status': 'cancelled', 'message': str(error),
                          'resume': 'Use the saved queue ID and explicit resume; verified completed outputs remain.'},
                         ensure_ascii=False, indent=2))
        return 130
    finally:
        signal.signal(signal.SIGINT, previous)
