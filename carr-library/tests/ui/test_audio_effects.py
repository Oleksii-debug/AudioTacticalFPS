"""Typed effect UI controller against actual FFmpeg and real local files.

Native wx focus and control dispatch are a separate Windows smoke gate. These
portable checks do not substitute control doubles for a native GUI result.
"""
from array import array
import json
import math
import shutil
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio.effect_controls import EffectsBackend
from carr_library.audio.render import file_sha256, probe_audio
from carr_library.ui.audio_effects import EffectsController, entry_label


def finish(thread):
    thread.join(timeout=15)
    assert not thread.is_alive(), "Local audio worker did not finish within the test budget."


@pytest.fixture(scope="module")
def runtime():
    executable, probe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not executable or not probe:
        pytest.skip("Actual local FFmpeg and ffprobe are required for effects controller verification.")
    return executable, probe


@pytest.fixture
def controller(runtime):
    events = []
    model = EffectsController(ffmpeg=runtime[0], ffprobe=runtime[1], on_change=events.append)
    finish(model.initialize())
    assert model.state == "ready", model.message
    model.events = events
    yield model
    model.close()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "Original source.wav"
    rate = 24000
    values = array("h", (int(6000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate)))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(values.tobytes())
    return path


def test_real_runtime_schema_and_ordered_enabled_chain(controller):
    assert controller.schema["loudness"]["parameters"]["integrated_lufs"]["unit"] == "LUFS"
    assert {"flac", "wav"} <= set(controller.formats)
    controller.preset("Narration")
    assert [r.effect.kind for r in controller.rows] == ["highpass", "compressor", "limiter"]
    assert controller.move(0, 1) == 1
    assert [r.effect.kind for r in controller.rows] == ["compressor", "highpass", "limiter"]
    controller.update(1, {"frequency_hz": 100})
    controller.toggle(1)
    assert not controller.rows[1].enabled
    label = entry_label(2, controller.rows[1], controller.schema)
    assert "Bypassed" in label and "100 Hz" in label
    assert controller.remove(1) == 1
    assert [r.effect.kind for r in controller.rows] == ["compressor", "limiter"]


@pytest.mark.parametrize("name,parameters", [
    ("unknown", {}), ("gain", {"gain_db": True}), ("gain", {"gain_db": float("nan")}),
    ("gain", {"gain_db": float("inf")}), ("gain", {"gain_db": 25}),
    ("gain", {"gain_db": "1;amovie=https://example.invalid"}),
    ("gain", {"filter": "volume=1"}), ("gain", []),
])
def test_parameters_fail_closed_before_editing_the_chain(controller, name, parameters):
    controller.preset("Echo")
    before = controller.rows
    with pytest.raises(ValueError):
        controller.add(name, parameters)
    assert controller.rows is before


def test_preset_json_roundtrip_is_typed_atomic_and_no_clobber(controller, tmp_path):
    controller.preset("Noise reduction")
    controller.toggle(1)
    original = controller.rows
    path = tmp_path / "preset.json"
    controller.save_preset(path)
    checksum = file_sha256(path)
    controller.preset("Echo")
    controller.load_preset(path)
    assert controller.rows == original
    with pytest.raises(FileExistsError):
        controller.save_preset(path)
    assert file_sha256(path) == checksum
    data = json.loads(path.read_text())
    data["effects"][0]["parameters"]["reduction_db"] = "12,amovie=private.txt"
    bad = tmp_path / "invalid.json"
    bad.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        controller.load_preset(bad)
    assert controller.rows == original
    bad.write_text('{"schema_version":1,"schema_version":1,"kind":"carr-audio-effects","effects":[]}')
    with pytest.raises(ValueError):
        controller.load_preset(bad)
    assert controller.rows == original


def test_unavailable_preset_and_oversized_chain_do_not_partially_replace(controller):
    controller.preset("Echo")
    before = controller.rows
    controller.schema["gate"]["available"] = False
    with pytest.raises(ValueError, match="does not provide"):
        controller.preset("Noise reduction")
    assert controller.rows is before
    for _ in range(23):
        controller.add("gain")
    assert len(controller.rows) == 24
    with pytest.raises(ValueError, match="24"):
        controller.add("gain")
    assert len(controller.rows) == 24


@pytest.mark.parametrize("preset,format,rate", [
    ("Narration", "flac", 24000), ("Noise reduction", "wav", 44100),
    ("Echo", "wav", 24000), ("Loudness target", "flac", 48000),
])
def test_actual_effect_presets_convert_verified_new_files_and_preserve_source(
        controller, source, runtime, tmp_path, preset, format, rate):
    original = file_sha256(source)
    controller.preset(preset)
    destination = tmp_path / (preset.replace(" ", "-") + "." + format)
    finish(controller.convert(source, destination, format=format, sample_rate=rate))
    assert controller.state == "complete", controller.message
    assert controller.result.path == destination
    info = probe_audio(destination, ffprobe=runtime[1])
    assert info.sample_rate == rate and info.channels == 2 and info.frames > 0
    assert file_sha256(source) == original
    assert "Saved and verified" in controller.message and "source file is unchanged" in controller.message
    assert [e["state"] for e in controller.events][-2:] == ["running", "complete"]


def test_bypassed_chain_does_not_reach_actual_audio_filtering(controller, source, tmp_path):
    plain, bypassed = tmp_path / "plain.wav", tmp_path / "bypassed.wav"
    finish(controller.convert(source, plain, format="wav", sample_rate=24000))
    assert controller.state == "complete", controller.message
    controller.add("gain", {"gain_db": -20})
    controller.toggle(0)
    finish(controller.convert(source, bypassed, format="wav", sample_rate=24000))
    assert controller.state == "complete", controller.message
    assert plain.read_bytes() == bypassed.read_bytes()


def test_existing_output_and_invalid_clock_report_errors_without_overwriting(controller, source, tmp_path):
    output = tmp_path / "keep.wav"
    output.write_bytes(b"Keep this exact user-owned destination")
    original, target = file_sha256(source), file_sha256(output)
    finish(controller.convert(source, output, format="wav", sample_rate=24000))
    assert controller.state == "error" and "already exists" in controller.message
    assert controller.result is None and file_sha256(output) == target and file_sha256(source) == original
    controller.add("lowpass", {"frequency_hz": 12000})
    with pytest.raises(ValueError, match="below half"):
        controller.convert(source, tmp_path / "new.wav", format="wav", sample_rate=24000)
    assert not (tmp_path / "new.wav").exists()


def test_async_cancel_interrupts_real_backend_without_a_completed_file(runtime, source, tmp_path):
    entered, release = threading.Event(), threading.Event()
    events = []
    def factory(rate):
        backend = EffectsBackend(*runtime, sample_rate=rate)
        def convert(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return backend.convert(*args, **kwargs)
        return SimpleNamespace(effect_schema=backend.effect_schema, capabilities=backend.capabilities, convert=convert)
    controller = EffectsController(backend_factory=factory, on_change=events.append)
    finish(controller.initialize())
    output = tmp_path / "cancelled.wav"
    worker = controller.convert(source, output, format="wav", sample_rate=24000)
    assert entered.wait(5)
    with pytest.raises(ValueError):
        controller.add("gain")
    controller.cancel()
    release.set()
    finish(worker)
    assert controller.state == "cancelled" and not output.exists()
    assert controller.result is None and events[-1]["state"] == "cancelled"
    controller.close()


def test_dialog_disposal_ignores_queued_completion_callbacks(runtime, source, tmp_path):
    queued, events = [], []
    controller = EffectsController(ffmpeg=runtime[0], ffprobe=runtime[1],
        dispatch=queued.append, on_change=events.append)
    finish(controller.initialize())
    queued.pop(0)()
    assert controller.state == "ready"
    output = tmp_path / "already-published.wav"
    finish(controller.convert(source, output, format="wav", sample_rate=24000))
    assert output.is_file() and len(queued) == 1
    before = list(events)
    controller.close()
    queued.pop(0)()
    assert events == before and controller.result is None
    assert output.is_file(), "Closing a dialog must not delete an already published file."


def test_runtime_discovery_failure_is_visible_and_does_not_leave_controls_busy():
    events = []
    def unavailable(rate):
        raise RuntimeError("The selected local audio runtime could not be opened.")
    controller = EffectsController(backend_factory=unavailable, on_change=events.append)
    finish(controller.initialize())
    assert controller.state == "error" and not controller.busy
    assert "could not be opened" in controller.message
    assert events[-1]["state"] == "error" and not events[-1]["busy"]
    with pytest.raises(ValueError, match="not ready"):
        controller.add("gain")
    controller.close()


def test_reordered_chain_changes_actual_output(controller, source, tmp_path):
    controller.add("gain", {"gain_db": 12})
    controller.add("limiter", {"ceiling_db": -20})
    first, second = tmp_path / "gain-first.wav", tmp_path / "limiter-first.wav"
    finish(controller.convert(source, first, format="wav", sample_rate=24000))
    assert controller.state == "complete", controller.message
    controller.move(1, -1)
    finish(controller.convert(source, second, format="wav", sample_rate=24000))
    assert controller.state == "complete", controller.message
    def energy(path):
        with wave.open(str(path), "rb") as audio:
            samples = array("h", audio.readframes(audio.getnframes()))
        return sum(value * value for value in samples)
    assert 0 < energy(first) < energy(second)


def test_output_choice_extension_and_opus_clock_are_explicit(controller, source, tmp_path):
    with pytest.raises(ValueError, match="extension"):
        controller.convert(source, tmp_path / "wrong.mp3", format="wav", sample_rate=24000)
    if "opus" in controller.formats:
        with pytest.raises(ValueError, match="48000"):
            controller.convert(source, tmp_path / "wrong.opus", format="opus", sample_rate=44100)
    assert not list(tmp_path.glob("wrong.*"))


def test_parameterless_stages_remain_typed_and_editable(controller):
    for name in ("declick", "declip"):
        if not controller.schema[name]["available"]:
            continue
        index = controller.add(name)
        controller.update(index, {})
        controller.toggle(index)
        assert not controller.rows[index].enabled
        assert dict(controller.rows[index].effect.parameters) == {}
        assert "Bypassed" in entry_label(index + 1, controller.rows[index], controller.schema)


def test_oversized_or_malformed_preset_leaves_current_chain_intact(controller, tmp_path):
    controller.preset("Narration")
    before = controller.rows
    path = tmp_path / "rejected.json"
    data = {"schema_version": 1, "kind": "carr-audio-effects", "effects": [
        {"kind": "gain", "enabled": True, "parameters": {}}] * 25}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="24"):
        controller.load_preset(path)
    assert controller.rows is before
    data["effects"] = [{"kind": "gain", "enabled": 1, "parameters": {}}]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        controller.load_preset(path)
    assert controller.rows is before


def test_huge_integer_in_later_preset_stage_fails_transactionally(controller, tmp_path):
    controller.preset("Narration")
    before = controller.rows
    path = tmp_path / "huge-number.json"
    path.write_text(json.dumps({"schema_version": 1, "kind": "carr-audio-effects", "effects": [
        {"kind": "gain", "enabled": True, "parameters": {"gain_db": -6}},
        {"kind": "gain", "enabled": True, "parameters": {"gain_db": 10**400}},
    ]}), encoding="utf-8")
    assert path.stat().st_size < 64 * 1024
    with pytest.raises(ValueError, match="finite number"):
        controller.load_preset(path)
    assert controller.rows is before


@pytest.mark.parametrize("flags", ["..", "TS", "...", "TSC"])
def test_native_filter_inventory_accepts_current_and_legacy_columns(monkeypatch, flags):
    """FFmpeg 9.1 uses two flags; the supported Linux build uses three."""
    from carr_library.audio import render
    monkeypatch.setattr(render, "tool_path", lambda name, configured=None: name)
    inventories = {
        "-filters": "Filters:\n  T.. = Timeline support\n  .S. = Slice threading\n" +
            "\n".join(f" {flags} {name:17s} A->A Local filter"
                      for name in ("aresample", "alimiter", "volumedetect")),
        "-encoders": "Encoders:\n A..... pcm_s16le PCM\n A..... flac FLAC\n",
        "-muxers": "Muxers:\n  E  wav WAV\n  E  flac FLAC\n",
        "-version": "Local ffprobe inventory fixture\n",
    }
    monkeypatch.setattr(render, "run_process",
                        lambda args, **kwargs: (inventories[args[-1]], ""))
    backend = EffectsBackend()
    assert set(backend.capabilities()["formats"]) == {"wav", "flac"}
    assert backend.effect_schema()["limiter"]["available"]
    assert not backend.effect_schema()["loudness"]["available"]


@pytest.mark.parametrize("flags", ["..", "..."])
def test_filter_inventory_still_rejects_missing_verification_filter(monkeypatch, flags):
    from carr_library.audio import render
    monkeypatch.setattr(render, "tool_path", lambda name, configured=None: name)
    inventories = {
        "-filters": "\n".join(f" {flags} {name:17s} A->A Local filter"
                             for name in ("aresample", "alimiter")),
        "-encoders": " A..... pcm_s16le PCM\n",
        "-muxers": "  E  wav WAV\n",
        "-version": "Local ffprobe inventory fixture\n",
    }
    monkeypatch.setattr(render, "run_process",
                        lambda args, **kwargs: (inventories[args[-1]], ""))
    with pytest.raises(RuntimeError, match="volumedetect"):
        EffectsBackend().capabilities()
