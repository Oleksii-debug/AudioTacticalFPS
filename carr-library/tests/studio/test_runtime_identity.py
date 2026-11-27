"""Byte-bound production cache identity without executing fixture binaries."""
from types import SimpleNamespace

import pytest

from carr_library.studio.render import production_runtime_identity


@pytest.mark.parametrize('name', ['ffmpeg', 'ffprobe'])
def test_same_reported_version_does_not_reuse_changed_runtime_bytes(tmp_path, name):
    binaries = {}
    for key in ('ffmpeg', 'ffprobe'):
        path = tmp_path / (key + '.exe')
        path.write_bytes(b'identity-fixture-A; never executed')
        binaries[key] = path
    runtime = SimpleNamespace(**binaries)
    report = {'version': 'identical fixture banner', 'filters': ['amix']}
    first = production_runtime_identity(runtime, report)
    binaries[name].write_bytes(b'identity-fixture-B; never executed')
    assert production_runtime_identity(runtime, report) != first


def test_production_identity_cancelled_before_opening_runtime_assets(tmp_path):
    runtime = SimpleNamespace(ffmpeg=tmp_path / 'absent-ffmpeg', ffprobe=tmp_path / 'absent-ffprobe')
    with pytest.raises(InterruptedError):
        production_runtime_identity(runtime, {}, cancelled=lambda: True)
