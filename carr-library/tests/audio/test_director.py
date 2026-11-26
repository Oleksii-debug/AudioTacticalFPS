from dataclasses import replace
from pathlib import Path
import shutil
import socket

import pytest

from carr_library.audio.contracts import parse_script
from carr_library.audio.director import render_project, verify_production
from carr_library.audio.ffmpeg import AudioBackend
from carr_library.audio.project import ProjectStore
from carr_library.audio.voices import FFmpegFliteProvider, VoiceRegistry


def runtime():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Actual local FFmpeg is required.")
    backend = AudioBackend()
    provider = FFmpegFliteProvider(Path(backend.ffmpeg))
    try:
        voices = provider.list_voices()
    except RuntimeError:
        pytest.skip("Flite is unavailable in this FFmpeg build.")
    if not voices:
        pytest.skip("No Flite voice is available.")
    return backend, VoiceRegistry((provider,)), voices


def test_many_lines_use_streamed_concatenation_without_input_fanout_limit(tmp_path, monkeypatch):
    backend, registry, voices = runtime()
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Network forbidden")))
    text = "Narrator: Hello.\n" * 130
    plan = parse_script(text, voices[:1], stems=False)
    store = ProjectStore.create(tmp_path / "many-lines", plan)
    result = render_project(store, backend=backend, registry=registry)
    manifest = verify_production(result.manifest.parent, backend=backend)
    assert result.created < 10
    assert result.reused >= 129
    assert manifest["frames"] == result.frames
    assert store.load().project.source_text == text


def test_role_named_music_gets_its_own_audible_voice_stem(tmp_path):
    backend, registry, voices = runtime()
    plan = parse_script("Music: This role is a person.", voices[:1])
    role = replace(plan.roles[0], id="music")
    scene = replace(plan.scenes[0], lines=(replace(plan.scenes[0].lines[0], role="music"),))
    plan = replace(plan, roles=(role,), scenes=(scene,)).validate()
    result = render_project(ProjectStore.create(tmp_path / "role-music", plan), backend=backend, registry=registry)
    stem = result.manifest.parent / "stem-music.wav"
    assert backend.measure(stem)["integrated_lufs"] is not None
    assert not (result.manifest.parent / "stem-__background_music__.wav").exists()
