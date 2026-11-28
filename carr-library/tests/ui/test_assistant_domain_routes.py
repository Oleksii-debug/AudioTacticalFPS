"""New typed-domain routes retain the existing worker and native dialog ownership."""
import importlib.util
import json
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

_spec = importlib.util.spec_from_file_location(
    'assistant_domains_voice_fixture', Path(__file__).with_name('test_assistant_voice_preview.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
scenario = _fixture.scenario


def test_render_alias_uses_the_same_actual_worker_voice_review(scenario):
    state = scenario
    state.instruction.value = json.dumps({'steps': [
        {'tool': 'audio.render', 'arguments': {'text': 'Literal reviewed reading copy.'}},
        {'tool': 'audio.produce', 'arguments': {'text': 'Explicit later identity.',
                                              'voice_ids': ['explicit:preserved']}},
    ]})
    state.callbacks.preview()
    assert state.entered.wait(2)
    assert len(state.inventory_threads) == 1 and state.inventory_threads[0] != state.main_thread
    assert not state.chooser_threads and not state.buttons['run'].enabled
    assert state.service.jobs() == ()
    _fixture._finished(state)
    plan = state.callbacks.snapshot()['plan']
    assert plan.steps[0].tool == 'audio.render'
    assert plan.steps[0].arguments['voice_ids'] == ['fixture:voice']
    assert plan.steps[0].arguments['text'] == 'Literal reviewed reading copy.'
    assert plan.steps[1].arguments['voice_ids'] == ['explicit:preserved']
    assert state.chooser_threads == [state.main_thread] and state.buttons['run'].enabled
    assert state.service.jobs() == ()


def test_close_withdraws_current_reader_bridge_before_waiting_for_owned_voice_cleanup(scenario):
    state = scenario
    withdrawn = Event()
    bridge = Mock()
    bridge.close.side_effect = withdrawn.set
    state.dialog.assistant_player_bridge = bridge
    state.callbacks.preview()
    assert state.entered.wait(2)
    state.callbacks.close()
    bridge.close.assert_called_once_with()
    assert withdrawn.is_set() and state.closed
    assert not state.provider_closed.is_set()
    state.release.set()
    state.executor.shutdown(wait=True, cancel_futures=False)
    state.pump()
    assert state.provider_closed.is_set()
    assert all(thread != state.main_thread for thread in state.close_threads)
    assert state.chooser_threads == []
    state.callbacks.close()
    bridge.close.assert_called_once_with()
