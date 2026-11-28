"""Real clip controller and WavePlayer repeat state with a controlled native transport.

The PCM files, identity checks, A-B state, polling and quiet-owner holds are real.
Only native MCI calls, wx controls/timer delivery and the completed render fixture
are controlled; these tests do not qualify physical audio output.
"""
import ast
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.studio.project import Cue, Project, Scene


class Timer:
    def __init__(self, owner):
        self.owner = owner
        self.running = False
        self.start_ok = True
        self.stop_error = None
        self.intervals = []

    def Start(self, milliseconds):
        self.intervals.append(milliseconds)
        self.running = self.start_ok
        return self.start_ok

    def Stop(self):
        if self.stop_error is not None:
            raise self.stop_error
        self.running = False


class Hold:
    def __init__(self, progress):
        self.progress = progress
        self.closed = False

    @property
    def ready(self):
        return self.progress.ready

    def close(self):
        self.closed = True


class Progress:
    def __init__(self):
        self.ready = True
        self.holds = []
        self.registered = []
        self.releases = []
        self.refreshes = 0

    def hold_audio(self):
        hold = Hold(self)
        self.holds.append(hold)
        return hold

    def register_player(self, player):
        self.registered.append(player)

    def unregister_player(self, player, *, silence_confirmed):
        self.releases.append((player, silence_confirmed))

    def refresh_audio_state(self):
        self.refreshes += 1


class NativeTransport:
    def __init__(self):
        self.commands = []
        self.mode = 'stopped'
        self.position = 0
        self.refuse_close = False

    def command(self, command):
        self.commands.append(command)
        if command.startswith('status '):
            return self.mode if command.endswith(' mode') else str(self.position)
        if command.startswith('play '):
            self.mode = 'playing'
            self.position = int(command.split(' from ', 1)[1].split()[0])
        elif command.startswith('stop '):
            self.mode = 'stopped'
        elif command.startswith('close '):
            if self.refuse_close:
                raise RuntimeError('Controlled native close is still pending')
            self.mode = 'stopped'
        return ''

    @property
    def plays(self):
        return [command for command in self.commands if command.startswith('play ')]

    def finish(self, seconds):
        self.mode, self.position = 'stopped', round(seconds * 1000)


def pcm(path, value=7):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(int(value).to_bytes(2, 'little', signed=True) * 16000)
    return path


@pytest.fixture
def preview(tmp_path, monkeypatch):
    import carr_library
    import carr_library.speech.player as player_module
    source = Path(carr_library.__file__).parent / 'ui/studio_clips.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    classes = [node for node in parsed.body if isinstance(node, (ast.Assign, ast.ClassDef))]
    scheduled = []
    wx = SimpleNamespace(Dialog=object, ID_OK=1, ID_CANCEL=2, EVT_TIMER=object(),
                         Timer=Timer, CallLater=lambda delay, action: scheduled.append((delay, action)))
    namespace = dict(wx=wx, Path=Path, __name__='carr_library.ui._clip_repeat_test',
                     __package__='carr_library.ui')
    exec(compile(ast.Module(body=classes, type_ignores=[]), str(source), 'exec'), namespace)
    dialog = object.__new__(namespace['ClipDialog'])
    dialog.closed = dialog.closing = dialog.pending = dialog.destroyed = False
    dialog.cancelled = threading.Event()
    dialog.player = None
    dialog.playback_generation = 0
    dialog.previews = []
    dialog._preview_binding = dialog._preview_repeat_timer = dialog._preview_repeat_pending = None
    dialog.status, dialog._enable, dialog.Bind, dialog.Hide = Mock(), Mock(), Mock(), Mock()
    dialog.IsModal = Mock(return_value=False)
    dialog.library = SimpleNamespace(root=tmp_path / 'library')
    dialog.library_root = dialog.library.root
    dialog.library_root.mkdir()
    dialog._library_identity = dialog._library_stamp()
    progress = Progress()
    dialog.frame = SimpleNamespace(library=dialog.library, working=False, _closing=False,
                                   cancel_event=threading.Event(), progress_panel=progress)
    cue = Cue('first', 'inputs/' + 'a' * 64 + '.audio', duration=2)
    second = replace(cue, id='second')
    scene = Scene('scene', 'Scene', (), cues=(cue, second))
    project = Project('project', 'Project', (), (scene,), schema=2)
    dialog.project = project
    selected = [0]
    dialog._scene = lambda: dialog.project.scenes[0]
    dialog._clip = lambda: dialog._scene().cues[selected[0]]
    path = pcm(tmp_path / 'clip-preview-real-pcm.wav')
    dialog.editor = SimpleNamespace(preview=Mock(return_value=path))
    dialog._owns_editor = False
    dialog._service_release_requested = False
    dialog._worker_future = None
    work = []

    def run_job(title, operation, done, failed):
        future = Future()
        dialog.frame.working = True
        dialog.frame.cancel_event.clear()
        work.append((future, operation, done, failed))
        return future

    def settle():
        future, operation, done, failed = work.pop(0)
        try:
            result = operation()
        except BaseException as error:
            future.set_exception(error)
            dialog.frame.working = False
            failed(error)
            raise
        else:
            future.set_result(result)
            dialog.frame.working = False
            done(result)

    dialog.frame.run_job = run_job
    # Substitute only this module's platform selector, not process-wide os.name.
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    player = player_module.WavePlayer()
    transport = NativeTransport()
    monkeypatch.setattr(player, '_mci', transport.command)
    monkeypatch.setattr(player_module, 'WavePlayer', lambda: player)
    dialog.preview()
    settle()
    assert dialog._preview_binding[0] is player and dialog._preview_binding[1] == player.path == path
    assert player.state == 'playing' and len(transport.plays) == 1
    return SimpleNamespace(dialog=dialog, player=player, transport=transport, path=path,
                           progress=progress, scheduled=scheduled, wx=wx, project=project,
                           selected=selected, settle=settle, work=work)


def apply(preview, first=.25, last=.75):
    dialog = preview.dialog
    return dialog._apply_preview_repeat(dialog._preview_binding, dialog.playback_generation, first, last)


def modal(preview, *, choice=0, values=('.25', '.75'), cancel=None, change=None):
    pickers = []
    for stage, value in [('choice', choice), ('A', values[0]), ('B', values[1])]:
        picker = Mock()
        def show(stage=stage):
            if change is not None:
                change(stage)
            return preview.wx.ID_CANCEL if cancel == stage else preview.wx.ID_OK
        picker.ShowModal.side_effect = show
        picker.GetSelection.return_value = choice
        picker.GetValue.return_value = str(value)
        pickers.append(picker)
    preview.wx.SingleChoiceDialog = Mock(return_value=pickers[0])
    preview.wx.TextEntryDialog = Mock(side_effect=pickers[1:])
    return pickers


def test_explicit_repeat_reuses_exact_preview_and_real_player_state(preview):
    original = preview.path.read_bytes()
    fingerprint = preview.project.fingerprint
    assert apply(preview)
    assert preview.player.ab_loop == (.25, .75)
    assert preview.dialog._preview_repeat_timer.intervals == [100]
    assert len(preview.transport.plays) == 2
    preview.transport.finish(.75)
    preview.dialog._poll_preview_repeat()
    assert len(preview.transport.plays) == 3
    assert preview.transport.plays[-1].endswith(' from 250 to 750')
    assert preview.player.path == preview.path and preview.player.ab_loop == (.25, .75)
    assert preview.dialog.editor.preview.call_count == 1
    assert preview.project.fingerprint == fingerprint and preview.path.read_bytes() == original
    assert all(hold.closed for hold in preview.progress.holds)


def test_repeat_waits_for_real_quiet_hold_and_coalesces_timer_notifications(preview):
    apply(preview)
    preview.progress.ready = False
    preview.transport.finish(.75)
    for _ in range(4):
        preview.dialog._poll_preview_repeat()
    assert len(preview.transport.plays) == 2
    assert len(preview.scheduled) == 1 and preview.scheduled[0][0] == 25
    assert preview.dialog._preview_repeat_pending == preview.dialog.playback_generation
    assert not preview.progress.holds[-1].closed
    preview.progress.ready = True
    preview.scheduled.pop(0)[1]()
    assert len(preview.transport.plays) == 3
    assert preview.dialog._preview_repeat_pending is None
    assert all(hold.closed for hold in preview.progress.holds)


@pytest.mark.parametrize('boundary', ['stop', 'close', 'replace-library', 'change-project', 'change-clip'])
def test_stale_repeat_releases_quiet_hold_without_restarting_owned_preview(preview, tmp_path, boundary):
    apply(preview)
    preview.progress.ready = False
    preview.transport.finish(.75)
    preview.dialog._poll_preview_repeat()
    pending = preview.scheduled.pop(0)[1]
    generation = preview.dialog.playback_generation
    if boundary == 'stop':
        assert preview.dialog.stop()
    elif boundary == 'close':
        preview.dialog.close()
        assert preview.dialog.closed and not preview.path.exists()
    elif boundary == 'replace-library':
        root = preview.dialog.library_root
        root.rename(tmp_path / 'original-library')
        root.mkdir()
        (root / 'canary').write_bytes(b'untouched replacement')
        preview.dialog._poll_preview_repeat()
        assert sorted(p.name for p in root.iterdir()) == ['canary']
    elif boundary == 'change-project':
        preview.dialog.project = replace(preview.project, revision=2)
        preview.dialog._poll_preview_repeat()
    else:
        preview.selected[0] = 1
        preview.dialog._poll_preview_repeat()
    assert preview.dialog.playback_generation > generation
    preview.progress.ready = True
    pending()
    assert len(preview.transport.plays) == 2 and not preview.player._open
    assert all(hold.closed for hold in preview.progress.holds)
    assert not preview.dialog._preview_repeat_timer.running


@pytest.mark.parametrize('stage', ['choice', 'A', 'B'])
def test_modal_repeat_review_cannot_follow_replaced_library(preview, tmp_path, stage):
    original = preview.path.read_bytes()
    root = preview.dialog.library_root
    generation = preview.dialog.playback_generation

    def changed(current):
        if current == stage:
            root.rename(tmp_path / 'prior-library')
            root.mkdir()
            (root / 'canary').write_bytes(b'unchanged')

    pickers = modal(preview, change=changed)
    assert preview.dialog.preview_repeat() is False
    assert len(preview.transport.plays) == 1 and preview.player.ab_loop is None
    assert preview.dialog.playback_generation == generation
    assert preview.path.read_bytes() == original
    assert sorted(p.name for p in root.iterdir()) == ['canary']
    for picker in pickers[:['choice', 'A', 'B'].index(stage) + 1]:
        picker.Destroy.assert_called_once()


@pytest.mark.parametrize('first,last', [
    (float('nan'), .9), (.1, float('inf')), (-.1, .5), (.8, .2), (0, .049), (0, 2.1)])
def test_invalid_repeat_boundaries_do_not_change_playback_or_acquire_quiet_hold(preview, first, last):
    generation = preview.dialog.playback_generation
    holds = len(preview.progress.holds)
    with pytest.raises(ValueError, match='inside this preview'):
        apply(preview, first, last)
    assert preview.dialog.playback_generation == generation
    assert preview.player.ab_loop is None and len(preview.transport.plays) == 1
    assert len(preview.progress.holds) == holds
    assert preview.dialog._preview_repeat_timer is None


def test_clear_repeat_on_stopped_preview_does_not_start_audio(preview):
    apply(preview)
    preview.transport.finish(.75)
    assert preview.player.state == 'stopped'
    pickers = modal(preview, choice=1)
    assert preview.dialog.preview_repeat()
    assert preview.player.ab_loop is None and not preview.dialog._preview_repeat_timer.running
    assert len(preview.transport.plays) == 2 and preview.path.exists()
    assert preview.dialog.editor.preview.call_count == 1
    preview.wx.TextEntryDialog.assert_not_called()
    pickers[0].Destroy.assert_called_once()


def test_changed_pcm_is_refused_before_real_player_repeat_and_native_owner_closes(preview):
    import os
    apply(preview)
    old_identity = preview.player._identity
    original = preview.path.read_bytes()
    pcm(preview.path, value=11)
    # A same-size write can retain the observed Windows timestamp. Make this
    # real filesystem version change explicit; do not substitute player stamps.
    info = preview.path.stat()
    os.utime(preview.path, ns=(info.st_atime_ns, old_identity[3] + 2_000_000_000))
    assert preview.player._stamp(preview.path) != old_identity
    assert preview.player._stamp(preview.path)[:3] == old_identity[:3]
    assert preview.path.stat().st_mtime_ns != old_identity[3]
    changed = preview.path.read_bytes()
    assert changed != original and len(changed) == len(original)
    preview.transport.finish(.75)
    preview.dialog._poll_preview_repeat()
    assert len(preview.transport.plays) == 2
    assert not preview.player._open and preview.player.ab_loop is None
    assert preview.dialog._preview_binding is None
    assert not preview.dialog._preview_repeat_timer.running
    assert 'recording changed' in preview.dialog.status.ChangeValue.call_args.args[0]
    assert preview.path.read_bytes() == changed


def test_unconfirmed_native_close_retains_preview_until_real_release(preview):
    apply(preview)
    preview.progress.ready = False
    preview.transport.finish(.75)
    preview.dialog._poll_preview_repeat()
    pending = preview.scheduled.pop(0)[1]
    preview.transport.refuse_close = True
    event = Mock()
    event.CanVeto.return_value = True
    preview.dialog.close(event)
    assert not preview.dialog.closed and preview.path.exists()
    assert preview.player._open and preview.dialog._preview_binding is None
    assert not preview.dialog._preview_repeat_timer.running
    assert preview.progress.releases[-1] == (preview.player, False)
    event.Veto.assert_called_once()
    preview.progress.ready = True
    pending()
    assert len(preview.transport.plays) == 2 and all(hold.closed for hold in preview.progress.holds)
    preview.transport.refuse_close = False
    preview.dialog.close()
    assert preview.dialog.closed and not preview.path.exists() and not preview.player._open
    assert preview.progress.releases[-1] == (preview.player, True)


@pytest.mark.parametrize('failure', ['start', 'stop'])
def test_timer_failure_never_bypasses_owned_native_stop_or_deletes_live_preview(preview, failure):
    timer = Timer(preview.dialog)
    preview.dialog._preview_repeat_timer = timer
    if failure == 'start':
        timer.start_ok = False
        assert apply(preview) is False
        assert not preview.player._open and preview.path.exists()
    else:
        assert apply(preview)
        timer.stop_error = RuntimeError('Controlled timer Stop refusal')
        preview.dialog.close()
        assert not preview.dialog.closed and preview.path.exists() and not preview.player._open
        timer.stop_error = None
    preview.dialog.close()
    assert preview.dialog.closed and not preview.path.exists()
    assert preview.dialog._preview_repeat_pending is None


@pytest.mark.parametrize('stage', ['choice', 'A', 'B'])
def test_cancelled_repeat_review_keeps_current_playback_and_saved_project(preview, stage):
    fingerprint = preview.project.fingerprint
    generation = preview.dialog.playback_generation
    pickers = modal(preview, cancel=stage)
    assert preview.dialog.preview_repeat() is False
    assert preview.player.state == 'playing' and preview.player.ab_loop is None
    assert len(preview.transport.plays) == 1 and preview.dialog.playback_generation == generation
    assert preview.project.fingerprint == fingerprint
    assert preview.dialog._preview_repeat_timer is None
    for picker in pickers[:['choice', 'A', 'B'].index(stage) + 1]:
        picker.Destroy.assert_called_once()


@pytest.mark.parametrize('dry', [False, True])
def test_current_preview_generation_binds_saved_asset_without_automatic_repeat(preview, dry):
    first_binding = preview.dialog._preview_binding
    preview.dialog.preview(dry=dry)
    preview.settle()
    assert preview.dialog._preview_binding is not first_binding
    assert preview.dialog._preview_binding[0] is preview.player
    assert preview.dialog._preview_binding[1] == preview.path
    assert preview.player.ab_loop is None and preview.dialog._preview_repeat_timer is None
    options = preview.dialog.editor.preview.call_args.kwargs
    assert ('dry' in options) is dry
    assert preview.dialog.editor.preview.call_count == 2
    assert len(preview.transport.plays) == 2


def test_explicit_repeat_play_handles_eof_during_range_review_without_rerender(preview):
    original = preview.path.read_bytes()
    fingerprint = preview.project.fingerprint

    def natural_end(stage):
        if stage == 'A':
            preview.transport.finish(2)
            assert preview.player.state == 'stopped'

    modal(preview, change=natural_end)
    assert preview.dialog.preview_repeat()
    assert preview.player.state == 'playing' and preview.player.ab_loop == (.25, .75)
    assert preview.transport.plays[-1].endswith(' from 250 to 750')
    assert len(preview.transport.plays) == 2
    preview.transport.finish(.75)
    preview.dialog._poll_preview_repeat()
    assert len(preview.transport.plays) == 3 and preview.player.state == 'playing'
    assert preview.dialog.editor.preview.call_count == 1
    assert preview.project.fingerprint == fingerprint and preview.path.read_bytes() == original
    assert all(hold.closed for hold in preview.progress.holds)


# Native driver regressions execute the exact companion's main-level helpers.
# They control wx delivery only; real playback and a physical device are not
# admitted by these portable cases.
def _native_clip_driver_namespace():
    import ctypes
    from ctypes import wintypes
    import os
    import sys
    import traceback
    native = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_clip_editor_smoke.py'
    tree = ast.parse(native.read_text(encoding='utf-8'))
    mains = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main']
    assert len(mains) == 1
    names = ('pump', 'guarded', 'native_control_current', 'focus', 'release', 'press',
             'descendants', 'current_modal', 'choose', 'enter', 'modal_sequence',
             'observe_destroy', 'passive_output', 'begin_shutdown', 'shutdown_tick')
    functions = []
    for name in names:
        matches = [node for node in mains[0].body
                   if isinstance(node, ast.FunctionDef) and node.name == name]
        assert len(matches) == 1, name
        functions.append(matches[0])
    state = {'time': 0.0, 'focus': None, 'foreground': 0,
             'alive': {101, 202, 303, 404}, 'pid': {}, 'tops': [],
             'callbacks': [], 'events': [], 'errors': []}
    main_loop = SimpleNamespace(running=True, main=True)
    main_loop.IsRunning = lambda: main_loop.running
    main_loop.IsMain = lambda: main_loop.main
    modal_loop = SimpleNamespace(running=True, main=False)
    modal_loop.IsRunning = lambda: modal_loop.running
    modal_loop.IsMain = lambda: modal_loop.main
    state['active_loop'] = modal_loop

    class NativeWindow:
        def __init__(self, handle=303, identifier=30, title='Owned clip modal',
                     parent=None, modal=True, enabled=True):
            self.handle, self.identifier, self.title = handle, identifier, title
            self.parent, self.modal, self.enabled = parent, modal, enabled
            self.shown, self.children, self.name = True, [], 'Native clip control'
            self.top = self
            self.closed = self.pending = False
            self.cancelled = threading.Event()
        def GetHandle(self): return self.handle
        def GetId(self): return self.identifier
        def GetTitle(self): return self.title
        def GetParent(self): return self.parent
        def GetName(self): return self.name
        def GetChildren(self): return self.children
        def IsModal(self): return self.modal
        def IsEnabled(self): return self.enabled
        def IsShownOnScreen(self): return self.shown
        def Raise(self): state['events'].append('raise')
        def SetFocus(self): state['focus'] = self
        def EndModal(self, answer):
            state['events'].append(('end-modal', self.handle, answer))
            self.modal = False
            modal_loop.running = False
            state['active_loop'] = main_loop

    def call_later(milliseconds, callback):
        state['callbacks'].append((milliseconds, callback))
        return SimpleNamespace(Stop=lambda: None)
    def yield_once():
        state['events'].append('yield')
        if state['callbacks']:
            milliseconds, callback = state['callbacks'].pop(0)
            state['time'] += milliseconds / 1000
            callback()
    def sleep(seconds): state['time'] += seconds
    def pid(handle, result):
        result._obj.value = state['pid'].get(handle, os.getpid())
        return 1
    def foreground(handle):
        state['foreground'] = handle
        return True
    def key_down(key, modifiers=0):
        state['events'].append(('down', key, modifiers))
        return True
    def key_up(key, modifiers=0):
        state['events'].append(('up', key, modifiers))
        return True
    def check(name, condition):
        assert condition, name
        state['events'].append(('check', name))
    def remember_failure():
        state['errors'].append(sys.exc_info()[1])

    wx = SimpleNamespace(
        WXK_SPACE=32, MOD_NONE=0, MOD_CONTROL=2, ID_CANCEL=5101, ID_OK=5100,
        Dialog=NativeWindow, CallLater=call_later, CallAfter=lambda callback: call_later(0, callback),
        GetTopLevelParent=lambda control: control.top,
        GetTopLevelWindows=lambda: state['tops'],
        EventLoopBase=SimpleNamespace(GetActive=lambda: state['active_loop']),
        Window=SimpleNamespace(FindFocus=lambda: state['focus']),
        EventFilter=SimpleNamespace(Event_Skip=-1),
        EVT_WINDOW_DESTROY=SimpleNamespace(typeId=98))
    frame = NativeWindow(101, 10, 'Host', modal=False)
    frame.working = False
    frame.cancel_event = threading.Event()
    dialog = NativeWindow(202, 20, 'Clip editor', parent=frame, modal=False)
    namespace = dict(wx=wx, user32=SimpleNamespace(
        IsWindow=lambda handle: handle in state['alive'],
        GetWindowThreadProcessId=pid, SetForegroundWindow=foreground,
        GetForegroundWindow=lambda: state['foreground']),
        gui=SimpleNamespace(Yield=yield_once, ExitMainLoop=lambda: state['events'].append('loop-exit')),
        simulator=SimpleNamespace(KeyDown=key_down, KeyUp=key_up),
        check=check, remember_failure=remember_failure, monotonic=lambda: state['time'],
        sleep=sleep, ctypes=ctypes, wintypes=wintypes, os=os, sys=sys, traceback=traceback,
        errors=state['errors'], pressed=set(), modal_loops=[], destroyed=set(),
        frame=frame, dialog=dialog, host_handle=101, dialog_handle=202, host_id=10, dialog_id=20,
        closing={'scheduled': False, 'deadline': 45, 'dialog_destroy': False,
                 'host_close': False, 'complete': False, 'stalled': False})
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                 str(native), 'exec'), namespace)
    namespace.update(state=state, NativeWindow=NativeWindow,
                     main_loop=main_loop, modal_loop=modal_loop)
    return namespace


def _native_clip_destroy_event(namespace, identifier, *, kind=98, window_identifier=None):
    window = None if window_identifier is None else SimpleNamespace(GetId=lambda: window_identifier)
    return SimpleNamespace(GetEventType=lambda: kind, GetId=lambda: identifier,
                           GetWindow=lambda: window)


@pytest.mark.parametrize('case', ['host', 'clip', 'wrong-type', 'wrong-event-id',
                                  'missing-window', 'wrong-window-id'])
def test_native_loop_destroy_observer_requires_external_event_and_captured_ids(case):
    namespace = _native_clip_driver_namespace()
    identifier = 20 if case == 'clip' else 10
    event = _native_clip_destroy_event(namespace,
        99 if case == 'wrong-event-id' else identifier,
        kind=97 if case == 'wrong-type' else 98,
        window_identifier=None if case == 'missing-window' else
                          99 if case == 'wrong-window-id' else identifier)
    assert namespace['observe_destroy'](event) == -1
    expected = {('host', 101)} if case == 'host' else {('clip', 202)} if case == 'clip' else set()
    assert namespace['destroyed'] == expected


@pytest.mark.parametrize('blocked', ['held-escape', 'disabled-control'])
def test_native_loop_key_waits_for_prior_release_and_enabled_current_control(blocked):
    namespace = _native_clip_driver_namespace()
    control, state = namespace['dialog'], namespace['state']
    if blocked == 'held-escape':
        namespace['pressed'].add((27, 0))
    else:
        control.enabled = False
    def ready():
        assert not any(isinstance(item, tuple) and item[0] == 'down' for item in state['events'])
        namespace['pressed'].discard((27, 0))
        control.enabled = True
        state['events'].append('ready')
    namespace['wx'].CallLater(25, ready)
    namespace['press'](control)
    assert state['events'].index('ready') < state['events'].index(('down', 32, 0))
    assert state['events'].index(('down', 32, 0)) < state['events'].index(('up', 32, 0))
    assert not namespace['pressed'] and not state['errors']


def test_native_loop_focus_refusal_never_dispatches_a_key():
    namespace = _native_clip_driver_namespace()
    namespace['wx'].Window.FindFocus = lambda: None
    with pytest.raises(TimeoutError, match='foreground keyboard focus'):
        namespace['press'](namespace['dialog'])
    assert not any(isinstance(item, tuple) and item[0] == 'down'
                   for item in namespace['state']['events'])
    assert not namespace['pressed']


@pytest.mark.parametrize('foreign', ['parent', 'pid', 'dead-window', 'modeless'])
def test_native_loop_modal_discovery_refuses_foreign_or_inactive_windows(foreign):
    namespace = _native_clip_driver_namespace()
    modal = namespace['NativeWindow'](parent=namespace['dialog'])
    if foreign == 'parent':
        modal.parent = namespace['frame']
    elif foreign == 'pid':
        namespace['state']['pid'][303] = namespace['os'].getpid() + 1
    elif foreign == 'dead-window':
        namespace['state']['alive'].remove(303)
    else:
        modal.modal = False
    namespace['state']['tops'] = [modal]
    assert namespace['current_modal']('Owned clip modal') is None
    assert namespace['modal_loops'] == []


@pytest.mark.parametrize('invalid', ['ambiguous', 'stopped-loop', 'main-loop'])
def test_native_loop_modal_admission_requires_one_actual_running_owned_loop(invalid):
    namespace = _native_clip_driver_namespace()
    modal = namespace['NativeWindow'](parent=namespace['dialog'])
    namespace['state']['tops'] = [modal]
    if invalid == 'ambiguous':
        namespace['state']['tops'].append(namespace['NativeWindow'](
            handle=404, identifier=40, parent=namespace['dialog']))
    elif invalid == 'stopped-loop':
        namespace['modal_loop'].running = False
    else:
        namespace['state']['active_loop'] = namespace['main_loop']
    with pytest.raises(AssertionError, match='Ambiguous|actual running child loop'):
        namespace['current_modal']('Owned clip modal')
    assert namespace['modal_loops'] == []


def test_native_loop_cleanup_waits_for_the_real_shared_worker_before_destroy_and_exit():
    from carr_library.ui.worker_lifetime import DesktopExecutor
    namespace = _native_clip_driver_namespace()
    executor, entered, release = DesktopExecutor(), threading.Event(), threading.Event()
    events = namespace['state']['events']
    def worker():
        entered.set()
        assert release.wait(5), 'Controlled worker was not released.'
        return 'settled'
    future = executor.submit(worker)
    try:
        assert entered.wait(5)
        namespace['frame'].working = True
        def close_dialog():
            assert future.done()
            events.append('clip-close')
            namespace['dialog'].closed = True
        def destroy_dialog():
            assert future.done()
            events.append('clip-destroy')
            namespace['observe_destroy'](_native_clip_destroy_event(
                namespace, 20, window_identifier=20))
            namespace['state']['alive'].remove(202)
        def close_frame():
            assert future.done()
            events.append('host-close')
            executor.shutdown(wait=False, cancel_futures=True)
            namespace['observe_destroy'](_native_clip_destroy_event(
                namespace, 10, window_identifier=10))
            namespace['state']['alive'].remove(101)
        namespace['dialog'].close = close_dialog
        namespace['dialog'].Destroy = destroy_dialog
        namespace['frame'].Close = close_frame
        namespace['shutdown_tick']()
        assert namespace['frame'].cancel_event.is_set() and not future.done()
        assert 'clip-close' not in events and 'host-close' not in events and 'loop-exit' not in events
        release.set()
        assert future.result(timeout=5) == 'settled'
        namespace['frame'].working = False
        for _ in range(3):
            namespace['gui'].Yield()
        assert namespace['closing']['complete'] and not namespace['closing']['stalled']
        assert events.index('clip-close') < events.index('clip-destroy') < events.index('host-close')
        assert events.index('host-close') < events.index('loop-exit')
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
    assert not executor.cleanup_failures


def test_native_loop_failure_cleanup_closes_only_one_captured_modal_per_later_turn():
    namespace = _native_clip_driver_namespace()
    modal = namespace['NativeWindow'](parent=namespace['dialog'])
    namespace['state']['tops'] = [modal]
    assert namespace['current_modal']('Owned clip modal') is modal
    namespace['shutdown_tick']()
    assert [item for item in namespace['state']['events']
            if isinstance(item, tuple) and item[0] == 'end-modal'] == [('end-modal', 303, 5101)]
    assert not namespace['closing']['dialog_destroy'] and not namespace['closing']['host_close']
    assert len(namespace['state']['callbacks']) == 1
    assert namespace['state']['callbacks'][0][0] == 25
    assert 'loop-exit' not in namespace['state']['events']


@pytest.mark.parametrize('missing', ['host', 'clip'])
def test_native_loop_missing_destroy_acknowledgement_retains_original_loop(missing):
    namespace = _native_clip_driver_namespace()
    namespace['state']['alive'].difference_update((101, 202))
    namespace['destroyed'].add(('clip', 202) if missing == 'host' else ('host', 101))
    namespace['shutdown_tick']()
    assert namespace['closing']['stalled'] and not namespace['closing']['complete']
    assert 'loop-exit' not in namespace['state']['events']
    assert len(namespace['errors']) == 1
    assert 'actual clip and host Destroy events' in namespace['errors'][0]


def test_native_loop_passive_fixture_uses_real_pcm_and_player_without_opening_output(tmp_path):
    from carr_library.speech.player import WavePlayer
    namespace = _native_clip_driver_namespace()
    path = tmp_path / 'passive.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b'\0\0' * 12000)
    original = path.read_bytes()
    player = WavePlayer()
    try:
        player.set_playlist(WavePlayer.prepare_playlist([path]))
        player.select_queue(0)
        player.set_ab_loop(.1, .3)
        assert player.path == path.resolve() and player.ab_loop == (.1, .3)
        assert namespace['passive_output'](player)
        assert player.poll() is None
        player.clear_ab_loop()
        assert namespace['passive_output'](player) and player.ab_loop is None
    finally:
        player.close()
    assert namespace['passive_output'](player) and path.read_bytes() == original


def test_native_loop_keyup_failure_preserves_original_failure_and_modifier_release():
    namespace = _native_clip_driver_namespace()
    failure = RuntimeError('native release was not acknowledged')
    observed = []
    def failed_keyup(key, modifiers):
        observed.append((key, modifiers))
        raise failure
    namespace['simulator'].KeyUp = failed_keyup
    namespace['pressed'].add((65, 2))
    namespace['release'](65, 2, 'selected text')
    assert observed == [(65, 2)] and namespace['errors'] == [failure]
    assert not namespace['pressed']
