"""Real PCM input and existing player sequencing with an explicit MCI fixture."""
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import output_devices as devices
from carr_library.audio.output_devices import OutputDevice
from carr_library.speech import player as module


@pytest.fixture
def recording(tmp_path):
    target = tmp_path / 'Українська 🎧.wav'
    with wave.open(str(target), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\x01\0' * 1600)
    return target


@pytest.mark.parametrize('change', ['none', 'renumber-before', 'replace-before', 'replace-after-set', 'renumber-after-set', 'wrong-ack'])
def test_existing_player_binds_explicit_endpoint_before_any_sound(recording, monkeypatch, change):
    selected = OutputDevice(1, 'chosen-endpoint', 'Speakers')
    inventory = [selected]
    if change == 'renumber-before':
        inventory[:] = [replace(selected, index=3)]
    elif change == 'replace-before':
        inventory[:] = [replace(selected, endpoint_id='same-name-other-device')]
    monkeypatch.setattr(devices, 'output_devices', lambda: tuple(inventory))
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    player = module.WavePlayer(output_device=selected)
    commands = []
    assigned = [None]
    def mci(command):
        commands.append(command)
        if command.startswith('set ') and ' output ' in command:
            assigned[0] = int(command.rsplit(' ', 1)[1])
            if change == 'replace-after-set':
                inventory[:] = [replace(selected, endpoint_id='new-device')]
            elif change == 'renumber-after-set':
                inventory[:] = [replace(selected, index=7)]
        if command.startswith('status ') and command.endswith(' output'):
            return str(0 if change == 'wrong-ack' else assigned[0])
        return ''
    player._mci = mci
    original = sha256(recording.read_bytes()).hexdigest()
    try:
        if change in ('none', 'renumber-before'):
            player.play(recording)
            assert assigned[0] == (3 if change == 'renumber-before' else 1)
            assert commands[-1].startswith('play ')
            assert any(item.endswith(f'output {assigned[0]}') for item in commands)
            with pytest.raises(ValueError, match='Close the current'):
                player.select_output(None)
        else:
            with pytest.raises((ValueError, RuntimeError)):
                player.play(recording)
            assert not any(item.startswith('play ') for item in commands)
            assert not player._open
        assert player.output_device == selected
        assert sha256(recording.read_bytes()).hexdigest() == original
    finally:
        player.close()


def test_unconfirmed_native_close_keeps_the_existing_alias_for_retry(recording, monkeypatch):
    selected = OutputDevice(0, 'chosen', 'Speakers')
    monkeypatch.setattr(devices, 'output_devices', lambda: (selected,))
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    player = module.WavePlayer(output_device=selected)
    allow_close = False
    commands = []
    def mci(command):
        commands.append(command)
        if command.startswith('status '):
            return '1'  # Deliberately reject the requested output index.
        if command.startswith('close ') and not allow_close:
            raise RuntimeError('fixture native close pending')
        return ''
    player._mci = mci
    with pytest.raises(RuntimeError, match='close pending'):
        player.play(recording)
    assert player._open and recording.exists()
    assert not any(item.startswith('play ') for item in commands)
    allow_close = True
    assert player.close() is True and not player._open


def test_system_default_keeps_existing_transport_without_device_discovery(recording, monkeypatch):
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    def forbidden():
        raise AssertionError('Default output did not request an explicit inventory.')
    monkeypatch.setattr(devices, 'output_devices', forbidden)
    player, commands = module.WavePlayer(), []
    player._mci = lambda command: commands.append(command) or ''
    try:
        player.play(recording)
        assert commands[-1].startswith('play ')
        assert not any(' output ' in command or command.endswith(' output') for command in commands)
    finally:
        player.close()
