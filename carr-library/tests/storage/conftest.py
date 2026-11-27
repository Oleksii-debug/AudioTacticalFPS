"""Explicit historical backup fixtures; production readers remain strict."""
import shutil

import pytest


@pytest.fixture
def legacy_layout():
    def without_pending(folder, manifest):
        if manifest['version'] == 5:
            assert manifest.pop('layout') == 'sidecars-pending-v1'
            del manifest['pending']
            shutil.rmtree(folder / 'pending')
            manifest['version'] = 4
        return manifest
    return without_pending
