"""Actual FFmpeg graph and persisted dependency invalidation, with labelled PCM voice fixtures."""
from array import array
from dataclasses import replace
from hashlib import sha256
import math
from pathlib import Path
import wave

import pytest

from carr_library.audio import FFmpegAudio
from carr_library.studio import Budget, Clip, Line, MusicCue, PlanError, Project, ProjectStore, Role, Scene
from carr_library.studio.director import Director, RenderCancelled, RenderPaused
from carr_library.studio.store import digest_file


def controlled_speech(calls):
    def speak(request, target, cancelled=None):
        if cancelled:
            cancelled()
        calls.append(request.text)
        rate, frames = 24000, 2400
        frequency = {'left': 300, 'right': 700}.get(request.voice_id, 500)
        samples = array('h', (round(5000 * math.sin(2 * math.pi * frequency * i / rate)) for i in range(frames)))
        with wave.open(str(target), 'wb') as out:
            out.setparams((1, 2, rate, 0, 'NONE', 'PCM'))
            out.writeframes(samples.tobytes())
        return dict(path=str(target), frames=frames, sample_rate=rate, channels=1,
                    duration_seconds=frames/rate, sha256=digest_file(target), engine_fingerprint='fixture-1')
    return speak


def director(root, calls):
    return Director(ProjectStore(root), speech_renderer=controlled_speech(calls), engine_identity=lambda voice: 'fixture-1')


def test_real_mix_precise_pauses_pan_and_cached_resume(tmp_path):
    calls = []
    project = Project('Two roles', (Role('left', 'left', pan=-1), Role('right', 'right', pan=1)),
        (Scene('chapter', 'Chapter', (Line('a', 'left', 'First exact source.', pause_after=.2),
                                       Line('b', 'right', 'Second exact source.', pause_after=.3))),))
    engine = director(tmp_path / 'project', calls)
    result = engine.render(project)
    assert calls == ['First exact source.', 'Second exact source.']
    assert result['master']['frames'] == round(.7 * 24000)
    with wave.open(result['master']['path'], 'rb') as src:
        audio = array('h', src.readframes(src.getnframes()))
    left, right = audio[0::2], audio[1::2]
    assert max(map(abs, left[:2400])) > 1000 and max(map(abs, right[:2400])) == 0
    assert max(map(abs, right[7200:9600])) > 1000 and max(map(abs, left[7200:9600])) == 0
    assert max(map(abs, audio[5000:14000])) == 0
    again = engine.render(project)
    assert len(calls) == 2 and again['completed_chunks'] == 0
    assert again['master']['sha256'] == result['master']['sha256']
    assert [c['text'] for c in again['captions']] == ['First exact source.', 'Second exact source.']


def test_one_changed_scene_reuses_other_scene_and_source_chunks(tmp_path):
    calls = []
    project = Project('Source', (Role('n', 'center'),), (
        Scene('one', 'One', (Line('l', 'n', 'Original first scene.'),)),
        Scene('two', 'Two', (Line('l', 'n', 'Unchanged second scene.'),))))
    engine = director(tmp_path / 'project', calls)
    first = engine.render(project)
    changed = replace(project, scenes=(replace(project.scenes[0], lines=(Line('l', 'n', 'Edited first scene.'),)), project.scenes[1]))
    second = engine.render(changed)
    assert calls == ['Original first scene.', 'Unchanged second scene.', 'Edited first scene.']
    assert second['chapters'][1]['sha256'] == first['chapters'][1]['sha256']
    assert Path(first['master']['path']).is_file()
    assert first['project_revision'] != second['project_revision']


def test_more_than_runtime_track_budget_uses_bounded_role_stems(tmp_path):
    calls = []
    project = Project('Long project', (Role('n', 'center'),),
        (Scene('long', 'Long', tuple(Line(f'l{i}', 'n', 'Shared phrase.', pause_after=0) for i in range(150))),))
    backend = FFmpegAudio(max_tracks=128)
    actual = backend.render_timeline
    fanouts = []
    def record(tracks, *args, **kwargs):
        tracks = tuple(tracks)
        fanouts.append(len(tracks))
        return actual(tracks, *args, **kwargs)
    backend.render_timeline = record
    engine = Director(ProjectStore(tmp_path / 'project'), audio=backend,
                      speech_renderer=controlled_speech(calls), engine_identity=lambda voice: 'fixture-1')
    result = engine.render(project)
    assert max(fanouts) <= 32
    assert len(calls) == 1
    assert result['master']['frames'] == 150 * 2400
    assert len(result['captions']) == 150


def test_missing_musicgen_never_silently_substitutes_another_engine(tmp_path):
    project = Project('Neural music', (), (Scene('one', 'One', music=(
        MusicCue('m', 'Quiet instrumental music', engine='musicgen', duration=1),)),))
    with pytest.raises((ValueError, RuntimeError), match='model|MusicGen|runtime'):
        director(tmp_path / 'project', []).render(project)
    assert not list((tmp_path / 'project' / 'renders').rglob('master.wav'))


def test_actual_fluidsynth_scene_music_and_checkpointed_windows(tmp_path):
    from carr_library.audio.music import available_music_engines
    font = Path('/usr/share/sounds/sf2/TimGM6mb.sf2')
    if not font.is_file() or not available_music_engines()['fluidsynth']['runtime_available']:
        # This branch verifies explicit missing-asset behavior on a Windows
        # runtime without optional SoundFonts. It is not a music inference pass.
        cue = MusicCue('m', 'MIDI music', duration=1, soundfont=str(font),
                       soundfont_sha256='0' * 64, soundfont_license='GPL-2.0-only')
        with pytest.raises((ValueError, OSError, RuntimeError)):
            director(tmp_path / 'project', []).render(Project('Music', (), (Scene('one', 'One', music=(cue,)),)))
        return
    cue = MusicCue('m', 'MIDI piano arpeggio', duration=2.01, chunk_seconds=1,
                   crossfade_seconds=.1, soundfont=str(font), soundfont_sha256=digest_file(font),
                   soundfont_license='GPL-2.0-only', gain_db=-6)
    project = Project('Generated local score', (), (Scene('one', 'One', music=(cue,)),))
    engine = director(tmp_path / 'project', [])
    result = engine.render(project, allowed_audio_roots=(font.parent,))
    assert result['master']['frames'] == round(2.01 * 24000)
    assert 0 < result['master']['peak'] < 1
    assert result['music'][0]['windows'] == 3
    assert result['music'][0]['actual_neural_continuation'] is False
    resumed = engine.render(project, allowed_audio_roots=(font.parent,))
    assert resumed['completed_chunks'] == 0
    assert result['master']['sha256'] == resumed['master']['sha256']


def test_prepared_audio_tail_survives_narration_and_explicit_short_scene_is_rejected(tmp_path):
    prepared = tmp_path / 'tail.wav'
    controlled_speech([])(type('Request', (), {'voice_id': 'left', 'text': 'fixture'})(), prepared)
    project = Project('Audible tail', (Role('n', 'center'),), (Scene('one', 'One',
        lines=(Line('line', 'n', 'Words.', pause_after=0),),
        clips=(Clip('tail', str(prepared), start_seconds=.5, gain_db=0, role='effect'),)),))
    engine = director(tmp_path / 'project', [])
    result = engine.render(project, allowed_audio_roots=(tmp_path,))
    assert result['master']['frames'] == 14400  # 0.1s speech and a cue at 0.5–0.6s.
    with wave.open(result['master']['path'], 'rb') as stream:
        values = array('h', stream.readframes(stream.getnframes()))
    assert max(map(abs, values[-4800:])) > 1000
    short = replace(project, scenes=(replace(project.scenes[0], duration=.2),))
    with pytest.raises(PlanError, match='No speech, clip or music was truncated'):
        engine.render(short, allowed_audio_roots=(tmp_path,))


def test_prepared_snapshot_counts_against_normalization_disk_allowance(tmp_path):
    prepared = tmp_path / 'large.wav'
    with wave.open(str(prepared), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\0\1' * 275000)
    project = Project('Bounded copies', (), (Scene('one', 'One',
        clips=(Clip('clip', str(prepared)),)),), sample_rate=16000, channels=1,
        budget=Budget(disk_bytes=1024 * 1024))
    engine = director(tmp_path / 'project', [])
    with pytest.raises(RenderPaused, match='disk budget'):
        engine.render(project, allowed_audio_roots=(tmp_path,))
    assert sum(p.stat().st_size for p in engine.store.root.rglob('*') if p.is_file()) < project.budget.disk_bytes
    assert len(tuple(engine.store.cache.iterdir())) == 1
    assert prepared.stat().st_size == 550044


def test_prepared_input_initial_digest_can_be_cancelled_before_copy(tmp_path):
    prepared = tmp_path / 'selected.wav'
    prepared.write_bytes(b'x' * (3 * 1024 * 1024))
    engine = director(tmp_path / 'project', [])
    calls = []
    def check(*args):
        calls.append(True)
        if len(calls) == 2:
            raise RenderCancelled('cancel hashing')
    with pytest.raises(RenderCancelled, match='hashing'):
        engine._snapshot_clip(prepared, tmp_path, (tmp_path,), check)
    assert not tuple(engine.store.cache.iterdir())


def test_runtime_change_invalidates_dsp_cache_but_preserves_speech(tmp_path, monkeypatch):
    calls, mixes = [], []
    project = Project('Runtime resume', (Role('n', 'center'),), (
        Scene('one', 'One', (Line('a', 'n', 'First.'),)),
        Scene('two', 'Two', (Line('b', 'n', 'Second.', pause_after=.3),))))
    engine = director(tmp_path / 'project', calls)
    original = engine.audio.render_timeline
    def counted(*args, **kwargs):
        mixes.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(engine.audio, 'render_timeline', counted)
    monkeypatch.setattr(engine.audio, 'engine_fingerprint', lambda **kwargs: 'a' * 64)
    stopped = [False]
    def progress(state):
        if state['message'] == 'Completed scene 1 of 2':
            stopped[0] = True
    with pytest.raises(RenderPaused):
        engine.render(project, paused=lambda: stopped[0], progress=progress)
    assert len(mixes) == 1 and calls == ['First.']
    monkeypatch.setattr(engine.audio, 'engine_fingerprint', lambda **kwargs: 'b' * 64)
    result = engine.render(project)
    assert len(mixes) == 3 and calls == ['First.', 'Second.']
    assert result['pipeline'].endswith('b' * 64)
