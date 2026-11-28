"""Both published APIs render one real score and keep their distinct boundaries.

These tests use actual FFmpeg output. Optional model fixtures validate manifests
only and never stand in for MusicGen or ACE-Step inference acceptance.
"""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import wave

import pytest

from carr_library.audio import AudioBudget, AudioError, AudioRuntime, MusicSpec as RuntimeScore
from carr_library.audio.music import (MusicSpec, export_midi, generate_music, render_sfx,
                                     validate_model)
from carr_library.audio.models import LocalModelConfig
from carr_library.audio.music_runtime import MUSICGEN_REVISION


requires_ffmpeg = pytest.mark.skipif(
    not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
    reason='Actual FFmpeg and ffprobe are required for audio compatibility.')


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        assert stream.getsampwidth() == 2
        return (stream.getframerate(), stream.getnchannels(), stream.getnframes(),
                stream.readframes(stream.getnframes()))


def scores():
    direct = MusicSpec(.8, tempo_bpm=100, seed=37, key='A', mood='suspense')
    runtime = RuntimeScore(duration=.8, tempo=100, seed=37, key='A', mood='suspense',
                           intensity=.45, fade_in=.2, fade_out=.2)
    return direct, runtime


@requires_ffmpeg
def test_both_typed_profiles_render_identical_actual_pcm_and_midi(tmp_path):
    direct, score = scores()
    runtime = AudioRuntime(tmp_path)
    first = generate_music(direct, tmp_path / 'direct.wav')
    second = runtime.render_music(score, 'runtime.wav')
    assert pcm(first.path) == pcm(second.path)
    assert any(pcm(first.path)[3])
    first_midi = export_midi(spec=direct, output=tmp_path / 'direct.mid')
    second_midi = export_midi(runtime=runtime, spec=score, output='runtime.mid')
    assert first_midi.path.read_bytes() == second_midi.read_bytes()
    # Keep both originally published result shapes.
    assert first.duration_seconds == second.duration == .8
    assert isinstance(second_midi, Path)
    assert first_midi.as_dict()['operation'] == 'symbolic_midi_export'


@requires_ffmpeg
@pytest.mark.parametrize('name', ['door', 'click', 'chime', 'noise'])
def test_both_sfx_calling_conventions_share_the_actual_pcm_graph(tmp_path, name):
    runtime = AudioRuntime(tmp_path)
    first = render_sfx(name=name, output=tmp_path / 'direct.wav', duration_seconds=.2, seed=9)
    second = render_sfx(runtime, name=name, output='runtime.wav', duration=.2, seed=9)
    assert pcm(first.path) == pcm(second.path)
    assert first.metadata['operation'] == 'procedural_sfx'


@requires_ffmpeg
def test_runtime_controls_and_budget_survive_the_shared_generator(tmp_path):
    _, score = scores()
    runtime = AudioRuntime(tmp_path)
    quiet = runtime.render_music(replace(score, intensity=0), 'quiet.wav')
    loud = runtime.render_music(replace(score, intensity=1), 'loud.wav')
    assert pcm(quiet.path)[3] != pcm(loud.path)[3]
    confined = AudioRuntime(tmp_path, budget=AudioBudget(max_output_bytes=4096))
    with pytest.raises(AudioError, match='budget'):
        confined.render_music(score, 'too-large.wav')
    assert not (tmp_path / 'too-large.wav').exists()
    with pytest.raises(ValueError, match='inside'):
        confined.export_midi(score, tmp_path.parent / 'outside.mid')


def test_sdk_manifest_rejects_duplicate_keys_before_accepting_assets(tmp_path):
    directory = tmp_path / 'model'
    directory.mkdir()
    manifest = tmp_path / 'manifest.json'
    value = {'schema': 1, 'engine': 'musicgen', 'code_revision': MUSICGEN_REVISION}
    manifest.write_text(json.dumps(value)[:-1] + ', "schema": 1}', encoding='utf-8')
    config = LocalModelConfig('musicgen', Path(sys.executable), directory, manifest)
    with pytest.raises(ValueError, match='Duplicate'):
        validate_model(config)


def test_score_cache_identity_changes_with_actual_adapter_source_bytes(tmp_path, monkeypatch):
    from carr_library.audio import music_runtime
    from carr_library.studio.render import music_installation_identity
    source = tmp_path / 'adapter-source-fixture.py'
    source.write_bytes(b'# Source-identity fixture only; never executed.\n')
    monkeypatch.setattr(music_runtime, '__file__', str(source))
    first = music_installation_identity(None, 'procedural')
    source.write_bytes(b'# Changed source-identity fixture; never executed.\n')
    second = music_installation_identity(None, 'procedural')
    assert first != second
    assert first['implementation'].keys() == second['implementation'].keys()


@requires_ffmpeg
def test_extreme_finite_score_duration_fails_its_midi_budget_without_overflow(tmp_path):
    runtime = AudioRuntime(tmp_path, budget=AudioBudget(max_output_bytes=4096))
    with pytest.raises(AudioError, match='budget'):
        runtime.export_midi(RuntimeScore(duration=1e300), 'oversized.mid')
    assert not list(tmp_path.iterdir())


def test_model_inventory_checks_reparse_metadata_before_any_descent(tmp_path, monkeypatch):
    """Modeled Windows metadata order, not a claim of native junction execution."""
    import stat
    from types import SimpleNamespace
    from carr_library.audio import music_runtime
    calls = []
    redirected = tmp_path / 'redirected-directory'
    redirected.mkdir()
    real_lstat = Path.lstat
    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if path == redirected:
            calls.append(('lstat', str(path)))
            assert stat.S_ISDIR(info.st_mode)
            return SimpleNamespace(st_mode=info.st_mode,
                                   st_file_attributes=getattr(info, 'st_file_attributes', 0) | 0x400,
                                   st_nlink=info.st_nlink, st_size=info.st_size)
        return info
    class Entry:
        path = str(redirected)
        def stat(self, *, follow_symlinks):
            raise AssertionError('Read fresh Path.lstat metadata, not a cached DirEntry stat.')
        def is_dir(self, *args, **kwargs):
            raise AssertionError('Do not follow an entry to classify a possible junction.')
    class Listing:
        def __enter__(self):
            return iter((Entry(),))
        def __exit__(self, *args):
            pass
    def scan(directory):
        assert Path(directory) == tmp_path
        calls.append(('scan', str(directory)))
        return Listing()
    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(music_runtime.os, 'scandir', scan)
    with pytest.raises(ValueError, match='reparse'):
        list(music_runtime._plain_model_files(tmp_path))
    assert calls == [('scan', str(tmp_path)), ('lstat', str(redirected))]
