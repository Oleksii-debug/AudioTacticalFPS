"""Real PCM plus a pointer-owning WinMM test driver; hardware runs separately."""
import ctypes as C
from hashlib import sha256
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech import _wave_output as native
from carr_library.speech import player as module


@pytest.fixture
def pcm(tmp_path):
    path = tmp_path / 'Особистий канал.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((2, 2, 8000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(b'\x01\x00\x02\x00' * 8000)
    return path


class Driver:
    """Prepared headers stay strongly owned until the native Unprepare ACK."""
    def __init__(self):
        self.calls, self.prepared, self.queued = [], {}, set()
        self.volume, self.support = 0xFFFFFFFF, 12
        self.written = []
        self.position, self.unit = 0, 2
        self.fail, self.last_format = None, None

    def _call(self, name, handle=None):
        if handle is not None:
            assert handle.value == 0x10000  # Never a global device ordinal.
        self.calls.append(name)
        return 1 if self.fail == name else 0

    def waveOutOpen(self, result, index, fmt, callback, instance, flags):
        self.last_format = fmt._obj
        if result is not None:
            result._obj.value = 0x10000
        return self._call('open' if result is not None else 'query')

    def waveOutGetID(self, handle, result):
        result._obj.value = 3
        return self._call('id', handle)

    def waveOutGetDevCapsW(self, handle, result, size):
        assert handle == 0x10000
        result._obj.support = self.support
        return self._call('caps')

    def waveOutPause(self, handle): return self._call('pause', handle)
    def waveOutRestart(self, handle): return self._call('restart', handle)

    def waveOutSetVolume(self, handle, value):
        raise AssertionError('Playback must not change a device, stream or session volume.')

    def waveOutGetVolume(self, handle, result):
        raise AssertionError('Playback must not use shared native volume state.')

    def waveOutGetPosition(self, handle, result, size):
        result._obj.kind, result._obj.data.value = self.unit, self.position
        return self._call('position', handle)

    def waveOutPrepareHeader(self, handle, header, size):
        token = header._obj
        code = self._call('prepare', handle)
        if not code:
            self.prepared[id(token)] = token
        return code

    def waveOutWrite(self, handle, header, size):
        token = header._obj
        assert self.prepared[id(token)] is token
        code = self._call('write', handle)
        if not code:
            self.queued.add(id(token))
            self.written.append(C.string_at(token.data, token.length))
        return code

    def waveOutUnprepareHeader(self, handle, header, size):
        token = header._obj
        code = self._call('unprepare', handle)
        if not code:
            assert id(token) not in self.queued or token.flags & 1
            self.queued.discard(id(token))
            self.prepared.pop(id(token), None)
        return code

    def complete(self):
        for identity in tuple(self.queued):
            token = self.prepared[identity]
            token.flags |= 1
        self.queued.clear()

    def waveOutReset(self, handle):
        code = self._call('reset', handle)
        if not code:
            self.complete()
            self.position = 0
        return code

    def waveOutClose(self, handle):
        assert not self.prepared and not self.queued
        return self._call('close', handle)


@pytest.fixture
def driver(monkeypatch):
    value = Driver()
    monkeypatch.setattr(native, '_library', lambda: value)
    assert native._UNRELEASED_OUTPUTS == []
    yield value
    value.fail = None
    for output in tuple(native._UNRELEASED_OUTPUTS):
        output.close()
    assert native._UNRELEASED_OUTPUTS == []


@pytest.mark.parametrize('volume,muted,balance', [
    (-1, False, 0), (101, False, 0), (True, False, 0), (50, 1, 0),
    (50, False, 2), (50, False, True), (float('nan'), False, 0),
    (50, False, float('inf')), (10 ** 400, False, 0),
])
def test_invalid_levels_are_rejected_without_native_calls(volume, muted, balance, driver):
    with pytest.raises(ValueError):
        native.PlaybackLevels(volume, muted, balance)
    assert driver.calls == []


def queued_pcm(output):
    return b''.join(C.string_at(token.header.data, token.header.length) for token in output.pending)


def test_levels_use_only_owned_pcm_and_mute_preserves_requested_volume(pcm, driver):
    original = sha256(pcm.read_bytes()).hexdigest()
    output = native.WaveOutput()
    output.open(pcm, paused=True)
    before = queued_pcm(output)
    output.set_levels(native.PlaybackLevels(40, False, .5))
    changed = queued_pcm(output)
    assert before == b'\x01\x00\x02\x00' * 1600
    assert changed == b'\x00\x00\x01\x00' * 1600
    output.set_levels(native.PlaybackLevels(40, True, .5))
    assert queued_pcm(output) == b'\x00' * len(before)
    assert output.levels.volume == 40 and output.levels.muted
    output.set_levels(native.PlaybackLevels(40, False, .5))
    assert queued_pcm(output) == changed
    assert driver.volume == 0xFFFFFFFF and 'restart' not in driver.calls
    output.close()
    assert sha256(pcm.read_bytes()).hexdigest() == original


def test_native_pause_precedes_first_pcm_and_reviewed_bounds_use_sample_grid(pcm, driver):
    output = native.WaveOutput()
    output.open(pcm, start_seconds=.125, end_seconds=.375, paused=True)
    assert driver.calls.index('pause') < driver.calls.index('write')
    assert 'restart' not in driver.calls and output.first_frame == 1000 and output.last_frame == 3000
    assert len(output.pending) == 4
    assert sum(token.header.length for token in output.pending) == 4 * 400 * 4
    output.resume()
    assert driver.calls.count('restart') == 1
    driver.complete()
    output.pump()
    assert len(output.pending) == 1 and output.next_frame == 3000
    driver.complete()
    output.pump()
    assert output.ended and not output.pending
    output.close()
    before = list(driver.calls)
    output.close()
    assert driver.calls == before and not driver.prepared


@pytest.mark.parametrize('unit,position,expected', [(2, 800, .225), (4, 3200, .225), (1, 100, .225)])
def test_native_position_inspects_returned_unit(pcm, driver, unit, position, expected):
    output = native.WaveOutput()
    output.open(pcm, start_seconds=.125, paused=True)
    driver.unit, driver.position = unit, position
    assert output.position_seconds == pytest.approx(expected)
    output.close()


def test_unknown_position_unit_is_not_reported_as_a_real_time(pcm, driver):
    output = native.WaveOutput()
    output.open(pcm, paused=True)
    driver.unit = 8
    with pytest.raises(RuntimeError, match='unsupported'):
        _ = output.position_seconds
    output.close()


@pytest.mark.parametrize('operation', ['reset', 'unprepare', 'close'])
def test_uncertain_close_retains_memory_and_blocks_new_output_until_ack(pcm, driver, operation):
    output = native.WaveOutput()
    output.open(pcm, paused=True)
    buffers = tuple(output.pending)
    driver.fail = operation
    with pytest.raises(native.PlaybackCleanupError):
        output.close()
    assert output.handle.value and output in native._UNRELEASED_OUTPUTS
    assert not output.stream.closed
    if operation != 'close':
        assert tuple(output.pending) == buffers
    other = native.WaveOutput()
    before = list(driver.calls)
    with pytest.raises(native.PlaybackCleanupError):
        other.open(pcm)
    assert driver.calls == before and other.stream is None
    driver.fail = None
    output.close()
    assert not output.handle.value and not output.pending and output not in native._UNRELEASED_OUTPUTS


def test_stereo_balance_does_not_require_device_volume_capability(pcm, driver):
    driver.support = 0
    output = native.WaveOutput()
    output.open(pcm, paused=True, levels=native.PlaybackLevels(100, False, 1))
    assert queued_pcm(output) == b'\x00\x00\x02\x00' * 1600
    assert output.levels.balance == 1 and driver.volume == 0xFFFFFFFF
    output.close()


def test_mono_balance_is_refused_before_any_pcm_write(pcm, driver):
    with wave.open(str(pcm), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(b'\x01\x00' * 8000)
    output = native.WaveOutput()
    with pytest.raises(RuntimeError, match='Stereo balance'):
        output.open(pcm, levels=native.PlaybackLevels(37, False, .5))
    assert 'write' not in driver.calls and 'restart' not in driver.calls
    assert not output.handle.value and output.stream is None


def test_changed_expected_file_is_refused_before_native_open(pcm, driver):
    expected = module.WavePlayer._stamp(pcm)
    with pcm.open('ab') as stream:
        stream.write(b'x')
    with pytest.raises(ValueError, match='changed'):
        native.WaveOutput().open(pcm, expected=expected)
    assert driver.calls == []


def test_mutation_during_queued_playback_is_detected_before_more_pcm(pcm, driver):
    output = native.WaveOutput()
    output.open(pcm, paused=True)
    driver.complete()
    before = driver.calls.count('write')
    with pcm.open('ab') as stream:
        stream.write(b'x')
    with pytest.raises(ValueError, match='changed'):
        output.pump()
    assert driver.calls.count('write') == before
    output.close()


class Mci:
    def __init__(self):
        self.calls, self.mode, self.position, self.fail_close = [], 'stopped', 0, False

    def __call__(self, command):
        self.calls.append(command)
        action = command.split()[0]
        if action == 'status':
            return self.mode if command.endswith('mode') else str(self.position)
        if action == 'play': self.mode = 'playing'
        if action == 'pause': self.mode = 'paused'
        if action == 'resume': self.mode = 'playing'
        if action in ('stop', 'close'): self.mode = 'stopped'
        if action == 'close' and self.fail_close:
            raise RuntimeError('MCI close unconfirmed')
        return ''


@pytest.fixture
def owned_player(pcm, monkeypatch, driver):
    value = module.WavePlayer()
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    mci = Mci()
    value._mci = mci
    value.play(pcm)
    yield value, mci
    driver.fail = None
    mci.fail_close = False
    value.close()


def test_live_transition_closes_old_mci_before_open_and_preserves_controls(owned_player, driver):
    player, mci = owned_player
    mci.position = 250
    from carr_library.ui.playback_navigation import PlaybackNavigation
    navigation = PlaybackNavigation(player, generation=lambda: 1)
    navigation.bind()
    navigation.set_range(.1, .8)
    navigation.enable_repeat(True)
    navigation.set_sleep(30)
    sleep = navigation.sleep_deadline
    mci.position = 250
    player.set_levels(45, balance=-.5)
    assert not player._open and player._wave is not None
    assert any(call.startswith('close ') for call in mci.calls)
    assert player.position_seconds == .25 and player.state == 'playing'
    assert navigation.repeat and (navigation.a, navigation.b) == (.1, .8)
    assert navigation.sleep_deadline == sleep
    assert player._wave.last_frame == 8000 and player.backend == 'Windows waveOut'
    before = driver.calls.count('open')
    player.set_levels(45, muted=True, balance=-.5)
    assert driver.calls.count('open') == before and player._wave.levels.muted
    assert driver.volume == 0xFFFFFFFF
    navigation.close()


def test_mci_close_failure_cannot_open_a_second_output(owned_player, driver):
    player, mci = owned_player
    mci.fail_close = True
    with pytest.raises(RuntimeError, match='unconfirmed'):
        player.set_levels(50)
    assert player._open and player._wave is None and driver.calls == []


def test_paused_transition_and_seek_never_restart_before_explicit_resume(owned_player, driver):
    player, mci = owned_player
    mci.position = 200
    player.pause()
    player.set_levels(50)
    assert player.state == 'paused' and 'restart' not in driver.calls
    player.seek(.5, start_if_stopped=False)
    assert player._wave is None and player.position_seconds == .5 and player.state == 'paused'
    assert 'restart' not in driver.calls
    player.resume()
    assert player.state == 'playing' and player.position_seconds == .5
    assert driver.calls.count('restart') == 1


def test_stopped_level_request_is_passive(owned_player, driver):
    player, _ = owned_player
    player.stop()
    before = list(driver.calls)
    player.set_levels(0, muted=True, balance=1)
    assert driver.calls == before and player.state == 'stopped'
    assert player.levels.muted and player.levels.volume == 0
    player.play_current()
    assert player._wave is not None and queued_pcm(player._wave) == b'\x00' * 6400
    assert driver.volume == 0xFFFFFFFF


def test_unconfirmed_wave_close_keeps_private_listening_owner(owned_player, driver):
    player, _ = owned_player
    player.set_levels(50)
    released = []
    player._listening = SimpleNamespace(release=lambda owner: released.append(owner))
    driver.fail = 'close'
    with pytest.raises(native.PlaybackCleanupError):
        player.close()
    assert player._wave is not None and player._listening is not None and released == []
    driver.fail = None
    player.close()
    assert released == [player] and player._listening is None


def test_close_retry_cannot_queue_more_pcm_from_a_status_observer(owned_player, driver):
    player, _ = owned_player
    player.set_levels(50)
    driver.fail = 'close'
    with pytest.raises(native.PlaybackCleanupError):
        player.stop()
    assert player._wave is not None and not player._wave.pending
    before = driver.calls.count('write')
    with pytest.raises(native.PlaybackCleanupError):
        _ = player.state
    assert driver.calls.count('write') == before
    driver.fail = None
    player.close()


@pytest.mark.parametrize('width,values,expected', [
    (1, [0, 255, 128, 129], [64, 192, 128, 129]),
    (2, [-32768, 32767, 0, 1], [-16384, 16384, 0, 1]),
    (3, [-8388608, 8388607, 0, 1], [-4194304, 4194304, 0, 1]),
    (4, [-2147483648, 2147483647, 0, 1], [-1073741824, 1073741824, 0, 1]),
])
def test_actual_pcm_extremes_scale_without_overflow_and_preserve_input(width, values, expected):
    encode = lambda rows: b''.join(value.to_bytes(width, 'little', signed=width != 1) for value in rows)
    original = encode(values)
    assert native._pcm_levels(original, width=width, channels=2,
        levels=native.PlaybackLevels()) is original
    scaled = native._pcm_levels(original, width=width, channels=2,
        levels=native.PlaybackLevels(50))
    assert scaled == encode(expected) and original == encode(values)
    muted = native._pcm_levels(original, width=width, channels=2,
        levels=native.PlaybackLevels(50, True))
    assert muted == (b'\x80' if width == 1 else b'\x00') * len(original)
    left_only = native._pcm_levels(original, width=width, channels=2,
        levels=native.PlaybackLevels(100, False, -1))
    silence = 128 if width == 1 else 0
    assert left_only == encode([values[0], silence, values[2], silence])


@pytest.mark.parametrize('data,width,channels', [
    (b'x' * (native._MAX_BLOCK_BYTES + 4), 2, 2),
    (b'\x00', 2, 2), (b'\x00' * 4, 5, 2), (b'\x00' * 4, 2, 3),
    (bytearray(4), 2, 2), (b'\x00' * 4, True, 2),
], ids=['over-budget', 'unaligned', 'unsupported-width', 'unsupported-channels',
        'mutable-input', 'boolean-width'])
def test_pcm_processing_rejects_unbounded_or_misaligned_input(data, width, channels):
    with pytest.raises(ValueError, match='bounded'):
        native._pcm_levels(data, width=width, channels=channels, levels=native.PlaybackLevels(50))


def test_active_level_change_keeps_driver_owned_buffers_immutable_and_bounds_queue(pcm, driver):
    output = native.WaveOutput()
    output.open(pcm)
    old_tokens = tuple(output.pending)
    old_bytes = queued_pcm(output)
    assert len(old_tokens) == 4 and len(old_bytes) / (8000 * 4) == pytest.approx(.2)
    before = list(driver.calls)
    output.set_levels(native.PlaybackLevels(40, True))
    assert tuple(output.pending) == old_tokens and queued_pcm(output) == old_bytes
    assert driver.calls == before  # No reset, restart or shared native volume call.
    driver.complete()
    output.pump()
    assert queued_pcm(output) == b'\x00' * 6400
    assert output.levels.volume == 40 and output.levels.muted
    output.close()


def test_paused_level_change_requeues_from_observed_frame_without_resume(pcm, driver):
    output = native.WaveOutput()
    output.open(pcm, paused=True)
    driver.position = 200
    output.set_levels(native.PlaybackLevels(100, True))
    assert output.first_frame == 200 and output.next_frame == 1800
    assert output.position_seconds == pytest.approx(.025)
    assert queued_pcm(output) == b'\x00' * 6400
    assert driver.calls.index('reset') < len(driver.calls) - 1
    assert 'restart' not in driver.calls and output.paused
    output.resume()
    assert driver.calls.count('restart') == 1
    output.close()


@pytest.mark.parametrize('phase', ['reset', 'unprepare', 'pause'])
def test_failed_paused_requeue_retains_owner_until_native_close(pcm, driver, phase):
    output = native.WaveOutput()
    output.open(pcm, paused=True)
    driver.fail = phase
    with pytest.raises(RuntimeError):
        output.set_levels(native.PlaybackLevels(20, True))
    assert output.handle.value and output.stream is not None and output.stopping
    before = driver.calls.count('write')
    with pytest.raises(native.PlaybackCleanupError):
        output.pump()
    assert driver.calls.count('write') == before
    driver.fail = None
    output.close()
    assert not driver.prepared and not output.handle.value


def test_two_owned_players_have_separate_pcm_levels_and_no_native_volume_state(pcm, monkeypatch):
    first_driver, second_driver = Driver(), Driver()
    selected = iter((first_driver, second_driver))
    monkeypatch.setattr(native, '_library', lambda: next(selected))
    first, second = native.WaveOutput(), native.WaveOutput()
    try:
        first.open(pcm, paused=True)
        second.open(pcm, paused=True)
        original_second = queued_pcm(second)
        second_calls = list(second_driver.calls)
        first.set_levels(native.PlaybackLevels(25, True))
        assert queued_pcm(first) == b'\x00' * 6400
        assert queued_pcm(second) == original_second
        assert second_driver.calls == second_calls and second.levels == native.PlaybackLevels()
        assert first_driver.volume == second_driver.volume == 0xFFFFFFFF
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize('rate,block_frames', [(1, 1), (7, 1), (19, 1), (20, 1), (44101, 2205)])
def test_actual_wav_queue_uses_exact_sample_grid_even_at_low_rates(pcm, driver, rate, block_frames):
    with wave.open(str(pcm), 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'uncompressed'))
        stream.writeframes(b'\x01\x00' * (block_frames * 6))
    original = sha256(pcm.read_bytes()).hexdigest()
    output = native.WaveOutput()
    try:
        output.open(pcm, paused=True)
        assert len(output.pending) == 4 and output.block_frames == block_frames
        assert [token.header.length for token in output.pending] == [block_frames * 2] * 4
        assert output.next_frame == block_frames * 4
        if rate >= 20:
            assert output.next_frame / rate <= .2
        else:
            assert output.next_frame == 4
    finally:
        output.close()
    assert sha256(pcm.read_bytes()).hexdigest() == original
