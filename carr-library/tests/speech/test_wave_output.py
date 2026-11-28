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
        code = self._call('volume', handle)
        if not code:
            self.volume = value
        return code

    def waveOutGetVolume(self, handle, result):
        result._obj.value = self.volume if self.support & 8 else self.volume & 65535
        return self._call('get-volume', handle)

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


def test_levels_use_only_owned_handle_and_mute_preserves_requested_volume(pcm, driver):
    original = sha256(pcm.read_bytes()).hexdigest()
    output = native.WaveOutput()
    output.open(pcm, paused=True)
    output.set_levels(native.PlaybackLevels(40, False, .5))
    assert driver.volume == round(.2 * 65535) | (round(.4 * 65535) << 16)
    output.set_levels(native.PlaybackLevels(40, True, .5))
    assert driver.volume == 0 and output.levels.volume == 40 and output.levels.muted
    output.set_levels(native.PlaybackLevels(40, False, .5))
    assert driver.volume != 0
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


def test_mono_capability_confirms_only_defined_low_word(pcm, driver):
    driver.support = 4
    output = native.WaveOutput()
    output.open(pcm, paused=True, levels=native.PlaybackLevels(37))
    assert output.levels.volume == 37
    with pytest.raises(RuntimeError, match='balance'):
        output.set_levels(native.PlaybackLevels(37, False, .5))
    output.close()


def test_unsupported_volume_fails_before_any_pcm_write(pcm, driver):
    driver.support = 0
    output = native.WaveOutput()
    with pytest.raises(RuntimeError, match='volume'):
        output.open(pcm)
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
    player.set_ab_loop(.1, .8)
    mci.position = 250
    player.set_sleep_timer(30)
    sleep = player._sleep_deadline
    player.set_levels(45, balance=-.5)
    assert not player._open and player._wave is not None
    assert any(call.startswith('close ') for call in mci.calls)
    assert player.position_seconds == .25 and player.state == 'playing'
    assert player.ab_loop == (.1, .8) and player._sleep_deadline == sleep
    assert player._wave.last_frame == 6400 and player.backend == 'Windows waveOut'
    before = driver.calls.count('open')
    player.set_levels(45, muted=True, balance=-.5)
    assert driver.calls.count('open') == before and driver.volume == 0


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
    assert player._wave is not None and driver.volume == 0


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


def _prepared_copy(player, pcm, tmp_path):
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from carr_library.speech.listening import (
        PreparedListening, ListeningRequest, ListeningSettings, FrameMapping,
    )
    temporary = TemporaryDirectory(prefix='owned-listening-', dir=tmp_path)
    copied = Path(temporary.name) / 'listening.wav'
    copied.write_bytes(pcm.read_bytes())
    request = ListeningRequest(pcm, player._stamp(pcm), ListeningSettings(1), 0, object())
    prepared = PreparedListening(request, temporary, copied,
        sha256(pcm.read_bytes()).hexdigest(), sha256(copied.read_bytes()).hexdigest(),
        FrameMapping(8000, 8000, 8000), player._stamp(copied))
    player.path, player._identity, player.duration_seconds = pcm, request.identity, 1
    player._listening_request = request
    player.apply_listening(prepared)
    return prepared


def test_unconfirmed_native_close_retains_whole_listening_owner_after_ui_released(
        pcm, tmp_path, monkeypatch, driver):
    import gc
    import weakref
    value = module.WavePlayer()
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    value._mci = Mci()
    prepared = _prepared_copy(value, pcm, tmp_path)
    copied, original = prepared.path, pcm.read_bytes()
    value.set_levels(40)
    value.play_current()
    driver.fail = 'close'
    with pytest.raises(native.PlaybackCleanupError):
        value.close()
    owner_ref, copy_ref = weakref.ref(value), weakref.ref(prepared)
    del value, prepared
    gc.collect()
    assert owner_ref() is not None and copy_ref() is not None
    assert copied.is_file() and len(native._UNRELEASED_OUTPUTS) == 1
    assert native._UNRELEASED_OUTPUTS[0]._owner is owner_ref()
    driver.fail = None
    owner_ref().close()
    gc.collect()
    assert not copied.exists() and not native._UNRELEASED_OUTPUTS
    assert owner_ref() is None and copy_ref() is None
    assert pcm.read_bytes() == original


@pytest.mark.parametrize('boundary', ['start', 'mci-handoff'])
def test_changed_prepared_copy_cannot_be_reaccepted_at_native_open(
        pcm, tmp_path, monkeypatch, driver, boundary):
    value = module.WavePlayer()
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    value._mci = Mci()
    prepared = _prepared_copy(value, pcm, tmp_path)
    if boundary == 'mci-handoff':
        value.play_current()
    original_open = native.WaveOutput.open
    def changed(output, path, **kwargs):
        with prepared.path.open('ab') as stream:
            stream.write(b'changed between review and native open')
        return original_open(output, path, **kwargs)
    monkeypatch.setattr(native.WaveOutput, 'open', changed)
    try:
        with pytest.raises(ValueError, match='changed'):
            if boundary == 'mci-handoff':
                value.set_levels(45)
            else:
                value.set_levels(45)
                value.play_current()
        assert driver.calls == [] and value._wave is None
    finally:
        value.close()
