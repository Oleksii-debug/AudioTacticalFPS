"""Read-only preflight for a VoiceWorkshop corpus before local export and training."""
from pathlib import Path
import re
import unicodedata
import wave

from ..durable_state import digest_file, local_path, read_json_snapshot, regular


_MAX_CLIPS = 24_000
_MAX_AUDIO_BYTES = 8 * 1024**3


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or '\x00' in value:
        raise ValueError('A reviewed clip needs a valid ID, speaker and recording session.')
    return unicodedata.normalize('NFC', value.strip())


def audit_voiceworkshop_review(review, audio_root, speaker):
    """Check one speaker, session/source separation and actual WAV lengths.

    VoiceWorkshop still prepares clips, exports training rows and runs adaptation.
    The report contains no paths, transcripts, speaker name or per-clip audio hashes.
    """
    speaker_key = _identifier(speaker).casefold()
    root = local_path(audio_root, exists=True)
    regular(root, directory=True)
    rows, manifest_sha = read_json_snapshot(review, max_bytes=16 * 1024**2)
    if not isinstance(rows, list) or not 1 <= len(rows) <= _MAX_CLIPS:
        raise ValueError('Choose a nonempty VoiceWorkshop review with at most 24,000 rows.')

    identifiers, sources, seen_audio = set(), {}, {}
    sessions = {}
    ignored = duplicates = audio_bytes = 0
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f'Review row {index} must be an object.')
        if not all(row.get(key) is True for key in ('speaker_verified', 'text_verified', 'clean_verified')):
            ignored += 1
            continue
        candidate = _identifier(row.get('speaker')).casefold()
        if candidate != speaker_key:
            raise ValueError(f'Review row {index} contains a different verified speaker.')
        identifier, session = _identifier(row.get('id')), _identifier(row.get('session'))
        if identifier in identifiers:
            raise ValueError(f'Review row {index} repeats a clip ID.')
        identifiers.add(identifier)
        source_hash = row.get('source_sha256')
        if not isinstance(source_hash, str) or re.fullmatch(r'[0-9a-f]{64}', source_hash) is None:
            raise ValueError(f'Review row {index} must retain the VoiceWorkshop recording digest.')
        if sources.setdefault(source_hash, session) != session:
            raise ValueError(f'Review row {index} assigns one recording to multiple sessions.')
        transcript = row.get('text')
        if not isinstance(transcript, str) or not transcript.strip() or '\x00' in transcript:
            raise ValueError(f'Review row {index} has no usable verbatim transcript.')

        audio_path = row.get('audio_path')
        if (not isinstance(audio_path, str) or not audio_path or '\x00' in audio_path
                or audio_path.startswith(('//', '\\\\'))):
            raise ValueError(f'Review row {index} needs a local WAV clip.')
        path = local_path(root / Path(audio_path), root=root, exists=True)
        before = regular(path)
        audio_bytes += before.st_size
        if audio_bytes > _MAX_AUDIO_BYTES:
            raise ValueError('The reviewed WAV files exceed the 8 GiB preflight read budget.')
        try:
            with wave.open(str(path), 'rb') as clip:
                rate, frames = clip.getframerate(), clip.getnframes()
                if (clip.getnchannels() not in (1, 2) or clip.getsampwidth() not in (1, 2, 3, 4)
                        or not 8000 <= rate <= 192000 or not rate <= frames <= rate * 30
                        or frames * clip.getnchannels() * clip.getsampwidth() > before.st_size):
                    raise ValueError(f'Review row {index} must be a 1–30 second supported PCM WAV.')
        except (wave.Error, EOFError) as exc:
            raise ValueError(f'Review row {index} could not be read as PCM WAV.') from exc
        checksum = digest_file(path)
        after = regular(path)
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f'The WAV for review row {index} changed during preflight.')
        if checksum in seen_audio:
            if seen_audio[checksum] != session:
                raise ValueError(f'Review row {index} duplicates a WAV file in a different session.')
            duplicates += 1
            continue
        seen_audio[checksum] = session
        counts = sessions.setdefault(session, [0, 0.0])
        counts[0] += 1
        counts[1] += frames / rate

    if len(sessions) < 2:
        raise ValueError('At least two independent recording sessions with reviewed clips are required.')
    return {
        'schema': 1,
        'review_sha256': manifest_sha,
        'reviewed_unique_clips': len(seen_audio),
        'unapproved_rows': ignored,
        'duplicates_within_sessions': duplicates,
        'sessions': [{'clips': clips, 'seconds': round(seconds, 3)} for clips, seconds in sessions.values()],
        'total_seconds': round(sum(seconds for _, seconds in sessions.values()), 3),
        'training_or_listening_quality_verified': False,
    }
