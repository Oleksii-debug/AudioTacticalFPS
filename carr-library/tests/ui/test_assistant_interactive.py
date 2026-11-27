"""Real thread dispatch and owner lifetimes with explicit synthetic native controls."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
from pathlib import Path
import threading
import time

import pytest

from carr_library.reader.reading_state import ReadingLocation

_spec = importlib.util.spec_from_file_location('_native_interactive_fixture',
    Path(__file__).parents[1] / 'assistant/interactive_fixture.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
Harness, step, SCOPE = _fixture.Harness, _fixture.step, _fixture.SCOPE


def test_preview_marshals_to_ui_thread_without_performing_the_action(tmp_path):
    env = Harness(tmp_path)
    command = step(action='select')
    assert env.run(lambda: env.bridge.validate_step(command, SCOPE)) is command
    assert env.reader.actions == []


def test_exact_unicode_source_selection_is_applied_through_existing_native_control(tmp_path):
    env = Harness(tmp_path)
    command = step(action='select')
    command.arguments.update(start=7, end=8)
    receipt = env.execute(command)['interactive_receipt']
    assert env.reader.text.range == (7, 8)
    assert receipt['observation']['selection'] == [7, 8]
    assert env.reader.source == _fixture.SOURCE


@pytest.mark.parametrize('changed', ['window', 'source', 'library', 'workspace'])
def test_previewed_native_target_cannot_rebind_after_owner_or_source_changes(tmp_path, changed):
    env = Harness(tmp_path)
    command = step(action='select')
    env.run(lambda: env.bridge.validate_step(command, SCOPE))
    if changed == 'window':
        env.reader._assistant_session_id = '9' * 32
    elif changed == 'source':
        env.reader.source += ' changed'
    elif changed == 'library':
        env.frame.library = type(env.frame.library)(root=env.root)
    else:
        env.frame.active = ('other', None)
    with pytest.raises((ValueError, PermissionError)):
        env.execute(command)
    assert env.reader.actions == []


def test_settings_are_snapshotted_and_changed_voice_refuses_before_dispatch(tmp_path):
    env = Harness(tmp_path)
    command = step()
    env.run(lambda: env.bridge.validate_step(command, SCOPE))
    env.reader.settings['voice'] = 'sapi5:other'
    with pytest.raises(ValueError, match='settings'):
        env.execute(command)
    assert env.reader.actions == []


def test_prepare_waits_for_existing_owner_pcm_without_starting_playback(tmp_path):
    env = Harness(tmp_path)
    value = env.execute(step())['interactive_receipt']
    assert value['effect'] == 'utterance_prepared' and value['observation']['pcm'] == _fixture.PCM
    assert value['observation']['playback'] is None
    assert ('read', False) in env.reader.actions and not env.reader.playing
    assert env.reader.source == _fixture.SOURCE


def test_async_prepare_does_not_report_success_before_real_owner_settlement(tmp_path):
    env = Harness(tmp_path)
    env.reader.delay = True
    seen = []
    def poll():
        if env.reader.future is not None:
            seen.append('pending')
            env.reader.prepare()
    value = env.execute(step(), poll=poll)
    assert seen and value['interactive_receipt']['observation']['pcm'] == _fixture.PCM


def test_native_render_error_has_no_success_receipt_and_settles_owned_work(tmp_path):
    env = Harness(tmp_path)
    env.reader.failure = 'Denied by current audio admission'
    with pytest.raises(RuntimeError, match='audio admission'):
        env.execute(step())
    assert ('stop',) in env.reader.actions and env.reader.session is None


def test_cancel_before_ui_dispatch_discards_queued_native_action(tmp_path):
    env = Harness(tmp_path)
    cancelled = threading.Event()
    def poll():
        cancelled.set()
    with pytest.raises(InterruptedError):
        env.execute(step(action='select'), cancelled=cancelled.is_set, poll=poll)
    while not env.events.empty():
        env.events.get_nowait()()
    assert env.reader.actions == []


def test_cancel_during_preparation_waits_for_same_native_owner_and_publishes_no_result(tmp_path):
    env = Harness(tmp_path)
    env.reader.delay = True
    cancelled = threading.Event()
    def poll():
        if env.reader.future is not None:
            cancelled.set()
    with pytest.raises(InterruptedError):
        env.execute(step(), cancelled=cancelled.is_set, poll=poll)
    assert ('stop',) in env.reader.actions and env.reader.future is env.reader.session is None


def test_duplicate_operation_id_never_replays_an_already_dispatched_selection(tmp_path):
    env = Harness(tmp_path)
    command = step(action='select')
    env.execute(command)
    before = list(env.reader.actions)
    with pytest.raises(PermissionError, match='already attempted'):
        env.execute(command)
    assert env.reader.actions == before


@pytest.mark.parametrize('action', ['play', 'pause', 'resume', 'seek', 'stop'])
def test_player_controls_reuse_only_the_exact_selected_prepared_pcm(tmp_path, action):
    env = Harness(tmp_path)
    env.reader.prepare()
    if action in ('pause', 'resume', 'seek'):
        env.reader.play()
    if action == 'resume':
        env.reader.pause()
    value = env.execute(step('player.control', action))['interactive_receipt']
    assert value['effect'] == 'playback_' + {'play': 'started', 'pause': 'paused',
        'resume': 'resumed', 'seek': 'seeked', 'stop': 'stopped'}[action]
    assert value['observation']['pcm'] == (None if action == 'stop' else _fixture.PCM)


def test_replaced_prepared_pcm_is_refused_before_player_action(tmp_path):
    env = Harness(tmp_path)
    env.reader.prepare()
    command = step('player.control', 'play')
    env.run(lambda: env.bridge.validate_step(command, SCOPE))
    env.reader.session.current.sha256 = 'c' * 64
    with pytest.raises(ValueError, match='utterance changed'):
        env.execute(command)
    assert ('play',) not in env.reader.actions


def test_unavailable_native_player_never_uses_a_path_discovered_fallback(tmp_path):
    env = Harness(tmp_path)
    env.reader.prepare()
    env.reader.player.native = None
    with pytest.raises(ValueError, match='native Windows'):
        env.execute(step('player.control', 'play'))
    assert ('play',) not in env.reader.actions


def test_closed_assistant_bridge_cannot_dispatch_a_late_native_command(tmp_path):
    env = Harness(tmp_path)
    env.bridge.close()
    with pytest.raises((ValueError, InterruptedError)):
        env.execute(step(action='select'))
    assert env.reader.actions == []


def test_execution_on_ui_thread_is_refused_without_blocking_native_events(tmp_path):
    env = Harness(tmp_path)
    with pytest.raises(RuntimeError, match='worker'):
        env.bridge.execute_step(step(action='select'), SCOPE, operation_id='2' * 32,
                                cancelled=lambda: False, progress=lambda *_: None)
    assert env.reader.actions == []
