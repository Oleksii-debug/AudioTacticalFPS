"""Publish canonical reading-copy speech only after final source authorization."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from ..speech._io import check_cancelled, digest, local_file, new_output
from ..speech.live_adapter import SpeechRequest, render_speech
from ..speech.runtime import SpeechResult


def export_document_speech(sequence, destination, *, start, end, voice_id,
                           rate=1.0, volume=1.0, cancelled=lambda: False,
                           render=render_speech):
    """Use the shared renderer and atomic writer, with no premature output.

    The caller supplies an immutable canonical DocumentSequence snapshot.
    Rendered audio stays in the writer's private staging directory until the
    exact original source and its current authorization have been rechecked.
    """
    section = sequence.load(1, cancelled)
    if (type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(section.text)
            or not section.text[start:end].strip()):
        raise ValueError('Choose a nonempty exact reading-copy range to export.')

    receipt = None
    maximum = 512 * 1024 * 1024

    def checked_cancelled():
        check_cancelled(cancelled)
        if receipt is None:
            sequence.check(section, cancelled)
        else:
            # new_output checks this callback after the completed file has
            # been flushed and immediately before its no-clobber link. A
            # cheap database digest check cannot detect changed retained bytes.
            audio = local_file(staged)
            if audio.stat().st_size > maximum or digest(audio, cancelled=cancelled) != receipt.sha256:
                raise ValueError('The staged WAV no longer matches its rendering receipt.')
            sequence.check(section, cancelled, full=True)
        check_cancelled(cancelled)
        return False

    request = SpeechRequest(section.text[start:end], voice_id, rate=rate, volume=volume)
    with new_output(destination, cancelled=checked_cancelled) as staged:
        result = render(request, staged, cancelled=checked_cancelled, timeout_seconds=600,
                        max_output_bytes=maximum, configuration_root=sequence.root)
        if (not isinstance(result, SpeechResult) or result.path != staged
                or result.voice_id != request.voice_id
                or result.text_sha256 != sha256(request.text.encode('utf-8')).hexdigest()):
            raise ValueError('The rendering receipt does not match the requested text, voice or staging file.')
        receipt = result
    return replace(result, path=Path(destination).absolute())
