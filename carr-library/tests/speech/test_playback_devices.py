"""Real C ABI memory layouts and owned player routing with explicit API fixtures.

These portable tests do not enumerate or qualify physical Windows sound cards.
"""
import ctypes
from dataclasses import replace
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech import playback_devices as devices, player as player_module


class Function:
    def __init__(self, callback): self.callback = callback
    def __call__(self, *args): return self.callback(*args)


class WinMM:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.failure = None
        self.count_override = None
        self.size_override = None
        self.waveOutGetNumDevs = Function(self.count)
        self.waveOutGetDevCapsW = Function(self.caps)
        self.waveOutMessage = Function(self.message)

    def count(self):
        self.calls.append(('count',))
        return len(self.rows) if self.count_override is None else self.count_override

    def caps(self, index, pointer, length):
        self.calls.append(('caps', index, length))
        if self.failure == 'caps': return 2
        value = ctypes.cast(pointer, ctypes.POINTER(devices._Caps)).contents
        row = self.rows[index]
        value.manufacturer, value.product, value.driver, value.channels = row.manufacturer, row.product, row.driver, row.channels
        raw = (row.name + '\0').encode('utf-16-le')
        ctypes.memmove(ctypes.addressof(value) + devices._Caps.name.offset, raw, len(raw))
        return 0

    def message(self, handle, command, first, second):
        index = handle.value or 0
        self.calls.append(('message', index, command))
        raw = (self.rows[index].interface + '\0').encode('utf-16-le')
        if command == devices.DRV_QUERYFUNCTIONINSTANCEIDSIZE:
            if self.failure == 'size': return 8
            ctypes.cast(first, ctypes.POINTER(ctypes.c_size_t)).contents.value = len(raw) if self.size_override is None else self.size_override
        elif command == devices.DRV_QUERYFUNCTIONINSTANCEID:
            if self.failure == 'interface': return 8
            assert second >= len(raw)
            ctypes.memmove(first, raw, len(raw))
        else:
            raise AssertionError('Only documented endpoint ID messages are admitted')
        return 0


def device(index=0, interface='opaque-interface-a', name='Навушники 😀'):
    return devices.PlaybackDevice(index, name, interface, 1, 2, 0x01020003, 2)


def test_actual_abi_layout_utf16_identity_and_documented_queries(monkeypatch):
    assert ctypes.sizeof(devices._Caps) == 84
    assert devices._Caps.name.offset == 8 and devices._Caps.formats.offset == 72 and devices._Caps.support.offset == 80
    native = WinMM((device(), device(1, 'opaque-interface-b', 'Other headphones')))
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    assert devices.output_devices() == native.rows
    assert [row for row in native.calls if row[0] == 'caps'] == [('caps', 0, 84), ('caps', 1, 84)] * 2
    assert devices.DRV_QUERYFUNCTIONINSTANCEID == 0x811 and devices.DRV_QUERYFUNCTIONINSTANCEIDSIZE == 0x812
    assert native.waveOutMessage.argtypes == [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]


@pytest.mark.parametrize('failure', ['caps', 'size', 'interface'])
def test_failed_native_inventory_never_becomes_partial_success(monkeypatch, failure):
    native = WinMM((device(),)); native.failure = failure
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    with pytest.raises(RuntimeError): devices.output_devices()


@pytest.mark.parametrize('size', [1, 3, 65538, 2**32 - 1])
def test_interface_size_is_checked_before_allocating_or_calling_native_write(monkeypatch, size):
    native = WinMM((device(),)); native.size_override = size
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    with pytest.raises(RuntimeError, match='endpoint size'): devices.output_devices()
    assert not any(row[0] == 'message' and row[2] == 0x811 for row in native.calls)


def test_no_devices_and_devices_without_interfaces_are_truthful(monkeypatch):
    native = WinMM(())
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    assert devices.output_devices() == ()
    native.rows = (device(interface=''),); native.size_override = 0
    row, = devices.output_devices()
    assert not row.selectable
    with pytest.raises(ValueError, match='stable'): devices.resolve_output(row)


def test_excessive_count_and_duplicate_interfaces_fail_closed(monkeypatch):
    native = WinMM((device(),)); native.count_override = 129
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    with pytest.raises(RuntimeError, match='bound'): devices.output_devices()
    assert not any(row[0] == 'caps' for row in native.calls)
    native.count_override = None; native.rows = (device(), device(1))
    with pytest.raises(RuntimeError, match='ambiguous'): devices.output_devices()


def test_inventory_changed_between_two_passes_is_not_admitted(monkeypatch):
    native = WinMM((device(),)); reads = []
    actual = devices._inventory
    def changing(library, **options):
        value = actual(library, **options); reads.append(True)
        if len(reads) == 1: native.rows = (device(interface='replacement'),)
        return value
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    monkeypatch.setattr(devices, '_inventory', changing)
    with pytest.raises(RuntimeError, match='changed while'): devices.output_devices()


def test_interface_rebinds_changed_index_but_never_name_or_driver(monkeypatch):
    selected = device()
    moved = replace(selected, index=3)
    monkeypatch.setattr(devices, 'output_devices', lambda **options: (moved,))
    assert devices.resolve_output(selected) == moved
    for replacement in (replace(moved, interface='another'), replace(moved, driver=99), replace(moved, name='Renamed')):
        monkeypatch.setattr(devices, 'output_devices', lambda **options: (replacement,))
        with pytest.raises(RuntimeError, match='no fallback'): devices.resolve_output(selected)


def test_non_windows_reports_unavailable_without_loading_a_dll(monkeypatch):
    monkeypatch.setattr(devices, 'IS_WINDOWS', False)
    with pytest.raises(RuntimeError, match='available on Windows'): devices.output_devices()


@pytest.mark.parametrize('raw', [b'a', b'a\0', b'\x00\xd8\0\0'])
def test_native_utf16_requires_valid_termination(raw):
    with pytest.raises(RuntimeError): devices._text(raw, 'fixture')


@pytest.fixture
def player(tmp_path, monkeypatch):
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    class Player(player_module.WavePlayer):
        def __init__(self):
            super().__init__(); self.commands = []; self.mode = 'playing'; self.reported = '3'; self.reject = None
            self.position = 0
        def _mci(self, command):
            self.commands.append(command)
            if self.reject and self.reject in command: raise RuntimeError('Synthetic MCI rejection')
            if command.endswith(' output'): return self.reported
            if command.endswith(' mode'): return self.mode
            if command.endswith(' position'): return str(self.position)
            if command.startswith(('play ', 'resume ')): self.mode = 'playing'
            elif command.startswith('pause '): self.mode = 'paused'
            elif command.startswith(('stop ', 'close ', 'seek ')): self.mode = 'stopped'
            return ''
    selected, moved = device(), device(3)
    monkeypatch.setattr(devices, 'resolve_output', lambda value: moved)
    path = tmp_path / 'дані.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM')); stream.writeframes(b'\0\0' * 8000)
    return Player(), path, selected, moved


def test_player_selection_never_starts_audio_and_next_play_confirms_exact_route(player):
    playback, path, selected, moved = player
    playback.select_output(selected)
    assert playback.commands == [] and playback.output_device == moved
    playback.play(path)
    setting = f'set {playback._alias} output 3'
    assert setting in playback.commands
    assert playback.commands.index(setting) < next(i for i, value in enumerate(playback.commands) if value.startswith('play '))
    assert f'status {playback._alias} output' in playback.commands and playback._state == 'playing'


@pytest.mark.parametrize('state', ['playing', 'paused'])
def test_player_refuses_implicit_stop_or_route_change_during_audio(player, state):
    playback, _, selected, _ = player
    playback._state = state
    with pytest.raises(ValueError, match='Stop playback'): playback.select_output(selected)
    assert playback.commands == [] and playback.output_device is None


@pytest.mark.parametrize('kind', ['wrong_status', 'set_rejected', 'moved_during_admission'])
def test_route_failure_closes_owned_handle_and_never_plays_or_falls_back(player, monkeypatch, kind):
    playback, path, selected, moved = player
    playback.select_output(selected)
    if kind == 'wrong_status': playback.reported = '0'
    elif kind == 'set_rejected': playback.reject = ' output 3'
    else:
        seen = []
        def resolve(value):
            seen.append(value)
            return moved if len(seen) == 1 else replace(moved, index=4)
        monkeypatch.setattr(devices, 'resolve_output', resolve)
    with pytest.raises(RuntimeError): playback.play(path)
    assert not any(command.startswith('play ') for command in playback.commands)
    assert f'close {playback._alias}' in playback.commands and not playback._open
    assert playback.output_device == moved


def test_automatic_output_preserves_original_backend_command_path(player):
    playback, path, selected, _ = player
    playback.select_output(selected); playback.select_output(None); playback.play(path)
    assert playback.output_device is None
    assert not any(' output' in command for command in playback.commands)


def test_unplugged_selected_device_fails_before_opening_new_audio(player, monkeypatch):
    playback, path, selected, _ = player
    playback.select_output(selected)
    def missing(value): raise RuntimeError('Selected output unavailable; no fallback')
    monkeypatch.setattr(devices, 'resolve_output', missing)
    with pytest.raises(RuntimeError, match='no fallback'): playback.play(path)
    assert playback.commands == []


@pytest.mark.parametrize('stage', ['count', 'caps', 'size', 'interface'])
@pytest.mark.parametrize('kind', ['cancel', 'deadline'])
def test_inventory_checks_cancellation_and_budget_after_each_native_query(monkeypatch, stage, kind):
    native = WinMM((device(),))
    now, interrupted = [100.0], [False]
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    monkeypatch.setattr(devices.time, 'monotonic', lambda: now[0])
    target = {'count': native.waveOutGetNumDevs, 'caps': native.waveOutGetDevCapsW,
              'size': native.waveOutMessage, 'interface': native.waveOutMessage}[stage]
    real = target.callback
    def query(*args):
        result = real(*args)
        reached = stage in ('count', 'caps') or args[1] == (
            devices.DRV_QUERYFUNCTIONINSTANCEIDSIZE if stage == 'size' else devices.DRV_QUERYFUNCTIONINSTANCEID)
        if reached:
            interrupted[0] = True
            now[0] = 102.0
        return result
    target.callback = query
    expected = InterruptedError if kind == 'cancel' else TimeoutError
    with pytest.raises(expected):
        devices.output_devices(cancelled=lambda: kind == 'cancel' and interrupted[0])
    assert native.calls[-1][0] == ('message' if stage in ('size', 'interface') else stage)
    assert sum(call[0] == 'count' for call in native.calls) == 1


def test_already_cancelled_discovery_does_not_load_native_library(monkeypatch):
    calls = []
    monkeypatch.setattr(devices, '_winmm', lambda: calls.append(True))
    with pytest.raises(InterruptedError):
        devices.output_devices(cancelled=lambda: True)
    assert calls == []


@pytest.mark.parametrize('value', [True, 0, -1, 3, float('nan'), float('inf'), 10 ** 400])
def test_invalid_inventory_budget_is_rejected_before_any_native_access(monkeypatch, value):
    calls = []
    monkeypatch.setattr(devices, '_winmm', lambda: calls.append(True))
    with pytest.raises(ValueError): devices.output_devices(timeout_seconds=value)
    assert calls == []


@pytest.mark.parametrize('kind', ['missing', 'moved', 'status-error', 'wrong-status'])
@pytest.mark.parametrize('seek', [False, True])
def test_resume_revalidates_the_opened_route_and_closes_without_play_on_change(player, monkeypatch, kind, seek):
    playback, path, selected, moved = player
    playback.select_output(selected)
    playback.play(path)
    playback.position = 300
    assert playback.pause()
    if seek:
        playback.seek(.5)
    before = sum(command.startswith(('play ', 'resume ')) for command in playback.commands)
    if kind == 'missing':
        def missing(value): raise RuntimeError('Selected output unavailable; no fallback')
        monkeypatch.setattr(devices, 'resolve_output', missing)
    elif kind == 'moved':
        monkeypatch.setattr(devices, 'resolve_output', lambda value: replace(moved, index=4))
    elif kind == 'status-error':
        playback.reject = ' output'
    else:
        playback.reported = '0'
    with pytest.raises(RuntimeError): playback.resume()
    assert sum(command.startswith(('play ', 'resume ')) for command in playback.commands) == before
    assert playback.commands[-1] == f'close {playback._alias}'
    assert not playback._open and playback._opened_output is None and playback.state == 'stopped'
    assert playback.output_device == moved and path.is_file()


def test_matching_selected_route_resumes_with_existing_ab_sleep_and_source_guards(player):
    playback, path, selected, moved = player
    playback.select_output(selected)
    playback.play(path)
    playback.set_ab_loop(.1, .6)
    playback.set_sleep_timer(30)
    playback.position = 300
    playback.pause()
    playback.seek(.4)
    before = len(playback.commands)
    assert playback.resume()
    added = playback.commands[before:]
    assert added == [f'status {playback._alias} output', f'play {playback._alias} from 400 to 600']
    assert playback.ab_loop == (.1, .6) and playback.sleep_remaining_seconds > 0
    playback.close()
    assert not playback._open and playback.output_device == moved


def test_explicit_replay_rebinds_same_interface_after_ordinal_change(player, monkeypatch):
    playback, path, selected, moved = player
    playback.select_output(selected)
    playback.play(path)
    playback.stop()
    newer = replace(moved, index=5)
    monkeypatch.setattr(devices, 'resolve_output', lambda value: newer)
    playback.reported = '5'
    assert playback.play_current()
    assert playback._opened_output == newer and playback.output_device.identity == newer.identity
    assert f'set {playback._alias} output 5' in playback.commands


@pytest.mark.parametrize('action', ['repeat', 'play-current'])
def test_lost_route_cannot_start_an_ab_repeat_or_stopped_replay(player, monkeypatch, action):
    playback, path, selected, moved = player
    playback.select_output(selected)
    playback.play(path)
    playback.set_ab_loop(.1, .6)
    if action == 'repeat':
        playback.mode, playback.position = 'stopped', 600
        assert playback.repeat_pending
    else:
        playback.stop()
    before = sum(command.startswith(('play ', 'resume ')) for command in playback.commands)
    def missing(value): raise RuntimeError('Selected output unavailable; no fallback')
    monkeypatch.setattr(devices, 'resolve_output', missing)
    with pytest.raises(RuntimeError, match='no fallback'):
        (playback.repeat_if_ready if action == 'repeat' else playback.play_current)()
    assert sum(command.startswith(('play ', 'resume ')) for command in playback.commands) == before
    assert not playback.repeat_pending and playback.output_device == moved


def test_failed_output_change_keeps_old_identity_and_retryable_open_handle(player, monkeypatch):
    playback, path, selected, moved = player
    playback.select_output(selected)
    playback.play(path)
    playback.stop()
    other = device(7, 'other-interface')
    monkeypatch.setattr(devices, 'resolve_output', lambda value: other)
    playback.reject = 'close '
    with pytest.raises(RuntimeError): playback.select_output(other)
    assert playback.output_device == moved and playback._opened_output == moved and playback._open
    playback.reject = None
    playback.select_output(other)
    assert playback.output_device == other and playback._opened_output is None and not playback._open
    assert playback.state == 'stopped' and path.is_file()


class SharedAdapterWinMM(WinMM):
    """Two endpoint IDs can share the same older PnP adapter interface."""
    def message(self, handle, command, first, second):
        if command not in (0x80c, 0x80d):
            return super().message(handle, command, first, second)
        index = handle.value or 0
        self.calls.append(('message', index, command))
        raw = ('same-adapter-interface\0').encode('utf-16-le')
        if command == 0x80d:
            ctypes.cast(first, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(raw)
        else:
            assert second >= len(raw)
            ctypes.memmove(first, raw, len(raw))
        return 0


def test_distinct_endpoints_of_one_adapter_are_not_collapsed(monkeypatch):
    first = device(0, 'opaque-endpoint-a', 'Same device name')
    second = device(1, 'opaque-endpoint-b', first.name)
    native = SharedAdapterWinMM((first, second))
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    rows = devices.output_devices()
    assert rows == (first, second)
    assert len({row.interface for row in rows}) == 2
    assert [call[2] for call in native.calls if call[0] == 'message'] == [0x812, 0x811] * 4
    assert all(row.selectable for row in rows)


@pytest.mark.parametrize('change', ['reorder', 'add-sibling', 'remove-sibling'])
def test_endpoint_identity_survives_adapter_sibling_inventory_changes(monkeypatch, change):
    first = device(0, 'opaque-endpoint-a', 'Same device name')
    second = device(1, 'opaque-endpoint-b', first.name)
    native = SharedAdapterWinMM((first, second))
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    selected, _ = devices.output_devices()
    if change == 'reorder':
        current = replace(first, index=1)
        native.rows = (replace(second, index=0), current)
    elif change == 'add-sibling':
        current = replace(first, index=2)
        native.rows = (device(0, 'opaque-endpoint-c', first.name), second, current)
    else:
        current = first
        native.rows = (current,)
    resolved = devices.resolve_output(selected)
    assert resolved == current
    assert resolved.identity == selected.identity
    assert resolved.interface == 'opaque-endpoint-a'
    assert not any(call[0] == 'message' and call[2] in (0x80c, 0x80d) for call in native.calls)


@pytest.mark.parametrize('change', ['selected-removed', 'selected-replaced'])
def test_matching_adapter_name_caps_and_ordinal_do_not_rebind_another_endpoint(monkeypatch, change):
    first = device(0, 'opaque-endpoint-a', 'Same device name')
    second = device(1, 'opaque-endpoint-b', first.name)
    native = SharedAdapterWinMM((first, second))
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    selected, _ = devices.output_devices()
    replacement = replace(second, index=0) if change == 'selected-removed' else replace(first, interface='replacement-endpoint')
    native.rows = (replacement,)
    with pytest.raises(RuntimeError, match='no fallback'):
        devices.resolve_output(selected)
    assert selected.interface == 'opaque-endpoint-a'


def test_endpoint_size_t_write_is_bounded_before_allocating_native_buffer(monkeypatch):
    native = WinMM((device(),))
    reads = []
    def message(handle, command, first, second):
        assert command == devices.DRV_QUERYFUNCTIONINSTANCEIDSIZE
        size = ctypes.cast(first, ctypes.POINTER(ctypes.c_size_t)).contents
        reads.append(size.value)
        size.value = ctypes.c_size_t(-1).value
        return 0
    native.waveOutMessage.callback = message
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    with pytest.raises(RuntimeError, match='endpoint size'):
        devices.output_devices()
    assert reads == [0]


def test_endpoint_size_accepts_a_zero_extended_legacy_width_native_write(monkeypatch):
    native = WinMM((device(interface='opaque-endpoint-a'),))
    original = native.waveOutMessage.callback
    sizes = []
    def message(handle, command, first, second):
        if command == devices.DRV_QUERYFUNCTIONINSTANCEIDSIZE:
            assert ctypes.cast(first, ctypes.POINTER(ctypes.c_size_t)).contents.value == 0
            raw = (native.rows[0].interface + '\0').encode('utf-16-le')
            ctypes.cast(first, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(raw)
            sizes.append(ctypes.cast(first, ctypes.POINTER(ctypes.c_size_t)).contents.value)
            return 0
        return original(handle, command, first, second)
    native.waveOutMessage.callback = message
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    assert devices.output_devices() == native.rows
    assert sizes == [len(('opaque-endpoint-a\0').encode('utf-16-le'))] * 2


def test_empty_terminated_endpoint_is_visible_but_cannot_authorize_selection(monkeypatch):
    native = SharedAdapterWinMM((device(interface=''),))
    monkeypatch.setattr(devices, '_winmm', lambda: native)
    row, = devices.output_devices()
    assert row.interface == '' and not row.selectable
    with pytest.raises(ValueError, match='stable device identity'):
        devices.resolve_output(row)
