"""Real source/core/PCM/checkpoint controls with explicit GUI/device test seams.

The native queue and audio device below are controlled stand-ins. These cases
qualify current-owner handoff and never claim physical Windows playback.
"""
from hashlib import sha256
import json
from pathlib import Path
import threading
from time import monotonic
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.models import Scope
from carr_library.assistant.player_tools import LivePlayerBridge, check_arguments
from carr_library.durable_state import atomic_json
from carr_library.reader.live_reading import LiveBudget, LiveReader, SourceSequence
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.runtime import SpeechResult, Voice
from carr_library.ui.live_reading import LiveReadingControls


VOICE = Voice('synthetic:assistant-owner', 'Explicit synthetic voice', 'synthetic', 'uk', ('rate', 'volume'))


class UI:
    def __init__(self):
        self.main = threading.get_ident()
        self.lock, self.pending, self.queued = threading.Lock(), [], threading.Event()

    def IsMainThread(self):
        return threading.get_ident() == self.main

    def CallAfter(self, callback):
        with self.lock:
            self.pending.append(callback)
        self.queued.set()

    def pump(self):
        assert self.IsMainThread()
        with self.lock:
            pending, self.pending = self.pending, []
        for callback in pending:
            callback()


class Peer:
    def __init__(self):
        self.alive, self.calls, self.forbidden = True, [], []
    def __bool__(self):
        return self.alive
    def GetId(self):
        if not self.alive:
            self.forbidden.append('GetId')
            raise RuntimeError('A deleted native peer was accessed.')
        self.calls.append('GetId')
        return 7103


class Device:
    """Actual WAV/handle checks around an explicitly non-native audio device."""
    def __init__(self):
        self.state, self.duration, self.position = 'closed', 0., 0.
        self.handle, self.paths = None, []
        self.play_count = self.pause_count = self.resume_count = self.close_count = 0
        self.pause_entered, self.pause_release = threading.Event(), threading.Event()
        self.close_entered, self.close_release = threading.Event(), threading.Event()
        self.pause_release.set()
        self.close_release.set()
        self.fail_close = False

    def available(self):
        return True
    def status(self):
        return dict(state=self.state, position_seconds=self.position, duration_seconds=self.duration)
    def open(self, path):
        assert self.handle is None
        with wave.open(str(path)) as stream:
            assert stream.getsampwidth() == 2 and stream.getnchannels() == 1
            self.duration = stream.getnframes() / stream.getframerate()
        self.handle = Path(path).open('rb')
        self.paths.append(Path(path))
        self.state = 'stopped'
        return self.status()
    def play(self):
        self.play_count += 1
        self.state = 'playing'
        return self.status()
    def pause(self):
        self.pause_count += 1
        self.pause_entered.set()
        assert self.pause_release.wait(5), 'The controlled native pause was not released.'
        self.state = 'paused'
        return self.status()
    def resume(self):
        self.resume_count += 1
        self.state = 'playing'
        return self.status()
    def stop(self):
        self.state, self.position = 'stopped', 0.
        return self.status()
    def close(self):
        self.close_count += 1
        self.close_entered.set()
        assert self.close_release.wait(5), 'The controlled native close was not released.'
        if self.fail_close:
            raise RuntimeError('Synthetic native device has not released its WAV.')
        if self.handle is not None:
            assert self.paths[-1].exists(), 'An owned WAV was removed before its handle closed.'
            self.handle.close()
            self.handle = None
        self.state = 'closed'


def until(predicate, *, timeout=5, ui=None):
    deadline = monotonic() + timeout
    while not predicate():
        if ui is not None:
            ui.pump()
        if monotonic() >= deadline:
            pytest.fail('The bounded current-reader operation did not reach its expected state.')
        threading.Event().wait(.005)


@pytest.fixture
def owner(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit current source owner')
    raw = 'Поточний збережений текст 🚂.\r\nLiteral <speak> is source text, not a command.'
    book = app.add_text(workspace, 'Current source', raw, language='uk')
    desk = ReadingDesk(app, workspace, book['edition'])
    ui, peer, device = UI(), Peer(), Device()
    host = SimpleNamespace(library=app, active=(workspace, None), _closing=False)
    def guard():
        if host.library is not app or host.active != (workspace, None):
            raise PermissionError('The selected source host changed.')
    sequence = SourceSequence(desk, guard)
    rendered = []
    def render(request, target, **options):
        assert not ui.IsMainThread()
        if options['cancelled']():
            raise InterruptedError('Synthetic rendering observed cancellation.')
        rendered.append(request)
        with wave.open(str(target), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
            stream.writeframes(b'\x01\x00' * 1600)
        return SpeechResult(Path(target), sha256(Path(target).read_bytes()).hexdigest(),
                            .2, 8000, 1, 1600, VOICE.id,
                            sha256(request.text.encode('utf-8')).hexdigest(), 1)
    core = LiveReader(sequence, discover=lambda **kwargs: (VOICE,), render=render,
        player_factory=lambda: device, budget=LiveBudget(chunk_characters=512, minimum_free_bytes=1))
    core.discover_voices().result(5)
    controls = LiveReadingControls.__new__(LiveReadingControls)
    controls.wx, controls.dialog, controls._host = ui, peer, host
    controls.source, controls.core, controls.closed = sequence, core, False
    controls.ready = lambda: True
    controls._start_generation = 0
    controls.status = SimpleNamespace(ChangeValue=lambda value: None)
    controls.on_stop = lambda: None
    actions = []
    real_pause, real_stop = core.toggle_pause, core.stop
    def pause():
        assert ui.IsMainThread()
        actions.append('pause')
        return real_pause()
    def stop(reason=None):
        assert ui.IsMainThread()
        actions.append('stop')
        return real_stop(reason)
    core.toggle_pause, core.stop = pause, stop
    bridge = LivePlayerBridge(app, controls)
    e = SimpleNamespace(app=app, workspace=workspace, book=book, raw=raw, desk=desk,
        source=sequence, core=core, controls=controls, bridge=bridge, ui=ui, peer=peer,
        host=host, device=device, actions=actions, rendered=rendered, tasks=[],
        root=tmp_path, scope=Scope(workspace, max_seconds=5))
    yield e
    bridge.close()
    controls.closed = True
    device.pause_release.set()
    device.close_release.set()
    device.fail_close = False
    completion = core.close()
    if completion is not None:
        completion.result(5)
    for task in e.tasks:
        task.worker.join(2)
        assert not task.worker.is_alive()
    ui.pump()
    retained = core._retained_resources
    if retained is not None:
        # Release only this test's explicitly synthetic retained device.
        import carr_library.reader.live_reading as live_module
        player, directory = retained
        player.close()
        if directory is not None:
            directory.cleanup()
        live_module._RETAINED_MEDIA[:] = [item for item in live_module._RETAINED_MEDIA if item is not retained]
        core._retained_resources = None


def start(e):
    assert not e.core.busy
    e.device.pause_entered.clear()
    e.device.close_entered.clear()
    e.core.start(1, VOICE.id)
    until(lambda: e.core.status().state == 'playing')
    assert e.device.handle is not None and e.device.play_count >= 1


def step(action=None, receipt=None):
    if action is None:
        return SimpleNamespace(tool='player.status', arguments={})
    return SimpleNamespace(tool='player.control',
        arguments={key: receipt[key] for key in ('session', 'source_sha256')} | {'action': action})


def begin(e, selected=None, *, intent=None, cancelled=None, scope=None, operation=None):
    e.ui.queued.clear()
    intent = {} if intent is None else intent
    cancellation = cancelled or threading.Event()
    path = e.root / ('intent-' + str(len(e.tasks)) + '.json')
    def persist():
        atomic_json(path, intent, replace=path.exists())
    done, values, errors = threading.Event(), [], []
    def work():
        try:
            result = (operation() if operation is not None else
                      e.bridge.execute(scope or e.scope, selected or step(), intent, persist,
                                       cancellation.is_set))
            values.append(result)
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()
    worker = threading.Thread(target=work, name='explicit-assistant-test-worker')
    task = SimpleNamespace(worker=worker, done=done, values=values, errors=errors,
                           intent=intent, path=path, cancel=cancellation)
    e.tasks.append(task)
    worker.start()
    return task


def finish(e, task, *, error=None):
    until(task.done.is_set, timeout=6, ui=e.ui)
    task.worker.join(1)
    assert not task.worker.is_alive()
    if error is not None:
        assert len(task.errors) == 1 and isinstance(task.errors[0], error), task.errors
        assert task.values == []
        return task.errors[0]
    assert task.errors == [], task.errors
    assert len(task.values) == 1
    return task.values[0]


def status(e):
    return finish(e, begin(e))


def test_actual_source_core_status_pause_and_stop_share_one_owner(owner):
    e = owner
    start(e)
    original = e.app.assets.resolve(e.desk.identity.asset_sha256).read_bytes()
    receipt = status(e)
    assert receipt['state'] == 'playing' and receipt['busy'] is True
    assert receipt['asset_sha256'] == e.desk.identity.asset_sha256
    assert receipt['revision'] == e.desk.revision
    assert receipt['source_text_sha256'] == sha256(e.raw.encode('utf-8')).hexdigest()
    assert e.actions == []
    paused = finish(e, begin(e, step('pause', receipt)))
    assert paused['state'] == 'paused' and paused['quiescent'] is True
    assert e.actions == ['pause'] and e.device.resume_count == 0
    again = finish(e, begin(e, step('pause', receipt)))
    assert again['state'] == 'paused' and e.actions == ['pause']
    stopped_task = begin(e, step('stop', receipt))
    stopped = finish(e, stopped_task)
    assert stopped['state'] == 'stopped' and not stopped['busy'] and stopped['quiescent']
    assert not stopped['cleanup_pending']
    assert e.actions == ['pause', 'stop']
    assert not e.core.busy and e.core.quiescent and e.core._retained_resources is None
    assert e.device.handle is None and all(not path.exists() for path in e.device.paths)
    assert e.app.assets.resolve(e.desk.identity.asset_sha256).read_bytes() == original
    assert json.loads(stopped_task.path.read_bytes())['player_dispatch_complete'] is True
    assert len(e.rendered) == 1 and e.core.source is e.source


def test_pending_native_pause_is_never_toggled_into_resume(owner):
    e = owner
    start(e)
    receipt = status(e)
    e.device.pause_release.clear()
    first = begin(e, step('pause', receipt))
    until(e.device.pause_entered.is_set, ui=e.ui)
    assert e.core._pause and e.core.status().state == 'playing'
    second = begin(e, step('pause', receipt))
    until(lambda: second.path.exists(), ui=e.ui)
    # Run the second real queued request while the first native acknowledgement is held.
    for _ in range(12):
        e.ui.pump()
        threading.Event().wait(.005)
    assert e.actions == ['pause'] and e.device.resume_count == 0
    assert not first.done.is_set() and not second.done.is_set()
    e.device.pause_release.set()
    assert finish(e, first)['state'] == finish(e, second)['state'] == 'paused'
    assert e.actions == ['pause'] and e.device.resume_count == 0


def test_stop_does_not_complete_while_real_reading_future_owns_wav(owner):
    e = owner
    start(e)
    receipt = status(e)
    e.device.close_release.clear()
    task = begin(e, step('stop', receipt))
    until(e.device.close_entered.is_set, ui=e.ui)
    assert e.core.busy and e.device.handle is not None
    assert e.device.paths[-1].exists() and not task.done.is_set()
    assert 'player_dispatch_complete' not in json.loads(task.path.read_bytes())
    e.device.close_release.set()
    result = finish(e, task)
    assert result['state'] == 'stopped' and not e.core.busy
    assert e.device.handle is None and not e.device.paths[-1].exists()
    assert json.loads(task.path.read_bytes())['player_dispatch_complete'] is True


def test_uncertain_native_close_keeps_same_resources_and_never_acknowledges_stop(owner):
    e = owner
    start(e)
    receipt = status(e)
    e.device.fail_close = True
    task = begin(e, step('stop', receipt))
    error = finish(e, task, error=RuntimeError)
    assert 'native cleanup' in str(error)
    assert not e.core.busy and e.core.quiescent
    assert e.core._retained_resources is not None
    assert e.core._retained_resources[0] is e.device
    assert e.device.handle is not None and e.device.paths[-1].exists()
    assert 'player_dispatch_complete' not in json.loads(task.path.read_bytes())
    current = status(e)
    assert current['cleanup_pending'] is True
    assert e.device.handle is not None and e.device.paths[-1].exists()


@pytest.mark.parametrize('change', ['closed-control', 'deleted-peer', 'replaced-core', 'changed-library'])
def test_queued_command_cannot_touch_a_closed_or_replaced_native_owner(owner, change):
    e = owner
    start(e)
    receipt = status(e)
    task = begin(e, step('pause', receipt))
    assert e.ui.queued.wait(3)
    if change == 'closed-control':
        e.controls.closed = True
    elif change == 'deleted-peer':
        e.peer.alive = False
    elif change == 'replaced-core':
        e.controls.core = SimpleNamespace()
    else:
        e.host.active = (e.app.create_workspace('Another explicit library'), None)
    finish(e, task, error=PermissionError)
    assert e.actions == [] and e.peer.forbidden == []
    assert 'player_dispatch_complete' not in json.loads(task.path.read_bytes())


def test_reentrant_source_guard_cannot_touch_newly_deleted_peer(owner):
    e = owner
    start(e)
    receipt = status(e)
    old_guard = e.source.guard
    def closed_during_guard():
        old_guard()
        if e.ui.IsMainThread():
            e.peer.alive = False
    e.source.guard = closed_during_guard
    finish(e, begin(e, step('pause', receipt)), error=PermissionError)
    assert e.actions == [] and e.peer.forbidden == []


def test_cancelled_queued_action_remains_withdrawn_and_is_not_replayed(owner):
    e = owner
    start(e)
    receipt = status(e)
    task = begin(e, step('pause', receipt))
    assert e.ui.queued.wait(3)
    calls = list(e.peer.calls)
    task.cancel.set()
    task.worker.join(2)
    assert task.done.is_set() and isinstance(task.errors[0], InterruptedError)
    e.ui.pump()  # Deliver the genuine held callback after withdrawal.
    assert e.actions == [] and e.peer.calls == calls and e.peer.forbidden == []
    assert json.loads(task.path.read_bytes()) == {'player_dispatch': step('pause', receipt).arguments}
    refused = begin(e, step('pause', receipt), intent=task.intent)
    finish(e, refused, error=PermissionError)
    assert e.actions == []


def test_request_timeout_withdraws_the_real_queued_callback(owner):
    e = owner
    start(e)
    receipt = status(e)
    task = begin(e, step('stop', receipt), scope=Scope(e.workspace, max_seconds=.05))
    assert e.ui.queued.wait(3)
    task.worker.join(2)
    assert task.done.is_set() and isinstance(task.errors[0], InterruptedError)
    e.ui.pump()
    assert e.actions == [] and e.core.busy and e.core.status().state == 'playing'
    assert 'player_dispatch_complete' not in json.loads(task.path.read_bytes())


def test_closing_assistant_withdraws_queue_without_stopping_reader(owner):
    e = owner
    start(e)
    receipt = status(e)
    task = begin(e, step('stop', receipt))
    assert e.ui.queued.wait(3)
    e.bridge.close()
    task.worker.join(2)
    assert task.done.is_set() and isinstance(task.errors[0], InterruptedError)
    e.ui.pump()
    assert e.actions == [] and e.core.busy and e.device.handle is not None


def test_new_real_reading_future_cannot_inherit_old_reviewed_session(owner):
    e = owner
    start(e)
    old = status(e)
    e.core.stop()
    e.core._future.result(5)
    start(e)
    before = list(e.actions)
    finish(e, begin(e, step('pause', old)), error=PermissionError)
    assert e.actions == before and e.core.status().state == 'playing'
    current = status(e)
    assert current['session'] != old['session']
    assert current['source_sha256'] == old['source_sha256']
    assert finish(e, begin(e, step('pause', current)))['state'] == 'paused'


def test_changed_retained_original_fails_even_when_catalog_binding_stays_same(owner):
    e = owner
    start(e)
    e.core.stop()
    e.core._future.result(5)
    saved = status(e)
    path = e.app.assets.resolve(e.desk.identity.asset_sha256)
    original = path.read_bytes()
    path.write_bytes(b'X' * len(original))
    before = list(e.actions)
    failure = finish(e, begin(e, step('pause', saved)), error=(ValueError, PermissionError))
    assert failure is not None and e.actions == before
    assert path.read_bytes() == b'X' * len(original)


def test_source_metadata_revocation_rejects_queued_action_without_retargeting(owner):
    e = owner
    start(e)
    saved = status(e)
    task = begin(e, step('pause', saved))
    assert e.ui.queued.wait(3)
    e.app.update_metadata(e.workspace, e.book['edition'], title='A new current source revision')
    finish(e, task, error=PermissionError)
    assert e.actions == []


def test_completed_controls_are_verified_without_dispatching_them_again(owner):
    e = owner
    start(e)
    saved = status(e)
    selected = step('pause', saved)
    receipt = finish(e, begin(e, selected))
    before = list(e.actions)
    verified = begin(e, operation=lambda: e.bridge.verify_completed(e.scope, selected, receipt, lambda: False))
    assert finish(e, verified) is None
    assert e.actions == before
    e.core.stop()
    e.core._future.result(5)
    start(e)
    finish(e, begin(e, operation=lambda:
        e.bridge.verify_completed(e.scope, selected, receipt, lambda: False)), error=PermissionError)


def test_invalid_scope_and_small_receipt_budget_fail_before_any_native_action(owner):
    e = owner
    start(e)
    saved = status(e)
    for scope in (Scope('another-workspace', max_seconds=2), Scope(e.workspace, max_seconds=2, max_bytes=1)):
        finish(e, begin(e, step('pause', saved), scope=scope), error=(PermissionError, ValueError))
    assert e.actions == [] and e.core.status().state == 'playing'


def test_only_exact_current_owner_and_existing_worker_can_admit_requests(owner):
    e = owner
    with pytest.raises(TypeError):
        LivePlayerBridge(e.app, SimpleNamespace(**vars(e.controls)))
    with pytest.raises(RuntimeError, match='existing assistant worker'):
        e.bridge.execute(e.scope, step(), {}, lambda: None, lambda: False)
    done = begin(e, operation=lambda: LivePlayerBridge(e.app, e.controls))
    finish(e, done, error=RuntimeError)
    assert e.actions == []


@pytest.mark.parametrize('arguments', [
    {'session': '0' * 32, 'source_sha256': '0' * 64, 'action': 'play'},
    {'session': '0' * 32, 'source_sha256': '0' * 64, 'action': 'resume'},
    {'session': '0' * 32, 'source_sha256': '0' * 64, 'action': 'seek', 'seconds': 1},
    {'session': 'bad', 'source_sha256': '0' * 64, 'action': 'stop'},
])
def test_no_audible_or_arbitrary_callback_action_is_allowed(arguments):
    with pytest.raises(ValueError):
        check_arguments(SimpleNamespace(tool='player.control', arguments=arguments))
