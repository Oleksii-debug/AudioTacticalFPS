"""Threaded assistant -> existing UI queue -> exact owned WavePlayer tests.

The native queue and device are explicit stand-ins here. The production bridge,
desktop owner, WavePlayer state machine, SQLite app and assistant checkpoints
are real; Windows probe evidence is collected separately.
"""
from hashlib import sha256
from pathlib import Path
import threading
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.player_tools import LivePlayerBridge
from carr_library.speech.player import WavePlayer
from carr_library.ui.desktop_integration import DesktopIntegration


class UI:
    EVT_WINDOW_DESTROY = object()

    def __init__(self):
        self.main = threading.get_ident()
        self.pending = []
        self.lock = threading.Lock()
        self.queued = threading.Event()
    def IsMainThread(self):
        return threading.get_ident() == self.main
    def CallAfter(self, callback):
        with self.lock:
            self.pending.append(callback)
        self.queued.set()
    def pump(self):
        assert self.IsMainThread()
        with self.lock:
            work, self.pending = self.pending, []
        for callback in work:
            callback()


class Device:
    """Explicit non-native device seam underneath the actual player owner."""
    def __init__(self):
        self.position_seconds = .025
        self.ended = False
        self.closed = False
        self.pauses = 0
    def pump(self):
        pass
    def pause(self):
        self.pauses += 1
    def close(self):
        self.closed = True


@pytest.fixture
def owner(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit current reader')
    ui = UI()
    frame = SimpleNamespace(library=app, active=(workspace, None), _closing=False)
    desktop = DesktopIntegration.__new__(DesktopIntegration)
    desktop.wx, desktop.frame = ui, frame
    desktop.closed, desktop.reader, desktop._assistant_reader = False, None, None
    frame.desktop_integration = desktop
    dialog = SimpleNamespace(GetId=lambda: 191, Bind=Mock(), IsEnabled=lambda: True)
    source, alive, busy, actions = ['unchanged reading copy'], [True], [False], []
    player = WavePlayer()
    source_audio = tmp_path / 'already-owned.wav'
    with wave.open(str(source_audio), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\0\0' * 3000)
    device = Device()
    player.path, player.duration_seconds, player._identity = source_audio, .125, player._stamp(source_audio)
    player._wave, player._state = device, 'playing'
    def guard():
        assert ui.IsMainThread()
        if not alive[0] or busy[0]:
            raise PermissionError('Reader closed or busy.')
    def snapshot():
        guard()
        return sha256(source[0].encode('utf-8')).hexdigest()
    def pause():
        assert ui.IsMainThread()
        assert player.state != 'paused', 'A paused native action must not be toggled into resume.'
        actions.append(('pause', threading.get_ident()))
        player.pause()
    def stop():
        assert ui.IsMainThread()
        actions.append(('stop', threading.get_ident()))
        player.stop()
    button = SimpleNamespace(IsEnabled=lambda: True)
    detach = desktop.attach_reader(dialog, button, button, lambda: alive[0],
        player=player, source_snapshot=snapshot, player_guard=guard,
        pause_action=pause, stop_action=stop)
    bridge = LivePlayerBridge(app, desktop, dialog)
    service = AssistantService(app, player=bridge)
    result = SimpleNamespace(app=app, workspace=workspace, ui=ui, frame=frame, desktop=desktop,
        dialog=dialog, source=source, alive=alive, busy=busy, actions=actions, player=player,
        device=device, detach=detach, bridge=bridge, service=service,
        scope=Scope(workspace, max_seconds=5))
    yield result
    bridge.close()
    detach()
    player.close()


def begin(e, identity, **kwargs):
    e.ui.queued.clear()
    done, values, errors = threading.Event(), [], []
    def run():
        try:
            values.append(e.service.resume(identity, **kwargs))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()
    worker = threading.Thread(target=run)
    worker.start()
    return SimpleNamespace(done=done, values=values, errors=errors, worker=worker)


def finish(e, task):
    deadline = monotonic() + 6
    while not task.done.is_set() and monotonic() < deadline:
        e.ui.pump()
        task.done.wait(.005)
    e.ui.pump()
    task.worker.join(1)
    assert not task.worker.is_alive() and task.done.is_set()
    assert task.errors == []
    return task.values[0]


def submit(e, *steps):
    return e.service.submit(Plan(e.scope, steps))


def status(e):
    result = finish(e, begin(e, submit(e, Step('player.status'))))
    assert result.status == 'complete', result
    return result.results[0]


def control(receipt, action='pause'):
    return Step('player.control', {key: receipt[key] for key in ('session', 'source_sha256')} | {'action': action})


def test_status_and_pause_stop_use_exact_same_player_only_on_ui_thread(owner):
    e = owner
    before = e.player
    identity = submit(e, Step('player.status'))
    assert not e.ui.pending and e.actions == []
    task = begin(e, identity)
    assert e.ui.queued.wait(3) and e.actions == []
    snapshot = finish(e, task)
    assert snapshot.status == 'complete'
    receipt, = snapshot.results
    assert receipt['state'] == 'playing' and receipt['workspace'] == e.workspace
    assert receipt['source_sha256'] == sha256(e.source[0].encode()).hexdigest()
    identity = submit(e, control(receipt), control(receipt), control(receipt, 'stop'))
    result = finish(e, begin(e, identity))
    assert result.status == 'complete' and result.completed == 3, result
    assert [row['state'] for row in result.results] == ['paused', 'paused', 'stopped']
    assert e.player is before and e.device.pauses == 1 and e.device.closed
    assert e.actions == [('pause', e.ui.main), ('stop', e.ui.main)]
    assert finish(e, begin(e, identity)).results == result.results
    assert e.actions == [('pause', e.ui.main), ('stop', e.ui.main)]


@pytest.mark.parametrize('change', ['source', 'workspace', 'library', 'reader', 'busy', 'closing'])
def test_changed_live_owner_cannot_dispatch_queued_action(owner, change):
    e = owner
    receipt = status(e)
    identity = submit(e, control(receipt))
    task = begin(e, identity)
    assert e.ui.queued.wait(3)
    if change == 'source':
        e.source[0] = 'Different reading copy'
    elif change == 'workspace':
        e.frame.active = ('foreign', None)
    elif change == 'library':
        e.frame.library = SimpleNamespace(root=e.app.root)
    elif change == 'reader':
        e.detach()
    elif change == 'busy':
        e.busy[0] = True
    else:
        e.frame._closing = True
    result = finish(e, task)
    assert result.status in ('attention', 'failed') and result.completed == 0
    assert e.actions == [] and not e.device.closed and e.device.pauses == 0


def test_cancel_withdraws_queued_native_authority_and_uncertain_attempt_is_not_retried(owner):
    e = owner
    receipt = status(e)
    identity = submit(e, control(receipt, 'stop'))
    task = begin(e, identity)
    assert e.ui.queued.wait(3)
    e.service.cancel(identity)
    assert task.done.wait(3)  # No GUI callback is needed to stop the waiting worker.
    e.ui.pump()  # A late callback still has no authority.
    result = finish(e, task)
    assert result.status == 'cancelled' and result.completed == 0
    assert not e.actions and not e.device.closed
    again = finish(e, begin(e, identity))
    assert again.status == 'attention' and 'uncertain' in again.message
    assert not e.actions and not e.device.closed


def test_timeout_before_native_dispatch_leaves_late_callback_inert(owner, monkeypatch):
    e = owner
    receipt = status(e)
    from carr_library.assistant import player_tools
    clock = [0.0]
    monkeypatch.setattr(player_tools, 'monotonic', lambda: clock[0])
    identity = submit(e, control(receipt))
    task = begin(e, identity)
    assert e.ui.queued.wait(3)
    clock[0] = e.scope.max_seconds + 1
    assert task.done.wait(3)
    e.ui.pump()
    result = finish(e, task)
    assert result.status == 'paused' and result.completed == 0
    assert e.actions == [] and e.device.pauses == 0


def test_lost_acknowledgement_keeps_action_uncertain_without_replay(owner, monkeypatch):
    e = owner
    receipt = status(e)
    actual = e.bridge._request
    def lost(*args, **kwargs):
        value = actual(*args, **kwargs)
        if args[1] == 'stop':
            raise InterruptedError('Native acknowledgement lost after actual Stop.')
        return value
    monkeypatch.setattr(e.bridge, '_request', lost)
    identity = submit(e, control(receipt, 'stop'))
    first = finish(e, begin(e, identity))
    assert first.status == 'paused' and first.completed == 0
    assert e.device.closed and e.actions == [('stop', e.ui.main)]
    monkeypatch.setattr(e.bridge, '_request', actual)
    again = finish(e, begin(e, identity))
    assert again.status == 'attention' and e.actions == [('stop', e.ui.main)]


def test_closed_assistant_does_not_take_reader_resource_ownership(owner):
    e = owner
    receipt = status(e)
    identity = submit(e, control(receipt))
    task = begin(e, identity)
    assert e.ui.queued.wait(3)
    e.bridge.close()
    assert task.done.wait(3)
    result = finish(e, task)
    assert result.status == 'paused'
    assert e.desktop.reader is not None and e.player._wave is e.device
    assert not e.device.closed and e.actions == []


def test_wrong_or_unqualified_reader_and_arbitrary_callbacks_are_rejected(owner):
    e = owner
    with pytest.raises(PermissionError):
        LivePlayerBridge(e.app, e.desktop, object())
    with pytest.raises(TypeError):
        AssistantService(e.app, player=SimpleNamespace(app=e.app, execute=lambda *args: None))
    e.detach()
    button = SimpleNamespace(IsEnabled=lambda: True)
    e.desktop.attach_reader(e.dialog, button, button, lambda: True)
    with pytest.raises(PermissionError):
        LivePlayerBridge(e.app, e.desktop, e.dialog)


@pytest.mark.parametrize('action', ['play', 'resume', 'seek', 'open', 'shell'])
def test_no_player_creation_or_audible_command_is_in_the_allowlist(owner, action):
    e = owner
    receipt = status(e)
    with pytest.raises(ValueError):
        submit(e, control(receipt, action))
    assert e.actions == [] and e.player._wave is e.device


def test_new_reader_cannot_replay_completed_control(owner):
    e = owner
    receipt = status(e)
    identity = submit(e, control(receipt))
    first = finish(e, begin(e, identity))
    assert first.status == 'complete'
    e.source[0] = 'Changed source after the completed control'
    again = finish(e, begin(e, identity))
    assert again.status == 'attention'
    assert e.actions == [('pause', e.ui.main)]
