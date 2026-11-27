"""Keyboard-control state and per-request settings without simulated playback."""
from hashlib import sha256
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.audio.engines import Voice
from carr_library.audio.jobs import file_identity, regular_file
from carr_library.reader.live_playback import PlaybackRuntime, PlaybackSettings
from carr_library.reader.live_speech import SpeechSettings
from carr_library.ui.live_reading import LiveReadingDialog


class Control:
    def __init__(self, value=''):
        self.value, self.enabled = value, True

    def GetValue(self):
        return self.value

    def ChangeValue(self, value):
        self.value = value

    def Enable(self, enabled):
        self.enabled = bool(enabled)


@pytest.fixture
def window(tmp_path):
    value = object.__new__(LiveReadingDialog)
    value.playback_tempo, value.playback_pitch = Control('1'), Control('0')
    value.playback_reset = Control()
    value.playback_runtime = None
    value.future = value.session = value.operation = None
    value.close_pending = False
    value.trust_controller = None
    value.guard = lambda: None
    value.cancelled = Event()
    value.through_book = False
    value.source = 'Exact source.'
    value.text = SimpleNamespace(
        GetValue=lambda: value.source, GetRange=lambda first, last: value.source[first:last],
        GetLastPosition=lambda: len(value.source), GetSelection=lambda: (0, 0),
        GetInsertionPoint=lambda: 0)
    value.mode = SimpleNamespace(GetSelection=lambda: 0)
    value.modes = ['cursor']
    value._settings = lambda: SpeechSettings('fixture:voice', 'en')
    errors, notices, prepared = [], [], []
    value._error, value._say = errors.append, notices.append
    value._new_session = lambda *args: prepared.append(value.operation)
    binary = tmp_path / 'processor-identity'
    binary.write_bytes(b'Synthetic local processor identity.')
    runtime = PlaybackRuntime(binary, file_identity(regular_file(binary)),
                              sha256(binary.read_bytes()).hexdigest(), 'synthetic identity')
    yield value, runtime, errors, notices, prepared
    if value.operation is not None:
        value.operation.close()


@pytest.mark.parametrize('available,idle,enabled', [(False, True, False), (True, False, False),
                                                   (True, True, True)])
def test_playback_controls_require_verified_runtime_and_idle_request(window, available, idle, enabled):
    value, runtime, _, _, _ = window
    value.playback_runtime = runtime if available else None
    value._playback_controls(idle)
    assert value.playback_tempo.enabled is enabled
    assert value.playback_pitch.enabled is enabled
    assert value.playback_reset.enabled is idle


def test_unavailable_runtime_refuses_changed_playback_until_explicit_reset(window):
    value, _, errors, notices, prepared = window
    value.playback_tempo.ChangeValue('1.5')
    value.read(play=False)
    assert prepared == [] and value.operation is None
    assert 'verified local FFmpeg' in str(errors[-1])
    assert value.playback_tempo.GetValue() == '1.5'
    value.reset_playback()
    assert value._playback_settings() == PlaybackSettings()
    assert 'voice-native synthesis settings are unchanged' in notices[-1]
    value.read(play=False)
    assert len(prepared) == 1 and prepared[0].playback == PlaybackSettings()


def test_native_voice_pitch_stays_disabled_while_independent_playback_pitch_is_available(window):
    value, runtime, _, _, _ = window
    value.playback_runtime = runtime
    value.voice = SimpleNamespace(GetSelection=lambda: 0)
    value.voice_rows = [Voice('sapi5:fixture', 'Synthetic SAPI control identity', ('en',), 'sapi5',
                             controls=('rate', 'volume'))]
    value.rate, value.pitch, value.volume = Control('0'), Control('9'), Control('.8')
    value.playback_pitch.ChangeValue('3')
    value._voice_controls()
    value._playback_controls(True)
    assert not value.pitch.enabled and value.pitch.GetValue() == '0'
    assert value.playback_pitch.enabled and value.playback_pitch.GetValue() == '3'


def test_explicit_read_snapshots_controls_before_background_work_or_later_ui_changes(window):
    value, runtime, errors, _, prepared = window
    value.playback_runtime = runtime
    value.playback_tempo.ChangeValue('1.5')
    value.playback_pitch.ChangeValue('4')
    value.read(play=False)
    assert errors == [] and len(prepared) == 1
    operation = prepared[0]
    value.playback_tempo.ChangeValue('2')
    value.playback_pitch.ChangeValue('-3')
    assert operation.playback == PlaybackSettings(1.5, 4)
    assert operation.playback_runtime is runtime


@pytest.mark.parametrize('owner', ['worker', 'session'])
def test_reset_cannot_change_owned_or_prepared_request_settings(window, owner):
    value, _, _, notices, _ = window
    value.playback_tempo.ChangeValue('2')
    if owner == 'worker':
        value.future = object()
    else:
        value.session = object()
    value.reset_playback()
    assert value.playback_tempo.GetValue() == '2' and notices == []
