"""Report actual Windows output availability without starting playback.

Format queries and device inventory are observations only. No successful query
is a physical-playback, listening or NVDA qualification.
"""
import ctypes as C
import json
import os

def main():
    if os.name != 'nt':
        raise RuntimeError('Actual Windows is required for the output inventory.')
    from carr_library.audio.recording_monitor import _api, _WaveFormat
    from carr_library.audio.output_devices import _WaveOutCaps
    lib = _api()
    lib.waveOutGetNumDevs.argtypes = []
    lib.waveOutGetNumDevs.restype = C.c_uint32
    lib.waveOutGetDevCapsW.argtypes = [C.c_size_t, C.POINTER(_WaveOutCaps), C.c_uint32]
    lib.waveOutGetDevCapsW.restype = C.c_uint32
    lib.mciGetErrorStringW.argtypes = [C.c_uint32, C.c_wchar_p, C.c_uint32]
    lib.mciGetErrorStringW.restype = C.c_int
    count = int(lib.waveOutGetNumDevs())
    if not 0 <= count <= 256:
        raise ValueError('The native output-device inventory exceeds its explicit bound.')
    text = C.create_unicode_buffer(1024)
    error_text_available = bool(lib.mciGetErrorStringW(326, text, len(text)))
    devices = []
    for index in range(count):
        caps = _WaveOutCaps()
        code = int(lib.waveOutGetDevCapsW(index, C.byref(caps), C.sizeof(caps)))
        devices.append({'index': index, 'caps_result': code,
            'name': bytes(caps.name).decode('utf-16-le').split(chr(0), 1)[0] if code == 0 else '',
            'channels': int(caps.channels), 'formats': int(caps.formats), 'support': int(caps.support)})
    queries = []
    for index in [0xFFFFFFFF, *range(count)]:
        for rate, channels in ((24000, 1), (24000, 2), (44100, 2), (48000, 2)):
            align = channels * 2
            fmt = _WaveFormat(1, channels, rate, rate * align, align, 16, 0)
            code = int(lib.waveOutOpen(None, index, C.byref(fmt), 0, 0, 1))
            queries.append({'device': 'WAVE_MAPPER' if index == 0xFFFFFFFF else index,
                'sample_rate': rate, 'channels': channels, 'bits': 16,
                'format_query_result': code})
    print(json.dumps({'status': 'OBSERVED', 'wave_out_device_count': count,
        'devices': devices, 'queries': queries,
        'mci_326': {'description_available': error_text_available, 'description': text.value},
        'physical_playback': 'NOT_RUN', 'nvda': 'NOT_RUN',
        'scope': 'Native device inventory and WAVE_FORMAT_QUERY only; no output handle or sound was started.'},
        ensure_ascii=False, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
