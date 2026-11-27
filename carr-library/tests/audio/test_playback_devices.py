"""Real C ABI memory layouts and owned player routing with explicit API fixtures.

These portable tests do not enumerate or qualify physical Windows sound cards.
"""
import ctypes
from dataclasses import replace
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import playback_devices as devices


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
        if command == devices.DRV_QUERYDEVICEINTERFACESIZE:
            if self.failure == 'size': return 8
            ctypes.cast(first, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(raw) if self.size_override is None else self.size_override
        elif command == devices.DRV_QUERYDEVICEINTERFACE:
            if self.failure == 'interface': return 8
            assert second >= len(raw)
            ctypes.memmove(first, raw, len(raw))
        else:
            raise AssertionError('Only documented interface messages are admitted')
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
    assert devices.DRV_QUERYDEVICEINTERFACE == 0x80c and devices.DRV_QUERYDEVICEINTERFACESIZE == 0x80d
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
    with pytest.raises(RuntimeError, match='interface size'): devices.output_devices()
    assert not any(row[0] == 'message' and row[2] == 0x80c for row in native.calls)


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
    with pytest.raises(RuntimeError, match='requires Windows'): devices.output_devices()


@pytest.mark.parametrize('raw', [b'a', b'a\0', b'\x00\xd8\0\0'])
def test_native_utf16_requires_valid_termination(raw):
    with pytest.raises(RuntimeError): devices._text(raw, 'fixture')


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
            devices.DRV_QUERYDEVICEINTERFACESIZE if stage == 'size' else devices.DRV_QUERYDEVICEINTERFACE)
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


@pytest.mark.parametrize('value', [True, 0, -1, 3, float('nan'), float('inf'), 10 ** 400],
                         ids=['bool','zero','negative','too-long','nan','infinite','huge'])
def test_invalid_inventory_budget_is_rejected_before_any_native_access(monkeypatch, value):
    calls = []
    monkeypatch.setattr(devices, '_winmm', lambda: calls.append(True))
    with pytest.raises(ValueError): devices.output_devices(timeout_seconds=value)
    assert calls == []

