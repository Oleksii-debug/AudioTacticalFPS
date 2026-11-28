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
    apply(preview)
    old_identity = preview.player._identity
    pcm(preview.path, value=11)
    assert preview.player._stamp(preview.path) != old_identity
    changed = preview.path.read_bytes()
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
