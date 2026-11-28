"""Actual independent playback transforms, source-time mapping and cleanup."""
from array import array
from contextlib import contextmanager
import math
import os
from pathlib import Path
import sys
from threading import Event
import wave

import pytest

from carr_library.audio import playback_processing as module
from carr_library.audio.recording_process import CaptureBudget
from carr_library.audio.cleanup import AudioCleanupPending
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.operations import ResourceBroker


@pytest.fixture
def ffmpeg():
    value = resolve_ffmpeg()
    if value is None:
        pytest.fail('Independent playback acceptance requires the prepared local FFmpeg runtime.')
    return value


def tone(path, *, frames=16000, rate=8000, channels=2):
    values = array('h', (round(4000 * math.sin(2 * math.pi * (311 + 176 * channel) * i / rate))
                        for i in range(frames) for channel in range(channels)))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, 2, rate, frames, 'NONE', 'PCM'))
        stream.writeframes(values.tobytes())
    return path


def channel(path, which=0):
    info = wav_info(path)
    values = array('h')
    with Path(path).open('rb') as stream:
        stream.seek(info.data_offset)
        values.frombytes(stream.read(info.data_size))
    if sys.byteorder != 'little':
        values.byteswap()
    return values[which::info.channels]


def frequency(values, rate=8000):
    # The fixture is a sustained sine. Exclude boundary/analysis windows; count
    # only observed positive crossings, independently of FFmpeg metadata.
    values = values[len(values) // 4:len(values) * 3 // 4]
    crossings = [i for i in range(1, len(values)) if values[i - 1] <= 0 < values[i]]
    assert len(crossings) > 20
    return (len(crossings) - 1) * rate / (crossings[-1] - crossings[0])


def rms(values):
    return math.sqrt(sum(value * value for value in values) / len(values))


@contextmanager
def prepared(tmp_path, ffmpeg, controls, *, frames=16000, channels=2, **options):
    source = tone(tmp_path / 'original.wav', frames=frames, channels=channels)
    original = source.read_bytes()
    root = tmp_path / 'library'
    root.mkdir(exist_ok=True)
    scratch = tmp_path / 'scratch'
    scratch.mkdir(exist_ok=True)
    value = module.prepare_playback(source, controls, library_root=root,
        ffmpeg=ffmpeg, temporary_parent=scratch, budget=CaptureBudget(
            threads=1, max_output_bytes=16 * 1024**2, reserve_disk_bytes=0), **options)
    try:
        yield value
    finally:
        try:
            value.release()
        finally:
            value.close()
        assert source.read_bytes() == original
        assert not list(scratch.iterdir())


@pytest.mark.parametrize('speed,pitch,expected', [
    (1, 0, 311), (2, 0, 311), (.5, 0, 311), (1, 12, 622), (1, -12, 155.5),
    (1.5, 7, 311 * 2 ** (7 / 12)),
], ids=['original', 'double-speed', 'half-speed', 'octave-up', 'octave-down', 'independent-combined'])
def test_real_pitch_and_speed_are_independent_and_keep_declared_pcm_duration(
        tmp_path, ffmpeg, speed, pitch, expected):
    with prepared(tmp_path, ffmpeg, module.PlaybackControls(speed=speed, pitch_semitones=pitch)) as value:
        info = wav_info(value.audio)
        assert info.frames == round(value.source_frames / speed) and info.rate == 8000 and info.channels == 2
        assert abs(frequency(channel(value.audio)) - expected) < 4
        right = frequency(channel(value.audio, 1))
        assert abs(right - 487 * 2 ** (pitch / 12)) < 4
        assert value.ready() == value.audio
        assert value.source_to_audio(value.source_duration) == value.audio_duration
        assert value.audio_to_source(value.audio_duration) == value.source_duration
        assert value.source_time_accuracy == ('sample_frames' if (speed, pitch) == (1, 0)
                                              else 'estimated_after_time_stretch')


@pytest.mark.parametrize('volume,pan', [(1, 0), (.25, 0), (0, 0), (1, -1), (1, 1)])
def test_real_volume_mute_and_balance_keep_stereo_source_timing(tmp_path, ffmpeg, volume, pan):
    with prepared(tmp_path, ffmpeg, module.PlaybackControls(volume=volume, pan=pan)) as value:
        before = [channel(value.source, n) for n in (0, 1)]
        after = [channel(value.audio, n) for n in (0, 1)]
        gains = (volume * math.cos(max(0, pan) * math.pi / 2),
                 volume * math.cos(max(0, -pan) * math.pi / 2))
        assert value.audio_frames == value.source_frames
        for original, actual, gain in zip(before, after, gains):
            assert len(original) == len(actual)
            assert abs(rms(actual) - rms(original) * gain) < 2
        if volume == 0:
            assert not any(after[0]) and not any(after[1])


@pytest.mark.parametrize('frames', [1, 17, 79])
def test_short_recordings_stay_valid_bounded_pcm_with_independent_controls(tmp_path, ffmpeg, frames):
    with prepared(tmp_path, ffmpeg, module.PlaybackControls(speed=2, pitch_semitones=3),
                  frames=frames, channels=1) as value:
        assert wav_info(value.audio).frames == max(1, round(frames / 2))
        assert value.audio_frames > 0


@pytest.mark.parametrize('values', [
    {'speed': True}, {'speed': 0}, {'speed': 4.01}, {'speed': float('nan')},
    {'pitch_semitones': 13}, {'volume': 1.01}, {'volume': -1}, {'pan': -1.1},
], ids=['bool-speed', 'zero-speed', 'fast-speed', 'nan-speed', 'pitch', 'loud', 'negative-volume', 'pan'])
def test_control_bounds_do_not_become_arbitrary_filters(values):
    with pytest.raises(ValueError):
        module.PlaybackControls(**values)


def test_source_position_mapping_is_inclusive_monotonic_and_rejects_outside_ranges(tmp_path, ffmpeg):
    with prepared(tmp_path, ffmpeg, module.PlaybackControls(speed=1.7)) as value:
        assert value.source_to_audio(0) == value.audio_to_source(0) == 0
        sequence = [value.source_to_audio(value.source_duration * i / 100) for i in range(101)]
        assert sequence == sorted(sequence)
        for source in [0, .1, .7, 1.5, value.source_duration]:
            actual = value.audio_to_source(value.source_to_audio(source))
            assert abs(actual - source) <= 3 / value.sample_rate
        for bad in (True, -1, float('inf'), value.source_duration + .01):
            with pytest.raises(ValueError):
                value.source_to_audio(bad)
        for bad in (True, -1, float('nan'), value.audio_duration + .01):
            with pytest.raises(ValueError):
                value.audio_to_source(bad)


def test_pinned_derivative_cannot_be_discarded_before_player_release(tmp_path, ffmpeg):
    with prepared(tmp_path, ffmpeg, module.PlaybackControls()) as value:
        with pytest.raises(RuntimeError, match='Stop'):
            value.close()
        assert value.audio.is_file()
        value.release()
        with pytest.raises(ValueError, match='changed'):
            value.ready()
        value.pin()
        assert value.ready().is_file()


def test_delayed_play_checks_pinned_pcm_before_a_native_action(tmp_path, ffmpeg):
    source = tone(tmp_path / 'source.wav')
    root = tmp_path / 'library'
    root.mkdir()
    value = module.prepare_playback(source, module.PlaybackControls(speed=1.5),
        library_root=root, ffmpeg=ffmpeg, budget=CaptureBudget(threads=1, reserve_disk_bytes=0))
    offset = wav_info(value.audio).data_offset + 20
    try:
        if os.name == 'nt':
            with pytest.raises(PermissionError):
                with value.audio.open('r+b') as stream:
                    stream.seek(offset)
                    stream.write(b'\x01\x00')
            assert value.ready() == value.audio
            value.release()
        else:
            with value.audio.open('r+b') as stream:
                stream.seek(offset)
                stream.write(b'\x01\x00')
            with pytest.raises(ValueError, match='changed'):
                value.ready()
            with pytest.raises(RuntimeError, match='changed'):
                value.release()
            assert value.invalid
    finally:
        value.release()
        value.close()


def test_final_original_refusal_finishes_before_a_prepared_result_can_escape(tmp_path, ffmpeg, monkeypatch):
    source = tone(tmp_path / 'source.wav')
    root, scratch = tmp_path / 'library', tmp_path / 'scratch'
    root.mkdir(); scratch.mkdir()
    original, entered = module.verified_reader, []
    @contextmanager
    def verify(path, **kwargs):
        if Path(path) == source:
            entered.append(path)
        refuse = Path(path) == source and len(entered) == 2
        with original(path, **kwargs) as stream:
            yield stream
        if refuse:
            raise RuntimeError('Injected final source refusal')
    monkeypatch.setattr(module, 'verified_reader', verify)
    with pytest.raises(RuntimeError, match='final source refusal'):
        module.prepare_playback(source, module.PlaybackControls(), library_root=root,
            ffmpeg=ffmpeg, temporary_parent=scratch, budget=CaptureBudget(threads=1, reserve_disk_bytes=0))
    assert not list(scratch.iterdir())


def test_cancel_after_actual_native_processing_retains_no_private_derivative(tmp_path, ffmpeg, monkeypatch):
    source = tone(tmp_path / 'source.wav')
    before = source.read_bytes()
    root, scratch = tmp_path / 'library', tmp_path / 'scratch'
    root.mkdir(); scratch.mkdir()
    native, stopped, observed = module.run_process, Event(), []
    def run(*args, **kwargs):
        result = native(*args, **kwargs)
        observed.append(True)
        stopped.set()
        return result
    monkeypatch.setattr(module, 'run_process', run)
    with pytest.raises(InterruptedError, match='cancelled'):
        module.prepare_playback(source, module.PlaybackControls(), library_root=root,
            ffmpeg=ffmpeg, cancelled=stopped.is_set, temporary_parent=scratch,
            budget=CaptureBudget(threads=1, reserve_disk_bytes=0))
    assert observed == [True] and source.read_bytes() == before and not list(scratch.iterdir())


def test_pending_owned_cleanup_keeps_inputs_stage_and_admission_until_quiescent(tmp_path, ffmpeg, monkeypatch):
    source = tone(tmp_path / 'source.wav')
    root, scratch = tmp_path / 'library', tmp_path / 'scratch'
    root.mkdir(); scratch.mkdir()
    broker = ResourceBroker(tmp_path / 'coordinator')
    reserve, held, cleanup = broker.reserve, [False], []
    @contextmanager
    def admitted(*args, **kwargs):
        with reserve(*args, **kwargs) as lease:
            held[0] = True
            try:
                yield lease
            finally:
                held[0] = False
    monkeypatch.setattr(broker, 'reserve', admitted)
    class Owner:
        quiescent_event = Event()
        def close(self):
            assert held[0] and len(list(scratch.iterdir())) == 1
            cleanup.append(True)
            self.quiescent_event.set()
    owner = Owner()
    def pending(*args, **kwargs):
        raise AudioCleanupPending('Injected native cleanup wait', cleanup_owner=owner)
    monkeypatch.setattr(module, 'run_process', pending)
    with pytest.raises(AudioCleanupPending):
        module.prepare_playback(source, module.PlaybackControls(), library_root=root,
            ffmpeg=ffmpeg, broker=broker, temporary_parent=scratch,
            budget=CaptureBudget(threads=1, reserve_disk_bytes=0))
    assert cleanup == [True] and not held[0] and not list(scratch.iterdir())


def test_byte_allowance_refuses_before_native_processing(tmp_path, monkeypatch):
    source = tone(tmp_path / 'source.wav')
    root = tmp_path / 'library'; root.mkdir()
    monkeypatch.setattr(module, 'resolve_ffmpeg', lambda *a, **k: pytest.fail('Budget refusal must precede runtime.'))
    with pytest.raises(ValueError, match='byte allowance'):
        module.prepare_playback(source, module.PlaybackControls(speed=.25), library_root=root,
                                budget=CaptureBudget(max_output_bytes=4096))
