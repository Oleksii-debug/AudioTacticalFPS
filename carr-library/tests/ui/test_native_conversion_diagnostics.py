"""Run the actual native conversion observer functions without constructing wx.

The native companion is copied and source-hashed by the acceptance runners.
These cases prove transparent forwarding and failure ordering; they do not
qualify Pandoc, native controls, or a Windows desktop.
"""
import ast
from concurrent.futures import Future
from io import StringIO
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import traceback

import pytest


def helpers(**overrides):
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_document_conversion_smoke.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    main, = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main']
    names = ('observe_native', 'error_text', 'observed_host_job', 'cleanup_check', 'observed_extract',
             'text_shape', 'observed_reader', 'reader_copy_matches')
    functions = []
    for name in names:
        node, = [item for item in main.body if isinstance(item, ast.FunctionDef) and item.name == name]
        functions.append(node)
    stderr = StringIO()
    env = {
        'json': json, 'sys': SimpleNamespace(stderr=stderr), 'threading': threading,
        'traceback': traceback, 'worker_calls': [], 'conversion_calls': [],
        'extraction_state': {'started': 0, 'returned': 0, 'error': None},
        'ui_thread': threading.get_ident(), 'errors': [],
        'sha256': sha256, 'reader_handoffs': [], 'frame': object(),
    }
    env.update(overrides)
    module = ast.Module(body=functions, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), env)
    return env, stderr


def on_worker(function):
    results, errors = [], []
    def work():
        try:
            results.append(function())
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), 'The finite observer test worker did not return.'
    return results, errors


@pytest.mark.parametrize('admitted', [False, True])
def test_native_conversion_observer_forwards_actual_host_call_and_admission(admitted):
    operation, done, failed = object(), object(), object()
    returned = Future() if admitted else None
    calls = []
    def actual(*args, **kwargs):
        calls.append((args, kwargs))
        return returned
    env, stderr = helpers(actual_host_job=actual)
    result = env['observed_host_job']('Original title', operation, done=done, failed=failed)
    assert result is returned
    assert calls == [(('Original title', operation), {'done': done, 'failed': failed})]
    assert env['worker_calls'][0]['future'] is returned
    assert json.loads(stderr.getvalue().split('DIAG: actual native conversion ', 1)[1])['admitted'] is admitted


def test_native_conversion_observer_returns_exact_result_on_actual_worker():
    service, selected, cancel, receipt = object(), object(), object(), object()
    calls = []
    ui_thread = threading.get_ident()
    def actual(*args, **kwargs):
        assert threading.get_ident() != ui_thread
        calls.append((args, kwargs))
        return receipt
    env, stderr = helpers(actual_extract=actual)
    result, errors = on_worker(lambda: env['observed_extract'](service, selected, cancelled=cancel))
    assert errors == [] and result == [receipt] and result[0] is receipt
    assert calls == [((service, selected), {'cancelled': cancel})]
    assert env['conversion_calls'] == [receipt] and env['conversion_calls'][0] is receipt
    assert env['extraction_state'] == {'started': 1, 'returned': 1, 'error': None}
    assert 'extract-entered' in stderr.getvalue() and 'extract-returned' in stderr.getvalue()


@pytest.mark.parametrize('kind', [RuntimeError, InterruptedError, KeyboardInterrupt])
def test_native_conversion_observer_preserves_original_worker_error_and_bounds_diagnostic(kind):
    original = kind('original detail ' + '\u03a9' * 10000)
    def actual(*args, **kwargs):
        raise original
    env, stderr = helpers(actual_extract=actual)
    result, errors = on_worker(lambda: env['observed_extract'](object(), object()))
    assert result == [] and errors == [original] and errors[0] is original
    assert env['conversion_calls'] == []
    assert env['extraction_state']['started'] == 1 and env['extraction_state']['returned'] == 0
    lines = stderr.getvalue().splitlines()
    record = json.loads(next(line.split('DIAG: actual native conversion ', 1)[1]
                             for line in lines if '"event": "extract-error"' in line))
    assert record['error'].startswith(kind.__name__ + ': original detail ')
    assert len(record['error']) <= len(kind.__name__) + 2 + 4096
    assert len(record['traceback']) <= 8192 and kind.__name__ in record['traceback']
    assert stderr.getvalue().isascii()


def test_native_conversion_observer_preserves_original_admission_error():
    original = RuntimeError('actual shared worker admission failed')
    operation = object()
    calls = []
    def actual(*args, **kwargs):
        calls.append((args, kwargs))
        raise original
    env, stderr = helpers(actual_host_job=actual)
    with pytest.raises(RuntimeError) as caught:
        env['observed_host_job']('Original title', operation, with_progress=True)
    assert caught.value is original
    assert calls == [(('Original title', operation), {'with_progress': True})]
    assert env['worker_calls'][0]['future'] is None
    assert 'host-admission-error' in stderr.getvalue()


def test_native_conversion_cleanup_retains_primary_failure_and_checks_remaining_owners():
    checked = []
    def check(name, condition):
        checked.append((name, condition))
        if not condition:
            raise AssertionError(name)
    primary = 'Original conversion wait failed before any reader was created.'
    env, stderr = helpers(check=check, errors=[primary])
    outcomes = [env['cleanup_check']('reader owner missing', lambda: False),
                env['cleanup_check']('actual child closed', lambda: True),
                env['cleanup_check']('lease still owned', lambda: False)]
    assert outcomes == [False, True, False]
    assert checked == [('reader owner missing', False), ('actual child closed', True),
                       ('lease still owned', False)]
    assert env['errors'][0] == primary and len(env['errors']) == 3
    assert 'reader owner missing' in env['errors'][1] and 'lease still owned' in env['errors'][2]
    assert stderr.getvalue().count('FAIL: native conversion cleanup assertion') == 2


def test_native_conversion_optional_diagnostic_cannot_replace_actual_admission():
    class BrokenDisplay:
        def write(self, value):
            raise OSError('diagnostic display unavailable')
        def flush(self):
            raise OSError('diagnostic display unavailable')
    returned = Future()
    env, _ = helpers(actual_host_job=lambda *args, **kwargs: returned,
                     sys=SimpleNamespace(stderr=BrokenDisplay()))
    assert env['observed_host_job']('Original title', object()) is returned
    assert env['worker_calls'][0]['future'] is returned


def test_native_conversion_cleanup_predicate_failure_cannot_mask_primary_or_stop_later_checks():
    checked, predicates = [], []
    def check(name, condition):
        checked.append((name, condition))
        assert condition, name
    primary = 'Original conversion wait failed.'
    def broken_owner():
        predicates.append('broken owner')
        raise RuntimeError('actual owner inspection failed')
    def next_owner():
        predicates.append('next owner')
        return True
    env, _ = helpers(check=check, errors=[primary])
    assert env['cleanup_check']('owner inspection', broken_owner) is False
    assert env['cleanup_check']('remaining native owner', next_owner) is True
    assert predicates == ['broken owner', 'next owner']
    assert checked == [('remaining native owner', True)]
    assert env['errors'][0] == primary and len(env['errors']) == 2
    assert 'actual owner inspection failed' in env['errors'][1]


@pytest.mark.parametrize('fails', [False, True])
def test_native_conversion_reader_observer_preserves_exact_handoff_and_outcome(fails):
    parent, guard, result = object(), object(), object()
    text = 'Verified \u03a9\r\n\r\ncopy\n'
    failure = RuntimeError('original reader failure')
    calls = []
    def actual(*values, **options):
        calls.append((values, options))
        if fails:
            raise failure
        return result
    env, stderr = helpers(frame=parent, actual_reader=actual)
    options = {'text': text, 'title': 'Exact original title',
               'source_guard': guard, 'recovery_slot': 'converted-reading-copy'}
    if fails:
        with pytest.raises(RuntimeError) as caught:
            env['observed_reader'](parent, **options)
        assert caught.value is failure
    else:
        assert env['observed_reader'](parent, **options) is result
    assert calls == [((parent,), options)] and calls[0][1]['text'] is text
    assert calls[0][1]['source_guard'] is guard
    assert env['reader_handoffs'] == [{'parent_is_host': True, 'text': env['text_shape'](text)}]
    assert 'reader-handoff-entered' in stderr.getvalue()


def test_native_conversion_text_diagnostic_bounds_and_distinguishes_native_line_endings():
    raw = 'Exact \u03a9\r\n' + 'x' * 10000 + '\r\n'
    native = raw.replace('\r\n', '\n')
    env, stderr = helpers()
    first, second = env['text_shape'](raw), env['text_shape'](native)
    assert first['characters'] == len(raw) and second['characters'] == len(native)
    assert first['utf8_sha256'] == sha256(raw.encode('utf-8')).hexdigest()
    assert first['utf8_sha256'] != second['utf8_sha256']
    assert (first['cr'], first['lf'], first['crlf']) == (2, 2, 2)
    assert (second['cr'], second['lf'], second['crlf']) == (0, 2, 0)
    assert all(len(row[field]) == 16 for row in (first, second)
               for field in ('leading_codepoints', 'trailing_codepoints'))
    assert len(json.dumps(first, ensure_ascii=True)) < 1000
    assert stderr.getvalue() == ''  # A shape query alone emits no report or result.


@pytest.mark.parametrize('raw', [
    'Verified \u03a9\r\nFirst line  \r\nLast\r\n',
    'Verified \u03a9\nFirst line  \nLast\n',
    'Verified \u03a9\r\nA lone\rmarker\nLast  \r\n',
])
def test_native_conversion_reader_lf_view_keeps_exact_verified_source_and_saved_draft(tmp_path, raw):
    from carr_library.speech.reading import ReadingDocument
    from carr_library.speech.session import ReaderDraftStore, ReaderResume

    original = tmp_path / 'verified-copy.txt'
    original.write_bytes(raw.encode('utf-8'))
    native = raw.replace('\r\n', '\n')
    env, _ = helpers()
    env['reader_handoffs'].append({'parent_is_host': True, 'text': env['text_shape'](raw)})
    assert env['reader_copy_matches'](raw, native)

    # The real reader synchronizes the native TextCtrl value before Ctrl+S.
    # Persist through the actual draft API; no source field is rewritten.
    document = ReadingDocument(raw, title='Verified copy')
    document.set_text(native)
    store = ReaderDraftStore(tmp_path / 'library', slot='converted-reading-copy')
    store.save(ReaderResume(document))
    resume = store.load()
    assert resume is not None
    assert env['reader_copy_matches'](raw, native, resume.document)
    assert resume.document.source_text == raw
    assert resume.document.source_sha256 == sha256(original.read_bytes()).hexdigest()
    assert resume.document.text == native and resume.document.source_anchor is None
    assert original.read_bytes() == raw.encode('utf-8')


@pytest.mark.parametrize('changed', [
    'word', 'space', 'lone-cr', 'raw-widget', 'normalized-handoff', 'foreign-parent',
    'normalized-source',
])
def test_native_conversion_reader_lf_contract_refuses_every_unreviewed_change(changed):
    from carr_library.speech.reading import ReadingDocument

    raw = 'Exact \u03a9\r\nTwo  spaces\rlone\r\n'
    native = raw.replace('\r\n', '\n')
    document = ReadingDocument(raw, title='Verified copy')
    document.set_text(native)
    env, _ = helpers()
    handoff = {'parent_is_host': True, 'text': env['text_shape'](raw)}
    env['reader_handoffs'].append(handoff)
    assert env['reader_copy_matches'](raw, native, document)

    if changed == 'word':
        native = native.replace('Exact', 'Other')
    elif changed == 'space':
        native = native.replace('Two  spaces', 'Two spaces')
    elif changed == 'lone-cr':
        native = native.replace('\r', '')
    elif changed == 'raw-widget':
        native = raw
    elif changed == 'normalized-handoff':
        handoff['text'] = env['text_shape'](native)
    elif changed == 'foreign-parent':
        handoff['parent_is_host'] = False
    elif changed == 'normalized-source':
        document = ReadingDocument(native, title='Verified copy')
    else:
        raise AssertionError(changed)
    assert not env['reader_copy_matches'](raw, native, document)


def keyboard_driver(*, held=False, enabled=True, focus=True):
    env, diagnostic = helpers()
    source = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_document_conversion_smoke.py'
    tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
    main, = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main']
    functions = []
    for name in ('press', 'release'):
        node, = [item for item in main.body if isinstance(item, ast.FunctionDef) and item.name == name]
        functions.append(node)
    pending, calls = [], []
    state = dict(now=0.0, live=True, enabled=enabled, focused=None, foreground=0, admit_focus=focus)
    control = SimpleNamespace(GetName=lambda: 'Import verified reading copy into selected library',
                              IsEnabled=lambda: state['enabled'])
    def set_focus():
        calls.append(('focus', control))
        if state['admit_focus']:
            state['focused'] = control
    control.SetFocus = set_focus
    top = SimpleNamespace(GetHandle=lambda: 71, Raise=lambda: calls.append(('raise', 71)))
    def foreground(handle):
        calls.append(('foreground', handle))
        state['foreground'] = handle
    def down(code):
        calls.append(('down', code))
        return True
    def up(code):
        calls.append(('up', code))
        return True
    env.update(
        wx=SimpleNamespace(WXK_SPACE=32, GetTopLevelParent=lambda item: top,
                           Window=SimpleNamespace(FindFocus=lambda: state['focused'])),
        user32=SimpleNamespace(IsWindow=lambda handle: state['live'] and handle == 71,
                               SetForegroundWindow=foreground,
                               GetForegroundWindow=lambda: state['foreground']),
        simulator=SimpleNamespace(KeyDown=down, KeyUp=up), pressed={27} if held else set(),
        monotonic=lambda: state['now'], later=lambda delay, callback: pending.append((delay, callback)))
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                 str(source), 'exec'), env)
    return env, state, control, pending, calls, diagnostic


@pytest.mark.parametrize('blocked', ['escape-held', 'control-disabled', 'both'])
def test_native_conversion_key_waits_for_prior_release_and_enabled_target(blocked):
    env, state, control, pending, calls, diagnostic = keyboard_driver(
        held=blocked != 'control-disabled', enabled=blocked == 'escape-held')
    env['press'](control)
    delay, callback = pending.pop(0)
    assert delay == 1
    callback()
    assert calls == [] and diagnostic.getvalue() == ''
    delay, callback = pending.pop(0)
    assert delay == 25
    state['now'] = .026
    callback()
    assert calls == [] and diagnostic.getvalue() == ''
    # Release the actual earlier gesture using this native probe's own release
    # function; no premature synthetic key or focus is allowed.
    if 27 in env['pressed']:
        env['release'](27)
    state['enabled'] = True
    delay, callback = pending.pop(0)
    assert delay == 25
    state['now'] = .051
    callback()
    assert [item for item in calls if item[0] == 'down'] == [('down', 32)]
    assert env['pressed'] == {32} and state['focused'] is control
    record = json.loads(diagnostic.getvalue().split('DIAG: actual native conversion ', 1)[1])
    assert record['event'] == 'native-key-dispatch' and record['held_keys'] == []
    assert record['enabled'] is True and record['key'] == 32
    delay, callback = pending.pop(0)
    assert delay == 40
    callback()
    assert env['pressed'] == set() and calls[-1] == ('up', 32) and pending == []


@pytest.mark.parametrize('blocked', ['escape-held', 'control-disabled', 'focus-refused'])
def test_native_conversion_key_keeps_five_second_bound_without_dispatch(blocked):
    env, state, control, pending, calls, diagnostic = keyboard_driver(
        held=blocked == 'escape-held', enabled=blocked != 'control-disabled',
        focus=blocked != 'focus-refused')
    env['press'](control)
    pending.pop(0)[1]()
    state['now'] = 5.0
    with pytest.raises(TimeoutError, match='Native conversion'):
        pending.pop(0)[1]()
    assert not any(item[0] in ('down', 'up') for item in calls)
    assert diagnostic.getvalue() == '' and pending == []
    assert env['pressed'] == ({27} if blocked == 'escape-held' else set())


def test_native_conversion_key_refuses_a_destroyed_owned_control_before_focus():
    env, state, control, pending, calls, diagnostic = keyboard_driver()
    env['press'](control)
    state['live'] = False
    with pytest.raises(AssertionError, match='selected conversion control is gone'):
        pending.pop(0)[1]()
    assert calls == [] and diagnostic.getvalue() == '' and pending == []
    assert env['pressed'] == set()
