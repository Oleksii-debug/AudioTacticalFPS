"""Explicit finite resource allowances for the existing complete-backup wire formats.

The selected profile is a caller decision, never authority taken from a manifest.
These are safety allowances, not measured release-capacity or throughput claims.
Per-source intake and typed sidecar producer limits remain unchanged.
"""

MIB = 1024**2
GIB = 1024**3
LARGE_SOURCE_PROFILE = 'large-source-v1'

_STANDARD = dict(
    database=512 * MIB, journal=512 * MIB, assets=8 * GIB,
    pending=8 * GIB, manifest=8 * MIB, asset_count=10000,
    jobs=10000, files=25000, checkpoints=100000, journal_seconds=60,
)
_LARGE = dict(
    database=64 * GIB, journal=32 * GIB, assets=128 * GIB,
    pending=64 * GIB, manifest=64 * MIB, asset_count=100000,
    jobs=100000, files=250000, checkpoints=2097152, journal_seconds=3600,
)


def capacity(value=None):
    """Return an independent allowance; ordinary limits are unchanged."""
    if value is None:
        return dict(_STANDARD)
    if type(value) is not str or value != LARGE_SOURCE_PROFILE:
        raise ValueError('Choose the explicit large-source-v1 backup profile or the ordinary profile.')
    return dict(_LARGE)
