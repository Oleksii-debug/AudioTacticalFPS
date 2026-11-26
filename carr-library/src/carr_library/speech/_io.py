"""Strict reading/playback file validation; executables use Audio Studio adapters."""
from pathlib import Path, PureWindowsPath
import os
import stat

from ..audio.engines import _stop as stop_process
from ..audio.model_manifest import local_path


def text_value(value, label='text', *, maximum=2_000_000, empty=False):
    if (not isinstance(value, str) or len(value) > maximum
            or (not empty and not value.strip()) or '\0' in value):
        raise ValueError(f'{label} must be valid text of at most {maximum} characters.')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError(f'{label} contains invalid Unicode.') from exc
    return value


def check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Speech cancelled; no output was published.')


def local_file(path, *, must_exist=True):
    result = local_path(path, must_exist=must_exist)
    name = result.name
    if (not name or name.rstrip(' .') != name or PureWindowsPath(name).is_reserved()
            or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name)):
        raise ValueError('Choose a portable local filename.')
    try:
        info = result.lstat()
    except FileNotFoundError:
        if must_exist:
            raise
    else:
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Choose a regular reading file without links or reparse points.')
    return result
