"""Actual metadata-only cleanup ownership, without changing capture admission.

The stop request is deliberately refused while a real child owns a staging
file. The private directory and the actual broker reservation must survive
until that exact child has exited and both diagnostic readers have stopped.
"""
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from carr_library.audio import cleanup, process
from carr_library.audio.cleanup import AudioCleanupPending, retained_context, retained_temporary_directory
from carr_library.audio.models import AudioBudget
from carr_library.operations.broker import ResourceBroker
from carr_library.operations.models import ResourceLimits, ResourceRequest, ResourceTimeout


CHILD = """
from pathlib import Path
import sys
import time
with Path(sys.argv[1]).open('rb') as owned:
    assert owned.read() == b'reviewed-private-staging'
    Path(sys.argv[2]).write_text('running', encoding='ascii')
    while True:
        time.sleep(.02)
"""


@pytest.mark.parametrize('boundary', ['cancel', 'reader-start', 'cleanup-constructor'])
def test_actual_metadata_child_retains_stage_and_broker_until_confirmed_cleanup(
        tmp_path, monkeypatch, boundary):
    broker = ResourceBroker(tmp_path / 'broker', limits=ResourceLimits(
        cpu_slots=1, memory_bytes=1024**2, disk_bytes=1024**2, min_free_disk_bytes=0))
    request = ResourceRequest(cpu_slots=1, memory_bytes=1024, disk_bytes=4096,
                              disk_path=tmp_path)
    permit, released = threading.Event(), threading.Event()
    spawned, attempts, owned = [], [], {}
    real_start_process, real_terminate = process._start_process, process._stop_owned
    real_thread, real_start = threading.Thread, threading.Thread.start
    ready = tmp_path / 'actual-child-running'

    def observe_child(*args, **kwargs):
        child = real_start_process(*args, **kwargs)
        spawned.append(child)
        # Arm both failures only after the unchanged child owns the private file.
        deadline = time.monotonic() + 10
        while not ready.exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                real_terminate(child)
                raise AssertionError('Actual child did not reach its file-ownership barrier.')
            time.sleep(.01)
        return child

    def refused_stop(child):
        attempts.append(child.pid)
        if not permit.is_set():
            raise OSError('Deliberate unconfirmed native cleanup at the ownership barrier.')
        return real_terminate(child)

    failed_reader = False

    def start_reader(thread, *args, **kwargs):
        nonlocal failed_reader
        if boundary == 'reader-start' and thread.name != 'audio-cleanup' and not failed_reader:
            failed_reader = True
            raise RuntimeError('Deliberate diagnostic-reader admission failure.')
        return real_start(thread, *args, **kwargs)

    def create_thread(*args, **kwargs):
        if boundary == 'cleanup-constructor' and kwargs.get('name') == 'audio-cleanup':
            raise RuntimeError('Deliberate retained-cleanup Thread constructor failure.')
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(process, '_start_process', observe_child)
    monkeypatch.setattr(process, '_stop_owned', refused_stop)
    monkeypatch.setattr(threading.Thread, 'start', start_reader)
    monkeypatch.setattr(cleanup.threading, 'Thread', create_thread)

    def admitted():
        with retained_context(broker.reserve(request, operation_id='metadata-test',
                                              timeout=.5, run_timeout=30)):
            with retained_temporary_directory(prefix='.metadata-owned-', dir=tmp_path) as directory:
                owned['directory'] = Path(directory)
                source = owned['directory'] / 'source.mp3'
                source.write_bytes(b'reviewed-private-staging')
                process.run_process(
                    [sys.executable, '-I', '-B', '-c', CHILD, str(source), str(ready)],
                    budget=AudioBudget(timeout=15, reserve_disk_bytes=0),
                    cancelled=(lambda: ready.exists()) if boundary != 'reader-start' else lambda: False,
                    watch=(owned['directory'],), owned_tree=True)

    owner = None
    try:
        with pytest.raises(AudioCleanupPending, match='cleanup is still pending') as caught:
            admitted()
        owner = caught.value.cleanup_owner
        owner.add_quiescent_callback(released.set)
        if boundary == 'cleanup-constructor':
            assert isinstance(owner.last_error, RuntimeError) and 'constructor failure' in str(owner.last_error)
        assert len(spawned) == 1 and attempts
        child = spawned[0]
        assert child.poll() is None and not owner.quiescent
        assert (owned['directory'] / 'source.mp3').read_bytes() == b'reviewed-private-staging'
        assert broker.snapshot()['usage']['cpu_slots'] == 1
        assert len(broker.snapshot()['leases']) == 1
        with pytest.raises(ResourceTimeout):
            with broker.reserve(request, operation_id='competing-metadata', timeout=.05):
                pytest.fail('An unconfirmed child released its resource reservation.')
        assert not (tmp_path / 'published.mp3').exists()

        permit.set()
        owner.close()  # Also works when no cleanup thread could be constructed.
        assert released.wait(10), 'Exact child/reader cleanup did not release retained ownership.'
        assert owner.quiescent and child.poll() is not None
        assert child.stdout.closed and child.stderr.closed
        assert not owned['directory'].exists()
        assert broker.snapshot()['leases'] == []
        with broker.reserve(request, operation_id='after-confirmed-cleanup', timeout=.5):
            assert broker.snapshot()['usage']['cpu_slots'] == 1
    finally:
        permit.set()
        if owner is not None:
            owner.close()
            assert released.wait(10)
        for child in spawned:
            real_terminate(child)


@pytest.mark.parametrize('owned', [False, True])
def test_successful_actual_child_preserves_original_result_contract(tmp_path, owned):
    destination = tmp_path / 'child-proof'
    result = process.run_process(
        [sys.executable, '-I', '-B', '-c',
         "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'actual'); print('complete')",
         str(destination)],
        budget=AudioBudget(timeout=15, reserve_disk_bytes=0), owned_tree=owned)
    assert destination.read_bytes() == b'actual'
    assert result.returncode == 0 and result.stdout.strip() == b'complete'
    assert result.stderr == b'' and not result.stopped


def test_plain_metadata_process_cannot_request_capture_stop(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid metadata/capture ownership combination started a child.')
    monkeypatch.setattr(process.subprocess, 'Popen', forbidden)
    with pytest.raises(ValueError, match='requires an owned process'):
        process.run_process([sys.executable, '-I', '-B', '-c', 'pass'],
                            owned_tree=False, stop_requested=lambda: False)
