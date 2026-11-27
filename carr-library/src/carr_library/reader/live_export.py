"""Publish canonical reading-copy speech only after final source authorization."""
from dataclasses import replace
from pathlib import Path

from ..speech._io import check_cancelled, new_output
from ..speech.live_adapter import SpeechRequest, render_speech


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

    def checked_cancelled():
        check_cancelled(cancelled)
        sequence.check(section, cancelled)
        return False

    request = SpeechRequest(section.text[start:end], voice_id, rate=rate, volume=volume)
    with new_output(destination, cancelled=checked_cancelled) as staged:
        result = render(request, staged, cancelled=checked_cancelled, timeout_seconds=600,
                        max_output_bytes=512 * 1024 * 1024, configuration_root=sequence.root)
        sequence.check(section, cancelled, full=True)
    return replace(result, path=Path(destination).absolute())
