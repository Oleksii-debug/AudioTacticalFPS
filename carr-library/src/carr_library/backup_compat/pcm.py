"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
import os
from pathlib import Path
import re
import struct
import sys


def _local_path(value):
    """Reject network/device spellings before resolve/stat can touch a share."""
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or '\x00' in raw:
        raise ValueError('Expected a non-empty local filesystem path.')
    if len(raw) >= 2 and raw[0] in ('/', '\\') and raw[1] in ('/', '\\'):
        raise ValueError('Network shares and Windows device paths are unavailable in local-only audio mode.')
    scheme = re.match(r'^([A-Za-z][A-Za-z0-9+.-]*):', raw)
    if scheme and not (len(scheme[1]) == 1 and len(raw) > 2 and raw[2] in ('/', '\\')):
        raise ValueError('URLs and drive-relative paths are unavailable in local-only audio mode.')
    path = Path(raw)
    if sys.platform == 'win32':
        import ctypes
        absolute = path.absolute()
        drive = absolute.drive + '\\'
        function = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
        function.argtypes = [ctypes.c_wchar_p]
        function.restype = ctypes.c_uint32
        if function(drive) == 4:  # DRIVE_REMOTE, including mapped SMB drives.
            raise ValueError('Mapped network drives are unavailable in local-only audio mode.')
        # A parent junction can otherwise hide a remote share behind a drive
        # path. Inspect ancestors in order, before accessing the selected file.
        cursor = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            cursor /= part
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                break
            if getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Windows reparse-point paths require a regular local copy for audio processing.')
    return path


def _pcm_header(path):
    """Read RIFF or RF64 PCM16 bounds without loading the recording into RAM."""
    size = path.stat().st_size
    with path.open('rb') as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] not in (b'RIFF', b'RF64') or header[8:] != b'WAVE':
            raise ValueError('Not a RIFF/RF64 PCM recording.')
        fmt = None
        rf64_data = None
        while stream.tell() + 8 <= size:
            chunk, length = struct.unpack('<4sI', stream.read(8))
            start = stream.tell()
            if chunk == b'ds64':
                if length < 28:
                    raise ValueError('Invalid RF64 size descriptor.')
                _, rf64_data, _, _ = struct.unpack('<QQQI', stream.read(28))
            elif chunk == b'fmt ':
                if length < 16 or length > 4096:
                    raise ValueError('Invalid PCM format descriptor.')
                payload = stream.read(length)
                code, channels, rate, byte_rate, align, bits = struct.unpack('<HHIIHH', payload[:16])
                if code == 0xfffe and (len(payload) < 40 or payload[24:40] != bytes.fromhex('0100000000001000800000aa00389b71')):
                    raise ValueError('Unsupported extensible WAV subtype.')
                if (code not in (1, 0xfffe) or bits != 16 or channels not in (1, 2)
                        or align != channels * 2 or not 0 < rate <= 768000 or byte_rate != rate * align):
                    raise ValueError('Expected mono/stereo PCM16 audio.')
                fmt = rate, channels, align
            elif chunk == b'data':
                if fmt is None:
                    raise ValueError('PCM audio is missing its format header.')
                if length == 0xffffffff:
                    if header[:4] != b'RF64' or rf64_data is None:
                        raise ValueError('PCM data length is not verifiable.')
                    length = rf64_data
                if length <= 0 or start + length > size or length % fmt[2]:
                    raise ValueError('PCM recording is empty, truncated or not sample aligned.')
                return fmt[0], fmt[1], length // fmt[2], start, length
            if start + length > size:
                raise ValueError('Truncated PCM chunk.')
            stream.seek(start + length + (length & 1))
    raise ValueError('The recording has no PCM data.')
