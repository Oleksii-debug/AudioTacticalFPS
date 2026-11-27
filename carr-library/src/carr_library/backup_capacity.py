"""Named, finite complete-backup budgets. No default limit is widened."""

MIB = 1024**2
GIB = 1024**3
LARGE_SOURCE_PROFILE = 'large-source-v1'

_STANDARD = dict(database=512 * MIB, journal=512 * MIB, assets=8 * GIB,
                 pending=8 * GIB, manifest=8 * MIB, asset_count=10000,
                 checkpoints=100000)
_LARGE = dict(database=64 * GIB, journal=32 * GIB, assets=128 * GIB,
              pending=64 * GIB, manifest=64 * MIB, asset_count=100000,
              checkpoints=1048576)


def capacity(value=None):
    if value is None:
        return dict(_STANDARD)
    if type(value) is not str or value != LARGE_SOURCE_PROFILE:
        raise ValueError('Choose the explicit large-source-v1 backup profile or the ordinary profile.')
    return dict(_LARGE)


def manifest_capacity(manifest):
    if type(manifest) is not dict:
        raise ValueError('Invalid complete-backup capacity identity.')
    if manifest.get('version') == 6:
        if manifest.get('capacity_profile') != LARGE_SOURCE_PROFILE:
            raise ValueError('A version 6 backup needs the exact named capacity profile.')
        return capacity(LARGE_SOURCE_PROFILE)
    if 'capacity_profile' in manifest:
        raise ValueError('A legacy backup cannot acquire a larger capacity profile.')
    return capacity()


def pending_capacity(metadata):
    if type(metadata) is not dict:
        raise ValueError('Invalid pending-backup capacity identity.')
    if metadata.get('version') == 4:
        if metadata.get('capacity_profile') != LARGE_SOURCE_PROFILE:
            raise ValueError('A version 4 pending component needs the exact named capacity profile.')
        return capacity(LARGE_SOURCE_PROFILE)
    if 'capacity_profile' in metadata:
        raise ValueError('A legacy pending component cannot acquire a larger capacity profile.')
    return capacity()
