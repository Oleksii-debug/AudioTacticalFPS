"""Explicit preparation bound to the current reading copy, voice and selection."""
from dataclasses import dataclass
from hashlib import sha256

from ._io import check_cancelled, text_value
from .reading import ReadingChange, ReadingDocument
from ..reader.speech_normalization import (
    MAX_TEXT_CHARACTERS, SpeechOptions, SpeechPreview, prepare_speech, validate_preview,
)


@dataclass(frozen=True)
class SpeechSelection:
    identity: int
    revision: tuple
    text: str
    selection: tuple[int, int]
    language: str
    voice_id: str
    protected: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class SpeechProposal:
    snapshot: SpeechSelection
    bounds: tuple[int, int]
    result: SpeechPreview


def capture_speech(document, selection, *, language='', voice_id=''):
    if not isinstance(document, ReadingDocument):
        raise ValueError('Open an existing reading copy before preparing speech.')
    if type(selection) is not tuple or len(selection) != 2:
        raise ValueError('Choose an exact current reading range.')
    document._range(*selection)
    text_value(language, 'reader language', maximum=128, empty=True)
    text_value(voice_id, 'reader voice ID', maximum=2048, empty=True)
    return SpeechSelection(id(document), document._signature(), document.text, selection,
                           language, voice_id, document.changed_spans())


def _bounds(snapshot, bounds):
    if (type(snapshot) is not SpeechSelection or type(bounds) is not tuple or len(bounds) != 2
            or any(type(value) is not int for value in bounds)
            or not 0 <= bounds[0] < bounds[1] <= len(snapshot.text)):
        raise ValueError('Choose a nonempty current reading passage.')
    if bounds[1] - bounds[0] > MAX_TEXT_CHARACTERS:
        raise ValueError('Select at most 200,000 characters for one preparation preview.')


def prepare_selection(snapshot, bounds, options, *, cancelled=lambda: False, timeout=2.0):
    """Reuse the finite parser; previously accepted derivative runs always win."""
    check_cancelled(cancelled)
    _bounds(snapshot, bounds)
    start, end = bounds
    result = prepare_speech(snapshot.text[start:end], options, cancelled=cancelled, timeout=timeout)
    changes, protected_count = [], 0
    for edit in result.edits:
        check_cancelled(cancelled)
        if any(start + edit.start < finish and begin < start + edit.end
               for begin, finish in snapshot.protected):
            protected_count += 1
        else:
            changes.append(edit)
    if protected_count:
        pieces, position = [], 0
        for edit in changes:
            check_cancelled(cancelled)
            pieces.extend((result.source[position:edit.start], edit.after))
            position = edit.end
        pieces.append(result.source[position:])
        output = ''.join(pieces)
        notice = ('Previously edited or pronunciation-adjusted spans were kept exactly. '
                  'Use the existing Pronunciations action after preparation for explicit overrides.')
        result = SpeechPreview(result.source_sha256, sha256(output.encode('utf-8')).hexdigest(),
            result.source, output, result.options, tuple(changes), result.notices + (notice,))
    validate_preview(result)
    check_cancelled(cancelled)
    return SpeechProposal(snapshot, bounds, result)


def apply_speech(document, proposal, current):
    """One atomic compound history action after all current scope checks."""
    if (type(proposal) is not SpeechProposal or type(current) is not SpeechSelection
            or proposal.snapshot != current or id(document) != current.identity
            or document._signature() != current.revision or document.text != current.text
            or document.changed_spans() != current.protected):
        raise ValueError('The reading copy, selection, language or voice changed. Preview again.')
    _bounds(current, proposal.bounds)
    validate_preview(proposal.result)
    start, end = proposal.bounds
    if proposal.result.source != current.text[start:end]:
        raise ValueError('The reviewed proposal belongs to different source text.')
    changes = []
    for edit in proposal.result.edits:
        begin, finish = start + edit.start, start + edit.end
        if any(begin < right and left < finish for left, right in current.protected):
            raise ValueError('The proposal would overwrite an already accepted manual reading change.')
        changes.append(ReadingChange(begin, finish, edit.before, edit.after))
    if not changes:
        raise ValueError('This preview makes no reading-text changes.')
    return document.apply_changes(tuple(changes), label='Reviewed speech preparation')


def undo_speech(document, applied, current):
    if (type(applied) is not SpeechSelection or current != applied
            or id(document) != current.identity or document._signature() != current.revision):
        raise ValueError('The reading copy changed after preparation; use its regular history controls.')
    if not document.undo():
        raise ValueError('The prepared reading change is no longer available to undo.')
