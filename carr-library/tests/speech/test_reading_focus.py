"""Visual pacing is explicit; audio positions are measured-duration estimates."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.speech import focus
from carr_library.speech.focus import FocusPlan, ReadingAudio, VisualPacer
from carr_library.speech.reading import ReadingDocument


def audio(doc, start=0, end=None):
    end = len(doc.text) if end is None else end
    text = doc.text[start:end]
    result = SimpleNamespace(path=Path('original.wav'), frames=240000, sample_rate=24000,
                             text_sha256=sha256(text.encode('utf-8')).hexdigest())
    return ReadingAudio.from_render(doc, doc._signature(), start, end, text, result)


def test_visual_plan_preserves_unicode_source_and_passive_resume_changes():
    doc = ReadingDocument('😀 One\r\nДругий рядок. Three.')
    plan = FocusPlan(doc)
    assert [plan.text[a:b] for a, b in plan.spans] == ['One', 'Другий', 'рядок', 'Three']
    assert plan.index_at(0) == 0 and plan.index_at(len(doc.text)) == 3
    assert 'exact text mapping' in plan.source_label(plan.spans[1])
    original = doc.source_text
    from dataclasses import replace
    from carr_library.speech.session import ReaderResume
    initial = ReaderResume(doc, cursor=0)
    changed = replace(initial, cursor=2, volume=60)
    assert changed.document is doc and initial.cursor == 0
    plan.check(changed.document)
    doc.set_text('Changed reading copy.')
    with pytest.raises(ValueError, match='changed'):
        plan.check(doc)
    assert doc.source_text == original


def test_visual_plan_refuses_other_tab_even_when_text_is_identical():
    first, second = ReadingDocument('Same text.'), ReadingDocument('Same text.')
    with pytest.raises(ValueError, match='tab changed'):
        FocusPlan(first).check(second)


def test_visual_plan_bounds_unit_count_and_requires_readable_words(monkeypatch):
    monkeypatch.setattr(focus, 'MAX_FOCUS_UNITS', 3)
    with pytest.raises(ValueError, match='chapter'):
        FocusPlan(ReadingDocument('one two three four'))
    with pytest.raises(ValueError, match='no word'):
        FocusPlan(ReadingDocument('   😀 !!!'))


def test_visual_pacer_starts_pauses_moves_and_finishes_without_external_effects():
    doc = ReadingDocument('one two three four five')
    before = doc.to_dict()
    pace = VisualPacer(FocusPlan(doc))
    assert not pace.running and pace.tick(100) == 0
    pace.start(100, 120)
    assert pace.tick(100.6) == 1
    pace.pause(101.1)
    assert not pace.running and pace.index == 2
    assert pace.tick(999) == 2
    assert pace.move(-1) == 1 and not pace.running
    pace.start(1000, 120)
    assert pace.tick(1010) == 4 and not pace.running
    assert pace.move(1) == 4
    assert doc.to_dict() == before


@pytest.mark.parametrize('speed', [True, 0, 39, 1001, '250', float('nan'), float('inf')])
def test_visual_speed_requires_bounded_finite_reviewed_number(speed):
    pace = VisualPacer(FocusPlan(ReadingDocument('one two')))
    with pytest.raises(ValueError):
        pace.start(10, speed)
    assert not pace.running and pace.index == 0


def test_visual_clock_cannot_move_backwards_or_restore_a_saved_running_timer():
    doc = ReadingDocument('one two three')
    pace = VisualPacer(FocusPlan(doc))
    pace.start(20, 250)
    with pytest.raises(ValueError, match='backwards'):
        pace.tick(19)
    reopened = VisualPacer(FocusPlan(ReadingDocument.from_dict(doc.to_dict())))
    assert not reopened.running and reopened.index == 0


def test_audio_follow_uses_actual_pcm_duration_and_stays_inside_rendered_selection():
    doc = ReadingDocument('😀 Alpha beta gamma delta.')
    plan = FocusPlan(doc)
    binding = audio(doc, 4, 18)
    assert binding.duration_seconds == 10
    starts = []
    for seconds in (0, 1, 3, 5, 8, 10, 100):
        index, span = binding.span_at(plan, Path('original.wav'), seconds)
        assert 4 <= span[0] < span[1] <= 18
        starts.append(span[0])
    assert starts == sorted(starts)
    assert doc.source_text == doc.text


@pytest.mark.parametrize('change', ['text', 'checksum', 'zero_frames', 'bool_frames', 'zero_rate'])
def test_audio_binding_requires_the_actual_rendered_reading_snapshot(change):
    doc = ReadingDocument('Original text.')
    signature, text = doc._signature(), doc.text
    result = SimpleNamespace(path='recording.wav', frames=100, sample_rate=10,
        text_sha256=sha256(text.encode('utf-8')).hexdigest())
    if change == 'text': doc.set_text('Changed text.')
    elif change == 'checksum': result.text_sha256 = '0' * 64
    elif change == 'zero_frames': result.frames = 0
    elif change == 'bool_frames': result.frames = True
    else: result.sample_rate = 0
    with pytest.raises(ValueError):
        ReadingAudio.from_render(doc, signature, 0, len(doc.text), text, result)


def test_audio_binding_rejects_other_audio_other_tab_and_changed_text():
    doc = ReadingDocument('Original exact text.')
    binding, plan = audio(doc), FocusPlan(doc)
    with pytest.raises(ValueError, match='audio'):
        binding.span_at(plan, Path('other.wav'), 1)
    with pytest.raises(ValueError, match='changed'):
        binding.span_at(FocusPlan(ReadingDocument(doc.text)), binding.path, 1)
    doc.set_text('Changed exact text.')
    with pytest.raises(ValueError, match='changed'):
        binding.span_at(plan, binding.path, 1)


@pytest.mark.parametrize('text', ['a' * 200000, 'a-' * 100000 + 'a'],
                         ids=['letters', 'internal-hyphen-jumps'])
def test_word_preparation_observes_cancel_inside_a_long_single_word(text):
    calls = []
    def stopped():
        calls.append(True)
        return len(calls) > 2
    with pytest.raises(InterruptedError, match='cancelled'):
        FocusPlan(ReadingDocument(text), cancelled=stopped)
    assert len(calls) == 3


def test_caption_only_punctuation_and_foreign_boundaries_are_not_language_alignment():
    doc = ReadingDocument("П’ять café e\u0301 test-word 123 中文。")
    plan = FocusPlan(doc)
    assert [plan.text[a:b] for a, b in plan.spans] == ['П’ять', 'café', 'e\u0301', 'test-word', '123', '中文']
    assert doc.text == doc.source_text


def test_identical_text_in_another_tab_cannot_accept_an_old_edit_preview():
    from carr_library.speech.reading_tools import ReadingSnapshot, ReadingPreview
    first, second = ReadingDocument('Identical'), ReadingDocument('Identical')
    snapshot = ReadingSnapshot.capture(first)
    preview = ReadingPreview(snapshot, 0, 9, 'Reviewed only for first', 'Tab-bound edit', 1)
    with pytest.raises(ValueError, match='changed'):
        preview.apply(second)
    assert first.text == second.text == 'Identical'
    assert preview.apply(first)
    assert first.source_text == second.source_text == 'Identical'


@pytest.mark.parametrize('change', ['wrong-offset', 'outside-frames', 'reversed-order', 'untyped'])
def test_measured_pause_binding_refuses_an_unrelated_or_untyped_map(change):
    from carr_library.speech.runtime import SpeechPause
    document = ReadingDocument('First. Second. Third.')
    text = document.text
    first = SpeechPause(7, 100, 200, 'sentence')
    second = SpeechPause(15, 300, 400, 'sentence')
    pauses = (first, second)
    if change == 'wrong-offset':
        pauses = (SpeechPause(len(text), 100, 200, 'sentence'),)
    elif change == 'outside-frames':
        pauses = (SpeechPause(7, 100, 1000, 'sentence'),)
    elif change == 'reversed-order':
        pauses = (second, first)
    else:
        pauses = ({'text_offset': 7, 'start_frame': 100, 'end_frame': 200, 'kind': 'sentence'},)
    result = SimpleNamespace(path='measured.wav', frames=1000, sample_rate=8000,
                             text_sha256=sha256(text.encode('utf-8')).hexdigest(), pauses=pauses)
    with pytest.raises(ValueError, match='pause map'):
        ReadingAudio.from_render(document, document._signature(), 0, len(text), text, result)
