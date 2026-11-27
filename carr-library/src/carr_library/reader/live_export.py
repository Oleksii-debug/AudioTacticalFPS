"""Publish canonical reading-copy speech only after final source authorization."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import wave

from ..speech._io import check_cancelled, digest, local_file, new_output
from ..speech.live_adapter import SpeechRequest, render_speech
from ..speech.runtime import SpeechResult, _wave_info


def export_document_speech(sequence, destination, *, start, end, voice_id,
                           rate=1.0, volume=1.0, cancelled=lambda: False,
                           render=render_speech):
    """Use the shared renderer and atomic writer, with no premature output.

    The caller supplies an immutable canonical DocumentSequence snapshot.
    Rendered audio stays in the writer's private staging directory until the
    exact original source and its current authorization have been rechecked.
    """
    # A replacement reading copy can have the same displayed text but a
    # different (or absent) authenticated source. Pin the actual snapshot,
    # not just the selected string, across rendering and final publication.
    document = sequence.document
    locations = sequence.source_locations
    root = sequence.root
    snapshot = document.to_dict()

    def check_binding(*, full=False):
        if (sequence.document is not document or sequence.source_locations != locations
                or sequence.root != root or document.revision != snapshot['revision']
                or (full and document.to_dict() != snapshot)):
            raise ValueError('The reading-copy source binding changed during export.')

    section = sequence.load(1, cancelled)
    check_binding(full=True)
    if (type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(section.text)
            or not section.text[start:end].strip()):
        raise ValueError('Choose a nonempty exact reading-copy range to export.')

    receipt = None
    maximum = 512 * 1024 * 1024

    def checked_cancelled():
        check_cancelled(cancelled)
        check_binding()
        sequence.check(section, cancelled)
        check_binding()
        check_cancelled(cancelled)
        return False

    def attest_output(staged):
        # Validate the flushed output once at the shared publication boundary.
        # A valid byte digest alone cannot establish PCM geometry or duration.
        check_cancelled(cancelled)
        audio = local_file(staged)
        if audio.stat().st_size > maximum or digest(audio, cancelled=cancelled) != receipt.sha256:
            raise ValueError('The staged WAV no longer matches its rendering receipt.')
        try:
            rate, channels, frames, _ = _wave_info(
                audio, cancelled=cancelled, max_output_bytes=maximum)
        except (wave.Error, EOFError, ValueError) as exc:
            raise ValueError('The staged WAV is not valid PCM for its rendering receipt.') from exc
        if (any(type(value) is not int for value in
                (receipt.sample_rate, receipt.channels, receipt.frames, receipt.chunks))
                or (receipt.sample_rate, receipt.channels, receipt.frames) != (rate, channels, frames)
                or receipt.chunks < 1
                or type(receipt.duration_seconds) not in (float, int)
                or receipt.duration_seconds != frames / rate):
            raise ValueError('The staged WAV geometry or duration does not match its rendering receipt.')
        # A cheap database digest check cannot detect changed retained bytes.
        check_binding(full=True)
        sequence.check(section, cancelled, full=True)
        check_binding(full=True)
        check_cancelled(cancelled)

    request = SpeechRequest(section.text[start:end], voice_id, rate=rate, volume=volume)
    with new_output(destination, cancelled=checked_cancelled,
                    before_publish=attest_output) as staged:
        result = render(request, staged, cancelled=checked_cancelled, timeout_seconds=600,
                        max_output_bytes=maximum, configuration_root=sequence.root)
        if (not isinstance(result, SpeechResult) or result.path != staged
                or result.voice_id != request.voice_id
                or result.text_sha256 != sha256(request.text.encode('utf-8')).hexdigest()):
            raise ValueError('The rendering receipt does not match the requested text, voice or staging file.')
        receipt = result
    return replace(result, path=Path(destination).absolute())
