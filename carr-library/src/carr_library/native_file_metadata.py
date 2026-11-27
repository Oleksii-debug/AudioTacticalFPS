"""Shared bounded Windows change-time queries for already selected files."""
import ctypes
from functools import lru_cache
import os


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [('CreationTime', ctypes.c_int64), ('LastAccessTime', ctypes.c_int64),
                ('LastWriteTime', ctypes.c_int64), ('ChangeTime', ctypes.c_int64),
                ('FileAttributes', ctypes.c_uint32)]


@lru_cache(maxsize=1)
def _windows_file_api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create, query, close = (kernel.CreateFileW, kernel.GetFileInformationByHandleEx,
                            kernel.CloseHandle)
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    query.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32]
    query.restype = ctypes.c_int32
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int32
    return create, query, close


def stream_change_time(stream):
    """Retain native write freshness without reopening or closing this stream.

    Windows CPython3.11 st_ctime represents creation time. Its ordinary stat
    tuple can therefore miss a same-size write whose timestamp is unchanged.
    POSIX callers keep their existing descriptor/path stat checks.
    """
    if os.name != 'nt':
        return None
    import msvcrt
    _, query, _ = _windows_file_api()
    basic = _WindowsFileBasicInfo()
    if not query(msvcrt.get_osfhandle(stream.fileno()), 0,
                 ctypes.byref(basic), ctypes.sizeof(basic)):
        raise ctypes.WinError(ctypes.get_last_error())
    if basic.FileAttributes & (0x400 | 0x10) or basic.ChangeTime <= 0:
        raise ValueError('The selected input has no usable regular-file change time.')
    return basic.ChangeTime
