"""Resolve the same explicitly selected private data directory as the desktop."""
from ..data_paths import guard_data_directory


def data_directory(value=None):
    if value is None:
        from ..preferences import selected_directory
        from platformdirs import user_data_path
        value = selected_directory() or user_data_path('Universal Research Library',
            appauthor=False, roaming=False)
    return guard_data_directory(value)
