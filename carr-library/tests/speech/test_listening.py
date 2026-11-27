"""Actual FFmpeg listening copies plus the existing MCI command boundary."""
from array import array
from dataclasses import replace
from hashlib import sha256
import math
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import AudioRuntime, AudioCancelled
from carr_library.audio import dsp
from carr_library.speech import listening, player as implementation


class MCI:
    def __init__(self):
        self.commands, self.mode, self.position = [], 'stopped', 0
        self.fail_close = False
    def __call__(self, command):
        self.commands.append(command)
        if command.endswith(' mode'): return self.mode
        if command.endswith(' position'): return str(self.position)
        if command.startswith('play '): self.mode = 'playing'
        elif command.startswith('pause '): self.mode = 'paused'
        elif command.startswith('resume '): self.mode = 'playing'
        elif command.startswith('stop '): self.mode = 'stopped'
        elif command.startswith('close '):
            if self.fail_close: raise RuntimeError('Synthetic unacknowledged MCI Close')
            self.mode = 'stopped'
        elif command.startswith('seek '): self.position = int(command.split(' to ')[1].split()[0])
        return ''


@pytest.fixture
def reader(tmp_path, monkeypatch):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'This required suite needs actual FFmpeg.'
    source = tmp_path / 'оригінальний голос.wav'
    pcm = array('h', (round(6000 * math.sin(2 * math.pi * 440 * n / 24000)) for n in range(72000)))
    if __import__('sys').byteorder != 'little': pcm.byteswap()
    with wave.open(str(source), 'wb') as writer:
        writer.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        writer.writeframes(pcm.tobytes())
    directory = tmp_path / 'private-listening'
    directory.mkdir()
    player, driver = implementation.WavePlayer(), MCI()
    # Only the playback command boundary is doubled. Conversion/inspection run
    # their unchanged real child processes against genuine local PCM files.
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='nt'))
    player._mci = driver
    player.play(source)
    player.stop()
    yield SimpleNamespace(player=player, driver=driver, source=source, directory=directory,
                          original=source.read_bytes(), runtime=lambda root, **kw: AudioRuntime(root, **kw))
    driver.fail_close = False
    player.close()


def prepare(reader, speed=2, pitch=0, position=.75, **kwargs):
    request = reader.player.listening_request(speed, pitch, position_seconds=position)
    value = reader.player.prepare_listening(request, reader.directory, runtime_factory=reader.runtime, **kwargs)
    return value


def frequency(path):
    with wave.open(str(path), 'rb') as audio:
        rate = audio.getframerate()
        data = array('h')
        data.frombytes(audio.readframes(audio.getnframes()))
    if __import__('sys').byteorder != 'little': data.byteswap()
    # Ignore filter onset/flush, measuring actual retained central PCM.
    values = data[len(data) // 4:3 * len(data) // 4]
    rising = [index for index in range(1, len(values)) if values[index - 1] < 0 <= values[index]]
    assert len(rising) > 20
    return (len(rising) - 1) * rate / (rising[-1] - rising[0])


@pytest.mark.parametrize('speed,pitch', [(2, 0), (.5, 0), (1, 12), (2, -12), (1.25, 7)])
def test_actual_speed_and_pitch_are_independent_and_original_bytes_stay_unchanged(reader, speed, pitch):
    value = prepare(reader, speed, pitch)
    try:
        assert reader.source.read_bytes() == reader.original
        assert value.source_sha256 == sha256(reader.original).hexdigest()
        assert value.output_sha256 == sha256(value.path.read_bytes()).hexdigest()
        assert value.mapping.source_frames == 72000 and value.mapping.sample_rate == 24000
        assert abs(value.mapping.output_frames - round(72000 / speed)) <= 1
        assert abs(frequency(value.path) - 440 * 2 ** (pitch / 12)) < 2
        before = sum(command.startswith(('play ', 'resume ')) for command in reader.driver.commands)
        reader.player.apply_listening(value)
        assert reader.player.state == 'stopped' and not reader.player._open
        assert reader.player.path == reader.source and reader.player.duration_seconds == 3
        assert reader.player.position_seconds == .75
        assert reader.player.listening_duration_seconds == value.mapping.output_duration
        assert sum(command.startswith(('play ', 'resume ')) for command in reader.driver.commands) == before
    finally:
        if value._owner is None: value.close()


def test_default_reset_copies_exact_original_without_invoking_optional_engine(reader):
    reader.runtime = lambda *args, **kwargs: pytest.fail('Default reset invoked FFmpeg.')
    value = prepare(reader, 1, 0)
    try:
        assert value.path.read_bytes() == reader.original
        assert value.mapping.source_frames == value.mapping.output_frames == 72000
        reader.player.apply_listening(value)
        assert reader.player.listening_settings == listening.ListeningSettings()
    finally:
        if value._owner is None: value.close()


def test_original_timeline_maps_native_position_seek_repeat_and_resume(reader):
    value = prepare(reader, 2, position=1)
    reader.player.apply_listening(value)
    player, driver = reader.player, reader.driver
    player.play_current()
    assert f'open "{value.path}" type waveaudio alias {player._alias}' in driver.commands
    assert driver.commands[-1] == f'play {player._alias} from 500'
    driver.position = 600
    assert player.position_seconds == 1.2
    player.pause()
    player.seek(1.8)
    assert driver.commands[-1] == f'seek {player._alias} to 900 wait'
    player.resume()
    assert driver.commands[-1] == f'play {player._alias} from 900'
    player.set_ab_loop(.6, 2.4)
    assert driver.commands[-1].endswith(' to 1200')
    driver.mode, driver.position = 'stopped', 1200
    assert player.repeat_pending and player.repeat_if_ready()
    assert driver.commands[-1] == f'play {player._alias} from 300 to 1200'
    player.stop()
    assert not player.repeat_pending and player.path == reader.source


def test_pause_and_close_failure_keep_private_pcm_until_device_acknowledges_release(reader):
    value = prepare(reader)
    reader.player.apply_listening(value)
    reader.player.play_current()
    reader.player.pause()
    with pytest.raises(RuntimeError, match='still owns'):
        value.close()
    reader.driver.fail_close = True
    with pytest.raises(RuntimeError, match='MCI Close'):
        reader.player.close()
    assert value.path.is_file() and not value.closed and value._owner is reader.player
    assert reader.player._open and reader.player._listening is value
    reader.driver.fail_close = False
    reader.player.close()
    assert value.closed and not value.path.exists() and reader.source.read_bytes() == reader.original


def test_new_original_play_resets_listening_only_after_valid_source_admission(reader):
    value = prepare(reader)
    reader.player.apply_listening(value)
    reader.player.play_current()
    with pytest.raises(FileNotFoundError):
        reader.player.play(reader.source.with_name('missing.wav'))
    assert reader.player._listening is value and value.path.exists() and reader.player.state == 'playing'
    reader.player.play(reader.source)
    assert value.closed and reader.player.listening_settings == listening.ListeningSettings()
    assert reader.player.listening_path == reader.source


def test_stop_revokes_pending_prepare_before_any_engine_or_source_copy(reader):
    request = reader.player.listening_request(2)
    reader.player.stop()
    with pytest.raises(AudioCancelled):
        reader.player.prepare_listening(request, reader.directory,
            runtime_factory=lambda *a, **k: pytest.fail('Revoked request opened an engine.'))
    assert list(reader.directory.iterdir()) == [] and reader.source.read_bytes() == reader.original


def test_late_stop_rejects_completed_copy_without_changing_active_settings(reader):
    value = prepare(reader)
    reader.player.stop()
    try:
        with pytest.raises(ValueError, match='stale'):
            reader.player.apply_listening(value)
        assert reader.player.listening_settings == listening.ListeningSettings()
    finally:
        value.close()
    assert list(reader.directory.iterdir()) == []


@pytest.mark.parametrize('which', ['original', 'snapshot'])
def test_actual_conversion_cannot_adopt_changed_original_or_private_snapshot(reader, which):
    actual_factory = reader.runtime
    def factory(root, **options):
        runtime = actual_factory(root, **options)
        convert = runtime.convert
        def changed(source, output, **kwargs):
            result = convert(source, output, **kwargs)
            path = reader.source if which == 'original' else Path(source)
            data = bytearray(path.read_bytes())
            data[-1] ^= 1
            path.write_bytes(data)
            return result
        runtime.convert = changed
        return runtime
    reader.runtime = factory
    with pytest.raises(ValueError, match='changed'):
        prepare(reader)
    assert reader.player.listening_settings == listening.ListeningSettings()
    assert not list(reader.directory.iterdir())


def test_output_changed_after_genuine_hash_cannot_acquire_fresh_adoption_stamp(reader, monkeypatch):
    actual, changed = listening._hash, []
    def verified_then_changed(path, expected, cancelled):
        result = actual(path, expected, cancelled)
        if Path(path).name == 'listening.wav':
            data = bytearray(Path(path).read_bytes())
            data[-1] ^= 1
            Path(path).write_bytes(data)
            changed.append(True)
        return result
    monkeypatch.setattr(listening, '_hash', verified_then_changed)
    with pytest.raises(ValueError, match='changed'):
        prepare(reader)
    assert changed == [True] and not list(reader.directory.iterdir())


def test_stop_after_actual_render_removes_only_private_derivative(reader):
    actual_factory = reader.runtime
    completed = []
    def factory(root, **options):
        runtime = actual_factory(root, **options)
        convert = runtime.convert
        def finished(*args, **kwargs):
            result = convert(*args, **kwargs)
            completed.append(result.sha256)
            reader.player.stop()
            return result
        runtime.convert = finished
        return runtime
    reader.runtime = factory
    with pytest.raises(AudioCancelled):
        prepare(reader)
    assert completed and not list(reader.directory.iterdir())
    assert reader.source.read_bytes() == reader.original


def test_actual_capability_child_obeys_cancellation_before_next_preflight_launch(reader, monkeypatch):
    stopped, launches = threading.Event(), []
    actual = dsp.run_process
    def completed(command, **options):
        if options['cancelled']():
            return actual(command, **options)  # Existing admission must reject before Popen.
        result = actual(command, **options)
        launches.append(command)
        stopped.set()
        return result
    monkeypatch.setattr(dsp, 'run_process', completed)
    with pytest.raises(AudioCancelled):
        prepare(reader, cancelled=stopped.is_set)
    assert len(launches) == 1 and '-version' in launches[0]
    assert not list(reader.directory.iterdir())


@pytest.mark.parametrize('speed,pitch', [(0, 0), (5, 0), (1, 13), (True, 0), (1, float('nan')), ('2', 0)])
def test_invalid_listening_controls_do_not_stop_or_launch_a_device(reader, speed, pitch):
    before = tuple(reader.driver.commands)
    with pytest.raises(ValueError):
        reader.player.listening_request(speed, pitch)
    assert tuple(reader.driver.commands) == before and not list(reader.directory.iterdir())


def test_private_copy_budget_refuses_before_creating_stage_or_calling_engine(reader):
    request = reader.player.listening_request(.25)
    budget = replace(listening.LISTENING_BUDGET, max_output_bytes=200000)
    with pytest.raises(RuntimeError, match='bounded space'):
        listening.prepare_listening(request, reader.directory, budget=budget,
            runtime_factory=lambda *a, **k: pytest.fail('Over-budget preparation opened an engine.'))
    assert not list(reader.directory.iterdir())


def test_measured_frame_mapping_uses_actual_counts_not_nominal_multiplier():
    mapping = listening.FrameMapping(101, 25, 24000)
    assert mapping.to_output(101 / 24000) == 25 / 24000
    assert mapping.to_source(25 / 24000) == 101 / 24000
    assert mapping.to_source(100) == 101 / 24000
