"""WinMM ABI and endpoint churn tests using explicit native-call fixtures."""
import ctypes
from dataclasses import replace
from types import SimpleNamespace

import pytest

from carr_library.audio import output_devices as module
from carr_library.audio.output_devices import OutputDevice


class Function:
    def __init__(self, action):
        self.action, self.calls = action, []
    def __call__(self, *arguments):
        self.calls.append(arguments)
        return self.action(*arguments)


class WinMM:
    def __init__(self, devices):
        self.devices = list(devices)
        self.count_result = None
        self.endpoint_size = None
        self.endpoint_raw = None
        self.caps_error = 0
        self.message_error = 0
        self.waveOutGetNumDevs = Function(self.count)
        self.waveOutGetDevCapsW = Function(self.caps)
        self.waveOutMessage = Function(self.message)

    def count(self):
        if self.count_result is not None:
            return self.count_result(len(self.waveOutGetNumDevs.calls))
        return len(self.devices)

    def caps(self, index, destination, size):
        assert size == 84
        value = self.devices[index]
        raw = module._WaveOutCaps.from_address(ctypes.addressof(destination._obj))
        raw.manufacturer, raw.product, raw.driver_version = 3, 7, 0x10000
        raw.formats, raw.channels, raw.support = 31, 2, 4
        name = value[0].encode('utf-16-le') + b'\0\0'
        assert len(name) <= 64
        ctypes.memmove(ctypes.addressof(raw) + 8, name, len(name))
        return self.caps_error

    def message(self, device, kind, first, second):
        index = device.value or 0
        raw = self.endpoint_raw
        if raw is None:
            raw = self.devices[index][1].encode('utf-16-le') + b'\0\0'
        if kind == 0x0812:
            assert second == 0
            ctypes.c_size_t.from_address(first).value = self.endpoint_size if self.endpoint_size is not None else len(raw)
        elif kind == 0x0811:
            assert second >= len(raw)
            ctypes.memmove(first, raw, len(raw))
        else:
            raise AssertionError('Unexpected native device message.')
        return self.message_error


def test_actual_fixed_width_windows_abi_preserves_unicode_and_distinct_endpoints():
    native = WinMM([('Навушники 🎧', 'endpoint-a'), ('Навушники 🎧', 'endpoint-b')])
    devices = module._enumerate(native)
    assert ctypes.sizeof(module._WaveOutCaps) == 84
    assert module._WaveOutCaps.name.offset == 8
    assert module._WaveOutCaps.channels.offset == 76
    assert [item.index for item in devices] == [0, 1]
    assert [item.name for item in devices] == ['Навушники 🎧'] * 2
    assert [item.endpoint_id for item in devices] == ['endpoint-a', 'endpoint-b']
    assert devices[0].identity != devices[1].identity
    assert devices[0].channels == 2 and devices[0].manufacturer == 3
    assert native.waveOutMessage.argtypes[-1] is ctypes.c_size_t
    assert native.waveOutGetDevCapsW.argtypes[0] is ctypes.c_size_t


@pytest.mark.parametrize('size', [0, 2, 3, 8194])
def test_endpoint_allocation_is_bounded_before_native_read(size):
    native = WinMM([('Speakers', 'endpoint')])
    native.endpoint_size = size
    with pytest.raises(ValueError, match='size'):
        module._enumerate(native)
    assert len(native.waveOutMessage.calls) == 1


@pytest.mark.parametrize('failure', ['count', 'changed', 'duplicate', 'caps', 'message', 'unterminated', 'unicode'])
def test_invalid_inventory_never_returns_partial_or_fabricated_devices(failure):
    native = WinMM([('Speakers', 'endpoint-a'), ('Headphones', 'endpoint-b')])
    if failure == 'count':
        native.count_result = lambda call: 257
    elif failure == 'changed':
        native.count_result = lambda call: 2 if call == 1 else 1
    elif failure == 'duplicate':
        native.devices[1] = ('Headphones', 'endpoint-a')
    elif failure == 'caps':
        native.caps_error = 2
    elif failure == 'message':
        native.message_error = 8
    elif failure == 'unterminated':
        native.endpoint_raw = b'a\0b\0'
    else:
        native.endpoint_raw = b'\x00\xd8\0\0'
    with pytest.raises((ValueError, RuntimeError)):
        module._enumerate(native)
    if failure == 'count':
        assert not native.waveOutGetDevCapsW.calls and not native.waveOutMessage.calls


def test_same_endpoint_can_be_reenumerated_but_same_label_is_never_identity(monkeypatch):
    selected = OutputDevice(1, 'endpoint-owner', 'Speakers')
    inventory = [replace(selected, index=3)]
    monkeypatch.setattr(module, 'output_devices', lambda: tuple(inventory))
    assert module.resolve_output_device(selected).index == 3
    assert module.confirm_output_index(selected, 3).endpoint_id == 'endpoint-owner'
    with pytest.raises(ValueError, match='order changed'):
        module.confirm_output_index(selected, 1)
    inventory[:] = [replace(selected, endpoint_id='different-endpoint')]
    with pytest.raises(ValueError, match='unavailable or changed'):
        module.resolve_output_device(selected)
    inventory[:] = [replace(selected, channels=1)]
    with pytest.raises(ValueError, match='unavailable or changed'):
        module.resolve_output_device(selected)
    inventory.clear()
    with pytest.raises(ValueError, match='unavailable or changed'):
        module.resolve_output_device(selected)


@pytest.mark.parametrize('value', [None, True, 0, 'Speakers', {'index': 0}])
def test_only_typed_explicit_choices_reach_native_discovery(monkeypatch, value):
    def forbidden():
        raise AssertionError('An unreviewed choice must not discover native devices.')
    monkeypatch.setattr(module, 'output_devices', forbidden)
    with pytest.raises(ValueError, match='Choose a device'):
        module.resolve_output_device(value)


def test_nonwindows_reports_its_explicit_capability_boundary(monkeypatch):
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='posix'))
    with pytest.raises(RuntimeError, match='available on Windows'):
        module.output_devices()


@pytest.mark.parametrize('changes', [
    {'index': True}, {'index': 256}, {'endpoint_id': ''}, {'endpoint_id': 'x\0y'},
    {'endpoint_id': 'x' * 4096}, {'name': '  '}, {'channels': 0}, {'support': True},
])
def test_invalid_device_records_are_rejected(changes):
    values = dict(index=0, endpoint_id='endpoint', name='Speakers')
    values.update(changes)
    with pytest.raises(ValueError):
        OutputDevice(**values)
