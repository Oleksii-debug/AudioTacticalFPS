"""Processed chapters retain the exact accepted master samples and cache."""
from array import array
from dataclasses import replace
from pathlib import Path
import math
import shutil
import sys
import wave

import pytest

from carr_library.audio import AudioCancelled, AudioError, AudioRuntime, Effect, Track
from carr_library.audio.process import run_process
from carr_library.speech import VoiceRegistry
from carr_library.studio.project import Cue, Project, Scene
from carr_library.studio.render import ProductionService


def require_audio():
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg and ffprobe are required.'


def tone(path, *, rate=24000, frames=24000, channels=1):
    values = array('h', (round(12000 * math.sin(2 * math.pi * 440 * n / rate))
                        for n in range(frames) for _ in range(channels)))
    if sys.byteorder != 'little':
        values.byteswap()
    raw = values.tobytes()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, 2, rate, 0, 'NONE', 'uncompressed'))
        stream.writeframes(raw)
    return raw


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        return (stream.getframerate(), stream.getnchannels(), stream.getnframes(),
                stream.readframes(stream.getnframes()))


def test_stateful_master_chapters_resume_without_restarting_effect_or_synthesis(tmp_path, monkeypatch):
    require_audio()
    source = tmp_path / 'selected.wav'
    original = tone(source)
    effects = (Effect('lowpass', frequency=300, q=.707),)
    project = Project('master-chapters', 'Continuous master', (), (
        Scene('one', 'One', (), cues=(Cue('first', str(source), gain_db=0, pan=-1),)),
        Scene('two', 'Two', (), cues=(Cue('second', str(source), gain_db=0, pan=-1),))),
        schema=3, effects=effects)
    service = ProductionService(tmp_path / 'library/state/studio',
        voices=VoiceRegistry([]), runtime_factory=AudioRuntime, input_roots=(tmp_path,))
    output = tmp_path / 'output'
    output.mkdir()
    actual_extract = AudioRuntime.extract_pcm_frames
    extracted = []

    def extract(runtime, *args, **kwargs):
        extracted.append((kwargs['start_frame'], kwargs['end_frame']))
        return actual_extract(runtime, *args, **kwargs)

    monkeypatch.setattr(AudioRuntime, 'extract_pcm_frames', extract)
    cancel, chapter_reports = [False], []
    def progress(message):
        if message == 'processed master chapter: rendering local audio':
            chapter_reports.append(message)
            if len(chapter_reports) == 2:
                cancel[0] = True

    try:
        job = service.submit(project, output, stems=False)
        with pytest.raises((InterruptedError, AudioCancelled)):
            service.run(job, cancelled=lambda: cancel[0], progress=progress)
        pending = service.get(job)
        accepted = [row for row in pending['nodes'].values()
                    if row['kind'] == 'processed master chapter']
        assert pending['status'] == 'paused' and len(accepted) == 1
        assert extracted == [(0, 24000)]
        assert not Path(pending['destination']).exists()

        cancel[0] = False
        result = service.run(job)
        chapters = sorted((Path(result['destination']) / 'chapters').glob('*.wav'))
        assert len(chapters) == 2 and extracted == [(0, 24000), (24000, 48000)]
        frames = [pcm(path) for path in chapters]
        master = pcm(Path(result['destination']) / 'master.wav')
        assert [(rate, channels, count) for rate, channels, count, _ in frames] == [
            (24000, 2, 24000), (24000, 2, 24000)]
        assert master[:3] == (24000, 2, 48000)
        assert b''.join(row[3] for row in frames) == master[3]
        assert result['result']['chapters'] == [
            {'id': 'one', 'title': 'One', 'start': 0.0, 'end': 1.0},
            {'id': 'two', 'title': 'Two', 'start': 1.0, 'end': 2.0}]
        accepted = [row for row in result['nodes'].values()
                    if row['kind'] == 'processed master chapter']
        assert len(accepted) == 2 and sum(row['reused'] for row in accepted) == 1

        # Real counterfactual: restarting the same stateful bus at chapter two
        # has different initial samples. The saved chapter must keep continuity.
        runtime = AudioRuntime(tmp_path / 'counterfactual', input_roots=(tmp_path,))
        dry_scene = tmp_path / 'counterfactual' / 'dry.wav'
        runtime.mix((Track(source, gain_db=0, pan=-1, role='sfx'),), dry_scene,
                    duration=1, sample_rate=24000)
        restarted = tmp_path / 'counterfactual' / 'restarted.wav'
        runtime.mix((Track(dry_scene, routing='stereo'),), restarted,
                    duration=1, sample_rate=24000, effects=effects)
        assert pcm(restarted)[3][:512] != frames[1][3][:512]

        completed_bytes = {path.name: path.read_bytes() for path in chapters}
        assert service.run(job)['status'] == 'complete'
        second = service.run(service.submit(project, output, stems=False))
        assert extracted == [(0, 24000), (24000, 48000)]
        reused = [row for row in second['nodes'].values()
                  if row['kind'] == 'processed master chapter']
        assert len(reused) == 2 and all(row['reused'] for row in reused)
        assert {path.name: path.read_bytes() for path in chapters} == completed_bytes
        assert b''.join(pcm(path)[3] for path in sorted(
            (Path(second['destination']) / 'chapters').glob('*.wav'))) == master[3]
        assert pcm(source)[3] == original
    finally:
        service.close()


def test_exact_frame_extraction_supports_actual_rf64_without_resampling(tmp_path):
    require_audio()
    source = tmp_path / 'source.wav'
    original = tone(source, rate=44100, frames=1033, channels=2)
    runtime = AudioRuntime(tmp_path / 'runtime', input_roots=(tmp_path,))
    rf64 = tmp_path / 'source-rf64.wav'
    run_process([runtime.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error',
                 '-n', '-i', str(source), '-map', '0:a:0', '-c:a', 'copy',
                 '-rf64', 'always', str(rf64)], budget=runtime.budget, owned_tree=True)
    assert rf64.read_bytes()[:4] == b'RF64'
    first, second = runtime.root / 'first.wav', runtime.root / 'second.wav'
    a = runtime.extract_pcm_frames(rf64, first, start_frame=0, end_frame=257)
    b = runtime.extract_pcm_frames(rf64, second, start_frame=257, end_frame=1033)
    assert (a.frames, b.frames) == (257, 776)
    assert pcm(first)[:3] == (44100, 2, 257)
    assert pcm(second)[:3] == (44100, 2, 776)
    assert pcm(first)[3] + pcm(second)[3] == original


@pytest.mark.parametrize(('start', 'end'), [
    (True, 2), (-1, 2), (1, 1), (0, 24001), (0, False), (1.5, 2),
], ids=['boolean-start', 'negative-start', 'empty', 'past-end', 'boolean-end', 'fractional-start'])
def test_invalid_frame_range_cannot_start_render_or_publish(tmp_path, monkeypatch, start, end):
    require_audio()
    source = tmp_path / 'source.wav'
    original = tone(source)
    runtime = AudioRuntime(tmp_path / 'runtime', input_roots=(tmp_path,))
    runtime.inspect(source)
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid frame ranges must be rejected before rendering.')
    monkeypatch.setattr(runtime, '_render', forbidden)
    output = runtime.root / 'chapter.wav'
    with pytest.raises(ValueError):
        runtime.extract_pcm_frames(source, output, start_frame=start, end_frame=end)
    assert not output.exists() and pcm(source)[3] == original


def test_extraction_rejects_measured_one_frame_short_output_before_publication(tmp_path, monkeypatch):
    require_audio()
    source = tmp_path / 'source.wav'
    original = tone(source)
    runtime = AudioRuntime(tmp_path / 'runtime', input_roots=(tmp_path,))
    inspect = runtime.inspect
    def measured(path, **kwargs):
        info = inspect(path, **kwargs)
        # Preserve the real process and all other measured output properties.
        return replace(info, frames=info.frames - 1) if Path(path).name == 'render.wav' else info
    monkeypatch.setattr(runtime, 'inspect', measured)
    output = runtime.root / 'chapter.wav'
    with pytest.raises(AudioError, match='exact measured master frame range'):
        runtime.extract_pcm_frames(source, output, start_frame=3, end_frame=1337)
    assert not output.exists() and pcm(source)[3] == original
