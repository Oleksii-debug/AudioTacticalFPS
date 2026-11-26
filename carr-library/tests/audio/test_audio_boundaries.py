"""Independent malformed-container and cross-platform path regressions."""
import json
import struct

import pytest

from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.model_manifest import ModelManifestError, local_path
from carr_library.audio.models import Sound
from carr_library.audio.production import load_plan, parse_script


def _rf64(path, *, sample_count=4, riff_size=None, sentinel=0xffffffff):
    data = b"\x01\x00" * 4
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    count = len(data) + 72 if riff_size is None else riff_size
    path.write_bytes(b"RF64" + struct.pack("<I", sentinel) + b"WAVEds64"
        + struct.pack("<IQQQI", 28, count, len(data), sample_count, 0)
        + b"fmt " + struct.pack("<I", 16) + fmt + b"data\xff\xff\xff\xff" + data)


@pytest.mark.parametrize("change", [{"sample_count": 7}, {"riff_size": 5000}, {"sentinel": 36}])
def test_rf64_rejects_inconsistent_declared_container_bounds(tmp_path, change):
    source = tmp_path / "inconsistent.wav"
    _rf64(source, **change)
    with pytest.raises(ValueError):
        wav_info(source)


def test_rf64_accepts_exact_metadata_without_allocating_large_buffers(tmp_path):
    source = tmp_path / "valid.wav"
    _rf64(source)
    info = wav_info(source)
    assert (info.frames, info.rate, info.channels) == (4, 8000, 1)


@pytest.mark.parametrize("name", ["CON.wav", "nul.WAV", "LPT1.wav", "COM\u00b9.wav", "CONIN$.wav",
                                  "clip.wav.", "clip.wav ", "clip?.wav", "clip|name.wav"])
def test_windows_device_and_alias_filenames_are_rejected_on_every_platform(tmp_path, name):
    with pytest.raises(ValueError):
        Sound("cue", name)
    with pytest.raises((ModelManifestError, ValueError)):
        local_path(tmp_path / name, must_exist=False)


@pytest.mark.parametrize("path", ["https://example.invalid/audio.wav", "//server/share/audio.wav",
                                  r"\\server\share\audio.wav", "audio.wav:hidden"])
def test_local_input_boundary_rejects_network_and_alternate_stream_paths(path):
    with pytest.raises((ModelManifestError, ValueError)):
        local_path(path, must_exist=False)


def test_production_plan_rejects_duplicate_json_fields(tmp_path):
    value = json.dumps(parse_script("Narrator: Exact source.").to_dict())
    source = tmp_path / "ambiguous.json"
    source.write_text(value[:-1] + ', "sample_rate": 8000}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_plan(source)
