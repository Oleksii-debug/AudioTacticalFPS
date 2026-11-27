"""Consumer tests against the real shared speech runtime, never acoustic proof."""

import hashlib
import importlib
import json
from pathlib import Path
import sys
import types
import runpy
import wave

import pytest

from carr_library.integrations.dordiai_speech import DordiaiSpeechProvider, install_dordiai_provider
from carr_library.integrations.dordiai_voices import PCMChunk
# Load only the checked-in synthetic fixture; product module resolution is unchanged.
_fixture = runpy.run_path(str(Path(__file__).with_name('test_dordiai_voices.py')))
ACTOR, runtime = _fixture['ACTOR'], _fixture['runtime']


@pytest.fixture
def shared_speech():
    # Always exercise the selected source or installed product package.
    return importlib.import_module("carr_library.speech.runtime")


@pytest.fixture
def provider(runtime, shared_speech):
    item = DordiaiSpeechProvider(runtime, python_executable=str(Path(sys.executable).resolve()),
                                 expected_worker_sha256=hashlib.sha256(ACTOR.encode()).hexdigest())
    try:
        yield item
    finally:
        item.close()


def test_actual_registry_enumerates_only_ready_pairs(provider, shared_speech):
    registry = shared_speech.VoiceRegistry([provider])
    voices = registry.voices()
    assert len(voices) == 5
    assert sorted(voice.language for voice in voices) == ["en", "en", "ja", "sk", "uk"]
    assert all(voice.engine == "dordiai-voices" and voice.capabilities == ("rate", "volume", "pitch") for voice in voices)
    assert not registry.notices


def test_actual_registry_frames_and_publishes_24k_mono_pcm(provider, shared_speech, tmp_path):
    registry = shared_speech.VoiceRegistry([provider])
    voice = next(voice for voice in registry.voices() if voice.language == "uk")
    result = registry.synthesize("Synthetic consumer fixture.", voice.id, tmp_path / "result.wav",
                                 settings=shared_speech.SpeechSettings(rate=1, pitch=55, volume=80))
    assert (result.sample_rate, result.channels, result.frames, result.chunks) == (24000, 1, 6, 1)
    with wave.open(str(result.path), "rb") as audio:
        assert audio.getsampwidth() == 2 and audio.getnframes() == 6
        assert audio.readframes(6) == b"\x01\x00\x02\x00\x03\x00" * 2
    assert hashlib.sha256(result.path.read_bytes()).hexdigest() == result.sha256


def test_existing_output_and_unsupported_punctuation_are_preserved(provider, shared_speech, tmp_path):
    registry = shared_speech.VoiceRegistry([provider])
    voice = registry.voices()[0]
    target = tmp_path / "existing.wav"
    target.write_bytes(b"keep existing bytes")
    with pytest.raises(FileExistsError):
        registry.synthesize("Fixture.", voice.id, target)
    assert target.read_bytes() == b"keep existing bytes"
    with pytest.raises(ValueError, match="punctuation"):
        registry.synthesize("Fixture.", voice.id, tmp_path / "punctuation.wav",
                            settings=shared_speech.SpeechSettings(punctuation=True))
    assert not (tmp_path / "punctuation.wav").exists()


def test_audio_budget_failure_publishes_no_partial_file(provider, shared_speech, tmp_path, monkeypatch):
    registry = shared_speech.VoiceRegistry([provider])
    def excessive(*args, **kwargs):
        yield PCMChunk(b"\0\0" * 4096)
    monkeypatch.setattr(provider.adapter, "stream_pcm", excessive)
    target = tmp_path / "over-budget.wav"
    with pytest.raises(ValueError, match="audio budget"):
        registry.synthesize("Fixture.", registry.voices()[0].id, target,
                            settings=shared_speech.SpeechSettings(max_output_bytes=4096))
    assert not target.exists()
    assert not list(tmp_path.glob(".speech-*"))


def test_midstream_cancellation_publishes_no_partial_file(provider, shared_speech, tmp_path, monkeypatch):
    registry = shared_speech.VoiceRegistry([provider])
    state = {"cancelled": False}
    def interrupted(*args, **kwargs):
        yield PCMChunk(b"\x01\0" * 4)
        state["cancelled"] = True
        yield PCMChunk(b"\x01\0" * 4)
    monkeypatch.setattr(provider.adapter, "stream_pcm", interrupted)
    target = tmp_path / "cancelled.wav"
    with pytest.raises(InterruptedError):
        registry.synthesize("Fixture.", registry.voices()[0].id, target,
                            cancelled=lambda: state["cancelled"])
    assert not target.exists()
    assert not list(tmp_path.glob(".speech-*"))


def test_content_fingerprint_changes_with_model_bytes_not_just_catalog(provider, shared_speech, runtime):
    registry = shared_speech.VoiceRegistry([provider])
    voice = registry.voices()[0]
    resource = runtime / "models/Holos/synthetic-resource.bin"
    resource.write_bytes(b"first model bytes")
    before = provider.fingerprint(voice.id)
    cache_before = registry.fingerprint(voice.id)
    assert provider.fingerprint(voice.id) == before
    resource.write_bytes(b"other model bytes")
    assert provider.fingerprint(voice.id) != before
    assert registry.fingerprint(voice.id) != cache_before
    assert len(before) == 64


def test_changed_installation_during_render_is_not_published(provider, shared_speech, runtime, tmp_path, monkeypatch):
    registry = shared_speech.VoiceRegistry([provider])
    def changed(*args, **kwargs):
        yield PCMChunk(b"\x01\0" * 4)
        (runtime / "models/Holos/added-resource.bin").write_bytes(b"new resource")
    monkeypatch.setattr(provider.adapter, "stream_pcm", changed)
    target = tmp_path / "changed.wav"
    with pytest.raises(ValueError, match="installation changed"):
        registry.synthesize("Fixture.", registry.voices()[0].id, target)
    assert not target.exists()


def test_changed_base_assets_restart_warm_worker(provider, shared_speech, runtime, tmp_path):
    registry = shared_speech.VoiceRegistry([provider])
    voice = registry.voices()[0]
    registry.synthesize("First fixture.", voice.id, tmp_path / "first.wav")
    previous = provider.adapter._worker.process
    (runtime / "models/Holos/added-resource.bin").write_bytes(b"changed base asset")
    registry.synthesize("Second fixture.", voice.id, tmp_path / "second.wav")
    assert previous.poll() is not None
    assert provider.adapter._worker.process.pid != previous.pid


def test_disabled_configuration_does_not_probe_and_enabled_registers(shared_speech, runtime):
    registry = shared_speech.VoiceRegistry()
    assert install_dordiai_provider(registry, None) is None
    assert install_dordiai_provider(registry, {"enabled": False, "runtime_directory": "/missing"}) is None
    assert not registry.voices()
    provider = install_dordiai_provider(registry, {
        "enabled": True, "runtime_directory": str(runtime), "python_executable": str(Path(sys.executable).resolve()),
        "expected_worker_sha256": hashlib.sha256(ACTOR.encode()).hexdigest(),
    })
    try:
        assert len(registry.voices()) == 5
    finally:
        provider.close()


def test_discover_opt_in_hook_registers_same_provider(shared_speech, runtime, monkeypatch):
    monkeypatch.setattr(shared_speech.shutil, "which", lambda _: None)
    if hasattr(shared_speech, "NativeSapiProvider"):
        monkeypatch.setattr(shared_speech, "NativeSapiProvider", lambda: types.SimpleNamespace(
            name="Synthetic empty system-voice provider", voices=lambda: ()))
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: False if path.name == "powershell.exe" else original_is_file(path))
    registry = shared_speech.VoiceRegistry.discover(include_flite=False, dordiai={
        "enabled": True, "runtime_directory": str(runtime), "python_executable": str(Path(sys.executable).resolve()),
        "expected_worker_sha256": hashlib.sha256(ACTOR.encode()).hexdigest(),
    })
    try:
        assert len(registry.voices()) == 5
        assert isinstance(registry._providers[-1], DordiaiSpeechProvider)
    finally:
        registry._providers[-1].close()
