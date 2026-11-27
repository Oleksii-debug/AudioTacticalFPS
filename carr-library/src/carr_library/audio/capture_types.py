"""Bounded native-capture values retained from the existing PR149 recorder.

No value in this module is a shell command, FFmpeg filter expression, URL, or
model executable.  Numeric controls deliberately describe capabilities which
the runtime can validate, rather than accepting arbitrary vendor arguments.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


class AudioError(RuntimeError):
    """A local audio operation failed without publishing an output."""


class AudioCancelled(AudioError):
    """An operation was cancelled; previous completed outputs are preserved."""


class AudioCleanupPending(AudioError):
    """A child is still owned; retain admission until cleanup_owner is quiescent."""

    def __init__(self, message, *, cleanup_owner):
        super().__init__(message)
        self.cleanup_owner = cleanup_owner


class AudioUnavailable(AudioError):
    """An optional installed engine or verified capability is unavailable."""


def finite(value, label, minimum=None, maximum=None):
    if type(value) not in (int, float):
        raise ValueError(f'{label} must be a finite number.')
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f'{label} must be a finite number.') from None
    if not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number.')
    if minimum is not None and value < minimum:
        raise ValueError(f'{label} must be at least {minimum}.')
    if maximum is not None and value > maximum:
        raise ValueError(f'{label} must be at most {maximum}.')
    return float(value)


def integer(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{label} must be an integer from {minimum} to {maximum}.')
    return value


@dataclass(frozen=True)
class AudioBudget:
    """Explicit local resource allowances, independent of project duration.

    Output bytes include staging files.  Increase the budget intentionally for
    larger jobs; there is no ten-minute (or similar) project-duration ceiling.
    The FFmpeg per-allocation limit is not a whole-process memory sandbox.
    """
    timeout: float = 3600
    max_output_bytes: int = 32 * 1024**3
    max_log_bytes: int = 2 * 1024**2
    reserve_disk_bytes: int = 32 * 1024**2
    max_inputs: int = 64
    threads: int = 2
    max_alloc_bytes: int = 256 * 1024**2
    max_staging_files: int = 10000

    def __post_init__(self):
        finite(self.timeout, 'Timeout', .05, 365 * 86400)
        integer(self.max_output_bytes, 'Output budget', 4096, 2**63 - 1)
        integer(self.max_log_bytes, 'Log budget', 1024, 128 * 1024**2)
        integer(self.reserve_disk_bytes, 'Free disk reserve', 0, 2**63 - 1)
        integer(self.max_inputs, 'Simultaneous inputs', 1, 1024)
        integer(self.threads, 'Worker threads', 1, 64)
        integer(self.max_alloc_bytes, 'Allocation budget', 1024**2, 2**31 - 1)
        integer(self.max_staging_files, 'Staging file budget', 1, 1000000)


