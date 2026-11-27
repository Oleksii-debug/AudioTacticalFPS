"""Actual POSIX package-registry links; never inferred as Windows evidence."""
import os
from pathlib import Path
import runpy
import time

import pytest

from carr_library.conversion import office
from carr_library.conversion.models import ConversionError

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='Explicit POSIX package registry layout')
_FIXTURE = runpy.run_path(str(Path(__file__).with_name('test_office_selection.py')))


def selected(tmp_path):
    executable = _FIXTURE['installed'](tmp_path)
    registry = executable.parent.parent / 'share' / 'registry'
    target = tmp_path / 'selected-system-registry'
    registry.rename(target)
    registry.symlink_to(target, target_is_directory=True)
    return executable, registry, target


def test_explicit_package_registry_target_is_hashed_and_bound_before_native_admission(tmp_path):
    executable, registry, target = selected(tmp_path)
    selection = office.OfficeSelection.capture(executable)
    snapshot = selection.snapshot()
    assert snapshot['registry_identity'][6] == str(target.resolve())
    assert snapshot['registry_identity'][5] == str(target)
    assert office._admit(snapshot, '.doc', time.monotonic() + 5) == executable
    assert registry.is_symlink()
    assert len(snapshot['files']) >= 6


@pytest.mark.parametrize('when', ['before-snapshot', 'after-snapshot'])
def test_equal_byte_retargeting_never_reuses_selected_registry_authority(tmp_path, when):
    executable, registry, target = selected(tmp_path)
    selection = office.OfficeSelection.capture(executable)
    snapshot = selection.snapshot()
    replacement = tmp_path / 'foreign-registry'
    replacement.mkdir()
    for path in target.iterdir():
        (replacement / path.name).write_bytes(path.read_bytes())
    registry.unlink()
    registry.symlink_to(replacement, target_is_directory=True)
    with pytest.raises(ConversionError, match='changed|inventory'):
        if when == 'before-snapshot':
            selection.snapshot()
        else:
            office._admit(snapshot, '.doc', time.monotonic() + 5)


def test_registry_target_cannot_contain_a_second_unselected_link(tmp_path):
    executable, registry, target = selected(tmp_path)
    nested = tmp_path / 'second-link'
    nested.symlink_to(target, target_is_directory=True)
    registry.unlink()
    registry.symlink_to(nested, target_is_directory=True)
    with pytest.raises(ValueError, match='links'):
        office.OfficeSelection.capture(executable)
