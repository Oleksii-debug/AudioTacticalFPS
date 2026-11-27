"""Actual isolated stdlib regex execution; no voice, model or network involved."""
import json
from pathlib import Path
from threading import Event, Timer
from time import monotonic

import pytest

from carr_library.reader.narration import new_project, regex_preview, revise, undo, redo, text_digest
from carr_library.reader.regex_worker import MAX_INPUT_BYTES, MAX_MATCHES


@pytest.mark.parametrize('text,pattern,replacement,ignore_case,expected,count', [
    ('Dr. Smith, 12 kg. 7 kg.', r'(\d+) kg', r'\1 kilograms', False,
     'Dr. Smith, 12 kilograms. 7 kilograms.', 2),
    ('Україна: 23 км. 😀\r\n', r'(?P<number>\d+) км', r'\g<number> кілометрів', False,
     'Україна: 23 кілометрів. 😀\r\n', 1),
    ('Київ КИЇВ київ', 'київ', 'Kyiv', True, 'Kyiv Kyiv Kyiv', 3),
    ('Київ КИЇВ київ', 'київ', 'Kyiv', False, 'Київ КИЇВ Kyiv', 1),
    ('ab', r'(?=.)|$', 'X', False, 'XaXbX', 3),
    ('a', r'(?=a)|a', 'X', False, 'XX', 2),
    ('A\r\nB\nC', r'\r?\n', r'\n', False, 'A\nB\nC', 2),
    ('Keep 😀 exactly.', 'absent', '', False, 'Keep 😀 exactly.', 0),
    ('aa', 'a', '', False, '', 2),
])
def test_actual_worker_unicode_groups_case_zero_width_and_unchanged_edges(
        text, pattern, replacement, ignore_case, expected, count):
    project = new_project(text)
    proposal = regex_preview(project.current_text, pattern, replacement, ignore_case=ignore_case)
    assert proposal.text == expected and proposal.match_count == count
    assert proposal.source_sha256 == text_digest(text)
    assert project.current_text == project.original_text == text and len(project.revisions) == 1
    edited = revise(project, proposal.text, label='Explicit regex apply')
    assert edited.original_text == text and edited.original_sha256 == project.original_sha256
    if expected != text:
        assert undo(edited).current_text == text and redo(undo(edited)).current_text == expected


@pytest.mark.parametrize('pattern,replacement', [('(', 'x'), ('a', r'\g<missing>'), ('a', r'\q')])
def test_actual_invalid_expression_or_template_fails_without_source_edit(pattern, replacement):
    project = new_project('Private synthetic source: a')
    with pytest.raises(ValueError, match='Regex preview was rejected') as error:
        regex_preview(project.current_text, pattern, replacement)
    assert project.current_text == project.original_text
    assert 'Private synthetic source' not in str(error.value)


def test_catastrophic_backtracking_is_terminated_and_reaped(monkeypatch):
    from carr_library.audio import engines
    original = engines.subprocess.Popen
    children = []
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(engines.subprocess, 'Popen', launch)
    project = new_project('a' * 2000 + '!')
    before = project.current_text
    started = monotonic()
    with pytest.raises(TimeoutError, match='time budget'):
        regex_preview(before, '(a+)+$', 'X', timeout=.1)
    assert monotonic() - started < 5
    assert len(children) == 1 and children[0].poll() is not None
    assert project.current_text == before and len(project.revisions) == 1


def test_actual_owned_worker_cancellation_reaps_without_result(monkeypatch):
    from carr_library.audio import engines
    original = engines.subprocess.Popen
    children, timers = [], []
    stopped = Event()
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        timer = Timer(.1, stopped.set)
        timers.append(timer)
        timer.start()
        return process
    monkeypatch.setattr(engines.subprocess, 'Popen', launch)
    try:
        with pytest.raises(InterruptedError):
            regex_preview('a' * 2000 + '!', '(a+)+$', 'X', cancelled=stopped.is_set, timeout=5)
    finally:
        for timer in timers:
            timer.cancel()
            timer.join()
    assert len(children) == 1 and children[0].poll() is not None


def test_cancelled_before_launch_does_not_open_a_child(monkeypatch):
    from carr_library.audio import engines
    monkeypatch.setattr(engines, '_run', lambda *a, **k: pytest.fail('Child started after cancellation'))
    with pytest.raises(InterruptedError):
        regex_preview('text', '.', 'x', cancelled=lambda: True)


@pytest.mark.parametrize('kind', ['matches', 'capture-expansion', 'wire-escaping'])
def test_actual_worker_output_budgets_are_enforced(kind):
    if kind == 'matches':
        args = ('a' * (MAX_MATCHES + 1), 'a', 'b')
    elif kind == 'capture-expansion':
        args = ('a' * 100_000, r'(a+)', r'\g<1>' * 200)
    else:
        # Raw replacements fit the worker's estimate; JSON control-character
        # escaping must still obey the exact transmitted-byte budget.
        args = ('a' * 500, 'a', '\x01' * 1800)
    with pytest.raises(ValueError, match='rejected'):
        regex_preview(*args)


def test_actual_worker_can_return_at_most_the_existing_reading_text_budget():
    text = 'a' * 2000 + 'x' * 7_700_000
    with pytest.raises(ValueError, match='8 MiB'):
        regex_preview(text, 'a', 'b' * 400, timeout=5)


def test_encoded_input_budget_is_enforced_before_launch(monkeypatch):
    from carr_library.audio import engines
    monkeypatch.setattr(engines, '_run', lambda *a, **k: pytest.fail('Oversized request started a child'))
    with pytest.raises(ValueError, match='8,000,000'):
        regex_preview('x' * MAX_INPUT_BYTES, 'x', 'x')


@pytest.mark.parametrize('options', [
    {'pattern':''}, {'pattern':'x' * 1025}, {'replacement':'x' * 4097},
    {'ignore_case':1}, {'ignore_case':'yes'}, {'timeout':True}, {'timeout':0},
    {'timeout':float('nan')}, {'timeout':float('inf')}, {'timeout':11},
    {'text':'bad\x00text'}, {'replacement':'bad\ud800'}, {'cancelled':None},
])
def test_invalid_request_shape_is_rejected_before_process_dispatch(monkeypatch, options):
    from carr_library.audio import engines
    monkeypatch.setattr(engines, '_run', lambda *a, **k: pytest.fail('Invalid request started a child'))
    arguments = dict(text='text', pattern='text', replacement='words')
    arguments.update(options)
    with pytest.raises(ValueError):
        regex_preview(**arguments)


@pytest.mark.parametrize('matches', [
    [[True, 1, 'x']], [[0, 4, 'x']], [[-1, 0, 'x']], [[2, 1, 'x']],
    [[0, 2, 'x'], [1, 3, 'y']], [[0, 0, 'x'], [0, 0, 'y']],
    [[0, 1, None]], [[0, 1, '\x00']], [[0, 1]], 'not a list',
])
def test_parent_checks_bounded_worker_receipt_before_accepting_offsets(monkeypatch, matches):
    from carr_library.audio import engines
    reply = json.dumps({'matches':matches}).encode()
    monkeypatch.setattr(engines, '_run', lambda *a, **k: reply)
    with pytest.raises(ValueError):
        regex_preview('abc', 'a', 'x')


def test_late_cancellation_after_actual_child_result_still_withdraws_preview(monkeypatch):
    from carr_library.audio import engines
    original = engines._run
    stop = Event()
    def completed(*args, **kwargs):
        result = original(*args, **kwargs)
        stop.set()
        return result
    monkeypatch.setattr(engines, '_run', completed)
    with pytest.raises(InterruptedError):
        regex_preview('abc', 'a', 'x', cancelled=stop.is_set)


def test_actual_worker_uses_isolated_bytecode_free_utf8_interpreter(monkeypatch, tmp_path):
    from carr_library.audio import engines
    original = engines.subprocess.Popen
    commands = []
    def launch(command, **options):
        commands.append((command, options))
        return original(command, **options)
    monkeypatch.setattr(engines.subprocess, 'Popen', launch)
    (tmp_path / 'sitecustomize.py').write_text('raise AssertionError("untrusted startup code")')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    assert regex_preview('Україна 😀', '😀', '🚂').text == 'Україна 🚂'
    command, options = commands[0]
    assert all(flag in command for flag in ('-I','-B','-S','utf8'))
    assert options['shell'] is False and 'PYTHONPATH' not in options['env']
    assert Path(command[-1]).name == 'regex_worker.py'
