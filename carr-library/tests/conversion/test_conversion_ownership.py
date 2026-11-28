"""Owned document workers retain their exact snapshot and shared reservation."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

import carr_library.audio.process as process_module
import carr_library.conversion.service as service_module
from carr_library.audio.cleanup import AudioCleanupPending
from carr_library.conversion import ConversionError, ConversionService
from carr_library.operations import (
    ResourceBroker, ResourceLimits, ResourceRequest, ResourceTimeout, ResourceUnavailable)


def _case(tmp_path, *, admitted=True, limits=None):
    inputs, outputs, library = (tmp_path / name for name in ('inputs', 'outputs', 'library'))
    inputs.mkdir()
    outputs.mkdir()
    library.mkdir()
    broker = ResourceBroker.for_app(library, limits=limits or ResourceLimits(
        cpu_slots=1, memory_bytes=2 * 1024**3, disk_bytes=512 * 1024**2,
        min_free_disk_bytes=0)) if admitted else None
    converter = ConversionService(outputs, input_roots=(inputs,), broker=broker)
    source = inputs / 'original.txt'
    original = '  Український текст 😀.\r\nExact selected source.\t '.encode('utf-8')
    source.write_bytes(original)
    return converter, source, original, outputs, broker


def _forbid_source_read(monkeypatch, source):
    opening = Path.open

    def guarded(path, mode='r', *args, **kwargs):
        if path == source and 'r' in mode:
            pytest.fail('Source bytes were opened before resource/source admission.')
        return opening(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guarded)


def test_actual_isolated_conversion_keeps_borrowed_admission_through_receipt(tmp_path, monkeypatch):
    converter, source, original, outputs, broker = _case(tmp_path)
    actual_run, actual_link = service_module.run_process, os.link
    requests, publications = [], []

    def run(args, **kwargs):
        assert kwargs['owned_tree'] is True
        assert args[1:4] == ['-I', '-B', '-m']
        assert args[4] == 'carr_library.conversion.worker'
        assert kwargs['budget'].threads == 1
        assert kwargs['env']['OMP_THREAD_LIMIT'] == '1'
        leases = broker.snapshot()['leases']
        assert len(leases) == 1
        requests.append(leases[0]['request'])
        return actual_run(args, **kwargs)

    def link(staged, destination, *args, **kwargs):
        if Path(destination).parent == outputs:
            leases = broker.snapshot()['leases']
            assert len(leases) == 1
            publications.append(Path(destination).name)
        return actual_link(staged, destination, *args, **kwargs)

    monkeypatch.setattr(service_module, 'run_process', run)
    monkeypatch.setattr(os, 'link', link)
    result = converter.extract(source, 'reading.txt')
    assert result.path.read_bytes() == original
    assert source.read_bytes() == original
    assert result.report['source_sha256'] == sha256(original).hexdigest()
    assert result.report['text_sha256'] == sha256(original).hexdigest()
    assert result.report['verified_for_quotation'] is False
    assert json.loads(result.report_path.read_text(encoding='utf-8')) == result.report
    assert publications == ['reading.txt', 'reading.txt.provenance.json']
    assert len(requests) == 1
    request = requests[0]
    assert request['cpu_slots'] == 1
    assert request['memory_bytes'] == converter.budget.memory_bytes
    assert request['disk_path'] == str(outputs)
    assert len(original) + converter.budget.max_text_bytes <= request['disk_bytes'] < 256 * 1024**2
    assert not tuple(outputs.glob('.pending-conversion-*'))
    assert broker.snapshot()['leases'] == []


def test_shared_memory_denial_occurs_before_snapshot_or_native_launch(tmp_path, monkeypatch):
    converter, source, original, outputs, broker = _case(tmp_path, limits=ResourceLimits(
        cpu_slots=1, memory_bytes=0, disk_bytes=512 * 1024**2, min_free_disk_bytes=0))
    with monkeypatch.context() as patch:
        _forbid_source_read(patch, source)
        patch.setattr(service_module, 'run_process',
                      lambda *_a, **_k: pytest.fail('Denied conversion launched a child.'))
        with pytest.raises(ResourceUnavailable):
            converter.extract(source, 'denied.txt')
    assert source.read_bytes() == original
    assert tuple(outputs.iterdir()) == ()
    assert broker.snapshot()['leases'] == []


def test_cancellation_while_actually_queued_preserves_the_other_reservation(tmp_path, monkeypatch):
    converter, source, original, outputs, broker = _case(tmp_path)
    queued = threading.Event()
    reason = broker._reason

    def observe_queue(*args):
        result = reason(*args)
        if result == 'cpu_slots':
            queued.set()
        return result

    monkeypatch.setattr(broker, '_reason', observe_queue)
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='other-document', timeout=0):
        with monkeypatch.context() as patch:
            _forbid_source_read(patch, source)
            patch.setattr(service_module, 'run_process',
                          lambda *_a, **_k: pytest.fail('Queued conversion launched a child.'))
            with pytest.raises(InterruptedError, match='cancelled'):
                converter.extract(source, 'cancelled.txt', cancelled=queued.is_set)
        assert queued.is_set()
        leases = broker.snapshot()['leases']
        assert len(leases) == 1 and leases[0]['operation_id'] == 'other-document'
        assert tuple(outputs.iterdir()) == ()
    assert source.read_bytes() == original
    assert broker.snapshot()['leases'] == []


@pytest.mark.parametrize('changed', ['source', 'destination', 'directory'])
def test_admission_wait_rechecks_selected_objects_before_opening_source(tmp_path, monkeypatch, changed):
    converter, source, original, outputs, broker = _case(tmp_path)
    replacement = source.with_name('replacement.txt')
    replacement.write_bytes(b'X' * len(original))
    target = outputs / 'reading.txt'
    reserve = broker.reserve

    @contextmanager
    def after_wait(*args, **kwargs):
        with reserve(*args, **kwargs) as lease:
            if changed == 'source':
                os.replace(replacement, source)
            elif changed == 'destination':
                target.write_bytes(b'Competing user file')
            else:
                outputs.rename(tmp_path / 'original-output-directory')
                outputs.mkdir()
            yield lease

    monkeypatch.setattr(broker, 'reserve', after_wait)
    with monkeypatch.context() as patch:
        _forbid_source_read(patch, source)
        patch.setattr(service_module, 'run_process',
                      lambda *_a, **_k: pytest.fail('Changed selection launched a child.'))
        expected = FileExistsError if changed == 'destination' else ConversionError
        with pytest.raises(expected):
            converter.extract(source, target)
    assert source.read_bytes() == (b'X' * len(original) if changed == 'source' else original)
    if changed == 'destination':
        assert target.read_bytes() == b'Competing user file'
    else:
        assert not target.exists()
    assert not target.with_name(target.name + '.provenance.json').exists()
    assert not tuple(outputs.glob('.pending-conversion-*'))
    assert broker.snapshot()['leases'] == []


_HELD_CHILD = """
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time
source, ready = map(Path, sys.argv[1:])
with source.open('rb') as stream:
    data = stream.read()
    staged = ready.with_name(ready.name + '.tmp')
    with staged.open('x', encoding='utf-8') as output:
        json.dump({'pid': os.getpid(), 'source_sha256': sha256(data).hexdigest()}, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(staged, ready)
    while True:
        time.sleep(.05)
"""


@pytest.mark.parametrize('admitted,wrapper', [
    (True, 'direct'), (True, 'cause'), (True, 'context'), (True, 'cancel'), (False, 'direct')],
    ids=['shared-direct', 'shared-cause', 'shared-context', 'shared-cancel', 'standalone-direct'])
def test_actual_live_child_retains_snapshot_and_lease_until_confirmed_exit(
        tmp_path, monkeypatch, admitted, wrapper):
    _assert_actual_live_child_retention(tmp_path, monkeypatch, admitted, wrapper)


def _assert_actual_live_child_retention(tmp_path, monkeypatch, admitted, wrapper, *,
                                        before_launch=None, after_ready=None):
    converter, source, original, outputs, broker = _case(tmp_path, admitted=admitted)
    allowed, cancel, callbacks_done = threading.Event(), threading.Event(), threading.Event()
    stages, native, pending, commands = [], [], [], []
    actual_stop, actual_run = process_module._stop_owned, process_module.run_process
    actual_start = process_module._start_process

    def start(args, **options):
        selected = bool(commands) and tuple(args) == commands[0]
        if selected:
            assert options['owned_tree'] is True and not native
        child = actual_start(args, **options)
        if selected:
            native.append(child)
        return child

    def temporarily_unconfirmed(process):
        # Other retained cleanup owners may retry in this same interpreter.
        # Delay only the exact child returned for this conversion's command.
        if not native or process is not native[0]:
            return actual_stop(process)
        assert native[0] is process
        if not allowed.is_set():
            raise RuntimeError('This exact child has not yet confirmed exit.')
        return actual_stop(process)

    def held_parser(args, **kwargs):
        assert kwargs['owned_tree'] is True
        stage = Path(kwargs['cwd'])
        stages.append(stage)
        snapshot, ready = stage / 'source.txt', stage / 'child-ready.json'
        original_cancelled = kwargs['cancelled']
        startup_deadline = time.monotonic() + 20

        def stopped():
            if ready.exists():
                if after_ready is not None and not cancel.is_set():
                    after_ready()
                cancel.set()
            elif time.monotonic() >= startup_deadline:
                raise AssertionError('The actual child did not acknowledge its open snapshot.')
            return original_cancelled()

        kwargs['cancelled'] = stopped
        command = [str(Path(sys.executable).absolute()), '-I', '-B', '-u', '-c',
                   _HELD_CHILD, str(snapshot), str(ready)]
        commands.append(tuple(command))
        try:
            if before_launch is not None:
                before_launch()
            return actual_run(command, **kwargs)
        except AudioCleanupPending as error:
            pending.append(error.cleanup_owner)
            if wrapper == 'cause':
                raise OSError('A later status write masked pending cleanup.') from error
            if wrapper == 'context':
                raise OSError('A later status write masked pending cleanup.')
            if wrapper == 'cancel':
                raise InterruptedError('Cancellation still owns an unconfirmed child.') from error
            raise

    monkeypatch.setattr(process_module, '_start_process', start)
    monkeypatch.setattr(process_module, '_stop_owned', temporarily_unconfirmed)
    monkeypatch.setattr(service_module, 'run_process', held_parser)
    try:
        expected = (AudioCleanupPending if wrapper == 'direct' else
                    InterruptedError if wrapper == 'cancel' else OSError)
        with pytest.raises(expected):
            converter.extract(source, 'never-published.txt', cancelled=cancel.is_set)
        assert len(stages) == len(native) == len(pending) == 1
        stage, process, cleanup = stages[0], native[0], pending[0]
        ready = json.loads((stage / 'child-ready.json').read_text(encoding='utf-8'))
        assert type(ready['pid']) is int and ready['pid'] > 0
        # A Windows venv redirector and its actual Python child may differ.
        # Native family ownership, not PID equality, is the cleanup authority.
        assert ready['source_sha256'] == sha256(original).hexdigest()
        assert (stage / 'source.txt').read_bytes() == original
        assert process.poll() is None
        assert not cleanup.quiescent and cleanup.close() is False
        assert not (outputs / 'never-published.txt').exists()
        assert not (outputs / 'never-published.txt.provenance.json').exists()
        if broker is not None:
            leases = broker.snapshot()['leases']
            assert len(leases) == 1
            with pytest.raises(ResourceTimeout):
                with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='competing-document', timeout=0):
                    pytest.fail('Live native ownership released its shared resource slot.')
        cleanup.add_quiescent_callback(callbacks_done.set)
        allowed.set()
        deadline = time.monotonic() + 15
        while not callbacks_done.is_set() and time.monotonic() < deadline:
            cleanup.close()
            callbacks_done.wait(.02)
        assert callbacks_done.is_set() and cleanup.quiescent
        assert process.poll() is not None
        assert not stage.exists()
        assert not tuple(outputs.iterdir())
        if broker is not None:
            assert broker.snapshot()['leases'] == []
        assert source.read_bytes() == original
    finally:
        # An assertion failure must not leave this test's actual child running.
        allowed.set()
        for cleanup in pending:
            deadline = time.monotonic() + 15
            while not cleanup.quiescent and time.monotonic() < deadline:
                cleanup.close()
                cleanup.quiescent_event.wait(.02)
            assert cleanup.quiescent, 'Actual owned child cleanup did not complete.'


@pytest.mark.parametrize('broker', [False, object()], ids=['false', 'untyped-object'])
def test_foreign_broker_declaration_is_rejected_before_creating_output(tmp_path, broker):
    output = tmp_path / 'must-not-be-created'
    with pytest.raises(TypeError, match='shared resource broker'):
        ConversionService(output, input_roots=(tmp_path,), broker=broker)
    assert not output.exists()


@pytest.mark.parametrize('phase', ['before-launch', 'after-ready'])
def test_conversion_cleanup_delay_leaves_a_distinct_owned_process_independent(
        tmp_path, monkeypatch, phase):
    import subprocess
    from carr_library.audio.cleanup import CleanupOwner

    actual_stop = process_module._stop_owned
    with process_module.owned_process(
            [str(Path(sys.executable).absolute()), '-I', '-B', '-c', 'pass'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False) as other:
        owner = CleanupOwner(lambda: process_module._stop_owned(other))
        observed = []
        try:
            # Keep a real native owner after its leader exits, just as a retained
            # cleanup can be retried while another operation starts or cancels.
            assert other.wait(timeout=5) == 0

            def close_other():
                assert not owner.quiescent
                assert owner.close() is True
                assert owner.quiescent
                observed.append(other)

            _assert_actual_live_child_retention(
                tmp_path, monkeypatch, True, 'direct',
                before_launch=close_other if phase == 'before-launch' else None,
                after_ready=close_other if phase == 'after-ready' else None)
            assert observed == [other]
        finally:
            # Dispose this distinct owner if a regression assertion interrupts
            # the scenario. The existing outer owner retains any uncertain exit.
            if not owner.quiescent:
                actual_stop(other)
