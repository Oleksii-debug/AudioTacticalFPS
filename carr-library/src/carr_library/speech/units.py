"""Finite reading-unit navigation over exact text; no linguistic inference."""
from bisect import bisect_right
from dataclasses import dataclass
from hashlib import sha256

from ._io import check_cancelled, text_value

KINDS = ('word', 'sentence', 'paragraph')
NOTICES = {
    'word': 'Words use Unicode letters/marks and internal apostrophes or hyphens. Numbers and symbols need another unit.',
    'sentence': 'Sentences use the existing punctuation-plus-whitespace rules; abbreviations and unspaced text may differ.',
    'paragraph': 'Paragraphs use stored line breaks, not visual wrapping or inferred document structure.',
}


@dataclass(frozen=True)
class UnitPlan:
    kind: str
    text_sha256: str
    length: int
    ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class SelectedUnit:
    kind: str
    start: int
    end: int
    index: int
    total: int
    text: str
    text_sha256: str

    @property
    def notice(self):
        return NOTICES[self.kind]


def plan_units(text, kind, *, cancelled=lambda: False):
    """Reuse the existing bounded spelling and conversion boundary planners."""
    check_cancelled(cancelled)
    text_value(text, 'Reading text', empty=True)
    if kind not in KINDS:
        raise ValueError('Choose word, sentence or paragraph navigation.')
    if not text.strip():
        raise ValueError('The reading copy has no nonempty text unit.')
    if kind == 'word':
        from .spelling import word_ranges
        _, words = word_ranges(text, cancelled=cancelled)
        ranges = tuple((start, end) for start, end, _ in words)
    else:
        from ..conversion.planning import SplitOptions, plan_split
        from .reading import ReadingDocument
        split = plan_split(ReadingDocument(text),
            SplitOptions(mode='sentences' if kind == 'sentence' else 'paragraphs'),
            source_name='reader-navigation', cancelled=cancelled)
        ranges = []
        for part in split.parts:
            check_cancelled(cancelled)
            start, end = part.start, part.end
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if start < end:
                ranges.append((start, end))
        ranges = tuple(ranges)
    check_cancelled(cancelled)
    if not ranges:
        raise ValueError('No letter-based words were found. Choose sentence or paragraph explicitly.')
    return UnitPlan(kind, sha256(text.encode('utf-8')).hexdigest(), len(text), ranges)


def choose_unit(plan, text, caret, selection, *, step=0):
    """Select current/previous/next without wrapping or silently widening scope.

    An exact existing unit selection is the anchor. Otherwise use the selection
    start, or the caret when selection is empty. At document end only, an exact
    final unit boundary remains current. Whitespace/symbol gaps have no current
    unit; Previous and Next choose an explicitly adjacent unit.
    """
    if (not isinstance(plan, UnitPlan) or plan.kind not in KINDS or plan.length != len(text)
            or plan.text_sha256 != sha256(text.encode('utf-8')).hexdigest()
            or not isinstance(plan.ranges, tuple) or not 1 <= len(plan.ranges) <= 20_000):
        raise ValueError('The reading-unit plan is stale or invalid. Select the current reading copy again.')
    if (type(caret) is not int or not 0 <= caret <= len(text)
            or type(selection) is not tuple or len(selection) != 2
            or any(type(value) is not int for value in selection)
            or not 0 <= selection[0] <= selection[1] <= len(text)
            or type(step) is not int or step not in (-1, 0, 1)):
        raise ValueError('Choose an exact current cursor/selection and previous, current or next unit.')
    previous = 0
    for row in plan.ranges:
        if (type(row) is not tuple or len(row) != 2 or any(type(value) is not int for value in row)
                or not previous <= row[0] < row[1] <= len(text)):
            raise ValueError('The reading-unit ranges are invalid.')
        previous = row[1]
    starts = tuple(row[0] for row in plan.ranges)
    ends = tuple(row[1] for row in plan.ranges)
    anchor = selection[0] if selection[0] != selection[1] else caret
    index = bisect_right(starts, anchor) - 1
    current = (index >= 0 and (anchor < ends[index] or selection == plan.ranges[index]
                              or anchor == len(text) == ends[index]))
    if step == 0:
        if not current:
            raise ValueError(f'No current {plan.kind} at this cursor. Move the cursor or choose Previous/Next explicitly.')
    elif current:
        index += step
    elif step > 0:
        index = bisect_right(starts, anchor)
    else:
        index = bisect_right(ends, anchor) - 1
    if not 0 <= index < len(plan.ranges):
        raise ValueError(f'There is no {"next" if step > 0 else "previous"} {plan.kind}. Navigation does not wrap.')
    start, end = plan.ranges[index]
    selected = text[start:end]
    if not selected.strip():
        raise ValueError('The selected text unit is empty.')
    return SelectedUnit(plan.kind, start, end, index + 1, len(plan.ranges), selected, plan.text_sha256)

