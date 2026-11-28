"""Actual queue/session/producer behavior with disclosed logical native peers.

The parent owns one real PlaybackNavigationHost and WavePlayer. The device and
GUI event queue are controlled here; the installed native probe is separate.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace

import pytest

from carr_library.speech import player as player_module
from carr_library.speech.playlist import PlaylistStore, SavedPlaylist
from carr_library.ui.playback_navigation import PlaybackNavigationHost
from carr_library.ui.playback_queue import create_playlist_dialog, show_playlist

_data = runpy.run_path(str(Path(__file__).parents[1] / 'speech' / 'test_playlist_recovery.py'))
_player = runpy.run_path(str(Path(__file__).parents[1] / 'speech' / 'test_playlist.py'))
_nav = runpy.run_path(str(Path(__file__).with_name('test_playback_navigation.py')))


class Control:
    serial = 100
    def __init__(self, parent=None, *args, label='', value='', **kwargs):
        Control.serial += 1
        self.parent, self.label, self.value = parent, label, value
        self.name, self.enabled, self.bindings = '', True, {}
        self.native_id, self._deleted, self.modal = Control.serial, False, False
        self.children, self.rows, self.selection = [], [], -1
        self.forbidden = parent.forbidden if isinstance(parent, Control) else []
        if isinstance(parent, Control):
            parent.children.append(self)
    def native(self):
        if self._deleted:
            self.forbidden.append(self.name or type(self).__name__)
            raise RuntimeError('Deleted native peer.')
    def __bool__(self): return not self._deleted
    def GetId(self): self.native(); return self.native_id
    def SetName(self, value): self.native(); self.name = value
    def GetName(self): self.native(); return self.name
    def GetValue(self): self.native(); return self.value
    def SetValue(self, value): self.native(); self.value = value
    def ChangeValue(self, value): self.native(); self.value = value
    def Bind(self, event, callback, source=None): self.native(); self.bindings[event] = callback
    def Enable(self, value=True): self.native(); self.enabled = value
    def SetSizer(self, value): self.native()
    def SetEscapeId(self, value): self.native()
    def SetMinSize(self, value): self.native()
    def SetScrollRate(self, *args): self.native()
    def Wrap(self, value): self.native()
    def SetFocus(self): self.native()
    def Set(self, values): self.native(); self.rows = list(values)
    def GetSelection(self): self.native(); return self.selection
    def SetSelection(self, value): self.native(); self.selection = value
    def IsModal(self): self.native(); return self.modal
    def ShowModal(self): self.native(); self.modal = True; return 0
    def EndModal(self, code): self.native(); self.modal = False
    def IsBeingDeleted(self): return self._deleted
    def Raise(self): self.native()
    def Destroy(self):
        self.native()
        if 'destroy' in self.bindings:
            self.bindings['destroy'](SimpleNamespace(GetId=lambda: self.native_id, Skip=lambda: None))
        for child in tuple(self.children):
            if child:
                Control.Destroy(child)
        self._deleted = True


class CheckBox(Control):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, value=False, **kwargs)


class Sizer:
    def __init__(self, *args, **kwargs): pass
    def Add(self, *args, **kwargs): pass
    def AddGrowableCol(self, value): pass


class Timer:
    def __init__(self, owner): self.owner, self.running = owner, False
    def Start(self, value): self.running = True
    def Stop(self): self.running = False
    def SetOwner(self, value): self.owner = value
    def GetOwner(self): return self.owner


@pytest.fixture
def wx():
    return SimpleNamespace(Dialog=Control, ScrolledWindow=Control, StaticText=Control,
        TextCtrl=Control, Button=Control, CheckBox=CheckBox, ListBox=Control,
        Timer=Timer, BoxSizer=Sizer, FlexGridSizer=Sizer,
        CallLater=lambda delay, callback: callback(),
        DEFAULT_DIALOG_STYLE=1, RESIZE_BORDER=2, VERTICAL=1, HORIZONTAL=2, ALL=4, EXPAND=8,
        ALIGN_RIGHT=16, TE_READONLY=32, TE_MULTILINE=64, VSCROLL=128, TAB_TRAVERSAL=256,
        ID_CANCEL=99, ID_CLOSE=98, ID_OK=1, FD_OPEN=2, FD_MULTIPLE=4, FD_FILE_MUST_EXIST=8,
        EVT_BUTTON='button', EVT_TIMER='timer', EVT_WINDOW_DESTROY='destroy', EVT_CLOSE='close')


@pytest.fixture
def owned(tmp_path, wx, monkeypatch):
    fixture = _data['queue'].__wrapped__(tmp_path)
    app, sources, player, store, saved, token = next(fixture)
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    mci = _player['Mci']()
    player._mci = mci
    parent, generation, current, reports = Control(), [0], [app], []
    host = None
    def stop():
        generation[0] += 1
        player.stop()
        if host is not None:
            host.invalidate()
    host = PlaybackNavigationHost(parent, player, generation=lambda: generation[0],
        alive=lambda: bool(parent), stop_current=stop, report=reports.append, wx_module=wx)
    def guard():
        if current[0] is not app:
            raise ValueError('The library changed.')
    def immediate(phase, operation, complete, failed):
        try:
            value = operation()
        except (Exception, KeyboardInterrupt) as exc:
            failed(exc)
        else:
            complete(value)
        return True
    # The captured fixture editor really retains through its existing
    # ProductionService/AudioRuntime; it does not create another executor.
    service = fixture.gi_frame.f_locals['service']
    editor = fixture.gi_frame.f_locals['editor']
    session = host.enable_queue(app, submit=immediate, retain=editor.retain, guard=guard)
    view = create_playlist_dialog(session, wx_module=wx)
    try:
        yield SimpleNamespace(app=app, sources=sources, player=player, store=store, saved=saved, token=token,
            parent=parent, host=host, session=session, view=view, generation=generation, current=current,
            mci=mci, reports=reports, immediate=immediate, service=service, editor=editor, wx=wx)
    finally:
        if view:
            view.Destroy()
        host.close()
        assert host.timer.GetOwner() is None
        if parent:
            parent.Destroy()
        assert parent.forbidden == []
        try:
            next(fixture)
        except StopIteration:
            pass


def reviewed(t):
    assert t.session.review_saved()
    assert t.session.pending_saved is None and t.player.state == 'stopped'
    return t


def test_open_and_explicit_saved_review_never_create_playback_or_new_owner(owned):
    t = owned
    assert t.session.pending_saved == t.saved and t.mci.starts == []
    assert all(button.GetName() for button in t.view.buttons.values())
    assert t.view.buttons['review'].enabled and not t.view.buttons['play'].enabled
    assert t.view.restore_controls.GetValue() is False
    t.view.buttons['review'].bindings['button'](None)
    assert t.session.pending_saved is None and t.player.position_seconds == .375
    assert t.mci.starts == [] and t.host.controller.audio is None
    assert t.host._queue_session is t.session and t.session.player is t.player
    assert t.host.enable_queue(t.app, submit=t.immediate, retain=t.editor.retain, guard=lambda: None) is t.session


def test_named_controls_reorder_select_and_save_without_starting(owned):
    t = reviewed(owned)
    t.view.items.SetSelection(1)
    t.view.buttons['up'].bindings['button'](None)
    assert [item.sha256 for item in t.player.playlist] == [
        t.saved.items[1].sha256, t.saved.items[0].sha256, t.saved.items[2].sha256]
    t.view.items.SetSelection(0)
    t.view.buttons['select'].bindings['button'](None)
    assert t.player.playlist_index == 0 and t.mci.starts == []
    t.view.buttons['next'].bindings['button'](None)
    assert t.player.playlist_index == 1 and t.mci.starts == []
    t.view.buttons['previous'].bindings['button'](None)
    assert t.player.playlist_index == 0 and t.mci.starts == []
    t.host.controller.set_skip(1000)
    t.view.buttons['save'].bindings['button'](None)
    saved, token = PlaylistStore(t.app.root).load()
    assert saved.skips == (1000, 1000) and saved.index == 0 and token != t.token
    assert saved.items[0].sha256 == t.saved.items[1].sha256 and t.mci.starts == []


def test_queue_natural_end_needs_same_existing_quiet_admission_and_keeps_sleep_deadline(owned):
    t = reviewed(owned)
    t.session.select(0)
    t.session.play()
    assert len(t.mci.starts) == 1
    pending = _nav['Pending']()
    t.host.controller.dispatch = pending
    clock = [100.0]
    t.host.controller.clock = lambda: clock[0]
    t.host.controller.set_sleep(5)
    t.mci.finish()
    t.host._tick()
    assert len(pending.actions) == 1 and len(t.mci.starts) == 1
    assert t.host.controller.sleep_deadline == 105
    clock[0] = 102
    pending.flush()
    assert t.player.playlist_index == 1 and len(t.mci.starts) == 2
    assert t.host.controller.sleep_deadline == 105
    t.mci.finish()
    t.host._tick()
    assert len(pending.actions) == 1
    clock[0] = 105
    pending.flush()
    assert t.player.state == 'stopped' and not t.player.queue_active
    assert t.host.controller.sleep_deadline is None and len(t.mci.starts) == 2


def test_same_controller_ab_repeat_has_precedence_over_next_item(owned):
    t = reviewed(owned)
    t.session.select(0); t.session.play()
    controller = t.host.controller
    pending = controller.dispatch = _nav['Pending']()
    controller.set_range(.1, .5)
    controller.enable_repeat(True)
    t.mci.position = 500
    t.host._tick()
    assert len(pending.actions) == 1 and t.host._queue_ticket is None
    pending.flush()
    assert t.player.playlist_index == 0
    assert len(t.mci.starts) == 2 and t.mci.starts[-1].endswith(' from 100')


@pytest.mark.parametrize('action', ['pause', 'stop', 'select', 'root', 'close'])
def test_waiting_queue_action_is_withdrawn_without_replay(owned, action):
    t = reviewed(owned)
    t.session.select(0); t.session.play()
    pending = t.host.controller.dispatch = _nav['Pending']()
    t.mci.finish(); t.host._tick()
    before = list(t.mci.starts)
    if action == 'pause': t.session.pause()
    elif action == 'stop': t.session.stop()
    elif action == 'select': t.session.select(2)
    elif action == 'root': t.current[0] = object()
    else: t.host.close()
    pending.flush()
    assert t.mci.starts == before and t.host._queue_ticket is None


@pytest.mark.parametrize('kind', ['false', 'none', 'error', 'interrupt'])
def test_rejected_existing_worker_releases_review_admission_for_explicit_retry(owned, kind):
    t = owned
    def refused(*args):
        if kind == 'error': raise OSError('authored queue admission refusal')
        if kind == 'interrupt': raise KeyboardInterrupt('authored queue admission interruption')
        return False if kind == 'false' else None
    t.session.submit = refused
    assert not t.session.review_saved()
    assert t.session.pending is None and t.view.buttons['review'].enabled
    assert t.session.pending_saved == t.saved and t.mci.starts == []
    t.session.submit = t.immediate
    assert t.session.review_saved()
    assert t.session.pending_saved is None and not t.mci.starts


@pytest.mark.parametrize('phase', ['pending-review', 'completed-review', 'completed-save'])
@pytest.mark.parametrize('direct', [True, False])
def test_actual_worker_result_after_direct_or_parent_death_keeps_data_and_no_native_access(owned, phase, direct):
    t = owned
    if phase == 'completed-save':
        reviewed(t)
        t.player.seek(.5, start_if_stopped=False)
    release, computed = threading.Event(), threading.Event()
    futures, callbacks, returned = [], [], []
    before = t.store.path.read_bytes()
    executor = ThreadPoolExecutor(max_workers=1)
    def submit(title, operation, completed, failed):
        def work():
            if phase == 'pending-review':
                assert release.wait(5)
            try:
                result = operation()
                returned.append(result)
            finally:
                computed.set()
            if phase != 'pending-review':
                assert release.wait(5)
            return result
        future = executor.submit(work)
        futures.append(future)
        def delivered():
            try:
                value = future.result()
            except (Exception, KeyboardInterrupt) as error:
                failed(error)
            else:
                completed(value)
        future.add_done_callback(lambda _: callbacks.append(delivered))
        return True
    t.session.submit = submit
    try:
        assert (t.session.save() if phase == 'completed-save' else t.session.review_saved())
        if phase != 'pending-review':
            assert computed.wait(5), 'The real review/save must finish before native deletion.'
        cached = t.view._native_id
        t.view.destroyed(SimpleNamespace(GetId=lambda: t.view.items.native_id, Skip=lambda: None))
        assert t.view.alive
        if direct:
            t.view.Destroy()
        else:
            Control.Destroy(t.parent)
        assert not t.view and not t.view.alive and t.view._native_id == cached
        release.set()
        executor.shutdown(wait=True)
        assert len(callbacks) == 1
        callbacks[0]()
        for button in t.view.buttons.values():
            button.bindings['button'](None)
        assert t.view.Destroy() is False and t.parent.forbidden == []
        assert t.mci.starts == []
        saved, _ = PlaylistStore(t.app.root).load()
        if phase == 'completed-save':
            assert saved.frame == 4000 and returned[0]['status'] == 'saved'
            assert t.store.path.read_bytes() != before
        else:
            assert t.store.path.read_bytes() == before
            assert t.session.pending_saved == t.saved
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_native_parent_identity_disposal_detaches_timer_before_late_worker(owned):
    t = owned
    queued = []
    def hold(title, operation, completed, failed):
        queued.append((operation, completed, failed))
        return True
    t.session.submit = hold
    assert t.session.review_saved()
    t.host._destroyed(SimpleNamespace(GetId=lambda: t.parent.native_id, Skip=lambda: None))
    assert t.host.closed and t.host.timer.GetOwner() is None and not t.host.timer.running
    assert t.session.closed and t.session.pending.is_set()
    with pytest.raises((ValueError, InterruptedError)):
        queued[0][0]()
    assert not t.mci.starts


def test_file_chooser_parent_death_reads_no_paths_and_admits_no_work(owned):
    t = owned
    calls = []
    class Chooser(Control):
        def ShowModal(self):
            Control.Destroy(t.parent)
            return t.wx.ID_OK
        def GetPaths(self):
            pytest.fail('A deleted chooser returned new source authority.')
    t.wx.FileDialog = Chooser
    t.session.submit = lambda *args: calls.append(args)
    t.view.add()
    assert not t.view.alive and not calls and t.parent.forbidden == []


def test_repeated_queue_modal_close_keeps_the_existing_audio_owner(owned):
    t = reviewed(owned)
    t.session.select(0); t.session.play()
    before = t.player.queue_token
    t.view.close()
    assert t.player.state == 'playing' and t.player.queue_token == before
    assert t.host.timer.running and not t.session.closed
    assert t.session.view is None and t.mci.starts


def test_actual_studio_handler_interrupt_releases_busy_and_allows_explicit_retry():
    from carr_library.progress import ProgressState
    harness = runpy.run_path(str(Path(__file__).with_name('test_studio_progress_results.py')))['harness']
    run, env = harness()
    original = env.executor.submit
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt('controlled existing Studio admission interruption')
    env.executor.submit = interrupted
    observed = []
    assert run(lambda: {'status': 'saved'}, observed.append) is False
    assert env.dialog.working is False
    assert env.progress.active_task.snapshot().state == ProgressState.FAILED
    assert observed == [] and env.pending == []
    env.executor.submit = original
    assert run(lambda: {'status': 'saved'}, observed.append) is True
    assert observed == [{'status': 'saved'}]
    assert env.dialog.working is False
    assert env.progress.active_task.snapshot().state == ProgressState.SUCCEEDED
