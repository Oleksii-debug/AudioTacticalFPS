from dataclasses import replace
from types import SimpleNamespace

import pytest

from carr_library.audio.contracts import MusicCue
from carr_library.ui.audio_studio import StudioSession

VOICE = SimpleNamespace(provider="flite", id="slt")


def session():
    result = StudioSession()
    result.voices = (VOICE,)
    return result


def test_verbatim_source_mode_does_not_interpret_document_instructions():
    studio = session()
    source = "# This is source text\nSomeone: These are source words.\n[pause 36000]"
    project = studio.prepare(source, interpret_speakers=False)
    assert len(project.scenes) == len(project.roles) == len(project.scenes[0].lines) == 1
    assert project.scenes[0].lines[0].text == source
    assert project.scenes[0].lines[0].pause_after == .3
    assert not project.interpret_speaker_syntax


def test_save_open_and_unavailable_voice_preserve_speaker_settings(tmp_path):
    studio = session()
    studio.prepare("Anna: Hello.\nBob: Good morning.")
    studio.assign("Anna", provider="flite", voice="rms", pan=-1, rate=1.1, volume=.8)
    studio.save(new_directory=tmp_path / "project")
    reopened = StudioSession()
    reopened.open(tmp_path / "project")
    reopened.prepare("Anna: Hello.\nBob: Good morning.")
    reopened.save()
    assert reopened.project.roles[0].voice == "rms"
    assert reopened.project.roles[0].pan == -1
    assert not reopened.dirty


def test_unchanged_script_save_preserves_per_scene_timing_and_music(tmp_path):
    studio = session()
    studio.prepare("# First\nA: One.\n# Second\nA: Two.")
    first, second = studio.project.scenes
    studio.project = replace(studio.project, scenes=(replace(first, music=MusicCue(mood="tense"), pause_before=4),
                                                    replace(second, music=MusicCue(mood="bright"))))
    studio.save(new_directory=tmp_path / "project")
    loaded = StudioSession()
    loaded.open(tmp_path / "project")
    loaded.prepare(loaded.project.source_text)
    loaded.save()
    assert loaded.project.scenes[0].pause_before == 4
    assert [s.music.mood for s in loaded.project.scenes] == ["tense", "bright"]


def test_failed_save_keeps_dirty_editor_project(tmp_path):
    studio = session()
    studio.prepare("Narrator: First version.")
    studio.save(new_directory=tmp_path / "project")
    other = StudioSession()
    other.open(tmp_path / "project")
    other.prepare("Narrator: External version.")
    other.save()
    studio.prepare("Narrator: My unsaved words.")
    with pytest.raises(ValueError, match="changed on disk"):
        studio.save()
    assert studio.dirty
    assert "My unsaved words" in studio.project.source_text
