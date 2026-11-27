"""Native ABI/identity models and real PCM; Windows routing is a separate probe."""
import ctypes
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech import output_devices as devices
from carr_library.speech import player as playback


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
    def __call__(self, *args):
        return self.implementation(*args)


class NativeInventory:
    def __init__(self, rows):
        self.rows, self.calls = rows, []
        self.count_override = self.size_override = self.raw_override = None
        self.failure = None
        self.after = lambda: None
        self.waveOutGetNumDevs = Function(self.count)
        self.waveOutGetDevCapsW = Function(self.caps)
        self.waveOutMessage = Function(self.message)

    def count(self):
        self.calls.append(('count',))
        return len(self.rows) if self.count_override is None else self.count_override

    def caps(self, index, pointer, size):
        self.calls.append(('caps', index, size))
        if self.failure == 'caps':
            return 2
        if index >= len(self.rows):
            return 2
        row = self.rows[index]
        value = ctypes.cast(pointer, ctypes.POINTER(devices._WaveOutputCaps)).contents
        raw = row.name.encode('utf-16-le') + b'\0\0'
        assert len(raw) <= 64
        ctypes.memmove(ctypes.addressof(value) + devices._WaveOutputCaps.name.offset, raw, len(raw))
        value.channels = 2
        self.after()
        return 0

    def message(self, index, operation, address, size):
        self.calls.append(('message', index, operation, size))
        if self.failure == operation:
            return 8
        raw = self.raw_override
        if raw is None:
            raw = self.rows[index].interface.encode('utf-16-le') + b'\0\0'
        if operation == 0x0812:
            value = len(raw) if self.size_override is None else self.size_override
            ctypes.c_size_t.from_address(address).value = value
        elif operation == 0x0811:
            assert size >= len(raw)
            ctypes.memmove(address, raw, len(raw))
        else:
            raise AssertionError(operation)
        self.after()
        return 0


def item(index=0, name='Reviewed output', identity='opaque-device-interface-A'):
    return devices.OutputDevice(index, name, identity)


@pytest.fixture
def inventory(monkeypatch):
    value = NativeInventory([item(), item(1, 'Other output', 'opaque-device-interface-B')])
    monkeypatch.setattr(devices, '_native', lambda: value)
    return value


def test_native_inventory_reads_identity_without_opening_output(inventory):
    result = devices.output_devices()
    assert result == tuple(inventory.rows)
    assert result[0].identity != result[1].identity
    assert len(result[0].identity) == 64
    assert all(call[0] in ('count', 'caps', 'message') for call in inventory.calls)



def test_two_endpoints_on_one_driver_interface_remain_independently_selectable(inventory):
    # One native adapter may publish two separately selectable endpoints.
    # A fallback to DEVICEINTERFACE would collapse them into one shared ID.
    original = inventory.waveOutMessage.implementation
    shared = 'same-driver-interface'.encode('utf-16-le') + b'\0\0'
    def with_shared_adapter(index, operation, address, size):
        if operation == 0x080D:
            ctypes.c_uint32.from_address(address).value = len(shared)
            return 0
        if operation == 0x080C:
            ctypes.memmove(address, shared, len(shared))
            return 0
        return original(index, operation, address, size)
    inventory.waveOutMessage = Function(with_shared_adapter)
    first, second = devices.output_devices()
    assert first.identity != second.identity
    assert devices.resolve_output_device(first).index == 0
    assert devices.resolve_output_device(second).index == 1
    assert devices.verify_output_device(second) == second
    assert all(call[2] in (0x0811, 0x0812)
               for call in inventory.calls if call[0] == 'message')


def test_same_identity_reordered_resolves_to_new_index_but_same_name_does_not(inventory):
    selected = inventory.rows[0]
    inventory.rows = [item(0, selected.name, 'different-interface'), item(1, 'Renamed', selected.interface)]
    resolved = devices.resolve_output_device(selected)
    assert resolved.index == 1 and resolved.interface == selected.interface
    assert resolved.name == 'Renamed'
    with pytest.raises(RuntimeError, match='moved or was replaced'):
        devices.verify_output_device(selected)
    assert devices.verify_output_device(resolved) == resolved


def test_removed_selection_never_becomes_same_named_device_or_default(inventory):
    selected = inventory.rows[0]
    inventory.rows = [item(0, selected.name, 'replacement-interface')]
    with pytest.raises(RuntimeError, match='removed'):
        devices.resolve_output_device(selected)


def test_duplicate_native_identity_is_not_ambiguous_user_selection(inventory):
    inventory.rows[1] = item(1, 'Duplicate', inventory.rows[0].interface)
    with pytest.raises(ValueError, match='ambiguous'):
        devices.output_devices()


@pytest.mark.parametrize('size', [0, 1, 3, 65538, 2**32 - 1])
def test_native_identity_size_rejects_before_interface_allocation(inventory, size):
    inventory.size_override = size
    with pytest.raises(ValueError, match='bounded'):
        devices.output_devices()
    assert not any(call[0] == 'message' and call[2] == 0x0811 for call in inventory.calls)


@pytest.mark.parametrize('raw', [b'\0\0', b'a\0', b'\x00\xd8\0\0'])
def test_empty_unterminated_or_malformed_identity_does_not_select(inventory, raw):
    inventory.raw_override = raw
    with pytest.raises(ValueError):
        devices.output_devices()


@pytest.mark.parametrize('failure', ['caps', 0x0812, 0x0811])
def test_native_query_failure_is_explicit_not_empty_success(inventory, failure):
    inventory.failure = failure
    with pytest.raises(RuntimeError, match='WinMM'):
        devices.output_devices()


def test_inventory_limit_and_changed_count_reject(inventory):
    inventory.count_override = 65
    with pytest.raises(ValueError, match='allowance'):
        devices.output_devices()
    assert inventory.calls == [('count',)]
    inventory.count_override = None
    inventory.after = lambda: setattr(inventory, 'count_override', 1)
    with pytest.raises(RuntimeError, match='changed during discovery'):
        devices.output_devices()


def test_cancel_is_polled_between_individual_native_queries(inventory):
    stopped = False
    def cancelled():
        return stopped
    def after():
        nonlocal stopped
        stopped = True
    inventory.after = after
    with pytest.raises(InterruptedError, match='cancelled'):
        devices.output_devices(cancelled=cancelled)
    assert [call[0] for call in inventory.calls] == ['count', 'caps']


def test_precancel_does_not_load_native_library(monkeypatch):
    monkeypatch.setattr(devices, '_native', lambda: pytest.fail('No native access after cancellation'))
    with pytest.raises(InterruptedError):
        devices.output_devices(cancelled=lambda: True)


@pytest.mark.parametrize('index', [True, False, -1, 64, 1.0, '1'])
def test_only_bounded_integer_indices_can_enter_an_mci_command(index):
    with pytest.raises(ValueError):
        item(index=index)


@pytest.mark.parametrize(('name', 'identity'), [
    ('', 'valid'), ('bad\nname', 'valid'), ('valid', ''),
    ('valid', 'bad\0identity'), ('valid', 'x' * 32768),
], ids=('empty-name', 'linebreak-name', 'empty-identity', 'nul-identity', 'oversized-identity'))
def test_native_selection_requires_bounded_nonempty_identity(name, identity):
    with pytest.raises(ValueError):
        item(name=name, identity=identity)


def test_windows_abi_is_fixed_and_message_pointers_are_not_truncated(monkeypatch):
    native = NativeInventory([])
    monkeypatch.setattr(devices, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(devices.ctypes, 'WinDLL', lambda *args, **kwargs: native, raising=False)
    assert devices._native() is native
    assert ctypes.sizeof(devices._WaveOutputCaps) == 84
    assert devices._WaveOutputCaps.name.offset == 8
    assert devices._WaveOutputCaps.formats.offset == 72
    assert native.waveOutGetDevCapsW.argtypes[0] is ctypes.c_size_t
    assert native.waveOutMessage.argtypes == (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_size_t)
    assert native.waveOutMessage.restype is ctypes.c_uint32


def test_non_windows_inventory_explains_actual_backend_limit(monkeypatch):
    monkeypatch.setattr(devices, 'os', SimpleNamespace(name='posix'))
    with pytest.raises(RuntimeError, match='Windows MCI'):
        devices.output_devices()


class MCI:
    def __init__(self):
        self.commands, self.output, self.mode, self.position = [], None, 'stopped', 0
        self.reject_output = self.wrong_status = False
        self.after_set = lambda: None
    def send(self, command):
        self.commands.append(command)
        words = command.split()
        if words[0] == 'open':
            self.output = None
        elif words[0] == 'set' and words[2] == 'output':
            if self.reject_output:
                raise RuntimeError('Native endpoint rejected this format')
            self.output = int(words[3])
            self.after_set()
        elif words[0] == 'status':
            if words[2] == 'output':
                return str(63 if self.wrong_status else self.output)
            if words[2] == 'position':
                return str(self.position)
            if words[2] == 'mode':
                return self.mode
        elif words[0] == 'play':
            self.mode = 'playing'
            self.position = int(words[words.index('from') + 1])
        elif words[0] == 'pause':
            self.mode = 'paused'
        elif words[0] == 'resume':
            self.mode = 'playing'
        elif words[0] in ('stop', 'close'):
            self.mode = 'stopped'
        return ''


@pytest.fixture
def transport(tmp_path, monkeypatch, inventory):
    source = tmp_path / 'retained source.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * 16000)
    original = source.read_bytes()
    monkeypatch.setattr(playback, 'os', SimpleNamespace(name='nt'))
    # Model the MCI protocol independently of this host's temporary-root length.
    # Unmodified filename routing is exercised in test_wave_output.py.
    monkeypatch.setattr(playback, '_mci_filename_supported', lambda path: True)
    player, native = playback.WavePlayer(), MCI()
    monkeypatch.setattr(player, '_mci', native.send)
    monkeypatch.setattr(player, '_mci_open',
        lambda path: native.send(f'open "{path}" type waveaudio alias {player._alias}'))
    value = SimpleNamespace(player=player, native=native, source=source, inventory=inventory)
    yield value
    player.close()
    assert source.read_bytes() == original


def test_original_default_route_never_uses_new_inventory(transport, monkeypatch):
    monkeypatch.setattr(playback, 'verify_output_device', lambda *args: pytest.fail('Unexpected explicit route'))
    transport.player.play(transport.source)
    assert transport.player.output_device is None
    assert not any(' output ' in command for command in transport.native.commands)


def test_explicit_route_is_set_and_verified_before_any_play(transport):
    t = transport
    selected = t.inventory.rows[1]
    t.player.set_output_device(selected)
    assert not t.native.commands
    t.player.play(t.source)
    commands = t.native.commands
    applied = commands.index(f'set {t.player._alias} output 1')
    played = next(i for i, value in enumerate(commands) if value.startswith('play '))
    queried = commands.index(f'status {t.player._alias} output')
    assert applied < queried < played
    assert t.player.active_output_index == 1
    assert t.player.output_device == selected


@pytest.mark.parametrize('paused', [False, True])
def test_route_change_never_autoplays_and_keeps_position_for_explicit_resume(transport, paused):
    t = transport
    t.player.play(t.source)
    t.native.position = 600
    if paused:
        assert t.player.pause()
    t.player.set_ab_loop(.1, 1.5)
    t.player.enable_repeat()
    t.native.position = 600
    t.player.set_sleep_timer(20)
    starts = sum(command.startswith('play ') for command in t.native.commands)
    generation = t.player.session_generation
    t.player.set_output_device(t.inventory.rows[1])
    assert t.player.state == 'paused' and t.player.position_seconds == .6
    assert t.player.session_generation == generation + 1
    assert not t.player.repeat_enabled and t.player.ab_loop is None
    assert t.player.sleep_remaining_seconds is None
    assert sum(command.startswith('play ') for command in t.native.commands) == starts
    assert t.player.resume()
    assert t.player.active_output_index == 1
    # The independent status query above is itself an MCI command. Inspect the
    # actual resumed play operation, not that later observation.
    assert next(command for command in reversed(t.native.commands)
                if command.startswith('play ')).endswith('from 600')


def test_disconnected_or_replaced_device_stops_before_open_or_play(transport):
    t = transport
    t.player.set_output_device(t.inventory.rows[0])
    t.inventory.rows[0] = item(0, 'Same friendly name', 'replacement')
    with pytest.raises(RuntimeError, match='replaced'):
        t.player.play(t.source)
    assert not any(command.startswith(('open ', 'play ')) for command in t.native.commands)
    assert t.player.output_device.interface != 'replacement'


def test_identity_change_during_native_set_closes_without_playing(transport):
    t = transport
    t.player.set_output_device(t.inventory.rows[0])
    t.native.after_set = lambda: t.inventory.rows.__setitem__(0, item(0, identity='replacement'))
    with pytest.raises(RuntimeError, match='replaced'):
        t.player.play(t.source)
    assert any(command.startswith('close ') for command in t.native.commands)
    assert not any(command.startswith('play ') for command in t.native.commands)


@pytest.mark.parametrize('attribute', ['reject_output', 'wrong_status'])
def test_native_rejection_or_wrong_route_does_not_fall_back(transport, attribute):
    t = transport
    t.player.set_output_device(t.inventory.rows[1])
    setattr(t.native, attribute, True)
    with pytest.raises(RuntimeError):
        t.player.play(t.source)
    assert not any(command.startswith('play ') for command in t.native.commands)
    assert not t.player._open and t.player.output_device.index == 1


def test_paused_resume_rechecks_device_identity_and_never_rearms_policy(transport):
    t = transport
    t.player.set_output_device(t.inventory.rows[0])
    t.player.play(t.source)
    t.player.pause()
    t.inventory.rows[0] = item(0, identity='replacement')
    with pytest.raises(RuntimeError, match='replaced'):
        t.player.resume()
    assert not any(command.startswith('resume ') for command in t.native.commands)
    assert not t.player._open


def test_seek_and_repeat_reopen_the_same_selected_route(transport):
    t = transport
    t.player.set_output_device(t.inventory.rows[1])
    t.player.play(t.source)
    t.player.seek(.2)
    t.player.set_ab_loop(.3, .7)
    t.player.enable_repeat()
    t.native.mode = 'stopped'
    assert t.player.poll(restart=False) == 'repeat_due'
    assert t.player.restart_repeat()
    commands = t.native.commands
    assert sum(command.startswith('play ') for command in commands) == 4
    assert sum(command == f'set {t.player._alias} output 1' for command in commands) == 4


def test_explicit_default_clears_only_this_readers_selection(transport, monkeypatch):
    t = transport
    other, native = playback.WavePlayer(), MCI()
    monkeypatch.setattr(other, '_mci', native.send)
    monkeypatch.setattr(other, '_mci_open',
        lambda path: native.send(f'open "{path}" type waveaudio alias {other._alias}'))
    other.set_output_device(t.inventory.rows[0])
    t.player.set_output_device(t.inventory.rows[1])
    t.player.play(t.source)
    t.native.position = 400
    t.player.set_output_device(None)
    assert other.output_device == t.inventory.rows[0]
    assert not native.commands
    t.native.commands.clear()
    assert t.player.resume()
    assert not any(' output ' in command for command in t.native.commands)
    assert t.player.position_seconds == .4 and t.player.output_device is None


def test_unsupported_player_rejects_explicit_route_without_native_access(monkeypatch):
    monkeypatch.setattr(playback, 'os', SimpleNamespace(name='posix'))
    player = playback.WavePlayer()
    with pytest.raises(RuntimeError, match='does not support'):
        player.set_output_device(item())
    assert player.output_device is None
