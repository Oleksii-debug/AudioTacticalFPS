"""Explicit Windows waveOut identities for the existing MCI WAV player.

Uses installed winmm only. Interface names are opaque device identities, not
paths to open. They are session choices, never restored hardware authority.
"""
import ctypes
from dataclasses import dataclass
import os

IS_WINDOWS = os.name == 'nt'
MAX_DEVICES = 128
MAX_INTERFACE_BYTES = 64 * 1024
# Reviewed Microsoft SDK mmddk.h at b07213e28bcc48221c155024f9c5e0e1a92c6497.
DRV_QUERYDEVICEINTERFACE = 0x0800 + 12
DRV_QUERYDEVICEINTERFACESIZE = 0x0800 + 13


class _Caps(ctypes.Structure):
    # Explicit UTF-16 and DWORD widths also allow ABI tests on POSIX hosts.
    _fields_ = [('manufacturer', ctypes.c_uint16), ('product', ctypes.c_uint16),
                ('driver', ctypes.c_uint32), ('name', ctypes.c_uint16 * 32),
                ('formats', ctypes.c_uint32), ('channels', ctypes.c_uint16),
                ('reserved', ctypes.c_uint16), ('support', ctypes.c_uint32)]


@dataclass(frozen=True)
class PlaybackDevice:
    index: int
    name: str
    interface: str
    manufacturer: int
    product: int
    driver: int
    channels: int

    def __post_init__(self):
        if type(self.index) is not int or not 0 <= self.index < MAX_DEVICES:
            raise ValueError('Invalid enumerated playback device index.')
        if (type(self.name) is not str or not self.name or len(self.name) > 32
                or any(ord(char) < 32 for char in self.name)):
            raise ValueError('Invalid enumerated playback device name.')
        if (type(self.interface) is not str or len(self.interface) > MAX_INTERFACE_BYTES // 2
                or any(ord(char) < 32 for char in self.interface)):
            raise ValueError('Invalid enumerated playback interface identity.')
        for value, maximum in ((self.manufacturer, 65535), (self.product, 65535),
                               (self.driver, 2**32 - 1), (self.channels, 65535)):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError('Invalid enumerated playback device capabilities.')

    @property
    def identity(self):
        return (self.interface, self.name, self.manufacturer, self.product, self.driver, self.channels)

    @property
    def selectable(self):
        return bool(self.interface)


def _text(raw, label):
    try:
        value = raw.decode('utf-16-le', 'strict')
    except UnicodeError as exc:
        raise RuntimeError(f'Windows returned an invalid {label}. Reload devices.') from exc
    if '\0' not in value:
        raise RuntimeError(f'Windows returned an unterminated {label}. Reload devices.')
    return value.split('\0', 1)[0]


def _winmm():
    if not IS_WINDOWS:
        raise RuntimeError('Explicit output selection is available on Windows; the current backend uses automatic output.')
    # System32 only; do not load a namesake DLL from an application/data folder.
    return ctypes.WinDLL('winmm.dll', winmode=0x0800)


def _inventory(library):
    count = library.waveOutGetNumDevs
    count.argtypes, count.restype = [], ctypes.c_uint
    caps = library.waveOutGetDevCapsW
    caps.argtypes = [ctypes.c_size_t, ctypes.POINTER(_Caps), ctypes.c_uint]
    caps.restype = ctypes.c_uint
    message = library.waveOutMessage
    message.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
    message.restype = ctypes.c_uint
    total = count()
    if type(total) is not int or not 0 <= total <= MAX_DEVICES:
        raise RuntimeError('The playback device inventory exceeds its bound.')
    result = []
    for index in range(total):
        value = _Caps()
        if caps(index, ctypes.byref(value), ctypes.sizeof(value)) != 0:
            raise RuntimeError('A playback device changed or could not be inspected. Reload devices.')
        name = _text(bytes(value.name), 'playback device name')
        size = ctypes.c_uint32()
        if message(ctypes.c_void_p(index), DRV_QUERYDEVICEINTERFACESIZE, ctypes.addressof(size), 0) != 0:
            raise RuntimeError('Windows could not identify a playback device. Reload devices.')
        interface = ''
        if size.value:
            if size.value < 2 or size.value > MAX_INTERFACE_BYTES or size.value % 2:
                raise RuntimeError('Windows returned an invalid playback interface size.')
            buffer = ctypes.create_string_buffer(size.value)
            if message(ctypes.c_void_p(index), DRV_QUERYDEVICEINTERFACE,
                       ctypes.addressof(buffer), size.value) != 0:
                raise RuntimeError('A playback device changed during identification. Reload devices.')
            interface = _text(buffer.raw, 'playback interface')
        result.append(PlaybackDevice(index, name, interface, value.manufacturer,
                                     value.product, value.driver, value.channels))
    if count() != total:
        raise RuntimeError('The playback device inventory changed. Reload devices.')
    identities = [row.interface for row in result if row.selectable]
    if len(set(identities)) != len(identities):
        raise RuntimeError('Windows returned ambiguous playback device identities.')
    return tuple(result)


def output_devices():
    """Enumerate a bounded stable inventory; never open a sound stream."""
    library = _winmm()
    first, second = _inventory(library), _inventory(library)
    if first != second:
        raise RuntimeError('The playback devices changed while being listed. Reload devices.')
    return first


def resolve_output(device):
    """Rebind an explicitly selected interface after index changes, never by name."""
    if not isinstance(device, PlaybackDevice) or not device.selectable:
        raise ValueError('Choose an enumerated output with a stable device identity.')
    found = [row for row in output_devices() if row.identity == device.identity]
    if len(found) != 1:
        raise RuntimeError('The selected playback output is unavailable or changed. Choose an output explicitly; no fallback was used.')
    return found[0]
