"""Engine-independent integrity tests; actual native execution is a separate probe."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from carr_library.speech import spelling
from carr_library.speech.reading import ReadingDocument
from carr_library.ui.spelling_dialog import (
    ReadingSnapshot, SpellingReview, apply_correction, preview_correction,
)


@pytest.fixture
def profile(tmp_path):
    paths = [tmp_path / name for name in ('selected-library.bin', 'synthetic.aff', 'synthetic.dic')]
    # No executable code is loaded by profile review or synthetic protocol tests.
    for path, data in zip(paths, (b'not an executable library', b'SET UTF-8\n', b'2\nhello\nworld\n')):
        path.write_bytes(data)
    return spelling.review_hunspell(*paths, language='en-test',
        provenance='Original test fixtures; executable payload deliberately unavailable')


def reply(request, *, issues=None):
    data = json.loads(request)
    return json.dumps({'encoding': 'UTF-8', 'checked': len(data['words']),
        'issues': [] if issues is None else issues, 'request_sha256': sha256(request).hexdigest()}).encode('utf-8')


def test_review_hashes_exact_selected_files_without_loading_runtime(profile, monkeypatch):
    monkeypatch.setattr(spelling, 'run_process', lambda *args, **kwargs: pytest.fail('Profile review executed native code'))
    selected = spelling.review_hunspell(profile.library.path, profile.affix.path, profile.dictionary.path,
        language=profile.language, provenance=profile.provenance)
    assert selected == profile
    assert selected.library.sha256 == sha256(b'not an executable library').hexdigest()
    assert len(selected.identity) == 64
    assert replace(selected, language='different').identity != selected.identity


@pytest.mark.parametrize('field', ['language', 'provenance'])
def test_explicit_profile_requires_language_and_provenance(profile, field):
    values = dict(language=profile.language, provenance=profile.provenance)
    values[field] = ''
    with pytest.raises(ValueError):
        spelling.review_hunspell(profile.library.path, profile.affix.path, profile.dictionary.path, **values)


def test_same_file_and_hardlinked_dictionary_are_refused(profile, tmp_path):
    with pytest.raises(ValueError, match='distinct'):
        spelling.review_hunspell(profile.library.path, profile.affix.path, profile.affix.path,
            language='en', provenance='explicit review')
    link = tmp_path / 'linked.dic'
    os.link(profile.dictionary.path, link)
    with pytest.raises(ValueError, match='links'):
        spelling.review_hunspell(profile.library.path, profile.affix.path, link,
            language='en', provenance='explicit review')


def test_mutation_during_profile_hash_is_refused(profile):
    path = Path(profile.dictionary.path)
    calls = 0
    def mutate():
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b'2\nchanged\nbytes\n')
        return False
    with pytest.raises(ValueError, match='changed'):
        spelling._review_file(path, 'dictionary', mutate)


def test_profile_hash_budget_and_cancel_stop_before_any_runtime(profile, monkeypatch):
    monkeypatch.setitem(spelling.FILE_BUDGETS, 'dictionary', 3)
    with pytest.raises(ValueError, match='size budget'):
        spelling._review_file(profile.dictionary.path, 'dictionary', lambda: False)
    with pytest.raises(InterruptedError):
        spelling._review_file(profile.library.path, 'library', lambda: True)


def test_exact_word_ranges_preserve_emoji_crlf_combining_marks_and_apostrophes():
    text = "😀 hello\r\nп’ять cafe\u0301 well-known 123 end!"
    bounds, words = spelling.word_ranges(text)
    assert bounds == (0, len(text))
    assert tuple(word for _, _, word in words) == ('hello', 'п’ять', 'cafe\u0301', 'well-known', 'end')
    assert all(text[first:last] == word for first, last, word in words)
    assert words[1][0] == 9
    selected = (words[1][0], words[2][1])
    assert spelling.word_ranges(text, bounds=selected)[1] == words[1:3]


@pytest.mark.parametrize('bounds', [(True, 1), (0, 999), (-1, 2), (2, 1), [0, 2]])
def test_invalid_reading_scope_is_refused(bounds):
    with pytest.raises(ValueError):
        spelling.word_ranges('words', bounds=bounds)


def test_word_scan_is_bounded_and_cancellable_even_inside_one_long_token(monkeypatch):
    monkeypatch.setattr(spelling, 'MAX_WORDS', 2)
    with pytest.raises(ValueError, match='smaller spelling scope'):
        spelling.word_ranges('one two three')
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 3
    with pytest.raises(InterruptedError):
        spelling.word_ranges('a' * 20_000, cancelled=cancelled)


def test_protocol_deduplicates_requests_but_retains_each_exact_issue_range(profile, monkeypatch):
    calls = []
    def worker(argv, **kwargs):
        calls.append((argv, kwargs))
        data = json.loads(kwargs['input_bytes'])
        assert data['words'] == ['hello', 'wrld', 'слово']
        return reply(kwargs['input_bytes'], issues=[{'index': 1, 'reason': 'not_in_dictionary',
                                                    'suggestions': ['world']}]), b'', 0
    monkeypatch.setattr(spelling, 'run_process', worker)
    result = spelling.check_spelling('😀 hello\r\nwrld слово wrld', profile)
    assert result.words == 4 and len(result.issues) == 2
    assert [(issue.start, issue.end) for issue in result.issues] == [(9, 13), (20, 24)]
    assert all(issue.suggestions == ('world',) for issue in result.issues)
    argv, options = calls[0]
    assert argv[1:4] == ['-I', '-B', '-S'] and argv[4].endswith('spelling_worker.py')
    assert options['allow_failure'] is True and options['timeout'] == 30


@pytest.mark.parametrize('malformation', ['identity', 'count', 'duplicate', 'index', 'suggestion', 'extra', 'unencodable'])
def test_unbound_or_malformed_worker_result_cannot_be_used(profile, monkeypatch, malformation):
    def worker(argv, **kwargs):
        value = json.loads(reply(kwargs['input_bytes'], issues=[
            {'index': 0, 'reason': 'not_in_dictionary', 'suggestions': ['hello']}]))
        if malformation == 'identity': value['request_sha256'] = '0' * 64
        elif malformation == 'count': value['checked'] = True
        elif malformation == 'duplicate': value['issues'] *= 2
        elif malformation == 'index': value['issues'][0]['index'] = 2
        elif malformation == 'suggestion': value['issues'][0]['suggestions'] = ['x\0y']
        elif malformation == 'extra': value['unreviewed'] = True
        else: value['issues'][0]['reason'] = 'unencodable'
        return json.dumps(value).encode(), b'', 0
    monkeypatch.setattr(spelling, 'run_process', worker)
    with pytest.raises(ValueError):
        spelling.check_spelling('helo', profile)


@pytest.mark.parametrize('code,payload,diagnostics', [
    (0, b'{"error":"changed"}', b''), (0, b'{"error":"unavailable"}', b''),
    (1, b'', b''), (0, b'not JSON', b''), (0, b'{}', b'native dictionary warning'),
])
def test_changed_unavailable_crashed_or_warning_runtime_fails_closed(profile, monkeypatch, code, payload, diagnostics):
    monkeypatch.setattr(spelling, 'run_process', lambda *args, **kwargs: (payload, diagnostics, code))
    with pytest.raises(ValueError):
        spelling.check_spelling('hello', profile)


def test_too_long_words_are_visible_unchecked_issues_not_silently_accepted(profile, monkeypatch):
    monkeypatch.setattr(spelling, 'run_process', lambda *args, **kwargs: (reply(kwargs['input_bytes']), b'', 0))
    text = 'x' * 129 + ' hello'
    result = spelling.check_spelling(text, profile)
    assert result.words == 2 and len(result.issues) == 1
    assert result.issues[0].reason == 'too_long' and result.issues[0].word == 'x' * 129


def test_repeated_unknown_words_cannot_overflow_review(profile, monkeypatch):
    def worker(*args, **kwargs):
        return reply(kwargs['input_bytes'], issues=[{'index': 0, 'reason': 'not_in_dictionary', 'suggestions': []}]), b'', 0
    monkeypatch.setattr(spelling, 'run_process', worker)
    with pytest.raises(ValueError, match='500'):
        spelling.check_spelling('unknown ' * 501, profile)


def test_actual_child_refuses_changed_dictionary_before_loading_dummy_library(profile):
    path = Path(profile.dictionary.path)
    path.write_bytes(b'2\nother\nwords\n')
    with pytest.raises(ValueError, match='changed'):
        spelling.check_spelling('hello', profile)


def test_actual_child_refuses_non_library_without_importing_application(profile):
    with pytest.raises(ValueError, match='C API'):
        spelling.check_spelling('hello', profile)


@pytest.mark.parametrize('failure', [InterruptedError, TimeoutError, RuntimeError])
def test_parent_removes_owned_staging_after_reaped_worker_failure(profile, monkeypatch, failure):
    staged = []
    def fail_after_staging(argv, **kwargs):
        root = Path(kwargs['cwd']); staged.append(root)
        binding = json.loads(kwargs['input_bytes'])['staging']
        assert binding['path'] == str(root) and binding['inode'] == root.stat().st_ino
        (root / 'dictionary.dic').write_bytes(b'owned partial private dictionary')
        (root / 'hunspell.dll').write_bytes(b'owned partial native library')
        raise failure('The owned child has exited; synthetic failure boundary.')
    monkeypatch.setattr(spelling, 'run_process', fail_after_staging)
    with pytest.raises(failure):
        spelling.check_spelling('hello', profile)
    assert staged and not staged[0].exists()


def make_review(profile, text='😀 hello\r\nwrld'):
    document = ReadingDocument(text)
    start = text.index('wrld')
    snapshot = ReadingSnapshot.capture(document, (start, start + 4))
    result = spelling.SpellingResult(profile, sha256(text.encode()).hexdigest(), (0, len(text)),
        2, 'UTF-8', (spelling.SpellingIssue(start, start + 4, 'wrld', ('world',)),))
    return document, SpellingReview(snapshot, result)


def test_correction_is_explicit_one_undoable_edit_with_original_source_retained(profile):
    document, review = make_review(profile)
    before = document.to_dict()
    preview = preview_correction(review, 0, 'world', review.snapshot)
    assert document.to_dict() == before
    assert apply_correction(document, preview, review.snapshot)
    assert document.revision == 1 and document.text == '😀 hello\r\nworld'
    assert document.source_text == '😀 hello\r\nwrld'
    assert document.undo() and document.text == document.source_text


@pytest.mark.parametrize('change', ['text', 'selection', 'document', 'range', 'identity'])
def test_stale_or_tampered_correction_preserves_reading_document(profile, change):
    document, review = make_review(profile)
    preview = preview_correction(review, 0, 'world', review.snapshot)
    current = review.snapshot
    if change == 'text':
        document.replace(0, 1, 'new')
    elif change == 'selection':
        current = ReadingSnapshot.capture(document, (0, 0))
    elif change == 'document':
        document = ReadingDocument(document.text)
    elif change == 'range':
        result = replace(review.result, issues=(replace(review.result.issues[0], end=10000),))
        preview = replace(preview, review=replace(review, result=result))
    else:
        preview = replace(preview, review=replace(review, result=replace(review.result, text_sha256='0' * 64)))
    before = document.to_dict()
    with pytest.raises(ValueError):
        apply_correction(document, preview, current)
    assert document.to_dict() == before
