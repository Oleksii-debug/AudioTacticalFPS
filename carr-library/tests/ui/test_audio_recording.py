"""Actual Studio recording entrypoint; no native/device qualification doubles."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.audio.player import WavePlayer
from carr_library.ui import audio_recording
from carr_library.ui.audio_studio import AudioStudioDialog, StudioController


def studio(tmp_path):
    project = tmp_path / 'selected-project'
    project.mkdir()
    controller = StudioController(private_root=tmp_path / 'library')
    source = 'Дослівний оригінал [pause 3]\r\nDo not interpret this direction.'
    controller.new(source)
    controller.configure(output_dir=project)
    value = AudioStudioDialog.__new__(AudioStudioDialog)
    value.controller, value.busy, value.closed = controller, False, False
    value._script_dirty, value.voice_rows = False, []
    value.voice = SimpleNamespace(GetSelection=lambda: -1)
    value.title = SimpleNamespace(GetValue=lambda: controller.title)
    value.language = SimpleNamespace(GetValue=lambda: controller.language)
    value.input_kind = SimpleNamespace(GetSelection=lambda: 0)
    value.output = SimpleNamespace(GetValue=lambda: str(project))
    value.script = SimpleNamespace(GetValue=lambda: controller.script.replace('\r\n', '\n'))
    value.differences = SimpleNamespace(ChangeValue=Mock())
    value.playback = WavePlayer()
    value._stop_for_project_change = Mock(return_value=True)
    value._refresh_playback_choices, value._message, value._error = Mock(), Mock(), Mock()
    return value, source


def saved_pcm(project):
    path = project / 'assets' / ('a' * 32 + '.wav')
    path.parent.mkdir()
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b'\x01\x00' * 2400)
    return path


def test_explicit_verified_selection_uses_current_studio_player_without_editing_source(tmp_path, monkeypatch):
    dialog, original = studio(tmp_path)
    selected = saved_pcm(dialog.controller.output_dir)
    before = selected.read_bytes()
    observed = []
    def choose(actual):
        observed.append(actual)
        assert actual is dialog
        assert actual.controller.output_dir == selected.parent.parent
        assert actual.controller.input_kind == 'narration'
        return {'id': 'a' * 32, 'path': str(selected)}
    monkeypatch.setattr(audio_recording, 'show_recording', choose)
    try:
        dialog.record_microphone()
        assert observed == [dialog]
        assert dialog.playback.playlist == (selected.resolve(),)
        assert dialog.playback.path is None  # Explicit Add never starts playback.
        assert dialog.controller.script == dialog.controller.draft.source_text == original
        assert dialog.controller.input_kind == 'narration' and selected.read_bytes() == before
        dialog._stop_for_project_change.assert_called_once()
        dialog._refresh_playback_choices.assert_called_once()
        dialog._error.assert_not_called()
    finally:
        dialog.playback.close()


@pytest.mark.parametrize('boundary', ['busy', 'unconfirmed-playback'])
def test_action_never_opens_capture_after_failed_existing_admission(tmp_path, monkeypatch, boundary):
    dialog, original = studio(tmp_path)
    opened = Mock(side_effect=AssertionError('Capture dialog must not open.'))
    monkeypatch.setattr(audio_recording, 'show_recording', opened)
    if boundary == 'busy':
        dialog.busy = True
    else:
        dialog._stop_for_project_change.return_value = False
    try:
        dialog.record_microphone()
        opened.assert_not_called()
        assert dialog.controller.script == original
        assert dialog.playback.playlist == ()
        if boundary == 'busy':
            dialog._error.assert_called_once()
    finally:
        dialog.playback.close()


@pytest.mark.parametrize('outcome', ['close', 'refused'])
def test_closed_or_refused_recording_selection_preserves_existing_playlist(tmp_path, monkeypatch, outcome):
    dialog, original = studio(tmp_path)
    selected = saved_pcm(dialog.controller.output_dir)
    dialog.playback.enqueue([selected])
    def choose(actual):
        if outcome == 'refused':
            raise ValueError('The selected recording no longer matches its saved digest.')
        return None
    monkeypatch.setattr(audio_recording, 'show_recording', choose)
    try:
        dialog.record_microphone()
        assert dialog.playback.playlist == (selected.resolve(),)
        assert dialog.controller.script == original
        dialog._refresh_playback_choices.assert_not_called()
        if outcome == 'refused':
            dialog._error.assert_called_once()
        else:
            dialog._error.assert_not_called()
    finally:
        dialog.playback.close()
