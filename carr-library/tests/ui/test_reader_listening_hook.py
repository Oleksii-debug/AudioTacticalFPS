"""Same-reader playback adoption with actual retained PCM and delayed GUI dispatch."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.speech.player import WavePlayer
from carr_library.speech.listening import PreparedListening, FrameMapping
from carr_library.speech.reading_tools import ReadingSnapshot
from carr_library.speech import ReadingDocument
from carr_library.ui.reader_transport import ReaderPlaybackSession


@pytest.fixture
def bridge(tmp_path):
    path = tmp_path / 'original.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(b'\0\0' * 800)
    player = WavePlayer()
    player.set_playlist(player.prepare_playlist((path,)))
    player.select_queue(0)
    state = SimpleNamespace(root=tmp_path, player=player, alive=True, active=True,
        busy=False, valid=True, cancel=Event(), executor=ThreadPoolExecutor(max_workers=1),
        document=ReadingDocument('Unchanged source'), queued=[], quiet=[], errors=[],
        prepared=[], workers=[], completions=[], notices=[], refreshes=0)
    def guard():
        if not state.valid:
            raise ValueError('Source authorization revoked.')
    def capture():
        guard()
        return ReadingSnapshot.capture(state.document)
    def verify(snapshot):
        guard()
        snapshot.check(state.document)
    def submit(work, done, *, on_error=None):
        if state.busy:
            return False
        state.busy = True
        state.cancel.clear()
        future = state.executor.submit(work)
        state.queued.append((future, done, on_error))
        return True
    def stop():
        state.cancel.set()
        player.stop()
    def prepare(request, directory, *, state_root, cancelled, broker):
        state.workers.append(get_ident())
        assert state_root == tmp_path / 'state' and broker is None
        assert not cancelled()
        temporary = TemporaryDirectory(prefix='actual-prepared-', dir=tmp_path)
        output = Path(temporary.name) / 'prepared.wav'
        output.write_bytes(path.read_bytes())
        digest = sha256(path.read_bytes()).hexdigest()
        value = PreparedListening(request, temporary, output, digest, digest,
            FrameMapping(800, 800, 8000), player._stamp(output))
        state.prepared.append(value)
        return value
    player.prepare_listening = Mock(side_effect=prepare)
    state.session = ReaderPlaybackSession(player, submit=submit, guard=guard,
        capture=capture, verify=verify, alive=lambda: state.alive,
        busy=lambda: state.busy, cancelled=state.cancel.is_set, clear_cancel=state.cancel.clear,
        start=lambda action, on_error: state.quiet.append((action, on_error)),
        stop=stop, pause=player.pause, report=lambda message, **kwargs: state.notices.append(message),
        refresh=lambda: setattr(state, 'refreshes', state.refreshes + 1),
        state_root=tmp_path / 'state')
    def start():
        request = player.listening_request(1, 0, position_seconds=.025)
        state.cancel_request = state.session.prepare_listening(request,
            lambda: state.completions.append(get_ident()), state.errors.append,
            active=lambda: state.active)
    def finish(*, deliver=True):
        future, done, failed = state.queued.pop(0)
        state.busy = False
        try:
            value = future.result(timeout=5)
            if deliver:
                done(value)
        except Exception as exc:
            if deliver:
                failed(exc)
    state.start, state.finish = start, finish
    yield state
    state.session.close()
    state.executor.shutdown(wait=True, cancel_futures=True)
    state.session.close(worker_drained=True)
    player.close()


def test_actual_prepared_pcm_transfers_to_same_player_on_gui_without_autoplay(bridge):
    caller = get_ident()
    bridge.start()
    bridge.finish()
    value, = bridge.prepared
    assert bridge.workers == [bridge.workers[0]] and bridge.workers[0] != caller
    assert bridge.completions == [caller] and not bridge.errors
    assert value._owner is bridge.player and not value.closed
    assert bridge.player.state == 'stopped' and bridge.player.position_seconds == .025
    assert bridge.player._process is None and not bridge.player._open and bridge.player._wave is None
    assert bridge.player.listening_path.read_bytes() == bridge.player.path.read_bytes()
    bridge.player.close()
    assert value.closed and not value.path.exists()


@pytest.mark.parametrize('boundary', ['modal', 'generation', 'scope', 'document', 'source', 'cancel'])
def test_post_worker_stale_result_is_released_before_any_player_adoption(bridge, boundary):
    bridge.start()
    bridge.queued[-1][0].result(timeout=5)
    value = bridge.prepared[0]
    if boundary == 'modal':
        bridge.active = False
    elif boundary == 'generation':
        bridge.session.generation += 1
    elif boundary == 'scope':
        bridge.valid = False
    elif boundary == 'document':
        bridge.document = ReadingDocument('Unchanged source')
    elif boundary == 'source':
        with bridge.player.path.open('ab') as stream:
            stream.write(b'mutated')
    else:
        bridge.cancel_request()
    bridge.finish()
    assert not bridge.completions and value.closed and not value.path.exists()
    assert bridge.player._listening is None
    assert bridge.player._process is None and not bridge.player._open


@pytest.mark.parametrize('boundary', ['reader-close', 'direct-destroy'])
def test_dropped_queued_completion_is_drained_by_owner_close(bridge, boundary):
    bridge.start()
    bridge.queued[-1][0].result(timeout=5)
    value = bridge.prepared[0]
    if boundary == 'direct-destroy':
        bridge.alive = False
    bridge.session.close()
    bridge.finish(deliver=False)
    assert value.closed and not value.path.exists() and not bridge.session._values
    assert bridge.player._listening is None and not bridge.completions


@pytest.mark.parametrize('boundary', ['cancel', 'modal', 'close'])
def test_cancel_before_worker_entry_cannot_begin_pcm_preparation(bridge, boundary):
    entered, release = Event(), Event()
    hold = bridge.executor.submit(lambda: (entered.set(), release.wait(5)))
    assert entered.wait(5)
    bridge.start()
    if boundary == 'cancel':
        bridge.cancel_request()
    elif boundary == 'modal':
        bridge.active = False
    else:
        bridge.session.close()
    release.set()
    hold.result(timeout=5)
    bridge.finish()
    bridge.player.prepare_listening.assert_not_called()
    assert not bridge.prepared and not bridge.session._values


def test_failed_private_copy_cleanup_remains_owned_until_explicit_retry(bridge, monkeypatch):
    bridge.start()
    bridge.queued[-1][0].result(timeout=5)
    value = bridge.prepared[0]
    original = value.close
    monkeypatch.setattr(value, 'close', Mock(side_effect=OSError('Synthetic file is busy')))
    with pytest.raises(RuntimeError, match='busy'):
        bridge.session.close()
    assert value in bridge.session._values and value.path.exists() and not value.closed
    monkeypatch.setattr(value, 'close', original)
    bridge.session.close()
    bridge.finish(deliver=False)
    assert not bridge.session._values and value.closed and not value.path.exists()


def test_failed_submission_retains_no_new_operation_or_derivative(bridge):
    bridge.session.submit = lambda *args, **kwargs: False
    with pytest.raises(RuntimeError, match='did not accept'):
        bridge.start()
    assert bridge.session.operation is None and not bridge.prepared


def test_missing_shared_resource_owner_rejects_before_worker_or_source_access(bridge):
    bridge.session.state_root = None
    with pytest.raises(ValueError, match='resource owner'):
        bridge.start()
    bridge.player.prepare_listening.assert_not_called()
    assert not bridge.queued


@pytest.mark.parametrize('boundary', ['stop', 'close', 'source', 'document', 'quiet', 'cancel'])
def test_deferred_manual_audio_never_outlives_captured_reader_session(bridge, boundary):
    action = Mock()
    bridge.session.execute(action, audible=True)
    assert len(bridge.quiet) == 1
    if boundary == 'stop':
        bridge.session.stop()
    elif boundary == 'close':
        bridge.session.close()
    elif boundary == 'source':
        bridge.valid = False
    elif boundary == 'document':
        bridge.document = ReadingDocument('Unchanged source')
    elif boundary == 'quiet':
        bridge.player.stop()
    else:
        bridge.cancel.set()
    admitted, on_error = bridge.quiet.pop(0)
    try:
        admitted()
    except ValueError as exc:
        on_error(exc)
    action.assert_not_called()


def test_explicit_idle_action_can_resume_after_stop_without_old_callback_reviving(bridge):
    old, new = Mock(), Mock()
    bridge.session.execute(old, audible=True)
    bridge.session.stop()
    bridge.session.execute(new, audible=True)
    for action, on_error in bridge.quiet:
        action()
    old.assert_not_called()
    new.assert_called_once_with()
    assert not bridge.cancel.is_set()


def test_adoption_error_never_deletes_a_native_players_owned_pcm(bridge, monkeypatch):
    bridge.start()
    bridge.queued[-1][0].result(timeout=5)
    original = bridge.player.apply_listening
    def adopted_then_failed(value):
        original(value)
        raise RuntimeError('Synthetic notification failure after adoption')
    monkeypatch.setattr(bridge.player, 'apply_listening', adopted_then_failed)
    bridge.finish()
    value = bridge.prepared[0]
    assert value._owner is bridge.player and not value.closed and value.path.exists()
    assert bridge.errors and not bridge.completions
    bridge.player.close()
    assert value.closed


def test_discard_acknowledges_only_the_matching_finished_operation(bridge):
    bridge.start()
    value = bridge.queued[-1][0].result(timeout=5)
    operation = bridge.session.operation
    assert operation is not None
    class Foreign:
        request = object()
        _owner = None
        close = Mock()
    foreign = Foreign()
    bridge.session.discard(foreign)
    foreign.close.assert_not_called()
    assert bridge.session.operation is operation
    bridge.session.discard(value)
    assert bridge.session.operation is None and not bridge.session._values
    assert value.closed and not value.path.exists()
    bridge.finish(deliver=False)


def test_close_does_not_acknowledge_an_active_worker(bridge):
    entered, release = Event(), Event()
    def blocked(request, directory, **kwargs):
        entered.set()
        assert release.wait(5)
        if kwargs['cancelled']():
            raise InterruptedError('Exact active worker settled after cancellation.')
        raise AssertionError('Close did not cancel the worker.')
    bridge.player.prepare_listening.side_effect = blocked
    bridge.start()
    assert entered.wait(5)
    operation = bridge.session.operation
    bridge.session.close()
    assert operation is not None and bridge.session.operation is operation
    release.set()
    bridge.finish(deliver=False)
    bridge.session.close()
    assert bridge.session.operation is None and not bridge.session._values


def test_failed_worker_with_suppressed_gui_completion_is_acknowledged_after_join(bridge):
    bridge.player.prepare_listening.side_effect = OSError('Synthetic converter admission failure')
    bridge.start()
    bridge.finish(deliver=False)
    assert bridge.session.operation is not None
    bridge.session.close()
    assert bridge.session.operation is None and not bridge.session._values


def test_cancelled_queued_future_settles_only_after_existing_executor_drains(bridge):
    from carr_library.ui.worker_lifetime import DesktopExecutor

    bridge.executor.shutdown(wait=True)
    bridge.executor = DesktopExecutor()
    entered, release = Event(), Event()
    held = bridge.executor.submit(lambda: (entered.set(), release.wait(5)))
    assert entered.wait(5)
    bridge.start()
    future, _, _ = bridge.queued[-1]
    operation = bridge.session.operation
    bridge.executor.register_cleanup(
        lambda: bridge.session.close(worker_drained=True))
    bridge.session.close()
    bridge.executor.shutdown(wait=False, cancel_futures=True)
    assert future.cancelled()
    assert bridge.session.operation is operation and operation is not None
    bridge.player.prepare_listening.assert_not_called()
    release.set()
    held.result(timeout=5)
    bridge.executor.shutdown(wait=True, cancel_futures=True)
    bridge.finish(deliver=False)
    assert bridge.session.operation is None and not bridge.session._values
    bridge.player.prepare_listening.assert_not_called()
