"""Explicit Windows output selection for the existing MCI playback owner.

Endpoint IDs are opaque, session-only Windows identities. No device is opened by
discovery, and an unavailable explicit choice never becomes the default route.
"""
from dataclasses import dataclass
import ctypes
import os

MAX_OUTPUT_DEVICES = 256
MAX_ENDPOINT_BYTES = 8192
_DRV_QUERYFUNCTIONINSTANCEID = 0x0811
_DRV_QUERYFUNCTIONINSTANCEIDSIZE = 0x0812


@dataclass(frozen=True)
class OutputDevice:
    index: int
    endpoint_id: str
    name: str
    channels: int = 2
    manufacturer: int = 0
    product: int = 0
    driver_version: int = 0
    formats: int = 0
    support: int = 0

    def __post_init__(self):
        if type(self.index) is not int or not 0 <= self.index < MAX_OUTPUT_DEVICES:
            raise ValueError('Invalid output device index.')
        if (type(self.endpoint_id) is not str or not self.endpoint_id
                or '\0' in self.endpoint_id
                or len(self.endpoint_id.encode('utf-16-le')) > MAX_ENDPOINT_BYTES - 2):
            raise ValueError('Invalid output endpoint identity.')
        if (type(self.name) is not str or not self.name.strip() or len(self.name) > 256
                or '\0' in self.name):
            raise ValueError('Invalid output device name.')
        if type(self.channels) is not int or not 1 <= self.channels <= 65535:
            raise ValueError('Invalid output device channel capability.')
        for value in (self.manufacturer, self.product, self.driver_version, self.formats, self.support):
            if type(value) is not int or not 0 <= value <= 0xffffffff:
                raise ValueError('Invalid output device capability.')

    @property
    def identity(self):
        return (self.endpoint_id, self.name, self.channels, self.manufacturer,
                self.product, self.driver_version, self.formats, self.support)

    @property
    def label(self):
        return f'{" ".join(self.name.split())}; output {self.index + 1}; {self.channels} channel(s)'


class _WaveOutCaps(ctypes.Structure):
    # Fixed-width Windows ABI types also make this layout inspectable elsewhere.
    _fields_ = [('manufacturer', ctypes.c_uint16), ('product', ctypes.c_uint16),
        ('driver_version', ctypes.c_uint32), ('name', ctypes.c_uint16 * 32),
        ('formats', ctypes.c_uint32), ('channels', ctypes.c_uint16),
        ('reserved', ctypes.c_uint16), ('support', ctypes.c_uint32)]


def _library():
    if os.name != 'nt':
        raise RuntimeError('Explicit output-device selection is available on Windows. This backend uses its system output.')
    return ctypes.WinDLL('winmm.dll', use_last_error=True)


def _endpoint(message, index):
    size = ctypes.c_size_t()
    code = message(ctypes.c_void_p(index), _DRV_QUERYFUNCTIONINSTANCEIDSIZE,
                   ctypes.addressof(size), 0)
    if code:
        raise RuntimeError(f'Windows could not identify output {index + 1} (WinMM {code}). Refresh the device list.')
    if not 2 < size.value <= MAX_ENDPOINT_BYTES or size.value % 2:
        raise ValueError('Windows reported an invalid output endpoint size.')
    buffer = (ctypes.c_ubyte * size.value)(*([255] * size.value))
    code = message(ctypes.c_void_p(index), _DRV_QUERYFUNCTIONINSTANCEID,
                   ctypes.addressof(buffer), size.value)
    if code:
        raise RuntimeError(f'Windows could not read output {index + 1} identity (WinMM {code}). Refresh the device list.')
    raw = bytes(buffer)
    terminator = next((i for i in range(0, len(raw), 2) if raw[i:i + 2] == b'\0\0'), None)
    if terminator is None or terminator == 0:
        raise ValueError('Windows returned an empty or unterminated output endpoint.')
    return raw[:terminator].decode('utf-16-le', errors='strict')


def _enumerate(library):
    count = library.waveOutGetNumDevs
    count.argtypes, count.restype = (), ctypes.c_uint32
    caps = library.waveOutGetDevCapsW
    caps.argtypes = (ctypes.c_size_t, ctypes.POINTER(_WaveOutCaps), ctypes.c_uint32)
    caps.restype = ctypes.c_uint32
    message = library.waveOutMessage
    message.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_size_t)
    message.restype = ctypes.c_uint32
    total = count()
    if not 0 <= total <= MAX_OUTPUT_DEVICES:
        raise ValueError('The Windows output-device inventory exceeds its bound.')
    devices = []
    for index in range(total):
        value = _WaveOutCaps()
        code = caps(index, ctypes.byref(value), ctypes.sizeof(value))
        if code:
            raise RuntimeError(f'Windows output {index + 1} changed during discovery (WinMM {code}). Refresh the device list.')
        name = bytes(value.name).decode('utf-16-le', errors='strict').split('\0', 1)[0]
        devices.append(OutputDevice(index, _endpoint(message, index), name, value.channels,
            value.manufacturer, value.product, value.driver_version, value.formats, value.support))
    if count() != total or len({item.endpoint_id for item in devices}) != len(devices):
        raise ValueError('The Windows output-device list changed or contains ambiguous identities. Refresh it.')
    return tuple(devices)


def output_devices():
    """Enumerate bounded exact endpoints without opening an audio stream."""
    return _enumerate(_library())


def resolve_output_device(selected):
    """Find the same reviewed endpoint; numeric indices may be re-enumerated."""
    if type(selected) is not OutputDevice:
        raise ValueError('Choose a device from the current Windows output list.')
    matches = [item for item in output_devices() if item.endpoint_id == selected.endpoint_id]
    if len(matches) != 1 or matches[0].identity != selected.identity:
        raise ValueError('The selected output device is unavailable or changed. Choose an output device again.')
    return matches[0]


def confirm_output_index(selected, index):
    """Recheck the index immediately before native playback, never substitute."""
    current = resolve_output_device(selected)
    if type(index) is not int or current.index != index:
        raise ValueError('The output-device order changed before playback. Choose the device again.')
    return current
