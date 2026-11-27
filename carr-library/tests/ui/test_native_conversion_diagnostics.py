"""Run the actual native conversion observer functions without constructing wx.

The native companion is copied and source-hashed by the acceptance runners.
These cases prove transparent forwarding and failure ordering; they do not
qualify Pandoc, native controls, or a Windows desktop.
"""
import ast
from concurrent.futures import Future
from io import StringIO
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
    names = ('observe_native', 'error_text', 'observed_host_job', 'cleanup_check', 'observed_extract')
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
