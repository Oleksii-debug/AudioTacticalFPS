"""Pure saved contracts never reopen native sessions or grant replay authority."""
from copy import deepcopy
import builtins
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.assistant.interactive_tools import (
    validate_arguments, validate_result, validate_saved_scope,
)

_spec = importlib.util.spec_from_file_location('_interactive_fixture', Path(__file__).with_name('interactive_fixture.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
step, result, SCOPE = _fixture.step, _fixture.result, _fixture.SCOPE
INTENT = {'interactive_dispatch': '2' * 32}
ACTIONS = [('reader.control', action) for action in ('select', 'prepare', 'read', 'stop')] + [
           ('player.control', action) for action in ('play', 'pause', 'resume', 'seek', 'stop')]


@pytest.mark.parametrize('tool,action', ACTIONS)
def test_supported_commands_require_exact_source_and_typed_observations(tool, action):
    command = step(tool, action)
    assert validate_arguments(command) is command
    assert validate_saved_scope(command, SCOPE, '/not-opened') is command
    value = result(command)
    assert validate_result(command, value, SCOPE, INTENT) is value


@pytest.mark.parametrize('mutation', [
    'unknown_action', 'extra_argument', 'missing_target', 'bad_session', 'missing_source_digest',
    'bool_source_length', 'outside_range', 'empty_speech', 'unselected_voice',
    'unsupported_tempo', 'nonfinite_pitch', 'unknown_settings',
])
def test_malformed_or_unbounded_commands_are_refused(mutation):
    command = step()
    value = command.arguments
    if mutation == 'unknown_action':
        value['action'] = 'run-book-instructions'
    elif mutation == 'extra_argument':
        value['execute'] = 'book content'
    elif mutation == 'missing_target':
        del value['target']
    elif mutation == 'bad_session':
        value['target']['session_id'] = 'saved-window'
    elif mutation == 'missing_source_digest':
        del value['target']['location']['asset_sha256']
    elif mutation == 'bool_source_length':
        value['target']['source_chars'] = True
    elif mutation == 'outside_range':
        value['end'] = 2_000_001
    elif mutation == 'empty_speech':
        value['end'] = value['start']
    elif mutation == 'unselected_voice':
        value['settings']['voice'] = 'automatic'
    elif mutation == 'unsupported_tempo':
        value['settings']['playback_tempo'] = 5
    elif mutation == 'nonfinite_pitch':
        value['settings']['pitch'] = float('nan')
    else:
        value['settings']['runtime_path'] = '/untrusted'
    with pytest.raises(ValueError):
        validate_arguments(command)


@pytest.mark.parametrize('workspace', [None, '', 'another-workspace'])
def test_interactive_commands_cannot_inherit_absent_or_other_workspace(workspace):
    with pytest.raises(ValueError, match='workspace'):
        validate_saved_scope(step(), dict(SCOPE, workspace_id=workspace), '/not-opened')


@pytest.mark.parametrize('mutation', [
    'operation', 'tool', 'target', 'effect', 'artifacts', 'range', 'pcm_digest',
    'settings', 'silent_playback',
])
def test_saved_receipt_cannot_substitute_another_action_or_prepared_source(mutation):
    command = step()
    value = result(command)
    receipt, observed = value['interactive_receipt'], value['interactive_receipt']['observation']
    if mutation == 'operation':
        receipt['operation_id'] = '3' * 32
    elif mutation == 'tool':
        receipt['tool'] = 'player.control'
    elif mutation == 'target':
        receipt['target']['session_id'] = '4' * 32
    elif mutation == 'effect':
        receipt['effect'] = 'whole_book_completed'
    elif mutation == 'artifacts':
        value['artifacts'] = ['/invented.wav']
    elif mutation == 'range':
        observed['pcm']['end'] = command.arguments['end'] + 1
    elif mutation == 'pcm_digest':
        observed['pcm']['sha256'] = 'not-a-digest'
    elif mutation == 'settings':
        observed['settings']['voice'] = 'sapi5:another'
    else:
        observed['playback'] = dict(state='playing', position_frames=0, total_frames=24000,
            sample_rate=24000, position_accuracy='driver_reported')
    with pytest.raises(ValueError):
        validate_result(command, value, SCOPE, INTENT)


def test_started_playback_receipt_never_means_whole_book_completion():
    command = step(action='read')
    value = result(command)
    assert validate_result(command, value, SCOPE, INTENT)['interactive_receipt']['effect'] == 'playback_started'
    value['interactive_receipt']['effect'] = 'complete'
    with pytest.raises(ValueError):
        validate_result(command, value, SCOPE, INTENT)


def test_saved_contract_validation_opens_no_paths_or_native_target(monkeypatch):
    command, value = step(), result()
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Pure checkpoint validation attempted filesystem access.')
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(Path, 'stat', forbidden)
    assert validate_saved_scope(command, SCOPE, '/missing/restored/library') is command
    assert validate_result(command, value, SCOPE, INTENT) is value


@pytest.mark.parametrize('intent', [{}, {'interactive_dispatch': 'bad'},
    {'interactive_dispatch': '3' * 32}, {'interactive_dispatch': '2' * 32, 'replay': True},
    {'interactive_dispatch': None}])
def test_saved_interactive_result_requires_its_exact_original_dispatch(intent):
    with pytest.raises(ValueError):
        validate_result(step(), result(), SCOPE, intent)


@pytest.mark.parametrize('frames', [0, 24000])
def test_saved_seek_receipt_requires_the_same_measured_position_bound(frames):
    command = step('player.control', 'seek')
    value = result(command)
    value['interactive_receipt']['observation']['playback']['position_frames'] = frames
    with pytest.raises(ValueError, match='sought position'):
        validate_result(command, value, SCOPE, INTENT)
