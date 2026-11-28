"""CLI argument glue for the same reviewed conversion batch used by the GUI."""
from pathlib import Path
import json
import sys

from ..audio.conversion import ConversionSettings
from ..audio.conversion_batch import AudioConversionBatch, FORMATS
from ..audio.effect_presets import read_effect
from ..audio.models import AudioBudget
from ..durable_state import read_json


def add_arguments(parser):
    parser.add_argument('--input', action='append', type=Path, required=True,
                        help='Explicit source file; repeat in the required output order (maximum 64)')
    parser.add_argument('--output-dir', type=Path, required=True,
                        help='Existing external folder; all output filenames are create-only')
    parser.add_argument('--format', choices=sorted(value[1:] for value in FORMATS), default='wav')
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--duration', type=float)
    parser.add_argument('--sample-rate', type=int, default=24000)
    parser.add_argument('--channels', type=int, default=2)
    parser.add_argument('--bit-depth', type=int, choices=(16, 24, 32), default=16)
    parser.add_argument('--bitrate', type=int, default=192)
    parser.add_argument('--effects-json', type=Path,
                        help='Explicit UTF-8 JSON list of typed effects; no arbitrary filter expressions')
    parser.add_argument('--max-bytes', type=int, default=32 * 1024**3,
                        help='Aggregate completed output allowance; each conversion keeps its existing staging limits')
    parser.add_argument('--timeout', type=float, default=3600,
                        help='Existing per-file codec time allowance in seconds')
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--resume', action='store_true',
                        help='Re-review the exact saved batch; never infer success from an intent or matching bytes')
    parser.add_argument('--approve', metavar='REVIEW_SHA256',
                        help='Run only if a fresh review matches this exact fingerprint; omission reviews without encoding')


def execute(args, app):
    effects = []
    if args.effects_json is not None:
        effects = read_json(args.effects_json, maximum=64 * 1024)
        if type(effects) is not list or len(effects) > 32:
            raise ValueError('The selected effects file must contain at most 32 typed effects.')
    settings = ConversionSettings(start=args.start, duration=args.duration,
        sample_rate=args.sample_rate, channels=args.channels, bit_depth=args.bit_depth,
        bitrate=args.bitrate, effects=tuple(read_effect(value) for value in effects))
    budget = AudioBudget(max_output_bytes=args.max_bytes, timeout=args.timeout, threads=args.threads)
    service = AudioConversionBatch(app.root)
    review = service.prepare(args.input, args.output_dir, format=args.format, settings=settings,
                             budget=budget, resume=args.resume)
    if args.approve is None:
        result = review.to_dict()
        result['review_only'] = True
        code = 0
    else:
        if args.approve != review.fingerprint:
            raise ValueError('The explicit files, settings, runtime or output changed. Review the new fingerprint.')
        result = service.run(review, progress=lambda done, total, phase:
            print(f'{done}/{total}: {phase}', file=sys.stderr, flush=True))
        code = 0 if result['status'] == 'completed' else (130 if result['status'] == 'cancelled' else 2)
    # JSON remains valid on a Windows redirected pipe with a legacy code page.
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
    return code
