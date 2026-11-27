"""Bounded, read-only CF_UNICODETEXT snapshots without newline conversion.

wxPython4.2.4 pins wxWidgets896e4f587615b832ce27b8325357cb504997e1d3.
Its wxTextDataObject::SetData translates native newlines to Unix newlines.
The explicit Windows reader therefore copies the native Unicode format under
its own OpenClipboard/GlobalLock lifetime and never frees the borrowed handle.
"""
import sys

from ._io import text_value

MAX_TEXT_CHARACTERS = 2_000_000
# Include bounded allocator padding; the decoded character limit stays exact.
MAX_UNICODE_BYTES = 4 * MAX_TEXT_CHARACTERS + 4096
CF_UNICODETEXT = 13


def decode_unicode_text(raw):
    """Decode only the aligned NUL-terminated UTF-16LE string, preserving bytes."""
    if type(raw) is not bytes or not 2 <= len(raw) <= MAX_UNICODE_BYTES:
        raise ValueError('Clipboard Unicode storage exceeds its byte allowance.')
    ending, offset = -1, 0
    while offset < len(raw):
        found = raw.find(b'\0\0', offset)
        if found < 0:
            break
        if found % 2 == 0:
            ending = found
            break
        offset = found + 1
    if ending < 0:
        raise ValueError('The clipboard Unicode text is not NUL-terminated.')
    try:
        text = raw[:ending].decode('utf-16-le', errors='strict')
    except UnicodeError as exc:
        raise ValueError('The clipboard contains invalid Unicode.') from exc
    return text_value(text, 'Clipboard reading text', maximum=MAX_TEXT_CHARACTERS)


def _windows_api():
    if sys.platform != 'win32':
        raise RuntimeError('Native Unicode clipboard capture requires Windows.')
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    user = ctypes.WinDLL('user32', use_last_error=True, winmode=0x00000800)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True, winmode=0x00000800)
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.OpenClipboard.restype = wintypes.BOOL
    user.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user.GetClipboardData.argtypes = [wintypes.UINT]
    user.GetClipboardData.restype = wintypes.HANDLE
    user.CloseClipboard.argtypes = []
    user.CloseClipboard.restype = wintypes.BOOL
    kernel.GlobalSize.argtypes = [wintypes.HANDLE]
    kernel.GlobalSize.restype = ctypes.c_size_t
    kernel.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel.GlobalUnlock.restype = wintypes.BOOL
    return SimpleNamespace(open=user.OpenClipboard, available=user.IsClipboardFormatAvailable,
        get=user.GetClipboardData, close=user.CloseClipboard, size=kernel.GlobalSize,
        lock=kernel.GlobalLock, unlock=kernel.GlobalUnlock, copy=ctypes.string_at,
        clear_error=lambda: ctypes.set_last_error(0), error=ctypes.get_last_error)


def capture_windows_text(*, _api=None):
    """Copy one explicit snapshot; release native ownership before validation."""
    api = _windows_api() if _api is None else _api
    if not api.open(None):
        raise ValueError('The clipboard is busy. Try again when the other application releases it.')
    try:
        if not api.available(CF_UNICODETEXT):
            raise ValueError('The clipboard does not contain Unicode text to read.')
        handle = api.get(CF_UNICODETEXT)
        if not handle:
            raise ValueError('The clipboard text could not be read. Copy the text again and retry.')
        size = api.size(handle)
        if type(size) is not int or not 2 <= size <= MAX_UNICODE_BYTES:
            raise ValueError('Clipboard Unicode storage exceeds its byte allowance.')
        pointer = api.lock(handle)
        if not pointer:
            raise ValueError('The clipboard Unicode storage could not be locked for reading.')
        try:
            raw = api.copy(pointer, size)
        finally:
            api.clear_error()
            if not api.unlock(handle) and api.error():
                raise OSError('The clipboard Unicode read lock did not release.')
    finally:
        if not api.close():
            raise OSError('The native clipboard did not confirm closure.')
    return decode_unicode_text(raw)
