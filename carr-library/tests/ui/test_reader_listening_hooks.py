"""Execute the real reader bridge: serial ownership, cancellation and late cleanup."""
import ast
from concurrent.futures import Future, ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
import threading
import queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


class Queue:
    def __init__(self): self.pending = []
    def submit(self, callback, *args):
        future = Future()
        self.pending.append((callback, args, future))
        return future
    def drain(self):
        while self.pending:
            callback, args, future = self.pending.pop(0)
            try: future.set_result(callback(*args))
            except BaseException as exc: future.set_exception(exc)


class Prepared:
    def __init__(self): self.closed, self.owned, self.error = False, False, None
    def close(self):
        if self.error: raise self.error
        if self.owned: raise RuntimeError('Native player still owns this PCM.')
        self.closed = True


def closures(state):
    path = Path(find_spec('carr_library.ui.speech_dialog').origin)
    module = ast.parse(path.read_text(encoding='utf-8'))
    outer = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'show_speech_reader')
    prepare = next(n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == 'prepare_listening')
    resources = next(n for n in ast.walk(outer) if isinstance(n, ast.ClassDef) and n.name == 'ReaderResources')
    factory = ast.parse("""
def factory(state):
    playback_generation, alive = 0, True
    cancel, player, executor = state.cancel, state.player, state.executor
    audio_files, library_root, owned_listening = state.audio_files, state.root, state.owned
    check_current = state.current
    owned_registries, owned_progress_voices = set(), set()
    silence_confirmed = state.silence
    wx, playback_scope, run, report = state.wx, state.guard, state.run, state.report
    def placeholder(): pass
    def resources_placeholder(): pass
    def invalidate():
        nonlocal playback_generation
        playback_generation += 1
    def close():
        nonlocal alive
        alive = False
    return prepare_listening, ReaderResources(), invalidate, close
""")
    body = factory.body[0].body
    for name, node in [('placeholder', prepare), ('resources_placeholder', resources)]:
        body[next(i for i, n in enumerate(body) if isinstance(n, ast.FunctionDef) and n.name == name)] = node
    namespace = dict(threading=threading, __package__='carr_library.ui')
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), namespace)
    return namespace['factory'](state)


@pytest.fixture
def bridge(tmp_path):
    state = SimpleNamespace(root=tmp_path, cancel=threading.Event(), owned=set(), executor=Queue(),
        audio_files=SimpleNamespace(name=str(tmp_path), cleanup=Mock()), silence=True,
        guard=Mock(), current=Mock(), report=Mock(), ui=[], works=[], busy=False, stopped=False)
    state.wx = SimpleNamespace(CallAfter=lambda callback, *args: state.ui.append((callback, args)))
    value = Prepared()
    state.value = value
    def prepared(request, directory, *, state_root, cancelled):
        assert directory == str(tmp_path) and state_root == tmp_path / 'state'
        assert not cancelled()
        return value
    def applied(result):
        assert result is value and not result.closed
        result.owned = True
    state.player = SimpleNamespace(prepare_listening=Mock(side_effect=prepared),
        apply_listening=Mock(side_effect=applied), play=Mock(), resume=Mock())
    def run(work, done, *, on_error):
        if state.busy: return False
        state.cancel.clear()
        state.works.append((work, done, on_error))
        return True
    state.run = run
    state.prepare, state.resources, state.invalidate, state.close = closures(state)
    state.completed, state.failed = Mock(), Mock()
    def start():
        state.cancel_request = state.prepare(object(), state.completed, state.failed,
                                            active=lambda: not state.stopped)
    state.start = start
    def finish(*, before_adoption=None):
        work, done, failed = state.works.pop(0)
        try:
            result = work()
            if before_adoption: before_adoption()
            done(result)
        except Exception as exc:
            failed(exc)
    state.finish = finish
    yield state
    state.player.play.assert_not_called()
    state.player.resume.assert_not_called()


def test_preparation_uses_existing_worker_and_transfers_ownership_without_autoplay(bridge):
    bridge.start()
    bridge.player.prepare_listening.assert_not_called()
    bridge.finish()
    bridge.completed.assert_called_once_with()
    bridge.failed.assert_not_called()
    assert bridge.value.owned and not bridge.value.closed and not bridge.owned
    bridge.resources.close()
    bridge.audio_files.cleanup.assert_called_once()


@pytest.mark.parametrize('change', ['modal', 'generation', 'cancel', 'reader'])
def test_cancelled_before_work_cannot_create_or_adopt_a_listening_copy(bridge, change):
    bridge.start()
    if change == 'modal': bridge.stopped = True
    elif change == 'generation': bridge.invalidate()
    elif change == 'cancel': bridge.cancel_request()
    elif change == 'reader': bridge.close()
    bridge.finish()
    bridge.player.prepare_listening.assert_not_called()
    bridge.player.apply_listening.assert_not_called()
    if change in {'modal', 'reader'}:
        bridge.failed.assert_not_called()
    else:
        bridge.failed.assert_called_once()
    assert not bridge.owned


@pytest.mark.parametrize('change', ['modal', 'generation', 'source', 'adoption'])
def test_result_that_becomes_stale_on_native_queue_is_released_on_serial_worker(bridge, change):
    bridge.start()
    def changed():
        if change == 'modal': bridge.stopped = True
        elif change == 'generation': bridge.invalidate()
        elif change == 'source': bridge.guard.side_effect = ValueError('Source scope revoked.')
        else: bridge.player.apply_listening.side_effect = ValueError('Playback request changed.')
    bridge.finish(before_adoption=changed)
    if change == 'modal':
        bridge.failed.assert_not_called()
    else:
        bridge.failed.assert_called_once()
    bridge.completed.assert_not_called()
    assert bridge.owned == {bridge.value} and not bridge.value.closed
    bridge.executor.drain()
    assert bridge.value.closed and not bridge.owned


def test_dropped_ui_completion_is_released_by_reader_cleanup_after_worker(bridge):
    bridge.start()
    work, done, failed = bridge.works.pop(0)
    value = work()
    assert value in bridge.owned
    bridge.close()  # The real run() drops its native completion after close.
    bridge.resources.close()
    assert value.closed and not bridge.owned
    bridge.player.apply_listening.assert_not_called()
    bridge.audio_files.cleanup.assert_called_once()


def test_cleanup_failure_retains_pcm_and_parent_directory_for_retry(bridge):
    bridge.owned.add(bridge.value)
    bridge.value.error = OSError('Owned derivative is still open.')
    with pytest.raises(RuntimeError, match='still open'):
        bridge.resources.close()
    assert bridge.owned == {bridge.value} and not bridge.value.closed
    bridge.audio_files.cleanup.assert_not_called()
    bridge.value.error = None
    bridge.resources.close()
    assert not bridge.owned and bridge.value.closed
    bridge.audio_files.cleanup.assert_called_once()


def test_failed_worker_postcheck_retains_cleanup_failure_until_resource_close(bridge):
    def make_and_revoke(*args, **kwargs):
        bridge.guard.side_effect = ValueError('Source authorization changed.')
        bridge.value.error = OSError('Temporary PCM close failed.')
        return bridge.value
    bridge.player.prepare_listening.side_effect = make_and_revoke
    bridge.start(); bridge.finish()
    bridge.failed.assert_called_once()
    assert bridge.owned == {bridge.value}
    bridge.audio_files.cleanup.assert_not_called()
    bridge.value.error = None
    bridge.resources.close()
    assert bridge.value.closed and not bridge.owned


def test_busy_admission_calls_failure_once_and_does_not_leave_modal_preparing(bridge):
    bridge.busy = True
    bridge.start()
    bridge.failed.assert_called_once()
    assert not bridge.works and not bridge.owned


def test_run_submission_error_is_not_reported_twice(tmp_path):
    state = SimpleNamespace(root=tmp_path, cancel=threading.Event(), owned=set(), executor=Queue(),
        audio_files=SimpleNamespace(name=str(tmp_path), cleanup=Mock()), silence=True,
        guard=Mock(), current=Mock(), report=Mock(), wx=SimpleNamespace(CallAfter=Mock()), player=Mock())
    def run(work, done, *, on_error):
        on_error(RuntimeError('Executor stopped.'))
        return False
    state.run = run
    prepare, _, _, _ = closures(state)
    failure = Mock()
    prepare(object(), Mock(), failure, active=lambda: True)
    failure.assert_called_once()



def actual_closures(state):
    """Extract the unchanged real run/finish and current-scope checks together."""
    from carr_library.durable_state import local_path, regular
    path = Path(find_spec('carr_library.ui.speech_dialog').origin)
    module = ast.parse(path.read_text(encoding='utf-8'))
    outer = next(node for node in module.body
                 if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    required = {'check_current', 'playback_scope', 'run', 'prepare_listening'}
    definitions = [node for node in outer.body
                   if isinstance(node, ast.FunctionDef) and node.name in required]
    assert {node.name for node in definitions} == required
    resources = next(node for node in ast.walk(outer)
                     if isinstance(node, ast.ClassDef) and node.name == 'ReaderResources')
    factory = ast.parse("""
def factory(state):
    playback_generation, alive, busy, source_loaded = 0, True, False, True
    cancel, player, executor = state.cancel, state.player, state.executor
    audio_files, library_root, owned_listening = state.audio_files, state.root, state.owned
    parent, library = state.parent, state.parent.library
    information = regular(local_path(library_root, exists=True), directory=True)
    library_identity = information.st_dev, information.st_ino
    source_guard = state.source_guard
    owned_registries, owned_progress_voices = set(), set()
    silence_confirmed = True
    wx, report = state.wx, state.report
    capabilities, reload_button = state.capabilities, state.reload_button
    def invalidate():
        nonlocal playback_generation
        playback_generation += 1
    def close_reader():
        nonlocal alive
        alive = False
        cancel.set()
    return prepare_listening, ReaderResources(), invalidate, close_reader
""")
    factory.body[0].body[-1:-1] = [*definitions, resources]
    namespace = dict(Path=Path, threading=threading, local_path=local_path,
                     regular=regular, __package__='carr_library.ui')
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), namespace)
    return namespace['factory'](state)


@pytest.fixture
def actual_bridge(tmp_path):
    # Only the conversion boundary is controlled. The real serial executor,
    # run.finish, root checks, queue ordering and private-file deletion execute.
    root = tmp_path / 'library'
    root.mkdir()
    original = tmp_path / 'retained-original.wav'
    original.write_bytes(b'original audio must remain unchanged')
    private = tmp_path / 'reader-private'
    private.mkdir()
    state = SimpleNamespace(root=root, original=original, owned=set(),
        parent=SimpleNamespace(library=SimpleNamespace(root=root), _closing=False),
        executor=ThreadPoolExecutor(max_workers=1, thread_name_prefix='real-listening-bridge'),
        cancel=threading.Event(), ui=queue.Queue(), report=Mock(),
        source_guard=Mock(), capabilities=Mock(), reload_button=Mock(),
        audio_files=SimpleNamespace(name=str(private), cleanup=Mock()),
        active=True, prepared_thread=None, cleanup_threads=[], cleanup_error=None)
    class FilePrepared:
        def __init__(self):
            self.path = private / 'unused-listening.wav'
            self.path.write_bytes(b'owned private listening bytes')
        def close(self):
            state.cleanup_threads.append(threading.get_ident())
            if state.cleanup_error is not None:
                raise state.cleanup_error
            self.path.unlink(missing_ok=True)
    def prepared(_request, directory, *, state_root, cancelled):
        assert directory == str(private) and state_root == root / 'state'
        assert not cancelled()
        state.prepared_thread = threading.get_ident()
        state.value = FilePrepared()
        return state.value
    state.player = SimpleNamespace(prepare_listening=Mock(side_effect=prepared),
        apply_listening=Mock(), play=Mock(), resume=Mock())
    state.wx = SimpleNamespace(CallAfter=lambda callback, *args: state.ui.put((callback, args)))
    state.prepare, state.resources, state.invalidate, state.close_reader = actual_closures(state)
    state.completed, state.failed = Mock(), Mock()
    def start():
        state.cancel_request = state.prepare(object(), state.completed, state.failed,
                                            active=lambda: state.active)
        callback, args = state.ui.get(timeout=10)
        assert args[0].done(), 'The actual worker has not completed.'
        assert args[0].result() is state.value
        return callback, args
    state.start = start
    try:
        yield state
    finally:
        state.cancel.set()
        state.cleanup_error = None
        state.executor.shutdown(wait=True, cancel_futures=False)
        # Assertions in each case precede this fallback, so it cannot hide a leak.
        for value in tuple(state.owned):
            value.close()
            state.owned.discard(value)
        state.player.play.assert_not_called()
        state.player.resume.assert_not_called()


@pytest.mark.parametrize('boundary', [
    'source', 'library-object', 'library-path', 'library-inode',
    'parent-closing', 'modal', 'generation', 'request-cancel',
])
def test_real_finish_rejection_discards_unused_result_on_the_same_worker(actual_bridge, tmp_path, boundary):
    state = actual_bridge
    callback, args = state.start()
    assert state.owned == {state.value} and state.value.path.is_file()
    if boundary == 'source':
        state.source_guard.side_effect = ValueError('Source authorization revoked.')
    elif boundary == 'library-object':
        state.parent.library = SimpleNamespace(root=state.root)
    elif boundary == 'library-path':
        state.parent.library.root = tmp_path / 'different-library'
    elif boundary == 'library-inode':
        state.root.rename(tmp_path / 'old-library')
        state.root.mkdir()
    elif boundary == 'parent-closing':
        state.parent._closing = True
    elif boundary == 'modal':
        state.active = False
    elif boundary == 'generation':
        state.invalidate()
    else:
        state.cancel_request()
    callback(*args)  # The real run.finish checks scope before its on_done call.
    state.executor.submit(lambda: None).result(timeout=10)
    assert not state.owned and not state.value.path.exists()
    assert state.cleanup_threads == [state.prepared_thread]
    assert state.prepared_thread != threading.get_ident()
    state.player.apply_listening.assert_not_called()
    state.completed.assert_not_called()
    if boundary in {'source', 'generation', 'request-cancel'}:
        state.failed.assert_called_once()
    else:
        state.failed.assert_not_called()
    state.report.assert_not_called()
    state.audio_files.cleanup.assert_not_called()
    assert state.original.read_bytes() == b'original audio must remain unchanged'


def test_real_reader_close_drops_completion_and_queues_cleanup_after_worker(actual_bridge):
    from carr_library.ui.worker_cleanup import close_after_worker
    state = actual_bridge
    callback, args = state.start()
    state.close_reader()
    cleanup = close_after_worker(state.executor, state.resources)
    callback(*args)  # The existing run.finish intentionally drops a closed UI.
    cleanup.result(timeout=10)
    assert not state.owned and not state.value.path.exists()
    assert state.cleanup_threads == [state.prepared_thread]
    assert state.prepared_thread != threading.get_ident()
    state.completed.assert_not_called()
    state.failed.assert_not_called()
    state.report.assert_not_called()
    state.player.apply_listening.assert_not_called()
    state.audio_files.cleanup.assert_called_once()
    assert state.original.read_bytes() == b'original audio must remain unchanged'


def test_real_rejected_finish_keeps_failed_cleanup_owned_until_worker_retry(actual_bridge):
    state = actual_bridge
    callback, args = state.start()
    state.parent.library = SimpleNamespace(root=state.root)
    state.cleanup_error = OSError('Native owner still holds this private PCM.')
    callback(*args)
    state.executor.submit(lambda: None).result(timeout=10)
    assert state.owned == {state.value} and state.value.path.is_file()
    state.audio_files.cleanup.assert_not_called()
    notice, arguments = state.ui.get(timeout=10)
    notice(*arguments)
    state.report.assert_not_called()
    state.failed.assert_not_called()
    state.completed.assert_not_called()
    state.player.apply_listening.assert_not_called()
    state.cleanup_error = None
    state.executor.submit(state.resources.close).result(timeout=10)
    assert not state.owned and not state.value.path.exists()
    assert state.cleanup_threads == [state.prepared_thread, state.prepared_thread]
    state.audio_files.cleanup.assert_called_once()
    assert state.original.read_bytes() == b'original audio must remain unchanged'
