"""Controlled native status-publication regression; synthetic media only.

This reproduces a reader/publication boundary independently of the intermittent
stopped-observer failure. A failure here does not establish its historical cause.
"""
import importlib.util
import json
import os
from pathlib import Path
from threading import Timer

import pytest


_spec = importlib.util.spec_from_file_location(
    'supplied_worker_protocol_for_status_reader',
    Path(__file__).with_name('test_supplied_worker_bridge.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
prepared = _fixture.prepared


@pytest.mark.skipif(os.name != 'nt', reason='Requires actual Windows file sharing semantics.')
def test_brief_status_reader_does_not_abort_actual_worker_publication(prepared):
    _, _, service, original, configure = prepared
    configure('progress-wait')
    original_bytes = original.read_bytes()
    identity = service.submit([original])
    path = service._path(identity)
    readers, timers = [], []

    def observe(done, total, phase):
        if readers or phase != 'running':
            return
        # This is a real concurrent reader, not a mocked os.replace failure.
        stream = (path / 'job_status.json').open('rb')
        readers.append(stream)
        timer = Timer(.35, stream.close)
        timers.append(timer)
        timer.start()
        # The child already published its initial status and is waiting here.
        (path / 'progress-observed').write_text('release synthetic worker', encoding='utf-8')

    try:
        result = service.run(identity, timeout=10, progress=observe)
    finally:
        for timer in timers:
            timer.join(timeout=2)
        for stream in readers:
            stream.close()
    diagnostic = {'result': {key: result.get(key) for key in
        ('status', 'done', 'failed', 'last_error', 'worker_run_id')}}
    failure = path / 'synthetic-worker-failure.json'
    if failure.exists():
        diagnostic['synthetic_child_exception'] = json.loads(failure.read_text(encoding='utf-8'))
    assert len(readers) == len(timers) == 1, diagnostic
    assert all(stream.closed for stream in readers)
    assert not any(timer.is_alive() for timer in timers)
    assert result['status'] == 'completed', json.dumps(diagnostic, ensure_ascii=True)
    assert result == service.get(identity) == service.verify(identity)
    assert result['done'] == 1 and result['failed'] == 0
    assert len(result['receipts']) == 1 and len(result['artifacts']) == 6
    assert original.read_bytes() == original_bytes
