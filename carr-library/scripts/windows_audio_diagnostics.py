"""Read-only bounded Windows audio-service/device/format diagnostics.

A format query opens no playback stream and is not native-player acceptance.
The unchanged installed MCI probe remains the authority for playback behavior.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import threading


def _services():
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    advapi.OpenSCManagerW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    advapi.OpenSCManagerW.restype = wintypes.HANDLE
    advapi.OpenServiceW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD)
    advapi.OpenServiceW.restype = wintypes.HANDLE
    advapi.QueryServiceStatusEx.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi.QueryServiceStatusEx.restype = wintypes.BOOL
    advapi.CloseServiceHandle.argtypes = (wintypes.HANDLE,)
    advapi.CloseServiceHandle.restype = wintypes.BOOL

    class Status(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            'service_type', 'state', 'accepted_controls', 'win32_exit_code',
            'service_exit_code', 'checkpoint', 'wait_hint', 'process_id', 'flags')]

    manager = advapi.OpenSCManagerW(None, None, 0x0001)  # SC_MANAGER_CONNECT
    if not manager:
        return {'query_error': ctypes.get_last_error()}
    values = {}
    states = {1: 'STOPPED', 2: 'START_PENDING', 3: 'STOP_PENDING', 4: 'RUNNING',
              5: 'CONTINUE_PENDING', 6: 'PAUSE_PENDING', 7: 'PAUSED'}
    try:
        for name in ('AudioEndpointBuilder', 'Audiosrv'):
            handle = advapi.OpenServiceW(manager, name, 0x0004)  # SERVICE_QUERY_STATUS
            if not handle:
                values[name] = {'query_error': ctypes.get_last_error()}
                continue
            try:
                current, needed = Status(), wintypes.DWORD()
                if not advapi.QueryServiceStatusEx(handle, 0, ctypes.byref(current),
                        ctypes.sizeof(current), ctypes.byref(needed)):
                    values[name] = {'query_error': ctypes.get_last_error()}
                else:
                    values[name] = {'state': states.get(current.state, 'UNKNOWN'),
                        'state_code': current.state, 'win32_exit_code': current.win32_exit_code,
                        'service_exit_code': current.service_exit_code}
            finally:
                advapi.CloseServiceHandle(handle)
    finally:
        advapi.CloseServiceHandle(manager)
    return values


def snapshot():
    if os.name != 'nt':
        raise RuntimeError('Actual Windows is required for WinMM diagnostics.')
    winmm = ctypes.WinDLL('winmm', use_last_error=True)

    class WaveFormat(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('format_tag', wintypes.WORD), ('channels', wintypes.WORD),
                    ('sample_rate', wintypes.DWORD), ('bytes_per_second', wintypes.DWORD),
                    ('block_align', wintypes.WORD), ('bits_per_sample', wintypes.WORD),
                    ('extra_bytes', wintypes.WORD)]

    class Capabilities(ctypes.Structure):
        _fields_ = [('manufacturer', wintypes.WORD), ('product', wintypes.WORD),
                    ('driver_version', wintypes.DWORD), ('name', wintypes.WCHAR * 32),
                    ('standard_formats', wintypes.DWORD), ('channels', wintypes.WORD),
                    ('reserved', wintypes.WORD), ('support', wintypes.DWORD)]

    winmm.waveOutGetNumDevs.argtypes = ()
    winmm.waveOutGetNumDevs.restype = wintypes.UINT
    winmm.waveOutGetDevCapsW.argtypes = (ctypes.c_size_t, ctypes.POINTER(Capabilities), wintypes.UINT)
    winmm.waveOutGetDevCapsW.restype = wintypes.UINT
    winmm.waveOutOpen.argtypes = (ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(WaveFormat),
                                 ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD)
    winmm.waveOutOpen.restype = wintypes.UINT
    winmm.waveOutGetErrorTextW.argtypes = (wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
    winmm.waveOutGetErrorTextW.restype = wintypes.UINT

    def message(code):
        buffer = ctypes.create_unicode_buffer(256)
        return buffer.value if winmm.waveOutGetErrorTextW(code, buffer, len(buffer)) == 0 else None

    def query(identity, rate, channels):
        format_value = WaveFormat(1, channels, rate, rate * channels * 2, channels * 2, 16, 0)
        # WAVE_FORMAT_QUERY: no device handle, callback, audio buffer or playback.
        code = int(winmm.waveOutOpen(None, identity, ctypes.byref(format_value), 0, 0, 0x0001))
        return {'sample_rate': rate, 'channels': channels, 'bits_per_sample': 16,
                'code': code, 'message': message(code)}

    count = int(winmm.waveOutGetNumDevs())
    services = _services()
    devices = []
    for identity in range(min(count, 32)):
        capabilities = Capabilities()
        code = int(winmm.waveOutGetDevCapsW(identity, ctypes.byref(capabilities),
                                          ctypes.sizeof(capabilities)))
        value = {'id': identity, 'capabilities_code': code}
        if code == 0:
            value.update(name=capabilities.name, manufacturer=capabilities.manufacturer,
                         product=capabilities.product, driver_version=capabilities.driver_version,
                         advertised_channels=capabilities.channels,
                         standard_formats=capabilities.standard_formats, support=capabilities.support)
        else:
            value['message'] = message(code)
        value['target_format'] = query(identity, 24000, 1)
        devices.append(value)
    mapper = [query(0xffffffff, rate, channels)
              for rate in (24000, 44100, 48000) for channels in (1, 2)]
    target = mapper[0]
    if count == 0:
        observation = 'NO_OUTPUT_DEVICE_ENUMERATED_OR_QUERY_ERROR'
    elif target['code'] == 0:
        observation = 'TARGET_FORMAT_QUERY_SUPPORTED_PLAYBACK_NOT_PROVEN'
    else:
        observation = 'OUTPUT_DEVICE_ENUMERATED_TARGET_FORMAT_QUERY_REFUSED'
    return {'schema': 'carr-windows-audio-diagnostics/1', 'observation': observation,
            'services': services, 'wave_output_device_count': count,
            'device_inventory_truncated': count > 32, 'devices': devices,
            'mapper_queries': mapper, 'query_only': True, 'playback_evidence': 'NOT_RUN'}


def main():
    if os.name != 'nt':
        print(json.dumps({'schema': 'carr-windows-audio-diagnostics/1',
                          'observation': 'ACTUAL_WINDOWS_REQUIRED', 'playback_evidence': 'NOT_RUN'}))
        return 77
    def expired():
        os.write(2, b'Bounded Windows audio diagnostics exceeded 30 seconds.\n')
        os._exit(124)
    watchdog = threading.Timer(30, expired)
    watchdog.daemon = True
    watchdog.start()
    try:
        print(json.dumps(snapshot(), ensure_ascii=True, sort_keys=True), flush=True)
        return 0
    finally:
        watchdog.cancel()


if __name__ == '__main__':
    raise SystemExit(main())
