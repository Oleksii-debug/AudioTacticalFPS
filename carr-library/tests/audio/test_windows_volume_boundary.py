"""Mock only the Windows drive API; never claim an actual network-device test."""
import ctypes
import os
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import render, voices
from carr_library.audio.playback import LocalPlayer


@pytest.fixture
def drive(monkeypatch):
    class DriveType:
        value = 4
        calls = []
        def __call__(self, anchor):
            self.calls.append(anchor)
            return self.value
    function = DriveType()
    # Replacing this module's os reference avoids changing pathlib or pytest's
    # host platform. The exercised Win32 function boundary remains explicit.
    monkeypatch.setattr(render, 'os', SimpleNamespace(name='nt', PathLike=os.PathLike,
                                                    fspath=os.fspath, path=os.path))
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(kernel32=SimpleNamespace(
        GetDriveTypeW=function)), raising=False)
    return function


def source(tmp_path):
    path = tmp_path / 'original.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 24000, 24, 'NONE', 'uncompressed'))
        stream.writeframes(b'\x00\x00' * 24)
    return path


def test_mapped_volume_is_rejected_before_source_or_destination_metadata(tmp_path, drive, monkeypatch):
    original = source(tmp_path)
    before = original.read_bytes()
    untouched = tmp_path / 'keep.wav'
    untouched.write_bytes(b'Existing user output')
    def forbidden(*args, **kwargs):
        pytest.fail('A mapped audio path was accessed before volume rejection.')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'lstat', forbidden)
        for operation in (lambda: render.local_file(original),
                          lambda: render.output_path(tmp_path / 'new.wav'),
                          lambda: render.output_path(untouched),
                          lambda: voices._local_path(original)):
            with pytest.raises(ValueError, match='Mapped network'):
                operation()
    assert original.read_bytes() == before and untouched.read_bytes() == b'Existing user output'
    assert not (tmp_path / 'new.wav').exists()
    assert drive.argtypes == (ctypes.c_wchar_p,) and drive.restype is ctypes.c_uint
    assert drive.calls == [tmp_path.anchor] * 4


def test_conversion_synthesis_and_preview_stop_before_tools_or_output_mutation(tmp_path, drive, monkeypatch):
    original = source(tmp_path)
    before = original.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Rejected mapped paths must not reach engines or native devices.')
    monkeypatch.setattr(render, 'run_process', forbidden)
    monkeypatch.setattr(voices.VoiceRegistry, 'list_voices', forbidden)
    registry = voices.VoiceRegistry.__new__(voices.VoiceRegistry)
    player = LocalPlayer(_api=SimpleNamespace(device_count=forbidden, send=forbidden))
    for operation in (
        lambda: render.convert_audio(original, tmp_path / 'converted.wav'),
        lambda: registry.synthesize('Original words.', tmp_path / 'spoken.wav', voice_id='unreached'),
        lambda: player.open(original),
    ):
        with pytest.raises(ValueError, match='Mapped network'):
            operation()
    assert original.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ['original.wav']


def test_fixed_drive_preserves_existing_local_path_checks(tmp_path, drive):
    drive.value = 3  # DRIVE_FIXED; remaining regular-file/no-clobber checks still apply.
    original = source(tmp_path)
    assert render.local_file(original) == original.resolve()
    assert voices._local_path(original) == original.absolute()
    assert render.output_path(tmp_path / 'new.wav') == tmp_path / 'new.wav'
    with pytest.raises(FileExistsError):
        render.output_path(original)
