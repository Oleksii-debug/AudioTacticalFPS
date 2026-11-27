"""Validate retained Studio media within the existing sealed state snapshot.

Recordings are local originals. Publication receipts are inert declarations;
their recorded external output and source-library paths are never opened here.
"""
from pathlib import Path
import re

from ..durable_state import read_json

_RECORDING = re.compile(r'studio/recordings/([0-9a-f]{32})\.json', re.IGNORECASE)
_PUBLICATION = re.compile(r'studio/publications/([0-9a-f]{32})\.json', re.IGNORECASE)


def validate_media_snapshot(root, entries, *, cancelled=lambda: False):
    """Check typed receipts against only the files declared in this snapshot."""
    root = Path(root)
    by_name = {entry['filename']: entry for entry in entries}
    for name in sorted(by_name):
        if cancelled():
            raise InterruptedError('Studio media verification cancelled.')
        recording, publication = _RECORDING.fullmatch(name), _PUBLICATION.fullmatch(name)
        if recording:
            from ..audio.recording import validate_recording_receipt, verify_recording_audio
            identity = recording[1]
            if name != f'studio/recordings/{identity}.json' or identity != identity.lower():
                raise ValueError('A recording receipt must use its exact portable filename.')
            receipt = validate_recording_receipt(read_json(root / name), recording_id=identity)
            audio_name = f'studio/recordings/{identity}.wav'
            entry = by_name.get(audio_name)
            if (entry is None or entry['sha256'] != receipt['audio']['sha256']
                    or entry['size_bytes'] != receipt['audio']['size_bytes']):
                raise ValueError('A recording receipt needs its exact retained original audio.')
            verify_recording_audio(root / audio_name, receipt, cancelled=cancelled)
        elif publication:
            from .publishing import validate_publication_receipt
            identity = publication[1]
            if name != f'studio/publications/{identity}.json' or identity != identity.lower():
                raise ValueError('A publication receipt must use its exact portable filename.')
            validate_publication_receipt(read_json(root / name), publication_id=identity)
