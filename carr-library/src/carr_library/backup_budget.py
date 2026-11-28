"""Caller-selected limits for the existing common4 backup and restore path.

A backup manifest never raises these allowances. Omitted policy retains the
historical limits; larger backups require an explicit policy again at restore.
These are bounded operation resources, not source or execution authority.
"""
from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
import shutil
import time

from .durable_state import local_path, regular

MIB = 1024**2
GIB = 1024**3
SCHEMA = 'carr-backup-budget/1'


@dataclass(frozen=True)
class BackupBudget:
    database_bytes: int = 512*MIB
    original_bytes: int = 64*MIB
    assets: int = 10_000
    asset_bytes: int = 8*GIB
    journal_bytes: int = 512*MIB
    pending_files: int = 25_000
    pending_bytes: int = 8*GIB
    jobs: int = 10_000
    checkpoints: int = 100_000
    segments: int = 1000
    manifest_bytes: int = 8*MIB
    seconds: float = 60.0
    memory_bytes: int = GIB
    staging_bytes: int = 16*GIB

    def __post_init__(self):
        counts = {'assets', 'pending_files', 'jobs', 'checkpoints', 'segments'}
        for name, value in asdict(self).items():
            if name == 'seconds':
                continue
            maximum = 2**31-1 if name in counts else 2**63-1
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError(name + ' must be a positive bounded integer.')
        if (type(self.seconds) not in (int, float) or not math.isfinite(self.seconds)
                or not .1 <= self.seconds <= 86400):
            raise ValueError('Choose a finite backup work allowance between 0.1 and 86400 seconds.')
        # The archive has one finite JSON manifest. Large media and checkpoint
        # payloads are streamed separately. This remains a decoder safety cap.
        if self.manifest_bytes > 256*MIB:
            raise ValueError('One backup manifest cannot exceed the bounded 256 MiB decoder.')
        if self.memory_bytes < 512*MIB + 8*self.manifest_bytes:
            raise ValueError('Declared memory does not cover the manifest and bounded record decoders.')
        object.__setattr__(self, 'seconds', float(self.seconds))

    def to_dict(self):
        return dict(schema=SCHEMA, **asdict(self))

    @classmethod
    def from_dict(cls, value):
        fields = set(cls().to_dict())
        if type(value) is not dict or set(value) != fields or value['schema'] != SCHEMA:
            raise ValueError('Choose an exact typed backup resource policy.')
        result = cls(**{key: item for key, item in value.items() if key != 'schema'})
        if result.to_dict() != value:
            raise ValueError('Noncanonical backup resource policy.')
        return result


def checked_budget(value):
    if value is not None and type(value) is not BackupBudget:
        raise TypeError('An exact reviewed BackupBudget is required.')
    return value


def effective_budget(value=None):
    return checked_budget(value) or BackupBudget()


def source_limit(mime, value=None):
    maximum = effective_budget(value).original_bytes
    return maximum if mime in ('text/plain', 'application/pdf') else min(maximum, 64*MIB)


def bounded_cancel(cancelled, selected):
    """Return one operation deadline; nested producers must reuse the callback."""
    if not callable(cancelled):
        raise TypeError('A backup cancellation callback is required.')
    checked_budget(selected)
    if selected is None:
        return cancelled
    deadline = time.monotonic() + selected.seconds
    def stopped():
        if cancelled():
            return True
        if time.monotonic() >= deadline:
            raise TimeoutError('Backup exceeded the reviewed work allowance; no new completion is implied.')
        return False
    return stopped


def available_memory():
    """Read current host availability; this is preflight, not an OS memory quota."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in ('ullTotalPhys', 'ullAvailPhys',
                'ullTotalPageFile', 'ullAvailPageFile', 'ullTotalVirtual',
                'ullAvailVirtual', 'ullAvailExtendedVirtual')]
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
        kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(status.ullAvailPhys)
    # sysconf is a bounded kernel observation and does not parse a source file.
    try:
        return int(os.sysconf('SC_AVPHYS_PAGES')) * int(os.sysconf('SC_PAGE_SIZE'))
    except (AttributeError, OSError, ValueError) as exc:
        raise ValueError('Current available memory cannot be measured for the selected backup policy.') from exc


def preflight(directory, selected, *, expected_bytes=None):
    """Require current free RAM/disk before explicit larger operations.

    Staging and the create-only final folder use hard links on this same volume.
    Restore additionally keeps one verified pending copy while checking bindings.
    The chosen staging bound covers that temporary work; it is never read from
    an archive as authorization.
    """
    checked_budget(selected)
    if selected is None:
        return
    directory = local_path(directory, exists=True)
    regular(directory, directory=True)
    if expected_bytes is not None and (type(expected_bytes) is not int
            or expected_bytes < 0 or expected_bytes > selected.staging_bytes):
        raise ValueError('Backup exceeds the reviewed staging-byte allowance.')
    if available_memory() < selected.memory_bytes:
        raise ValueError('Insufficient current free memory for the reviewed backup allowance.')
    if shutil.disk_usage(directory).free < selected.staging_bytes + 64*MIB:
        raise ValueError('Insufficient current free disk space for the reviewed backup staging allowance.')


def check_metadata_memory(raw_bytes, records, selected):
    """Conservative known-schema metadata accounting, separate from byte authority."""
    if selected is None:
        return
    policy = effective_budget(selected)
    if (type(raw_bytes) is not int or raw_bytes < 0 or type(records) is not int or records < 0
            or 512*MIB + 8*raw_bytes + 1024*records > policy.memory_bytes):
        raise ValueError('Pending metadata exceeds the reviewed memory allowance.')
