"""Opt-in timing for the existing synthetic ASR admission regressions.

Load this file with importlib.util and pass the module to pytest.main(plugins=...).
It does not add source-package paths, alter deadlines, or execute another worker.
Only bounded timings, native exit observations, and relative case names are
reported. Worker arguments, paths, profile contents and diagnostics stay private.
"""
from collections import deque
import functools
import json
from pathlib import Path
import sys
import threading
import time

import pytest


_REPORTS = []
_CASES = frozenset(('test_admission_boundaries.py', 'test_service.py'))


def pytest_addoption(parser):
    parser.addoption('--asr-timing-report', default=None,
                     help='Write bounded ASR admission timings to this explicit diagnostic JSON file.')


@pytest.fixture(autouse=True)
def _asr_admission_timing(request, monkeypatch):
    if request.node.path.name not in _CASES:
        yield
        return
    from carr_library.audio import process
    from carr_library.transcription import service

    beginning = time.monotonic()
    local, lock = threading.local(), threading.Lock()
    events, totals, starts = deque(maxlen=128), {}, []
    dropped, next_run = [0], [0]

    def child_state(context):
        child = context.get('child') if context is not None else None
        if child is None:
            return 'not-started'
        try:
            code = child.poll()
        except Exception:
            return 'unavailable'
        return 'running' if code is None else ('exited', int(code))

    def record(kind, started, context, before, error=None):
        elapsed = max(0.0, time.monotonic() - started)
        row = {'kind': kind, 'at_ms': round((started - beginning) * 1000, 3),
               'elapsed_ms': round(elapsed * 1000, 3),
               'run': context['id'] if context is not None else None,
               'child_before': before, 'child_after': child_state(context),
               'error_type': type(error).__name__[:64] if error is not None else None}
        with lock:
            aggregate = totals.setdefault(kind, {'calls': 0, 'total_ms': 0.0, 'max_ms': 0.0})
            aggregate['calls'] += 1
            aggregate['total_ms'] += row['elapsed_ms']
            aggregate['max_ms'] = max(aggregate['max_ms'], row['elapsed_ms'])
            if len(events) == events.maxlen:
                dropped[0] += 1
            events.append(row)

    original_verify = service.TranscriptionService._verify_profile
    original_run = service.run_process
    original_start = process._start_process

    @functools.wraps(original_verify)
    def verify(*args, **kwargs):
        context = getattr(local, 'context', None)
        before, started, error = child_state(context), time.monotonic(), None
        try:
            return original_verify(*args, **kwargs)
        except BaseException as failure:
            error = failure
            raise
        finally:
            record('verify_profile', started, context, before, error)

    @functools.wraps(original_start)
    def start(*args, **kwargs):
        context = getattr(local, 'context', None)
        started = time.monotonic()
        child = original_start(*args, **kwargs)
        if context is not None:
            context['child'] = child
            with lock:
                if len(starts) < 64:
                    starts.append({'run': context['id'],
                                   'native_start_ms': round((time.monotonic() - started) * 1000, 3)})
        return child

    @functools.wraps(original_run)
    def run(*args, **kwargs):
        previous = getattr(local, 'context', None)
        with lock:
            next_run[0] += 1
            context = {'id': next_run[0], 'child': None}
        local.context = context
        selected = kwargs.get('cancelled')
        if callable(selected):
            @functools.wraps(selected)
            def poll():
                before, started, error = child_state(context), time.monotonic(), None
                try:
                    return selected()
                except BaseException as failure:
                    error = failure
                    raise
                finally:
                    record('checked_poll', started, context, before, error)
            kwargs = dict(kwargs, cancelled=poll)
        started, error = time.monotonic(), None
        try:
            return original_run(*args, **kwargs)
        except BaseException as failure:
            error = failure
            raise
        finally:
            record('run_process', started, context, 'not-started', error)
            local.context = previous

    monkeypatch.setattr(service.TranscriptionService, '_verify_profile', verify)
    monkeypatch.setattr(service, 'run_process', run)
    monkeypatch.setattr(process, '_start_process', start)
    try:
        yield
    finally:
        with lock:
            report = {'schema': 1,
                      'case': request.node.path.name + '::' + request.node.name[:160],
                      'python': list(sys.version_info[:3]),
                      'elapsed_ms': round((time.monotonic() - beginning) * 1000, 3),
                      'totals': dict(totals), 'native_starts': list(starts),
                      'events': list(events), 'dropped_events': dropped[0]}
        if len(_REPORTS) < 64:
            _REPORTS.append(report)
        print('CARR_ASR_TIMING ' + json.dumps(report, ensure_ascii=True, separators=(',', ':')),
              flush=True)


def pytest_sessionfinish(session, exitstatus):
    target = session.config.getoption('--asr-timing-report')
    if target is None:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'schema': 1, 'pytest_exit': int(exitstatus),
                                'reports': _REPORTS}, ensure_ascii=True, indent=2),
                    encoding='utf-8')
