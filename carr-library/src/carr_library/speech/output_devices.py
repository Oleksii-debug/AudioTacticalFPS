"""Read-only WinMM output identity for the reader's existing MCI transport.

No system endpoint, voice, NVDA or mixer setting is changed here. Numeric WinMM
indices are transient; explicit selections retain the opaque Windows endpoint
identity and are revalidated before MCI can play.
"""
from dataclasses import dataclass, field
import ctypes
from hashlib import sha256
import os


_MAX_DEVICES = 64
_MAX_INTERFACE_BYTES = 65536
# Microsoft WinSDK mmddk.h: DRV_QUERYFUNCTIONINSTANCEID/SIZE (+17/+18).
# Device-interface names (+12/+13) can describe one adapter with multiple
# distinct output endpoints. Keep each endpoint opaque and independently bound.
# https://learn.microsoft.com/windows/win32/coreaudio/endpoint-id-strings
_QUERY_ENDPOINT = 0x0811
_QUERY_ENDPOINT_SIZE = 0x0812


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Reader output selection cancelled.')


@dataclass(frozen=True)
class OutputDevice:
    index: int
    name: str
    interface: str = field(repr=False)

    def __post_init__(self):
        if type(self.index) is not int or not 0 <= self.index < _MAX_DEVICES:
            raise ValueError('The output device index is outside its inventory allowance.')
        if (type(self.name) is not str or not self.name or len(self.name) > 128
                or '\x00' in self.name or any(ord(char) < 32 for char in self.name)):
            raise ValueError('The output device name is invalid.')
        if (type(self.interface) is not str or not self.interface or '\x00' in self.interface
                or len(self.interface.encode('utf-16-le')) + 2 > _MAX_INTERFACE_BYTES):
            raise ValueError('An explicit output requires a bounded native device identity.')

    @property
    def identity(self):
        return sha256(self.interface.encode('utf-16-le')).hexdigest()


class _WaveOutputCaps(ctypes.Structure):
    # Fixed Windows WCHAR/WORD/DWORD sizes also let portable ABI tests inspect
    # this structure without pretending the host's wchar_t is Windows WCHAR.
    _pack_ = 1
    _fields_ = [('manufacturer', ctypes.c_uint16), ('product', ctypes.c_uint16),
                ('driver_version', ctypes.c_uint32), ('name', ctypes.c_uint16 * 32),
                ('formats', ctypes.c_uint32), ('channels', ctypes.c_uint16),
                ('reserved', ctypes.c_uint16), ('support', ctypes.c_uint32)]


def _native():
    if os.name != 'nt':
        raise RuntimeError('Per-reader output selection is available with Windows MCI. '
                           'This backend uses its existing system-default output.')
    api = ctypes.WinDLL('winmm', use_last_error=True)
    api.waveOutGetNumDevs.argtypes = ()
    api.waveOutGetNumDevs.restype = ctypes.c_uint32
    api.waveOutGetDevCapsW.argtypes = (
        ctypes.c_size_t, ctypes.POINTER(_WaveOutputCaps), ctypes.c_uint32)
    api.waveOutGetDevCapsW.restype = ctypes.c_uint32
    api.waveOutMessage.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_size_t)
    api.waveOutMessage.restype = ctypes.c_uint32
    return api


def _error(code, operation):
    if code:
        # Device identities and source filenames do not belong in diagnostics.
        raise RuntimeError(f'Windows output {operation} failed (WinMM {int(code)}). '
                           'Refresh outputs and select the device again.')


def _utf16(raw, *, field_name):
    if len(raw) % 2:
        raise ValueError(f'The native output {field_name} is malformed.')
    ends = [index for index in range(0, len(raw), 2) if raw[index:index + 2] == b'\x00\x00']
    if not ends:
        raise ValueError(f'The native output {field_name} is not terminated.')
    try:
        value = raw[:ends[0]].decode('utf-16-le', errors='strict')
    except UnicodeError as exc:
        raise ValueError(f'The native output {field_name} is malformed.') from exc
    if not value:
        raise ValueError(f'This output has no stable native {field_name}; '
                         'explicit per-reader selection is unavailable for it.')
    return value


def _device(api, index, cancelled):
    _check(cancelled)
    caps = _WaveOutputCaps()
    _error(api.waveOutGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)), 'capability query')
    _check(cancelled)
    # The documented endpoint query uses size_t. A zeroed pointer-sized
    # buffer is also safe for providers which write only a 32-bit byte count.
    size = ctypes.c_size_t()
    _error(api.waveOutMessage(index, _QUERY_ENDPOINT_SIZE, ctypes.addressof(size), 0),
           'identity-size query')
    _check(cancelled)
    if not 2 <= size.value <= _MAX_INTERFACE_BYTES or size.value % 2:
        raise ValueError('The output does not expose a bounded native endpoint identity. '
                         'Explicit selection cannot be verified.')
    buffer = (ctypes.c_ubyte * size.value)()
    _error(api.waveOutMessage(index, _QUERY_ENDPOINT, ctypes.addressof(buffer), size.value),
           'identity query')
    _check(cancelled)
    return OutputDevice(index, _utf16(bytes(caps.name), field_name='name'),
                        _utf16(bytes(buffer), field_name='endpoint identity'))


def output_devices(*, cancelled=lambda: False):
    """Enumerate without opening a device or changing any routing setting."""
    _check(cancelled)
    api = _native()
    count = int(api.waveOutGetNumDevs())
    if not 0 <= count <= _MAX_DEVICES:
        raise ValueError('The native output inventory exceeds its device allowance.')
    result = []
    seen = set()
    for index in range(count):
        value = _device(api, index, cancelled)
        if value.interface in seen:
            raise ValueError('The native output identity is ambiguous. No device was selected.')
        seen.add(value.interface)
        result.append(value)
    _check(cancelled)
    if int(api.waveOutGetNumDevs()) != count:
        raise RuntimeError('Audio outputs changed during discovery. Refresh and select again.')
    return tuple(result)


def resolve_output_device(selected, *, cancelled=lambda: False):
    """Resolve the explicitly selected identity after a possible index reorder."""
    if not isinstance(selected, OutputDevice):
        raise ValueError('Select a device from the current native output inventory.')
    matches = [item for item in output_devices(cancelled=cancelled)
               if item.interface == selected.interface]
    if len(matches) != 1:
        raise RuntimeError('The selected output was removed or is no longer uniquely available. '
                           'Refresh outputs or explicitly choose System default output.')
    return matches[0]


def verify_output_device(selected, *, cancelled=lambda: False):
    """Fast exact-index recheck immediately before using a prepared selection.

    Full enumeration/reordering belongs to the existing background reader
    worker. A stale prepared index fails closed instead of playing elsewhere.
    """
    if not isinstance(selected, OutputDevice):
        raise ValueError('Select a verified native output device.')
    current = _device(_native(), selected.index, cancelled)
    if current.interface != selected.interface:
        raise RuntimeError('The selected output moved or was replaced. Refresh outputs and '
                           'select it again; no fallback device was opened.')
    return current
