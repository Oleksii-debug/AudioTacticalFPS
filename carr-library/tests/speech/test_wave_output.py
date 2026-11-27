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
    # Model the MCI protocol independently of this host's temporary-root length.
    # Unmodified filename routing is exercised in test_wave_output.py.
    monkeypatch.setattr(module, '_mci_filename_supported', lambda path: True)
    mci = Mci()
    value._mci = mci
    value._mci_open = lambda path: mci(f'open "{path}" type waveaudio alias {value._alias}')
    value.play(pcm)
    yield value, mci
    driver.fail = None
    mci.fail_close = False
    value.close()


def test_live_transition_closes_old_mci_before_open_and_preserves_controls(owned_player, driver):
    player, mci = owned_player
    mci.position = 250
    player.set_ab_loop(.1, .8)
    player.enable_repeat(True)
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


# Real PCM and the existing pointer-owning driver exercise filename admission.
# The native diagnostic separately measured the exact P3 174-unit failure.
@pytest.fixture
def filename_pair(pcm, driver):
    from pathlib import Path
    import tempfile
    raw = pcm.read_bytes()
    with tempfile.TemporaryDirectory(prefix='carr-path-') as temporary:
        root = Path(temporary)
        short = root / 's.wav'
        leaf = sha256(raw).hexdigest() + '.audio'
        base = root / 'r' / leaf
        padding = 174 - len(str(base).encode('utf-16-le')) // 2
        assert 0 <= padding <= 180
        long = root / ('r' + 'x' * padding) / leaf
        long.parent.mkdir()
        short.write_bytes(raw)
        long.write_bytes(raw)
        assert len(str(short).encode('utf-16-le')) // 2 < 128
        assert len(str(long).encode('utf-16-le')) // 2 == 174
        yield short, long, raw


@pytest.mark.parametrize('name, supported', [
    ('x' * 127, True),
    ('x' * 128, False),
    ('x' * 125 + '\U0001f682', True),
    ('x' * 126 + '\U0001f682', False),
    ('x' * 121 + '.audio', True),
    ('x' * 122 + '.audio', False),
], ids=('last-ascii-unit', 'first-long-ascii', 'last-surrogate-pair',
        'first-long-surrogate-pair', 'last-audio-extension', 'long-audio-extension'))
def test_mci_filename_budget_counts_utf16_and_terminator(name, supported):
    assert module._mci_filename_supported(name) is supported


def filename_player(monkeypatch, *, selected=True, clock=lambda: 100.0):
    from carr_library.speech.output_devices import OutputDevice
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    player = module.WavePlayer(clock=clock)
    player._mci = lambda *args: pytest.fail('Long filenames must not enter the MCI command path.')
    player._mci_open = lambda *args: pytest.fail('Long filenames must choose waveOut before MCI Open.')
    device = OutputDevice(3, 'Reviewed test output', 'exact-test-endpoint')
    checked = []
    def verify(value):
        assert value == device
        checked.append(value)
        return value
    monkeypatch.setattr(native, 'verify_output_device', verify)
    if selected:
        player.set_output_device(device)
    return player, device, checked


def test_long_pcm_uses_same_output_without_requesting_live_volume_and_keeps_transport_controls(
        filename_pair, monkeypatch, driver):
    # complete() acknowledges buffers, including Reset's unplayed returns.
    # This case separately models actual played TIME_SAMPLES and the WinMM
    # Open/Reset counter origin; neither operation changes the player policy.
    native_open, native_reset = driver.waveOutOpen, driver.waveOutReset
    def opened(result, *args):
        code = native_open(result, *args)
        if code == 0 and result is not None:
            driver.position = 0
        return code
    def reset(handle):
        code = native_reset(handle)
        if code == 0:
            driver.position = 0
        return code
    monkeypatch.setattr(driver, 'waveOutOpen', opened)
    monkeypatch.setattr(driver, 'waveOutReset', reset)
    short, long, original = filename_pair
    clock = [100.0]
    player, device, checked = filename_player(monkeypatch, clock=lambda: clock[0])
    driver.support, driver.volume = 0, 0x34561234
    try:
        player.play(long)
        assert player.backend == 'Windows waveOut' and not player._open
        assert player._wave.device == device and checked
        assert player._wave.path == long and player._wave.levels is None
        assert player._live_controls is False
        assert 'volume' not in driver.calls and 'get-volume' not in driver.calls
        assert driver.volume == 0x34561234
        driver.position = 800
        assert player.position_seconds == .1
        assert player.pause() and player.state == 'paused'
        assert player.resume() and player.state == 'playing'
        driver.position = 0
        player.seek(.25)
        assert player.position_seconds == .25
        player.set_ab_loop(.125, .875)
        player.enable_repeat(True)
        player.set_sleep_timer(10)
        for _ in range(20):
            assert driver.unit == 2 and driver.queued
            align = driver.last_format.align
            lengths = [driver.prepared[identity].length for identity in driver.queued]
            assert align > 0 and all(length > 0 and length % align == 0 for length in lengths)
            driver.position += sum(lengths) // align
            driver.complete()
            if player.state == 'stopped':
                break
        assert player.state == 'stopped' and player.position_seconds == .875
        before = driver.calls.count('open')
        assert player.poll(restart=False) == 'repeat_due'
        assert driver.calls.count('open') == before
        assert player.restart_repeat() and player.repeat_count == 1
        assert player.position_seconds == .125 and player.sleep_remaining_seconds == 10
        before = driver.calls.count('open')
        clock[0] = 111
        assert player.poll() == 'sleep'
        assert player.state == 'paused' and player._wave is None
        assert player.sleep_remaining_seconds is None and not player.repeat_enabled
        assert driver.calls.count('open') == before
        assert player.resume() and player._wave is not None
        player.stop()
        assert player.state == 'stopped' and player._wave is None
        assert not driver.prepared and not driver.queued
        assert 'volume' not in driver.calls and driver.volume == 0x34561234
    finally:
        driver.fail = None
        player.close()
    assert short.read_bytes() == long.read_bytes() == original


def test_long_pcm_explicit_levels_keep_mute_balance_and_later_short_route(
        filename_pair, monkeypatch, driver):
    short, long, original = filename_pair
    player, _, _ = filename_player(monkeypatch)
    try:
        player.play(long)
        before = driver.calls.count('open')
        player.set_levels(40, balance=.5)
        assert player._live_controls and driver.calls.count('open') == before
        assert driver.volume == round(.2 * 65535) | (round(.4 * 65535) << 16)
        player.set_levels(40, muted=True, balance=.5)
        assert driver.volume == 0 and player.levels.volume == 40
        assert player.levels.muted and player.levels.balance == .5
        player.play(short)
        assert player.backend == 'Windows waveOut' and not player._open
        assert driver.volume == 0 and player._wave.levels == player.levels
        driver.position = 800
        player.pause()
        before = driver.calls.count('open')
        player.seek(.5, start_if_stopped=False)
        assert player.state == 'paused' and player.position_seconds == .5
        assert player._wave is None and driver.calls.count('open') == before
        driver.position = 0
        assert player.resume() and player.position_seconds == .5
        assert player._wave.levels == native.PlaybackLevels(40, True, .5)
    finally:
        player.close()
    assert short.read_bytes() == long.read_bytes() == original


def test_long_pcm_default_choice_remains_mapper_without_inventory_substitution(
        filename_pair, monkeypatch, driver):
    _, long, _ = filename_pair
    player, _, checked = filename_player(monkeypatch, selected=False)
    indices = []
    original_open = driver.waveOutOpen
    def opened(result, index, *args):
        indices.append(index)
        return original_open(result, index, *args)
    monkeypatch.setattr(driver, 'waveOutOpen', opened)
    driver.support = 0
    try:
        player.play(long)
        assert indices == [0xFFFFFFFF, 0xFFFFFFFF]
        assert player.output_device is None and player._wave.device is None
        assert checked == [] and 'volume' not in driver.calls
    finally:
        player.close()


def test_long_pcm_stale_explicit_output_fails_before_native_launch_without_fallback(
        filename_pair, monkeypatch, driver):
    _, long, original = filename_pair
    player, device, _ = filename_player(monkeypatch)
    def replaced(value):
        assert value == device
        raise RuntimeError('The reviewed output was replaced.')
    monkeypatch.setattr(native, 'verify_output_device', replaced)
    try:
        with pytest.raises(RuntimeError, match='replaced'):
            player.play(long)
        assert driver.calls == []
        assert player._wave is None and not player._open and player.output_device == device
    finally:
        player.close()
    assert long.read_bytes() == original


def test_long_pcm_rechecks_exact_source_stamp_before_native_open(
        filename_pair, monkeypatch, driver):
    _, long, original = filename_pair
    player, _, _ = filename_player(monkeypatch)
    original_open = native.WaveOutput.open
    def changed(output, path, **kwargs):
        assert kwargs['expected'] == module.WavePlayer._stamp(path)
        with path.open('ab') as stream:
            stream.write(b'changed')
        return original_open(output, path, **kwargs)
    monkeypatch.setattr(native.WaveOutput, 'open', changed)
    try:
        with pytest.raises(ValueError, match='changed'):
            player.play(long)
        assert driver.calls == [] and player._wave is None and not player._open
        assert long.read_bytes() == original + b'changed'
    finally:
        player.close()


def test_long_pcm_native_open_error_is_not_retried_through_another_transport(
        filename_pair, monkeypatch, driver):
    _, long, original = filename_pair
    player, _, _ = filename_player(monkeypatch)
    driver.fail = 'open'
    try:
        with pytest.raises(RuntimeError, match='WinMM'):
            player.play(long)
        assert driver.calls.count('open') == 1 and 'restart' not in driver.calls
        assert player._wave is None and not player._open and not driver.prepared
    finally:
        driver.fail = None
        player.close()
    assert long.read_bytes() == original


@pytest.mark.parametrize('operation', ['reset', 'close'])
def test_long_pcm_uncertain_stop_retains_owned_source_buffers_until_exact_retry(
        filename_pair, monkeypatch, driver, operation):
    _, long, original = filename_pair
    player, _, _ = filename_player(monkeypatch)
    try:
        player.play(long)
        player.set_ab_loop(.1, .8)
        player.enable_repeat(True)
        player.set_sleep_timer(10)
        output = player._wave
        buffers = tuple(output.pending)
        driver.fail = operation
        with pytest.raises(native.PlaybackCleanupError):
            player.stop()
        assert player._wave is output and output in native._UNRELEASED_OUTPUTS
        assert output.handle.value and not output.stream.closed
        if operation == 'reset':
            assert tuple(output.pending) == buffers
        assert not player.repeat_enabled and player.sleep_remaining_seconds is None
        driver.fail = None
        player.close()
        assert player._wave is None and output not in native._UNRELEASED_OUTPUTS
        assert not output.handle.value and not driver.prepared and not driver.queued
    finally:
        driver.fail = None
        player.close()
    assert long.read_bytes() == original


@pytest.mark.parametrize('denied', [False, True], ids=('short-open', 'short-denied'))
def test_short_filename_keeps_mci_and_native_denial_never_starts_waveout(
        filename_pair, monkeypatch, driver, denied):
    short, _, original = filename_pair
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    player, mci = module.WavePlayer(), Mci()
    player._mci = mci
    opened = []
    def open_mci(path):
        opened.append(path)
        if denied:
            raise RuntimeError('Windows audio device rejected the operation (MCI 304).')
        return mci(f'open "{path}" type waveaudio alias {player._alias}')
    player._mci_open = open_mci
    try:
        if denied:
            with pytest.raises(RuntimeError, match='MCI 304'):
                player.play(short)
            assert not player._open and player._wave is None
        else:
            player.play(short)
            assert player._open and player._wave is None and player.backend == 'Windows MCI'
        assert opened == [short] and driver.calls == []
    finally:
        player.close()
    assert short.read_bytes() == original
