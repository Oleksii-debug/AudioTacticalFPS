"""Real bounded search worker and source-preserving reading edit boundaries."""
import json

import pytest

from carr_library.speech.reading import ReadingDocument, PronunciationRule, export_dictionary
from carr_library.speech import reading_tools as tools


def snapshot(text):
    document = ReadingDocument(text)
    return document, tools.ReadingSnapshot.capture(document)


def test_literal_unicode_crlf_replace_is_one_reversible_reading_edit(tmp_path):
    raw = '😀 Київ\r\nКиївський Київ.\r\nLast line.'
    original = tmp_path / 'source.txt'
    original.write_bytes(raw.encode('utf-8'))
    document = ReadingDocument(raw, source_path=original)
    current = tools.ReadingSnapshot.capture(document)
    checksum = document.source_sha256
    matches = tools.find_matches(current, 'Київ', r'C:\speech', whole_word=True)
    assert [(match.start, match.end) for match in matches] == [(2, 6), (18, 22)]
    preview = tools.preview_matches(current, matches)
    assert document.text == raw and document.revision == 0
    assert preview.apply(document)
    assert document.text == '😀 C:\\speech\r\nКиївський C:\\speech.\r\nLast line.'
    assert document.revision == 1
    assert document.source_text == raw and document.source_sha256 == checksum
    assert original.read_bytes() == raw.encode('utf-8')
    project = tmp_path / 'reading.json'
    document.save(project)
    reopened = ReadingDocument.load(project)
    assert reopened.undo() and reopened.text == raw
    assert reopened.redo() and reopened.text == document.text
    assert reopened.source_sha256 == checksum


def test_actual_regex_backreferences_respect_selected_range():
    document, current = snapshot('A12\nB34\nA56\noutside A78')
    matches = tools.find_matches(current, r'([AB])(\d+)', r'\2-\1',
                                 regex=True, scope=(0, 11))
    assert len(matches) == 3
    tools.preview_matches(current, matches).apply(document)
    assert document.text == '12-A\n34-B\n56-A\noutside A78'
    assert document.undo() and document.text == current.text


def test_whitespace_search_and_empty_replacement():
    document, current = snapshot('a  b\nc')
    matches = tools.find_matches(current, '  ', '')
    tools.preview_matches(current, matches).apply(document)
    assert document.text == 'ab\nc'
    current = tools.ReadingSnapshot.capture(document)
    tools.preview_matches(current, tools.find_matches(current, '\n', '\t')).apply(document)
    assert document.text == 'ab\tc'


def test_case_whole_word_and_selection_edges():
    document, current = snapshot('CAT cats cat cat_ scatter')
    matches = tools.find_matches(current, 'cat', 'dog', ignore_case=True, whole_word=True)
    assert [(item.start, item.end) for item in matches] == [(0, 3), (9, 12)]
    assert not tools.find_matches(current, 'cat', whole_word=True, scope=(4, 7))
    assert document.text == current.text


def test_find_next_previous_wrap_and_zero_width_positions():
    matches = tuple(tools.ReadingMatch(i, i) for i in (0, 2, 4))
    assert tools.next_match(matches, (0, 0), skip_current=False) == matches[0]
    assert tools.next_match(matches, (0, 0)) == matches[1]
    assert tools.next_match(matches, (4, 4), wrap=False) is None
    assert tools.next_match(matches, (4, 4)) == matches[0]
    assert tools.next_match(matches, (2, 2), backwards=True) == matches[0]
    assert tools.next_match(matches, (0, 0), backwards=True) == matches[-1]


def test_zero_width_regex_insertion_is_reversible():
    document, current = snapshot('ab')
    matches = tools.find_matches(current, r'(?=.)', 'X', regex=True)
    assert len(matches) == 2
    tools.preview_matches(current, matches).apply(document)
    assert document.text == 'XaXb'
    assert document.undo() and document.text == 'ab'


def test_actual_pathological_regex_child_has_a_deadline():
    document, current = snapshot('a' * 30000 + '!')
    with pytest.raises(TimeoutError):
        tools.find_matches(current, r'(a+)+$', regex=True, timeout=.15)
    assert document.text == current.text and not document.can_undo


def test_invalid_regex_child_does_not_edit():
    document, current = snapshot('unchanged')
    with pytest.raises(RuntimeError):
        tools.find_matches(current, '[', regex=True)
    assert document.text == 'unchanged' and not document.can_undo


def test_cancelled_search_never_launches_child(monkeypatch):
    _, current = snapshot('text')
    monkeypatch.setattr(tools, 'run_process',
                        lambda *a, **k: pytest.fail('Cancelled search launched a child.'))
    with pytest.raises(InterruptedError):
        tools.find_matches(current, 'text', cancelled=lambda: True)


def test_cancelled_preview_does_not_change_history():
    document, current = snapshot('A')
    preview = tools.preview_matches(current, [tools.ReadingMatch(0, 1, 'B')])
    with pytest.raises(InterruptedError):
        preview.apply(document, cancelled=lambda: True)
    assert document.text == 'A' and not document.can_undo


@pytest.mark.parametrize('change', ['typing', 'undo', 'other-source'])
def test_stale_preview_cannot_change_a_different_document_state(change):
    document, current = snapshot('original')
    preview = tools.preview_matches(current, [tools.ReadingMatch(0, 8, 'replacement')])
    if change == 'typing':
        document.set_text('newer edit')
    elif change == 'undo':
        document.set_text('other')
        document.undo()
        # Signature includes the redo branch, not merely equal current text.
    else:
        document = ReadingDocument('different original')
        document.set_text(current.text)
    before = document.to_dict()
    with pytest.raises(ValueError, match='changed'):
        preview.apply(document)
    assert document.to_dict() == before


def test_scoped_pronunciation_keeps_original_and_unaffected_mapping():
    text = 'Intro Kyiv and colour. Ending'
    document, current = snapshot(text)
    rules = (PronunciationRule('Kyiv', 'Київ', language='uk', voice_id='local:one'),
             PronunciationRule('colour', 'color', language='en'),
             PronunciationRule('Intro', 'wrong', voice_id='local:other'))
    before_prefix = document.source_span(0, 6)
    before_suffix = document.source_span(22, len(text))
    preview = tools.preview_pronunciations(current, rules, language='uk', voice_id='local:one')
    assert document.text == text
    assert preview.count == 1 and preview.changed
    assert preview.apply(document)
    assert document.text == 'Intro Київ and colour. Ending'
    assert document.source_span(0, 6) == before_prefix
    assert document.source_span(22, len(document.text)) == before_suffix
    assert document.source_text == text
    assert document.undo() and document.text == text


def test_actual_regex_pronunciation_applies_only_reviewed_selection():
    document, current = snapshot('A12 B34 A56')
    rules = (PronunciationRule(r'([AB])(\d+)', r'\2-\1', regex=True),)
    preview = tools.preview_pronunciations(current, rules, scope=(0, 7))
    preview.apply(document)
    assert document.text == '12-A 34-B A56'
    assert document.undo() and document.text == current.text


def test_pronunciation_has_total_budget_as_well_as_each_regex_limit(monkeypatch):
    document, current = snapshot('abc')
    ticks = iter((0, 11))
    monkeypatch.setattr(tools.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(TimeoutError, match='total time budget'):
        tools.preview_pronunciations(current, (PronunciationRule('a', 'b'),))
    assert document.text == current.text and not document.can_undo


@pytest.mark.parametrize('reply', [
    {'matches': [[False, 1, 'x']]},
    {'matches': [[-1, 1, 'x']]},
    {'matches': [[0, 4, 'x']]},
    {'matches': [[0, 2, 'x'], [1, 3, 'y']]},
    {'matches': [[0, 1, 1]]},
    {'matches': [], 'unknown': 1},
    {'matches': [[0, 1, '\0']]},
    {'matches': 'not a list'},
])
def test_untrusted_worker_reply_cannot_mutate_document(monkeypatch, reply):
    document, current = snapshot('abc')
    monkeypatch.setattr(tools, 'run_process',
                        lambda *a, **k: (json.dumps(reply).encode(), b'', 0))
    with pytest.raises(ValueError):
        tools.find_matches(current, 'a')
    assert document.text == 'abc' and not document.can_undo


@pytest.mark.parametrize('scope', [(0, 0), (3, 1), (-1, 2), (0, 4), (False, 1)])
def test_invalid_scope_is_rejected_before_child(monkeypatch, scope):
    _, current = snapshot('abc')
    monkeypatch.setattr(tools, 'run_process',
                        lambda *a, **k: pytest.fail('Invalid scope launched a child.'))
    with pytest.raises(ValueError):
        tools.find_matches(current, 'a', scope=scope)


def test_expansion_cannot_exceed_reading_copy_limit():
    document, current = snapshot('a' * 2_000_000)
    with pytest.raises(ValueError, match='budget'):
        tools.preview_matches(current, (tools.ReadingMatch(0, 0, 'x'),))
    assert document.text == current.text and not document.can_undo


def test_cancelled_dictionary_export_keeps_destination_absent(tmp_path):
    destination = tmp_path / 'pronunciations.json'
    with pytest.raises(InterruptedError):
        export_dictionary((PronunciationRule('A', 'B'),), destination,
                          cancelled=lambda: True)
    assert not destination.exists()
