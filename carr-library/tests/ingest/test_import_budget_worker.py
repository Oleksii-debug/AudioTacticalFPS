"""Actual budgeted worker protocol, native ownership and retained admission."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import process
from carr_library.audio.cleanup import AudioCleanupPending
from carr_library.ingest import worker
from carr_library.ingest.resource_budget import ImportBudget, MIB
from carr_library.ingest.streamed_text import TextProfile
from carr_library.operations import ResourceBroker, ResourceRequest, ResourceTimeout


def policy(**changes):
    return replace(ImportBudget(source_bytes=2*MIB, segment_bytes=MIB,
        segments=1000, steps=1001, seconds=30, memory_bytes=768*MIB,
        library_bytes=64*MIB, temporary_bytes=64*MIB), **changes)


def source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Real owned budgeted import')
    original = tmp_path / 'Original Ω.txt'
    original.write_bytes('Exact native source.\r\n\fSecond retained fragment.'.encode('utf-8'))
    return app, workspace, original


def inject_barrier(monkeypatch, tmp_path, *, committed):
    marker = tmp_path / 'owned-worker.json'
    actual_start, actual_stop = process._start_process, process._stop_owned
    children, stages, commands = [], [], []
    injection = """
import json, os, sys, time
from pathlib import Path
from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.ingest import worker as selected_worker
original = _CatalogPublisher.publish_revision if COMMITTED else LibraryApp._import_file
def barrier(self, *args, **kwargs):
    result = original(self, *args, **kwargs) if COMMITTED else None
    path = Path(sys.argv[1])
    with path.open('rb') as held:
        raw = held.read()
        target = Path(MARKER)
        incoming = target.with_suffix('.pending')
        with incoming.open('w', encoding='utf-8') as output:
            json.dump(dict(pid=os.getpid(), executable=sys.executable, prefix=sys.prefix,
                           module=selected_worker.__file__, protocol_sha256=__import__('hashlib').sha256(raw).hexdigest(),
                           isolated=sys.flags.isolated, bytecode=sys.dont_write_bytecode), output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(incoming, target)
        time.sleep(30)
    return result if COMMITTED else original(self, *args, **kwargs)
if COMMITTED:
    _CatalogPublisher.publish_revision = barrier
else:
    LibraryApp._import_file = barrier
""".replace('COMMITTED', repr(committed)).replace('MARKER', repr(str(marker)))
    def start(args, **options):
        command = list(args)
        if '-c' not in command:
            return actual_start(args, **options)
        index = command.index('-c') + 1
        if "_load_worker_input" not in command[index]:
            return actual_start(args, **options)
        assert options['owned_tree'] is True
        assert command[1:6] == ['-I', '-B', '-X', 'utf8', '-c']
        assert options['env']['TMPDIR'] == options['env']['TEMP'] == options['env']['TMP']
        assert options['env']['OMP_NUM_THREADS'] == '1'
        protocol = Path(command[index + 1])
        assert protocol.parent == Path(options['env']['TEMP'])
        assert protocol.read_bytes() and sha256(protocol.read_bytes()).hexdigest() == command[index + 2]
        command[index] = command[index].replace('runpy.run_module(',
            'exec(' + repr(injection) + '); runpy.run_module(', 1)
        child = actual_start(command, **options)
        children.append(child)
        stages.append(protocol.parent)
        commands.append(tuple(args))
        return child
    monkeypatch.setattr(process, '_start_process', start)
    return marker, children, stages, commands, actual_stop


def assert_child_closed(child):
    assert child.poll() is not None
    assert child.stdout.closed and child.stderr.closed
    if os.name == 'nt':
        assert child._carr_job.handle is None and child._carr_job.port is None


def test_actual_budgeted_reply_is_utf8_bytes_and_keeps_current_interpreter(tmp_path, monkeypatch):
    app, workspace, original = source(tmp_path)
    observed, actual = [], process.run_process
    def run(args, **options):
        if not any('_load_worker_input' in value for value in args):
            return actual(args, **options)
        assert options['owned_tree'] is True and options['allow_failure'] is True
        assert options['budget'].threads == 1 and options['budget'].timeout == policy().seconds
        result = actual(args, **options)
        assert type(result.stdout) is bytes
        observed.append((tuple(args), result))
        return result
    monkeypatch.setattr(process, 'run_process', run)
    result = app.import_file(workspace, original, title='Збережений оригінал', import_budget=policy())
    assert result['state'] == 'complete' and result['verified'] and result['isolated'], result
    assert len(observed) == 1
    command, native = observed[0]
    assert len(native.stdout) <= worker.MAX_REPLY
    assert json.loads(native.stdout.decode('utf-8'))['result']['state'] == 'complete'
    assert command[1:6] == ('-I', '-B', '-X', 'utf8', '-c')
    if os.name == 'nt' and sys.prefix != sys.base_prefix and sys._base_executable:
        assert Path(command[0]).resolve() == Path(sys._base_executable).resolve()
    else:
        assert Path(command[0]).resolve() == Path(sys.executable).resolve()
    request = worker.load_request(app.root, result['request_id'])
    assert request['source_profile'] == TextProfile(policy().source_bytes, policy()).to_dict()
    hit, = app.search(workspace, 'Second retained fragment')
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Second retained fragment'


def test_pending_native_import_keeps_protocol_request_lock_and_shared_reservation(
        tmp_path, monkeypatch):
    app, workspace, original = source(tmp_path)
    marker, children, stages, commands, actual_stop = inject_barrier(
        monkeypatch, tmp_path, committed=False)
    allow_cleanup = Event()
    errors = []
    def stop(child):
        if any(child is selected for selected in children) and not allow_cleanup.is_set():
            return False
        return actual_stop(child)
    monkeypatch.setattr(process, '_stop_owned', stop)
    owner = None
    try:
        with pytest.raises(AudioCleanupPending) as caught:
            app.import_file(workspace, original, import_budget=policy(), cancelled=marker.exists)
        owner = caught.value.cleanup_owner
        assert not owner.quiescent
        child, = children
        stage, = stages
        assert marker.exists() and child.poll() is None
        record = json.loads(marker.read_bytes())
        assert record['pid'] == child.pid
        assert Path(record['module']).resolve() == Path(worker.__file__).resolve()
        assert Path(record['prefix']).resolve() == Path(sys.prefix).resolve()
        assert record['isolated'] == 1 and record['bytecode'] is True
        protocol = stage / 'request.json'
        assert protocol.is_file() and record['protocol_sha256'] == sha256(protocol.read_bytes()).hexdigest()
        message = json.loads(protocol.read_bytes())
        request_id = message['request_id']
        current = worker.load_request(app.root, request_id)
        assert current['state'] == 'running' and 'result' not in current
        assert current['source_profile'] == TextProfile(policy().source_bytes, policy()).to_dict()
        with pytest.raises(ValueError, match='already running'):
            with worker.request_lock(app.root, request_id):
                pytest.fail('A pending native import released its exact request lock.')
        broker = ResourceBroker.for_app(app.root)
        leases = broker.snapshot()['leases']
        selected, = [entry for entry in leases if entry['operation_id'] == 'document:' + request_id]
        assert selected['request']['memory_bytes'] == policy().memory_bytes
        assert selected['request']['cpu_slots'] == 1
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots),
                                operation_id='conflicting-inspection', timeout=.05):
                pytest.fail('A pending import released its cooperative reservation.')
        assert stage.is_dir() and protocol.is_file() and original.is_file()
        with app.read(workspace) as catalog:
            assert catalog.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
        allow_cleanup.set()
        owner.close()
        assert owner.quiescent_event.wait(5)
        assert owner.notifications_complete_event.wait(5)
        assert_child_closed(child)
        assert not stage.exists()
        assert not [entry for entry in broker.snapshot()['leases']
                    if entry['operation_id'] == 'document:' + request_id]
        with worker.request_lock(app.root, request_id):
            pass
        assert worker.load_request(app.root, request_id)['state'] == 'running'
    finally:
        allow_cleanup.set()
        if owner is not None:
            owner.close()
            assert owner.quiescent_event.wait(5)
            assert owner.notifications_complete_event.wait(5)
        for child in children:
            actual_stop(child)


def test_budgeted_child_commit_survives_cancelled_acknowledgment_without_second_publication(
        tmp_path, monkeypatch):
    app, workspace, original = source(tmp_path)
    marker, children, stages, _, actual_stop = inject_barrier(
        monkeypatch, tmp_path, committed=True)
    try:
        result = app.import_file(workspace, original, import_budget=policy(), cancelled=marker.exists)
        assert result['state'] == 'complete' and result['publication_recovered'] is True, result
        child, = children
        assert json.loads(marker.read_bytes())['pid'] == child.pid
        assert_child_closed(child)
        assert not stages[0].exists()
        with app.read(workspace) as catalog:
            assert catalog.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
            assert catalog.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1
        saved = worker.load_request(app.root, result['request_id'])
        assert saved['state'] == 'complete' and saved['result'] == result
        repeated = app.resume_import(workspace, result['request_id'])
        assert repeated['id'] == result['id'] and repeated['state'] == 'complete'
        assert len(children) == 1
    finally:
        for child in children:
            actual_stop(child)


@pytest.mark.parametrize('payload', [
    b'{"result":{"state":"complete"}}' + b' ' * worker.MAX_REPLY,
    b'{"result":{"state":"\\xff"}}'.replace(b'\\xff', b'\xff'),
    '{"result":{"state":"complete"}}'.encode('utf-16'),
], ids=['oversized-json', 'invalid-utf8', 'utf16-json'])
def test_owned_reply_byte_and_utf8_limits_refuse_real_child_output(tmp_path, monkeypatch, payload):
    """Only the child payload is synthetic; native process/readers are real."""
    actual = process.run_process
    children = []
    payload_file = tmp_path / 'child-reply.bin'
    payload_file.write_bytes(payload)
    def run(args, **options):
        code = ('import os; from pathlib import Path; os.write(1, Path('
                + repr(str(payload_file)) + ').read_bytes())')
        command = list(args)
        index = command.index('-c') + 1
        command[index] = code
        result = actual(command, **options)
        children.append(result)
        return result
    monkeypatch.setattr(process, 'run_process', run)
    result = worker._invoke_owned({'protocol': 'finite fixture'}, timeout=5,
                                 cancelled=lambda: False, import_budget=policy())
    assert result['state'] == 'failed'
    assert len(children) == 1 and children[0].returncode == 0


def test_protocol_file_hash_is_checked_before_worker_input_is_returned(tmp_path):
    path = tmp_path / 'request.json'
    raw = b'{"request":"actual bounded bytes"}'
    path.write_bytes(raw)
    expected = sha256(raw).hexdigest()
    assert worker._load_worker_input(path, expected) == raw
    path.write_bytes(raw.replace(b'actual', b'edited'))
    with pytest.raises(ValueError, match='captured protocol'):
        worker._load_worker_input(path, expected)


def test_invalid_timeout_cannot_create_an_import_request(tmp_path):
    app, workspace, original = source(tmp_path)
    directory = app.root / 'import-requests'
    before = set(directory.iterdir()) if directory.exists() else set()
    with pytest.raises(ValueError, match='timeout'):
        app.import_file(workspace, original, import_budget=policy(), timeout=policy().seconds + 1)
    after = set(directory.iterdir()) if directory.exists() else set()
    assert not [path for path in after - before if path.suffix == '.json']
    with app.read(workspace) as catalog:
        assert catalog.execute('SELECT count(*) FROM revision').fetchone()[0] == 0


@pytest.mark.parametrize('origin', ['operation', 'runtime-close'])
@pytest.mark.parametrize('masked', [False, True], ids=['direct-owner', 'causal-owner'])
def test_portable_batch_cli_drains_same_real_owner_before_return_or_runtime_close(
        tmp_path, monkeypatch, origin, masked):
    import subprocess
    import threading
    from carr_library.audio.cleanup import retained_temporary_directory
    from carr_library.ingest import batch_cli
    from carr_library import native_runtime

    ready, release, finished = Event(), Event(), Event()
    children, stages, pending, delivered, handle_closed = [], [], [], [], []
    actual_stop = process._stop_owned
    def stop(child):
        if any(child is selected for selected in children) and not release.is_set():
            return False
        return actual_stop(child)
    monkeypatch.setattr(process, '_stop_owned', stop)

    def own_child():
        try:
            with retained_temporary_directory(prefix='capacity-cli-', dir=tmp_path) as name:
                stage = Path(name)
                (stage / 'retained-input').write_bytes(b'Exact temporary owned input.')
                stages.append(stage)
                with process.owned_process(
                        [str(Path(sys.executable).resolve()), '-I', '-c',
                         'import time; time.sleep(30)'],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL) as child:
                    children.append(child)
        except AudioCleanupPending as error:
            pending.append(error)
            ready.set()
            if masked:
                raise ValueError('Original caller error with retained native cause.') from error
            raise

    class Handle:
        def close(self):
            if origin == 'runtime-close':
                own_child()
            assert pending and pending[0].cleanup_owner.notifications_complete_event.is_set()
            handle_closed.append(True)

    def run(argv):
        assert argv == []
        if origin == 'operation':
            own_child()
        return 0

    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', lambda: Handle())
    monkeypatch.setattr(batch_cli, 'run', run)
    def execute():
        try:
            # The portable CLI function owns its own drain; managed entrypoint
            # leasing is covered separately by the real installation suite.
            batch_cli.main.__wrapped__([])
        except BaseException as error:
            delivered.append(error)
        finally:
            finished.set()
    runner = threading.Thread(target=execute)
    runner.start()
    try:
        assert ready.wait(10)
        assert len(children) == len(stages) == len(pending) == 1
        assert children[0].poll() is None and stages[0].is_dir()
        assert (stages[0] / 'retained-input').read_bytes() == b'Exact temporary owned input.'
        assert not finished.is_set() and not handle_closed
        assert not pending[0].cleanup_owner.quiescent
        release.set()
        runner.join(10)
        assert not runner.is_alive() and finished.is_set()
        assert len(delivered) == 1
        if masked:
            assert type(delivered[0]) is ValueError
            assert delivered[0].__cause__ is pending[0]
        else:
            assert delivered[0] is pending[0]
        assert pending[0].cleanup_owner.quiescent_event.is_set()
        assert pending[0].cleanup_owner.notifications_complete_event.is_set()
        assert children[0].poll() is not None and not stages[0].exists()
        if os.name == 'nt':
            assert children[0]._carr_job.handle is None
        assert handle_closed == ([True] if origin == 'operation' else [])
    finally:
        release.set()
        for error in pending:
            error.cleanup_owner.close()
            assert error.cleanup_owner.quiescent_event.wait(5)
            assert error.cleanup_owner.notifications_complete_event.wait(5)
        runner.join(10)
        for child in children:
            actual_stop(child)
        assert not runner.is_alive()
