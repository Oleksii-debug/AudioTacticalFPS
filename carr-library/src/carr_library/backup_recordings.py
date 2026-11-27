"""Passive current-recording receipt and retained-PCM validation for backups."""
from pathlib import Path
import re

from .ingest.restore import RestoreError

_RECORDING = re.compile(r'state/studio/recordings/([0-9a-f]{32})\.(json|wav)\Z')


def validate_collection(stage, entries, *, cancelled=lambda: False):
    """Validate receipt-bearing recordings without capture, playback or repair.

    WAV originals without a receipt and prepared intents without a WAV remain
    inert recovery data. A completed receipt must have its exact retained WAV;
    metadata cannot bless missing or changed
    PCM. The existing producer verifies both whole-file and segment hashes.
    """
    from .audio.models import AudioCancelled
    from .audio.recording import (_read_recording, validate_recording_receipt,
                                 verify_recording_segments)
    from .backup_sidecars import _check_cancelled, read_regular
    from .durable_state import _unique

    names = {entry['filename'] for entry in entries}
    for name in sorted(names):
        _check_cancelled(cancelled)
        match = _RECORDING.fullmatch(name)
        if match is None or match[2] != 'json':
            continue
        identity = match[1]
        wav = 'state/studio/recordings/' + identity + '.wav'
        try:
            raw = read_regular(Path(stage) / name, 2 * 1024**2)
            # Decode only the already captured bounded bytes, using the shared
            # strict producer validator before touching the retained WAV.
            import json
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON value.')))
            validate_recording_receipt(value, recording_id=identity)
            if wav not in names:
                if value.get('status') == 'prepared':
                    continue  # Retained recovery intent, without audio authority.
                raise RestoreError('A retained recording receipt is missing its exact PCM WAV.')
            audio_path = Path(stage) / wav
            _read_recording(audio_path, value['audio'], value['limits']['max_bytes'], cancelled)
            verify_recording_segments(audio_path, value, cancelled=cancelled)
        except AudioCancelled:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RestoreError('A retained recording differs from its receipt: ' + str(exc)) from exc
