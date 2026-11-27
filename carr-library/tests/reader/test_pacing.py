"""Exact source spans, finite pacing and delayed-GUI behavior."""
from hashlib import sha256
import pytest

from carr_library.reader.pacing import MAX_TEXT_CHARACTERS, MAX_TOKENS, PaceOptions, PacedText


def test_unicode_crlf_selection_is_exact_and_original_bytes_never_change():
    text = 'Outside 😀.\r\nFirst  e\u0301\t最後。  tail.\r\nOutside again.'
    start, end = text.index('First'), text.index('Outside again.')
    reader = PacedText(text, start, end, PaceOptions(240, 2))
    before = text.encode('utf-8')
    first = reader.play(0)
    assert first.text == 'First  e\u0301' and first.text == text[first.start:first.end]
    second = reader.tick(.5)
    assert second.text == '最後。  tail.' and second.text == text[second.start:second.end]
    assert reader.tick(1) is None and reader.state == 'complete'
    assert reader.source_sha256 == sha256(before).hexdigest() and text.encode('utf-8') == before


def test_slow_gui_never_skips_groups_or_catches_up_without_display_time():
    reader = PacedText('one two three four', 0, 18, PaceOptions(1200, 1))
    seen = [reader.play(0).text]
    seen.append(reader.tick(90).text)
    assert reader.tick(90.001) is None
    seen.append(reader.tick(91).text)
    seen.append(reader.tick(300).text)
    assert reader.tick(301) is None and reader.state == 'complete'
    assert seen == ['one', 'two', 'three', 'four']


def test_pause_manual_steps_rewind_and_restart_have_explicit_positions():
    reader = PacedText('a b c', 0, 5, PaceOptions(60, 1))
    assert reader.play(10).text == 'a'
    reader.pause()
    assert reader.tick(1000) is None
    assert reader.play(1001).text == 'a'
    assert reader.tick(1001.5) is None
    assert reader.step(1).text == 'b' and reader.state == 'paused'
    assert reader.step(-1).text == 'a'
    assert reader.step(-1).text == 'a'
    reader.stop()
    assert reader.current is None and reader.state == 'ready' and reader.deadline is None
    assert reader.step(1).text == 'a'
    reader.play(2000)
    reader.tick(2001); reader.tick(2002); reader.tick(2003)
    assert reader.state == 'complete'
    assert reader.play(3000).text == 'a'


def test_last_short_group_gets_its_own_exact_word_duration():
    reader = PacedText('a b c', 0, 5, PaceOptions(60, 2))
    assert reader.play(0).words == 2 and reader.deadline == 2
    assert reader.tick(2).words == 1 and reader.deadline == 3
    assert reader.tick(3) is None and reader.state == 'complete'


@pytest.mark.parametrize('value', [True, 39, 1201, 240.0, '240'])
def test_invalid_rates_are_rejected(value):
    with pytest.raises(ValueError):
        PaceOptions(words_per_minute=value)


@pytest.mark.parametrize('value', [True, 0, 6, 2.0])
def test_invalid_group_sizes_are_rejected(value):
    with pytest.raises(ValueError):
        PaceOptions(words_per_group=value)


@pytest.mark.parametrize('start,end', [(True, 3), (0, True), (-1, 3), (2, 1), (0, 4), (1, 1)])
def test_invalid_source_ranges_are_rejected(start, end):
    with pytest.raises(ValueError):
        PacedText('a b', start, end)


@pytest.mark.parametrize('text', ['', '   ', 'a\0b', '\ud800', 'x' * (MAX_TEXT_CHARACTERS + 1),
                                  'x ' * (MAX_TOKENS + 1)],
                         ids=('empty', 'whitespace', 'nul', 'surrogate', 'character-budget', 'token-budget'))
def test_empty_invalid_or_oversized_input_never_creates_a_presentation(text):
    with pytest.raises(ValueError):
        PacedText(text, 0, len(text))


@pytest.mark.parametrize('now', [True, -1, float('nan'), float('inf'), 10**12, '0'])
def test_presentation_time_is_bounded_and_typed(now):
    reader = PacedText('a', 0, 1)
    with pytest.raises(ValueError):
        reader.play(now)
    assert reader.state == 'ready' and reader.current is None
