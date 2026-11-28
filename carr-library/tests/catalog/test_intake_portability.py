"""The intake path uses the same portable policy as document containers."""
from pathlib import PureWindowsPath
import ntpath

import pytest

from carr_library.catalog.intake import IntakeError, snapshot


@pytest.fixture(params=['runtime', 'legacy-reserved-only'])
def windows_name_api(request, monkeypatch):
    # Python 3.11/3.12's pathlib fallback recognizes DOS devices, not all
    # invalid filename characters. For these non-device fixtures emulate
    # that narrower API on current Python; this is not a legacy runtime test.
    if request.param == 'legacy-reserved-only':
        monkeypatch.delattr(ntpath, 'isreserved', raising=False)
        monkeypatch.setattr(PureWindowsPath, 'is_reserved', lambda self: False)


@pytest.mark.parametrize('character', ['<', '>', '"', '|', '?', '*', '\x01', '\n'])
def test_invalid_non_device_source_name_rejected_before_io(tmp_path, windows_name_api, character):
    # No invalid path is created; reject before attempting filesystem access.
    with pytest.raises(IntakeError, match='unsafe relative path'):
        snapshot(tmp_path, f'unsafe{character}.txt')
