"""Exercise real reader playback callbacks with delayed quiet admission."""
import ast
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.speech.progress_speech import play_when_quiet
from carr_library.ui import speech_dialog


class Player:
    def __init__(self, path):
        self.path, self.state = path, 'stopped'
        self.position_seconds = .5
        self.armed = True
        self.starts, self.stops, self.sleep_due = 0, 0, False

    def poll(self):
        if self.sleep_due:
            self.sleep_due = False
            self.stop()
            return 'sleep'
        return 'repeat-pending' if self.armed and self.state == 'stopped' else None

    def repeat_if_ready(self):
        if not self.armed or self.state != 'stopped':
            return False
        self.starts += 1
        self.state = 'playing'
        return True

    def stop(self):
        self.stops += 1
        self.armed = False
        self.state = 'stopped'

    def pause(self):
        self.state, self.armed = 'paused', False
        return True

    def resume(self):
        if self.state != 'paused':
            return False
        self.state = 'playing'
        self.starts += 1
        return True


class Progress:
    def __init__(self):
        self.ready, self.holds, self.refreshes = False, [], 0

    def hold_audio(self):
        parent = self
        class Hold:
            closed = False
            @property
            def ready(self): return parent.ready
            def close(self): self.closed = True
        hold = Hold()
        self.holds.append(hold)
        return hold

    def refresh_audio_state(self):
        self.refreshes += 1


@pytest.fixture
def reader(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    state = SimpleNamespace(parent=SimpleNamespace(library=SimpleNamespace(root=root)),
        player=Player(tmp_path / 'owned-render.wav'), progress=Progress(), authorized=True,
        checks=0, reports=[], pending=[], timer=Mock())
    def guard():
        state.checks += 1
        if not state.authorized:
            raise ValueError('Source authorization was revoked.')
    def schedule(milliseconds, callback):
        assert milliseconds == 25
        state.pending.append(callback)
    values = dict(parent=state.parent, player=state.player, progress=state.progress,
        library=state.parent.library, library_root=root,
        library_identity=(root.stat().st_dev, root.stat().st_ino), source_guard=guard,
        prepare_listening=Mock(),
        alive=True, source_loaded=True, busy=False, playback_generation=1,
        repeat_scheduled=None, playback_refresh=None, playback_timer=state.timer,
        cancel=Event(), report=lambda message, **kwargs: state.reports.append(message),
        last_audio=state.player.path, dialog=Mock(), editor=Mock(),
        wx=SimpleNamespace(CallLater=schedule))
    tree = ast.parse(Path(speech_dialog.__file__).read_text(encoding='utf-8'))
    outer = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == 'show_speech_reader')
    names = {'check_current', 'playback_scope', 'playback_failed', 'start_playback', 'poll_audio',
             'playback_control', 'show_playback', 'pause_audio', 'stop_audio'}
    definitions = [node for node in outer.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == names
    result = ', '.join(repr(name) + ': ' + name for name in sorted(names))
    factory = ast.parse('def build(' + ', '.join(values) + '):\n    return {' + result + '}\n')
    factory.body[0].body[:0] = definitions
    namespace = dict(speech_dialog.__dict__, Path=Path, play_when_quiet=play_when_quiet)
    exec(compile(ast.fix_missing_locations(factory), speech_dialog.__file__, 'exec'), namespace)
    state.make_handlers = lambda: SimpleNamespace(**namespace['build'](**values))
    state.values = values
    state.handlers = state.make_handlers()
    state.cancel = values['cancel']
    return state


def ready(reader):
    reader.progress.ready = True
    while reader.pending:
        reader.pending.pop(0)()


def test_timer_reports_end_but_only_one_source_checked_quiet_callback_repeats(reader):
    reader.handlers.poll_audio()
    reader.handlers.poll_audio()
    assert reader.player.starts == 0 and reader.checks == 0
    assert len(reader.pending) == len(reader.progress.holds) == 1
    ready(reader)
    assert reader.player.starts == 1 and reader.checks == 1
    assert all(hold.closed for hold in reader.progress.holds)
    reader.handlers.poll_audio()
    assert not reader.pending and reader.player.starts == 1


@pytest.mark.parametrize('action', ['stop', 'pause', 'quiet'])
def test_pending_repeat_cannot_revive_after_user_stop_pause_or_quiet(reader, action):
    reader.handlers.poll_audio()
    if action == 'stop':
        reader.handlers.stop_audio()
        assert reader.cancel.is_set()
    elif action == 'pause':
        reader.handlers.pause_audio()
    else:
        # The existing quiet controller stops this registered player directly.
        reader.player.stop()
    ready(reader)
    assert reader.player.starts == 0
    assert reader.player.state == ('paused' if action == 'pause' else 'stopped')
    assert all(hold.closed for hold in reader.progress.holds)


@pytest.mark.parametrize('boundary', ['source', 'library-object', 'library-path', 'parent-closing'])
def test_pending_repeat_rechecks_actual_source_and_library_before_start(reader, tmp_path, boundary):
    reader.handlers.poll_audio()
    if boundary == 'source':
        reader.authorized = False
    elif boundary == 'library-object':
        reader.parent.library = SimpleNamespace(root=reader.parent.library.root)
    elif boundary == 'library-path':
        reader.parent.library.root = tmp_path / 'different-library'
    else:
        reader.parent._closing = True
    ready(reader)
    assert reader.player.starts == 0 and reader.player.stops == 1
    if boundary == 'source':
        assert any('Playback did not start' in message for message in reader.reports)
    else:
        assert reader.reports == [], 'A stale library dialog must not receive late results.'
    assert reader.cancel.is_set()
    assert all(hold.closed for hold in reader.progress.holds)


def test_library_switch_stops_owned_player_on_tick_without_reading_old_source(reader, tmp_path):
    reader.player.state = 'playing'
    reader.parent.library.root = tmp_path / 'new-library'
    reader.handlers.poll_audio()
    assert reader.player.stops == 1 and reader.player.starts == 0
    assert reader.checks == 0 and not reader.pending
    reader.timer.Stop.assert_called_once()


def test_sleep_tick_invalidates_repeat_already_waiting_for_quiet(reader):
    reader.handlers.poll_audio()
    reader.player.sleep_due = True
    reader.handlers.poll_audio()
    ready(reader)
    assert reader.player.starts == 0 and reader.player.stops == 1
    assert any('Sleep timer stopped' in message for message in reader.reports)
    assert all(hold.closed for hold in reader.progress.holds)


def test_resume_waits_for_quiet_and_stop_cancels_it(reader):
    reader.player.state, reader.player.armed = 'paused', False
    reader.handlers.pause_audio()
    assert reader.pending and reader.player.starts == 0
    reader.handlers.stop_audio()
    ready(reader)
    assert reader.player.starts == 0 and reader.player.state == 'stopped'


def test_manual_playback_control_checks_source_again_after_admission_delay(reader):
    action = Mock()
    reader.handlers.playback_control(action, audible=True)
    assert reader.checks == 1 and reader.pending
    reader.authorized = False
    ready(reader)
    assert reader.checks == 2
    action.assert_not_called()
    assert reader.player.stops == 1


def test_failed_progress_observer_cannot_prevent_stop_or_timer_shutdown(reader, tmp_path):
    reader.parent.library.root = tmp_path / 'changed'
    reader.progress.refresh_audio_state = Mock(side_effect=RuntimeError('Detached status widget'))
    reader.handlers.poll_audio()
    assert reader.player.stops == 1
    reader.timer.Stop.assert_called_once()
    assert reader.reports == [], 'Cleanup must not update the old library dialog.'
    assert reader.cancel.is_set()


def test_observer_failure_is_reported_without_hiding_source_stop_or_quiet_release(reader):
    reader.handlers.poll_audio()
    reader.authorized = False
    reader.progress.refresh_audio_state = Mock(side_effect=RuntimeError('Detached status widget'))
    ready(reader)
    assert reader.player.stops == 1 and reader.player.starts == 0
    assert 'Progress feedback is unavailable' in reader.reports[-1]
    assert 'Source authorization was revoked' in reader.reports[-1]
    assert reader.cancel.is_set() and all(hold.closed for hold in reader.progress.holds)


def test_explicit_idle_play_after_stop_gets_new_guarded_admission(reader):
    action = Mock()
    reader.handlers.stop_audio()
    assert reader.cancel.is_set()
    reader.handlers.playback_control(action, audible=True)
    assert not reader.cancel.is_set() and reader.pending
    action.assert_not_called()
    ready(reader)
    action.assert_called_once_with()
    assert all(hold.closed for hold in reader.progress.holds)


@pytest.mark.parametrize('audible', [False, True])
def test_busy_playback_controls_preserve_active_worker_cancellation(reader, audible):
    reader.values['busy'] = True
    reader.handlers = reader.make_handlers()
    reader.cancel.set()
    action = Mock()
    with pytest.raises(ValueError, match='speech operation'):
        reader.handlers.playback_control(action, audible=audible)
    action.assert_not_called()
    assert reader.cancel.is_set() and not reader.pending and not reader.progress.holds


@pytest.mark.parametrize('boundary', ['unchanged', 'source', 'library-object', 'library-path', 'parent-closing'])
def test_modal_return_invalidates_queued_controls_and_checks_scope_before_focus(reader, tmp_path, monkeypatch, boundary):
    from carr_library.ui import reader_playback
    action = Mock()
    def modal(parent, player, *, execute, stop, pause, subscribe, prepare_listening):
        assert prepare_listening is reader.values['prepare_listening']
        assert parent is reader.values['dialog'] and player is reader.player
        subscribe(Mock())
        execute(action, audible=True)
        assert reader.pending
        if boundary == 'source':
            reader.authorized = False
        elif boundary == 'library-object':
            reader.parent.library = SimpleNamespace(root=reader.parent.library.root)
        elif boundary == 'library-path':
            reader.parent.library.root = tmp_path / 'other-library'
        elif boundary == 'parent-closing':
            reader.parent._closing = True
        subscribe(None)
    monkeypatch.setattr(reader_playback, 'show_reader_playback', modal)
    reader.handlers.show_playback()
    if boundary == 'unchanged':
        reader.values['editor'].SetFocus.assert_called_once_with()
    else:
        reader.values['editor'].SetFocus.assert_not_called()
    ready(reader)
    action.assert_not_called()
    assert all(hold.closed for hold in reader.progress.holds)


def test_replaced_same_path_library_stops_playback_without_reading_old_source(reader):
    root = reader.parent.library.root
    previous = root.with_name('retained-old-library')
    root.rename(previous)
    root.mkdir()
    reader.handlers.poll_audio()
    assert reader.player.stops == 1
    assert reader.player.starts == 0
    assert reader.checks == 0
    assert reader.cancel.is_set()
    reader.timer.Stop.assert_called_once()
    assert previous.is_dir() and root.is_dir()


def test_replaced_same_path_library_rejects_already_admitted_repeat(reader):
    reader.handlers.poll_audio()
    assert len(reader.pending) == 1
    root = reader.parent.library.root
    previous = root.with_name('retained-before-repeat')
    root.rename(previous)
    root.mkdir()
    ready(reader)
    assert reader.player.starts == 0
    assert reader.player.stops == 1
    assert reader.checks == 0
    assert not reader.pending
    assert all(hold.closed for hold in reader.progress.holds)
