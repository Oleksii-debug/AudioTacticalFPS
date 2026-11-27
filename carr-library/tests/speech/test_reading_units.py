"""Exact, finite heuristic units over the unchanged reading text."""
from dataclasses import replace

import pytest

from carr_library.speech.units import choose_unit, plan_units


@pytest.mark.parametrize(('kind', 'expected'), [
    ('word', ('First', 'sentence', 'Second', 'third', 'line')),
    ('sentence', ('First sentence.', 'Second!', 'third line')),
    ('paragraph', ('First sentence. Second!', 'third line')),
])
def test_existing_boundary_planners_produce_exact_nonempty_ranges(kind, expected):
    text = '  First sentence. Second! \r\n\r\nthird line  '
    plan = plan_units(text, kind)
    assert tuple(text[start:end] for start, end in plan.ranges) == expected
    assert all(0 <= start < end <= len(text) for start, end in plan.ranges)


@pytest.mark.parametrize('word', ["п'ять", 'cafe\u0301', 'well-being', 'l’amour', 'Україна', '你好世界', 'λέξη'])
def test_letter_word_boundaries_preserve_original_unicode_and_joiners(word):
    text = '😀 ' + word + ' 42!'
    plan = plan_units(text, 'word')
    selected = choose_unit(plan, text, 3, (3, 3))
    assert selected.text == word and (selected.start, selected.end) == (2, 2 + len(word))
    assert selected.total == 1 and 'Numbers and symbols' in selected.notice


def test_current_unit_does_not_silently_select_a_neighbour_from_a_gap():
    text = 'Alpha   Beta'
    plan = plan_units(text, 'word')
    with pytest.raises(ValueError, match='No current word'):
        choose_unit(plan, text, 6, (6, 6))
    assert choose_unit(plan, text, 6, (6, 6), step=-1).text == 'Alpha'
    assert choose_unit(plan, text, 6, (6, 6), step=1).text == 'Beta'


@pytest.mark.parametrize('kind', ['word', 'sentence', 'paragraph'])
def test_exact_selection_anchors_repeat_previous_next_without_wrap(kind):
    text = 'One.\nTwo.\nThree.'
    plan = plan_units(text, kind)
    first = choose_unit(plan, text, 0, (0, 0))
    second = choose_unit(plan, text, first.end, (first.start, first.end), step=1)
    third = choose_unit(plan, text, second.end, (second.start, second.end), step=1)
    assert second.text.rstrip('.') == 'Two' and third.text.rstrip('.') == 'Three'
    assert choose_unit(plan, text, second.end, (second.start, second.end)).text == second.text
    assert choose_unit(plan, text, third.end, (third.start, third.end), step=-1).text == second.text
    with pytest.raises(ValueError, match='does not wrap'):
        choose_unit(plan, text, first.end, (first.start, first.end), step=-1)
    with pytest.raises(ValueError, match='does not wrap'):
        choose_unit(plan, text, third.end, (third.start, third.end), step=1)


def test_document_end_is_current_only_when_it_is_an_exact_final_unit_boundary():
    for text in ('Word', 'Word  '):
        plan = plan_units(text, 'word')
        if text.endswith(' '):
            with pytest.raises(ValueError, match='No current'):
                choose_unit(plan, text, len(text), (len(text), len(text)))
        else:
            assert choose_unit(plan, text, len(text), (len(text), len(text))).text == 'Word'


def test_paragraphs_are_stored_lines_and_sentence_rules_remain_explicit():
    text = 'Dr. Smith waits.\r\nA long single stored line continues without a visual break.\rLast'
    paragraphs = plan_units(text, 'paragraph')
    sentences = plan_units(text, 'sentence')
    assert len(paragraphs.ranges) == 3 and text[slice(*paragraphs.ranges[1])].startswith('A long single')
    assert text[slice(*sentences.ranges[0])] == 'Dr.'
    assert 'abbreviations' in choose_unit(sentences, text, 0, (0, 0)).notice
    assert 'visual wrapping' in choose_unit(paragraphs, text, 0, (0, 0)).notice


@pytest.mark.parametrize('text', ['', ' \r\n\t', '12345 😀 --'])
def test_empty_or_nonletter_word_scope_is_explicitly_unavailable(text):
    with pytest.raises(ValueError):
        plan_units(text, 'word')


@pytest.mark.parametrize(('caret', 'selection', 'step'), [
    (-1, (0, 0), 0), (99, (0, 0), 0), (True, (0, 0), 0),
    (0, (-1, 2), 0), (0, (2, 1), 0), (0, (0, 99), 0),
    (0, (0, 0), True), (0, (0, 0), 2),
])
def test_invalid_native_range_or_navigation_direction_is_not_widened(caret, selection, step):
    with pytest.raises(ValueError):
        choose_unit(plan_units('Text', 'word'), 'Text', caret, selection, step=step)


def test_stale_or_tampered_plan_cannot_select_other_text():
    plan = plan_units('Alpha Beta', 'word')
    with pytest.raises(ValueError, match='stale'):
        choose_unit(plan, 'Omega Beta', 0, (0, 0))
    with pytest.raises(ValueError, match='ranges'):
        choose_unit(replace(plan, ranges=((0, 5), (4, 10))), 'Alpha Beta', 0, (0, 0))


@pytest.mark.parametrize(('kind', 'text'), [('word', 'word ' * 20001),
    ('sentence', 'Sentence. ' * 10001), ('paragraph', 'line\n' * 10001)])
def test_existing_boundary_count_budgets_never_return_an_accepted_partial_plan(kind, text):
    with pytest.raises(ValueError):
        plan_units(text, kind)


@pytest.mark.parametrize('kind', ['word', 'sentence', 'paragraph'])
def test_boundary_planning_is_cancellable(kind):
    calls = []
    def cancelled():
        calls.append(True)
        return len(calls) >= 3
    with pytest.raises(InterruptedError):
        plan_units('Sentence word.\n' * 5000, kind, cancelled=cancelled)
    assert len(calls) >= 3

