"""Real local synthesis/assembly for runtime invalidation and long text.

Only the reported version identities are controlled by the test. Speech,
sample counts, panning, publication and reused outputs are actual FFmpeg/Flite.
"""
from array import array
from dataclasses import replace
import math
from pathlib import Path
import shutil
import wave

import pytest

from carr_library.audio.contracts import parse_script, split_speech
from carr_library.audio.director import render_project, verify_production
from carr_library.audio.ffmpeg import AudioBackend
from carr_library.audio.project import ProjectStore, file_sha256, read_json
from carr_library.audio.voices import FFmpegFliteProvider, VoiceRegistry, VoiceUnavailable


class VersionedFlite(FFmpegFliteProvider):
    def __init__(self, executable):
        super().__init__(executable)
        self.generation = "A"
        self.calls = []

    def list_voices(self):
        return tuple(replace(voice, identity=voice.identity + ":review-version:" + self.generation)
                     for voice in super().list_voices())

    def synthesize(self, text, voice_id, output, **settings):
        result = super().synthesize(text, voice_id, output, **settings)
        self.calls.append((self.generation, text, result.frames))
        return result


class VersionedBackend(AudioBackend):
    generation = "A"

    @property
    def identity(self):
        return super().identity + ":review-version:" + self.generation


@pytest.fixture
def runtime():
    executable, probe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not executable or not probe:
        pytest.skip("Actual local FFmpeg and ffprobe are required for director runtime checks.")
    provider = VersionedFlite(Path(executable))
    try:
        voices = provider.list_voices()
    except VoiceUnavailable:
        pytest.skip("Actual local Flite speech is required for director runtime checks.")
    assert voices
    return provider, voices[0], VoiceRegistry((provider,)), VersionedBackend(executable, probe)


@pytest.mark.parametrize("changed_runtime", ["voice", "backend"])
def test_completed_production_follows_current_runtime_and_reuses_each_revision(
        runtime, tmp_path, changed_runtime):
    provider, voice, registry, backend = runtime
    project = parse_script("Narrator: A local cache review.\n", [voice],
                           project_id="runtime-review", stems=False)
    store = ProjectStore.create(tmp_path / "project", project)
    first = render_project(store, registry=registry, backend=backend)
    original_audio, original_manifest = file_sha256(first.master), file_sha256(first.manifest)
    first_metadata = verify_production(first.manifest.parent, backend=backend)
    assert len(provider.calls) == 1

    changing = provider if changed_runtime == "voice" else backend
    changing.generation = "B"
    second = render_project(store, registry=registry, backend=backend)
    second_metadata = verify_production(second.manifest.parent, backend=backend)
    assert second.created > 0 and second.master != first.master
    assert second_metadata["project_sha256"] == first_metadata["project_sha256"] == project.sha256
    assert second_metadata["backend"] == backend.identity
    assert second_metadata["voices"][0]["engine_identity"] == provider.list_voices()[0].identity
    expected_calls = 2 if changed_runtime == "voice" else 1
    assert len(provider.calls) == expected_calls

    same = render_project(ProjectStore(store.root), registry=registry, backend=backend)
    assert same.created == 0 and same.master == second.master
    assert len(provider.calls) == expected_calls
    changing.generation = "A"
    restored_runtime = render_project(store, registry=registry, backend=backend)
    assert restored_runtime.created == 0 and restored_runtime.master == first.master
    assert len(provider.calls) == expected_calls
    assert file_sha256(first.master) == original_audio and file_sha256(first.manifest) == original_manifest
    assert second.master.is_file() and store.load().project == project


@pytest.mark.parametrize("spaces", [1700, 4000])
def test_long_line_keeps_exact_source_and_pan_across_real_speech_chunks(runtime, tmp_path, spaces):
    provider, voice, registry, backend = runtime
    body = "Hello." + " " * spaces + "Across this boundary." + " " * spaces + "Goodbye."
    script = "Narrator: " + body + "\n"
    project = parse_script(script, [voice], project_id="long-text-review", stems=True)
    project = replace(project, roles=(replace(project.roles[0], pan=.5),),
                      scenes=(replace(project.scenes[0], pause_before=.125),)).validate()
    chunks = split_speech(body)
    assert len(body) > 1800 and "".join(chunks) == body
    spoken_chunks = tuple(chunk for chunk in chunks if chunk.strip())
    assert len(spoken_chunks) > 1
    if spaces == 4000:
        assert len(spoken_chunks) < len(chunks)
    store = ProjectStore.create(tmp_path / "project", project)
    first = render_project(store, registry=registry, backend=backend)
    proof = verify_production(first.manifest.parent, backend=backend)
    assert tuple(call[1] for call in provider.calls) == spoken_chunks
    line = project.scenes[0].lines[0]
    expected_frames = sum(call[2] for call in provider.calls)
    expected_frames += round(project.scenes[0].pause_before * project.sample_rate)
    expected_frames += round(line.pause_after * project.sample_rate)
    assert first.frames == proof["frames"] == expected_frames
    assert (first.manifest.parent / "script.txt").read_bytes() == script.encode("utf-8")
    timing, = read_json(first.manifest.parent / "timing.json")["lines"]
    assert timing["text"] == body
    assert script[timing["source_start"]:timing["source_end"]] == body
    assert store.load().project.source_text == script
    with wave.open(str(first.master), "rb") as audio:
        assert audio.getnchannels() == 2 and audio.getsampwidth() == 2
        values = array("h", audio.readframes(audio.getnframes()))
    left_energy = sum(value * value for value in values[0::2])
    right_energy = sum(value * value for value in values[1::2])
    # Equal-power mono pan +0.5 gives sqrt(3) right/left. Applying stereo
    # balance a second time would double this ratio and fails the assertion.
    assert left_energy > 0
    assert math.sqrt(right_energy / left_energy) == pytest.approx(math.sqrt(3), rel=.005)
    second = render_project(store, registry=registry, backend=backend)
    assert second.created == 0 and second.master == first.master
    assert len(provider.calls) == len(spoken_chunks)
