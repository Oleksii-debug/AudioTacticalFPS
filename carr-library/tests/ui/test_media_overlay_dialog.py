"""Real EPUB preparation with widget stand-ins; native widgets have a separate probe."""
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import pytest

from carr_library.application import LibraryApp
from carr_library.ui import media_overlay_dialog as ui

_spec = importlib.util.spec_from_file_location('overlay_reader_fixtures',
    Path(__file__).parents[1] / 'reader' / 'test_media_overlay.py')
_fixtures = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_fixtures)
actual_job = _fixtures.actual_job
publication = _fixtures.publication
Player = _fixtures.Player


class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.value, self.label, self.choices = value, label, list(choices)
        self.selection, self.enabled, self.name = -1, True, ''
    def SetName(self, name): self.name = name
    def GetName(self): return self.name
    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def Set(self, rows): self.choices = list(rows); self.selection = -1
    def SetSelection(self, index): self.selection = index
    def GetSelection(self): return self.selection
    def Enable(self, value=True): self.enabled = value
    def IsEnabled(self): return self.enabled
    def Bind(self, *args): pass
    def SetSizer(self, *args): pass
    def SetScrollRate(self, *args): pass
    def FitInside(self): pass
    def Wrap(self, *args): pass
    def SetFocus(self): self.focused = True
    def IsModal(self): return False
    def Hide(self): self.hidden = True


class Sizer:
    def __init__(self, *args): pass
    def Add(self, *args): pass


class Timer:
    def __init__(self, *args): self.running = False
    def Start(self, *args): self.running = True
    def Stop(self): self.running = False


class Progress:
    def __init__(self):
        self.event = SimpleNamespace(token=1)
        self.players, self.holds, self.removed = [], [], []
        self.ready = True
    def register_player(self, player): self.players.append(player)
    def unregister_player(self, player, *, silence_confirmed=True):
        self.removed.append(silence_confirmed)
        self.players.remove(player)
    def refresh_audio_state(self): pass
    def update(self, **kwargs): pass
    def hold_audio(self):
        hold = SimpleNamespace(ready=self.ready, closed=False)
        hold.close = lambda: setattr(hold, 'closed', True)
        self.holds.append(hold)
        return hold


@pytest.fixture
def desktop(publication):
    root, path, _ = publication
    wx = SimpleNamespace(**{key: i + 1 for i, key in enumerate((
        'DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND',
        'TE_MULTILINE', 'TE_READONLY', 'EVT_BUTTON', 'EVT_CHOICE', 'EVT_TIMER', 'EVT_CLOSE',
        'ID_CLOSE', 'ID_OK', 'FD_OPEN', 'FD_FILE_MUST_EXIST'))})
    for name in ('Dialog', 'ScrolledWindow', 'StaticText', 'TextCtrl', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer, wx.Timer = Sizer, Timer
    wx.CallAfter = lambda callback, *args: callback(*args)
    wx.later = []
    wx.CallLater = lambda delay, callback: wx.later.append(callback)
    frame = SimpleNamespace(library=LibraryApp(root), working=False, _closing=False,
                            cancel_event=Event(), operation_progress=Progress(), operations=[], reports=[])
    frame.report = frame.reports.append
    def run_job(title, operation, *, done, failed):
        frame.working = True; frame.cancel_event.clear(); frame.operations.append(title)
        try:
            done(operation())
        except Exception as exc:
            failed(exc)
        finally:
            frame.working = False
    frame.run_job = run_job
    dialog = ui.create_media_overlay_dialog(frame, wx_module=wx, _player_factory=Player)
    dialog.path.ChangeValue(str(path))
    yield frame, dialog, wx
    dialog.on_close()


def load(dialog):
    dialog.on_load()
    assert dialog.session is not None, dialog.status.GetValue()


def test_load_and_keyboard_navigation_do_not_autoplay(desktop):
    frame, dialog, _ = desktop
    assert frame.operation_progress.players == [dialog.player]
    load(dialog)
    assert 'playback is stopped' in dialog.status.GetValue()
    assert not any(call[0] == 'play' for call in dialog.player.calls)
    dialog.on_next()
    assert dialog.session.index == 1 and dialog.interval.GetSelection() == 1
    dialog.on_previous()
    assert dialog.session.index == 0
    dialog.chapter.SetSelection(len(dialog.document.chapters) - 1); dialog.on_chapter()
    assert dialog.session.index == dialog.document.chapters[-1]['first_interval']
    dialog.seek_seconds.ChangeValue('.1'); dialog.on_seek()
    assert dialog.session.cursor == .1 and dialog.seek_seconds.GetValue() == '.1'
    assert not any(call[0] == 'play' for call in dialog.player.calls)
    assert dialog.player.state == 'stopped'


def test_explicit_play_verifies_audio_and_uses_existing_progress_quiet_hold(desktop):
    frame, dialog, _ = desktop
    load(dialog); dialog.on_play()
    assert dialog.player.state == 'playing', dialog.status.GetValue()
    assert frame.operations[-1] == 'Verifying prepared synchronized audio'
    assert frame.operation_progress.holds and all(hold.closed for hold in frame.operation_progress.holds)
    dialog.player.position_seconds = .1; dialog.on_pause()
    assert dialog.player.state == 'paused'
    dialog.on_play()
    assert dialog.player.calls[-1] == ('resume',)
    dialog.on_stop()
    assert dialog.player.state == 'stopped' and dialog.session.cursor == .1


def test_closing_pending_quiet_start_releases_hold_without_playback(desktop):
    frame, dialog, wx = desktop
    load(dialog)
    frame.operation_progress.ready = False
    dialog.on_play()
    assert wx.later and not any(call[0] == 'play' for call in dialog.player.calls)
    dialog.on_close()
    for callback in wx.later:
        callback()
    assert all(hold.closed for hold in frame.operation_progress.holds)
    assert not any(call[0] == 'play' for call in dialog.player.calls)
    assert frame.operation_progress.removed == [True]


def test_closing_while_playback_verification_runs_defers_file_cleanup(desktop):
    frame, dialog, _ = desktop
    load(dialog)
    path = dialog.document.audio
    pending = []
    frame.run_job = lambda title, operation, done, failed: pending.append((operation, done, failed))
    dialog.on_play(); assert dialog.busy
    dialog.on_close()
    assert path.exists()
    operation, done, failed = pending[0]
    try: done(operation())
    except Exception as exc: failed(exc)
    assert not path.exists() and not dialog.alive
    assert not any(call[0] == 'play' for call in dialog.player.calls)


def test_late_preparation_result_is_closed_after_dialog_close(desktop, monkeypatch):
    frame, dialog, _ = desktop
    prepared = ui.prepare_overlay(Path(dialog.path.GetValue()), library_root=frame.library.root)
    path = prepared.audio
    monkeypatch.setattr(ui, 'prepare_overlay', lambda *a, **k: prepared)
    pending = []
    frame.run_job = lambda title, operation, done, failed: pending.append((operation, done, failed))
    dialog.on_load(); assert dialog.busy
    dialog.on_close()
    operation, done, failed = pending[0]
    done(operation())
    assert not path.exists() and not dialog.alive


def test_device_close_failure_retains_audio_and_quiet_registration(desktop):
    frame, dialog, _ = desktop
    load(dialog)
    prepared, player = dialog.document, dialog.player
    path = prepared.audio
    original_close = player.close
    player.close = lambda: (_ for _ in ()).throw(RuntimeError('Controlled device close failure'))
    dialog.on_close()
    assert path.exists() and frame.operation_progress.removed == [False]
    assert frame._reader_playback_retained == [(player, prepared)]
    player.close = original_close
    player.close(); prepared.close()


def test_library_change_blocks_play_before_verification(desktop, tmp_path):
    frame, dialog, _ = desktop
    load(dialog)
    frame.library = LibraryApp(tmp_path / 'other-library')
    before = list(frame.operations)
    dialog.on_play()
    assert frame.operations == before and 'library changed' in dialog.status.GetValue()
    assert not any(call[0] == 'play' for call in dialog.player.calls)


def test_another_job_does_not_discard_loaded_reading(desktop):
    frame, dialog, _ = desktop
    load(dialog)
    original = dialog.document
    frame.working = True
    dialog.on_load()
    assert dialog.document is original and original.audio.exists()
    assert 'Another operation' in dialog.status.GetValue()


def test_transport_buttons_do_not_restart_running_audio_or_claim_a_stopped_pause(desktop):
    frame, dialog, _ = desktop
    load(dialog)
    before = list(frame.operations)
    dialog.on_pause()
    assert dialog.player.state == 'stopped'
    assert frame.operations == before and 'not running' in dialog.status.GetValue()
    assert not any(call[0] == 'pause' for call in dialog.player.calls)
    dialog.on_play()
    assert dialog.player.state == 'playing', dialog.status.GetValue()
    calls, operations = list(dialog.player.calls), list(frame.operations)
    dialog.on_play()
    assert dialog.player.calls == calls and frame.operations == operations
    assert 'already running' in dialog.status.GetValue()
    dialog.on_pause()
    assert dialog.player.state == 'paused' and 'Playback paused' in dialog.status.GetValue()
