"""Existing reader controls capture one explicit native punctuation setting."""
from types import SimpleNamespace
import pytest

from carr_library.audio.engines import Voice
from carr_library.ui.live_reading import LiveReadingDialog


class Control:
    def __init__(self, value):
        self.value, self.enabled = value, True
    def GetValue(self):
        return self.value
    def SetValue(self, value):
        self.value = value
    ChangeValue = SetValue
    def Enable(self, value):
        self.enabled = value


def window(*, supported=True):
    value = object.__new__(LiveReadingDialog)
    controls = ('rate', 'volume', 'punctuation') if supported else ('rate', 'volume')
    value.voice_rows = [Voice('fixture:voice', 'Selected voice', ('en',), 'fixture', controls=controls)]
    value.voice = SimpleNamespace(GetSelection=lambda: 0)
    value.language = Control('en')
    value.rate, value.pitch, value.volume = Control('0'), Control('0'), Control('0.8')
    value.punctuation = Control(True)
    value.session = value.future = None
    return value


@pytest.mark.parametrize('supported', [False, True])
def test_punctuation_control_is_capability_bound_and_resets_unsupported_choice(supported):
    value = window(supported=supported)
    value._voice_controls()
    assert value.punctuation.enabled is supported
    assert value.punctuation.GetValue() is supported
    settings = value._settings()
    assert settings.punctuation is supported
    assert settings.voice == 'fixture:voice'


@pytest.mark.parametrize('busy', ['session', 'future'])
def test_punctuation_control_is_disabled_during_current_request(busy):
    value = window()
    setattr(value, busy, object())
    value._voice_controls()
    assert value.punctuation.enabled is False
    assert value.punctuation.GetValue() is True


def test_prepared_request_keeps_its_captured_punctuation_setting():
    value = window()
    first = value._settings()
    value.punctuation.SetValue(False)
    second = value._settings()
    assert first.punctuation is True and second.punctuation is False
    assert first.voice == second.voice == 'fixture:voice'


def test_ui_does_not_silently_admit_punctuation_for_an_unsupported_voice():
    value = window(supported=False)
    with pytest.raises(ValueError, match='punctuation'):
        value._settings()
