"""Measured synthetic PCM tests through real FFmpeg; no speech-quality claim."""
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import struct
import wave

import pytest

from carr_library.audio.clip_audio import process_clip, mix_tracks
from carr_library.audio.clips import ClipEditor
from carr_library.audio.ffmpeg import wav_info, silence
from carr_library.audio.jobs import RenderCancelled, sha256_file
from carr_library.audio.models import Clip
from carr_library.audio.production import parse_script, render_project
from carr_library.audio.publishing import PublicationService, _segments
from carr_library.audio.runtime import resolve_ffmpeg


@pytest.fixture
def ffmpeg():
    binary = resolve_ffmpeg()
    assert binary is not None and binary.is_file(), 'This strict suite needs actual CARR_FFMPEG or installed FFmpeg.'
    return binary


def tone(path, *, seconds=1, channels=1, rate=8000, amplitude=6000, frequency=400):
    frames = round(seconds * rate)
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, 2, rate, frames, 'NONE', 'not compressed'))
        values = []
        for i in range(frames):
            sample = round(amplitude * math.sin(i * 2 * math.pi * frequency / rate))
            values.append(struct.pack('<' + 'h' * channels, *([sample] if channels == 1 else [sample, sample // 2])))
        stream.writeframes(b''.join(values))


def samples(path):
    info = wav_info(path)
    with open(path, 'rb') as stream:
        stream.seek(info.data_offset)
        values = struct.unpack('<' + 'h' * (info.data_size // 2), stream.read(info.data_size))
    return info, [values[n::info.channels] for n in range(info.channels)]


def rms(values):
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


class SyntheticVoice:
    def __init__(self):
        self.calls = []
    def cache_identity(self, voice, **kwargs):
        return {'fixture': 'clip-tone-v1'}
    def synthesize(self, text, output, **kwargs):
        self.calls.append(text)
        tone(output, seconds=.2, frequency=600)
        return output


def test_real_trim_pan_gain_and_fades_keep_exact_pcm_samples(ffmpeg, tmp_path):
    source = tmp_path / 'Stereo.wav'
    tone(source, channels=2)
    raw = source.read_bytes()
    plain = tmp_path / 'plain.wav'
    clip = Clip('c', source.name, trim_start=.2, duration=.4)
    process_clip(source, plain, clip, ffmpeg=ffmpeg, sample_rate=8000)
    info, channels = samples(plain)
    assert (info.frames, info.rate, info.channels) == (3200, 8000, 2)
    assert 1.9 < rms(channels[0][400:2800]) / rms(channels[1][400:2800]) < 2.1
    right = tmp_path / 'right.wav'
    process_clip(source, right, replace(clip, gain_db=-6, pan=1, fade_in=.1, fade_out=.1),
                 ffmpeg=ffmpeg, sample_rate=8000)
    info, changed = samples(right)
    assert info.frames == 3200 and max(map(abs, changed[0])) <= 1
    ratio = rms(changed[1][1000:2200]) / rms(channels[1][1000:2200])
    assert .48 < ratio < .52
    assert rms(changed[1][:100]) < rms(changed[1][1000:1100]) * .2
    assert rms(changed[1][-100:]) < rms(changed[1][1000:1100]) * .2
    assert source.read_bytes() == raw
    with pytest.raises(FileExistsError):
        process_clip(source, right, clip, ffmpeg=ffmpeg, sample_rate=8000)


def test_real_overlay_position_and_background_ducking_are_measured(ffmpeg, tmp_path):
    core, bed, empty = tmp_path / 'core.wav', tmp_path / 'bed.wav', tmp_path / 'empty.wav'
    tone(core, seconds=1, channels=2, amplitude=12000, frequency=500)
    tone(bed, seconds=.5, channels=2, amplitude=6000, frequency=1000)
    silence(empty, 8000, sample_rate=8000)
    positioned = tmp_path / 'positioned.wav'
    mix_tracks(empty, [(bed, 1600, 'sfx')], positioned, ffmpeg=ffmpeg)
    info, data = samples(positioned)
    assert info.frames == 8000
    assert max(map(abs, data[0][:1500])) <= 1
    assert rms(data[0][2400:4800]) > 3000
    assert max(map(abs, data[0][5700:])) <= 1
    # Measure the isolated bed via frequency projection: foreground is 500 Hz,
    # background is exactly 1000 Hz over an integer number of periods.
    dry, ducked = tmp_path / 'dry.wav', tmp_path / 'ducked.wav'
    mix_tracks(core, [(bed, 0, 'ambience')], dry, ffmpeg=ffmpeg, duck=False)
    mix_tracks(core, [(bed, 0, 'ambience')], ducked, ffmpeg=ffmpeg, duck=True)
    def component(path):
        _, values = samples(path)
        selected = values[0][1600:3200]
        return abs(sum(v * math.sin((n+1600) * 2 * math.pi * 1000 / 8000)
                       for n, v in enumerate(selected))) / len(selected)
    assert component(ducked) < component(dry) * .8


def test_real_clip_render_resume_stems_and_publication_sample_graph(ffmpeg, tmp_path, monkeypatch):
    root = tmp_path / 'project'
    (root / 'assets').mkdir(parents=True)
    source = root / 'assets' / 'retained.wav'
    tone(source, channels=2)
    original_source = source.read_bytes()
    plan = parse_script('[output sample_rate=8000]\n# First\nNarrator: One.\n'
        '[clip retained.wav id=c1 start=.1 trim_start=.2 duration=.4 track=ambience gain_db=-6]\n'
        '[duration .6]\n# Second\nNarrator: Two.')
    voice = SyntheticVoice()
    first = render_project(plan, root, engine=voice, ffmpeg=ffmpeg)
    assert first['frames'] == 6400 and len(voice.calls) == 2
    assert all(wav_info(path).frames == first['frames'] for path in first['stems'])
    assert 'Clip track: ambience' in first['stem_labels'].values()
    timeline = json.loads((Path(first['output']).parent / 'timeline.json').read_text(encoding='utf-8'))
    assert timeline['schema_version'] == 2
    clip = timeline['scenes'][0]['clips'][0]
    assert (clip['source_start'], clip['source_end'], clip['start_frame'], clip['frames']) == (1600, 4800, 800, 3200)
    _, intervals, duration = _segments(plan, timeline)
    assert duration == .8 and intervals[-1]['end_frame'] == first['frames']
    repeated = render_project(plan, root, engine=voice, ffmpeg=ffmpeg)
    assert repeated['output'] == first['output'] and len(voice.calls) == 2
    changed = parse_script(plan.original_script.replace('start=.1', 'start=.15'))
    second = render_project(changed, root, engine=voice, ffmpeg=ffmpeg)
    assert second['run_key'] != first['run_key'] and len(voice.calls) == 2
    assert source.read_bytes() == original_source
    (tmp_path / 'library').mkdir()
    service = PublicationService(root, private_root=tmp_path / 'library', ffmpeg=ffmpeg,
                                 max_bytes=256 * 1024 ** 2)
    result = service.export_epub(first['run_key'], tmp_path / 'clips.epub')
    assert service.verify(result['id'])['status'] == 'complete'
    forged = json.loads(json.dumps(timeline))
    forged['scenes'][0]['clips'][0]['source_start'] += 1
    with pytest.raises(ValueError, match='trim'):
        _segments(plan, forged)


def test_clip_only_render_needs_no_speech_and_cancel_reuses_checked_clip(ffmpeg, tmp_path, monkeypatch):
    root = tmp_path / 'project'
    (root / 'assets').mkdir(parents=True)
    tone(root / 'assets' / 'source.wav', seconds=.25)
    plan = parse_script('[output sample_rate=8000 stems=false]\n'
                        '[clip source.wav id=c1 start=.1 duration=.2 track=microphone]')
    voice = SyntheticVoice()
    stop = [False]
    def progress(message):
        if 'saved an audio step' in message:
            stop[0] = True
    with pytest.raises(RenderCancelled):
        render_project(plan, root, engine=voice, ffmpeg=ffmpeg, cancelled=lambda: stop[0], on_status=progress)
    assert not voice.calls
    journal = json.loads((root / 'render-journal.json').read_text(encoding='utf-8'))
    assert journal['state'] == 'cancelled'
    from carr_library.audio import clip_audio
    monkeypatch.setattr(clip_audio, 'process_clip', lambda *a, **k: pytest.fail('Resume must reuse the sealed clip.'))
    result = render_project(plan, root, engine=voice, ffmpeg=ffmpeg)
    assert result['frames'] == 2400 and not voice.calls


def test_preview_and_full_render_share_verified_clip_cache(ffmpeg, tmp_path, monkeypatch):
    root = tmp_path / 'project'
    (root / 'assets').mkdir(parents=True)
    tone(root / 'assets' / 'source.wav', seconds=.3)
    script = '[output sample_rate=8000 stems=false]\n[clip source.wav id=c1 duration=.2 start=.2]'
    editor = ClipEditor(script, root)
    preview = editor.preview('c1', ffmpeg=ffmpeg)
    assert wav_info(preview).frames == 1600
    from carr_library.audio import clip_audio
    monkeypatch.setattr(clip_audio, 'process_clip', lambda *a, **k: pytest.fail('The checked preview is the same DSP step.'))
    result = render_project(editor.plan, root, engine=SyntheticVoice(), ffmpeg=ffmpeg)
    assert result['frames'] == 3200
