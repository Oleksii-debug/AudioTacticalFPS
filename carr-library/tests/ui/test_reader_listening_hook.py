"""Execute the real reader bridge: serial ownership, cancellation and late cleanup."""
import ast
from concurrent.futures import Future
from importlib.util import find_spec
from pathlib import Path
import threading
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
    audio_files, reader_root, owned_listening = state.audio_files, state.root, state.owned
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
        guard=Mock(), report=Mock(), ui=[], works=[], busy=False, stopped=False)
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
    def run(work, done, *, on_failed):
        if state.busy: return False
        state.cancel.clear()
        state.works.append((work, done, on_failed))
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
        guard=Mock(), report=Mock(), wx=SimpleNamespace(CallAfter=Mock()), player=Mock())
    def run(work, done, *, on_failed):
        on_failed(RuntimeError('Executor stopped.'))
        return False
    state.run = run
    prepare, _, _, _ = closures(state)
    failure = Mock()
    prepare(object(), Mock(), failure, active=lambda: True)
    failure.assert_called_once()
