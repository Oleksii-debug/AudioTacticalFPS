"""Headless local transcription using the same model/session as the desktop."""
import argparse
import json
from pathlib import Path
import sys
from threading import Event


def main(argv=None):
    parser = argparse.ArgumentParser(description='Resonance: local transcription and existing model registration')
    commands = parser.add_subparsers(dest='command', required=True)
    register = commands.add_parser('register-model', help='Register an existing local model without downloading it')
    register.add_argument('directory', type=Path)
    register.add_argument('--name', required=True, help='Your descriptive name for this local model')
    register.add_argument('--license', dest='license_name', required=True, help='Licence of the selected model weights')
    register.add_argument('--accept-license', action='store_true', help='Confirm that you reviewed this model licence')
    register.add_argument('--trust-source', action='store_true', help='Confirm that you trust these installed model files')
    transcribe = commands.add_parser('transcribe', help='Transcribe selected local audio/video files')
    transcribe.add_argument('inputs', nargs='+', type=Path)
    transcribe.add_argument('--model-manifest', required=True, type=Path)
    transcribe.add_argument('--output', required=True, type=Path, help='New or existing dedicated transcription result folder')
    transcribe.add_argument('--language', default='auto', help='Source language code, for example uk, or auto')
    transcribe.add_argument('--task', choices=('transcribe', 'translate'), default='transcribe',
                            help='translate is Whisper speech translation to English only')
    transcribe.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    transcribe.add_argument('--compute-type', choices=('int8', 'float32', 'float16', 'int8_float16'), default='int8')
    transcribe.add_argument('--beam-size', type=int, default=5)
    transcribe.add_argument('--word-timestamps', action='store_true')
    transcribe.add_argument('--no-vad', action='store_true')
    transcribe.add_argument('--format', choices=('txt', 'srt', 'vtt', 'json'), action='append', dest='formats')
    args = parser.parse_args(argv)
    progress = lambda message: print(message, file=sys.stderr, flush=True)
    receipts = []
    try:
        if args.command == 'register-model':
            from .audio.transcription_models import register_local_model
            result = register_local_model(args.directory, model_id=args.name, license_name=args.license_name,
                license_accepted=args.accept_license, trusted_weights=args.trust_source, on_status=progress)
        else:
            from .audio.transcription import TranscriptionSession, TranscriptionSettings, receipt_notes
            settings = TranscriptionSettings(language=args.language, task=args.task,
                beam_size=args.beam_size, vad_filter=not args.no_vad,
                word_timestamps=args.word_timestamps, device=args.device, compute_type=args.compute_type)
            session = TranscriptionSession(args.model_manifest)
            cancelled = Event()
            for source in args.inputs:
                receipt = session.transcribe(source, args.output, settings=settings,
                    formats=tuple(dict.fromkeys(args.formats or ('txt', 'srt', 'vtt', 'json'))),
                    cancelled=cancelled.is_set, on_status=progress)
                if receipt.get('status') != 'complete':
                    raise ValueError('The service did not confirm a complete transcript.')
                receipts.append(receipt)
                for warning in receipt_notes(receipt):
                    progress(warning)
            result = {'status': 'complete', 'results': receipts, 'network_access': False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('status') == 'cleanup_required' else 0
    except KeyboardInterrupt:
        if receipts:
            print(json.dumps({'status': 'interrupted', 'results': receipts, 'network_access': False},
                             ensure_ascii=False, indent=2))
        print('Transcription interrupted. No incomplete transcript is reported as complete.', file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, ImportError) as exc:
        if receipts:
            print(json.dumps({'status': 'partial', 'results': receipts, 'network_access': False},
                             ensure_ascii=False, indent=2))
        print(f'Resonance transcription: {exc}', file=sys.stderr)
        for note in getattr(exc, '__notes__', ()):
            progress(str(note))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
