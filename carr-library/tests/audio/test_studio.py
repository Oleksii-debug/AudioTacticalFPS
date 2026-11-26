"""Actual local FFmpeg rendering, checkpoint recovery and source preservation.

All sound is synthetic PCM. Tone/impulse tests measure DSP behavior, not TTS,
neural music, native device playback or perceptual narration quality.
"""
from array import array
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from threading import Event
import wave

import pytest

from carr_library.audio import studio
from carr_library.audio.studio import Scene, Track, RenderLimits, render_project
from carr_library.ingest.writer_guard import exclusive_writer, WriterBusy


@pytest.fixture
def ffmpeg():
    binary = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    assert binary, 'Real Audio Studio tests require a trusted installed FFmpeg (CARR_FFMPEG).'
    return str(Path(binary).resolve())


def tone(path, frequency, milliseconds, *, rate=24000, amplitude=0.4, channels=1):
    frames = milliseconds * rate // 1000
    samples = array('h')
    for index in range(frames):
        value = round(32767 * amplitude * math.sin(2 * math.pi * frequency * index / rate))
        samples.extend([value] * channels)
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())
    return sha256(path.read_bytes()).hexdigest()


def samples(path):
    with wave.open(str(path), 'rb') as audio:
        rate, channels, frames = audio.getframerate(), audio.getnchannels(), audio.getnframes()
        values = array('h', audio.readframes(frames))
    if sys.byteorder != 'little':
        values.byteswap()
    return rate, tuple(tuple(values[offset::channels]) for offset in range(channels))


def amplitude(values, frequency, rate, start, end):
    first, last = int(start * rate), int(end * rate)
    selected = values[first:last]
    real = sum(value * math.cos(2 * math.pi * frequency * (first + index) / rate)
               for index, value in enumerate(selected))
    imaginary = sum(value * math.sin(2 * math.pi * frequency * (first + index) / rate)
                    for index, value in enumerate(selected))
    return 2 * math.hypot(real, imaginary) / len(selected) / 32768


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / 'Вхідні звуки čítanie'
    root.mkdir()
    values = {
        "ліва роль's.wav": tone(root / "ліва роль's.wav", 440, 500),
        'right.wav': tone(root / 'right.wav', 660, 500),
        'center.wav': tone(root / 'center.wav', 880, 500),
        'music.wav': tone(root / 'music.wav', 220, 4000, amplitude=0.5, channels=2),
        'door.wav': tone(root / 'door.wav', 1100, 100, amplitude=0.2),
    }
    return root, values


def scene():
    return Scene('chapter-one', (
        Track("ліва роль's.wav", 1000, pan=-1),
        Track('right.wav', 2000, pan=1),
        Track('center.wav', 3000),
        Track('music.wav', kind='music', gain=0.25, fade_in_ms=100, fade_out_ms=100),
        Track('door.wav', 3600, kind='effect', gain=0.5),
    ), 4000)


def test_three_role_mix_stems_ducking_pause_and_originals(prepared, tmp_path, ffmpeg):
    root, original = prepared
    events = []
    result = render_project(root, (scene(), Scene('pause', (), 500)),
                            tmp_path / 'Готове аудіо', ffmpeg=ffmpeg, progress=events.append)
    assert result.reused_scenes == 0
    assert result.master.frames == 216000 and result.master.sample_rate == 48000
    assert result.master.channels == 2
    assert set(result.scenes[0].stems) == {'voice', 'music', 'effect'}
    assert result.scenes[1].stems == {}
    _, (voice_left, voice_right) = samples(result.scenes[0].stems['voice'].path)
    assert not any(voice_left[:48000]) and any(voice_left[48000:49000])
    assert not any(voice_right[:96000]) and any(voice_right[96000:97000])
    assert not any(voice_left[168000:]) and not any(voice_right[168000:])
    rate, (left, right) = samples(result.master.path)
    assert amplitude(left, 440, rate, 1.15, 1.4) > 0.35
    assert amplitude(right, 440, rate, 1.15, 1.4) < 0.001
    assert amplitude(right, 660, rate, 2.15, 2.4) > 0.35
    assert amplitude(left, 660, rate, 2.15, 2.4) < 0.001
    centered_left = amplitude(left, 880, rate, 3.15, 3.4)
    centered_right = amplitude(right, 880, rate, 3.15, 3.4)
    assert centered_left > 0.25 and abs(centered_left - centered_right) < 0.001
    quiet_music = amplitude(left, 220, rate, 0.3, 0.8)
    ducked_music = amplitude(left, 220, rate, 1.15, 1.4)
    assert quiet_music > 0.1 and ducked_music < quiet_music * 0.6
    assert amplitude(left, 1100, rate, 3.62, 3.68) > 0.05
    assert max(abs(value) for values in (left, right) for value in values) <= 0.951 * 32768
    assert not any(left[192000:]) and not any(right[192000:])
    assert {name: sha256((root / name).read_bytes()).hexdigest() for name in original} == original
    manifest = json.loads(result.manifest.read_bytes())
    assert manifest['active']['completed'] is True
    assert manifest['active']['chapters'] == [
        {'scene_id': 'chapter-one', 'start_frame': 0, 'end_frame': 192000},
        {'scene_id': 'pause', 'start_frame': 192000, 'end_frame': 216000}]
    entry = manifest['scenes'][result.scenes[0].cache_key]
    assert {row['path']: row['sha256'] for row in entry['plan']['inputs']} == original
    assert [event['state'] for event in events] == [
        'rendering', 'rendered', 'rendering', 'rendered', 'assembling', 'complete']
    assert all(path.stat().st_nlink == 1 for path in result.manifest.parent.rglob('*.wav'))


def test_resume_reuses_completed_audio_and_only_regenerates_changed_scene(prepared, tmp_path, ffmpeg):
    root, _ = prepared
    output = tmp_path / 'output'
    plans = (scene(), Scene('ending', (Track('center.wav'),), 500))
    first = render_project(root, plans, output, ffmpeg=ffmpeg)
    existing = {path: path.read_bytes() for path in output.rglob('*.wav')}
    second = render_project(root, plans, output, ffmpeg=ffmpeg)
    assert second.reused_scenes == 2 and second.master == first.master
    assert {path: path.read_bytes() for path in output.rglob('*.wav')} == existing
    changed = replace(plans[1], tracks=(Track('center.wav', gain=0.2),))
    third = render_project(root, (plans[0], changed), output, ffmpeg=ffmpeg)
    assert third.reused_scenes == 1 and third.scenes[0].mix == first.scenes[0].mix
    assert third.scenes[1].mix.path != first.scenes[1].mix.path
    assert third.master.path != first.master.path
    assert all(path.read_bytes() == raw for path, raw in existing.items())


def test_corrupt_cache_is_repaired_without_overwriting_the_changed_file(prepared, tmp_path, ffmpeg):
    root, _ = prepared
    output = tmp_path / 'output'
    plans = (scene(), Scene('ending', (), 100))
    first = render_project(root, plans, output, ffmpeg=ffmpeg)
    damaged = first.scenes[0].mix.path
    damaged.write_bytes(b'Externally changed audio, kept for inspection.')
    second = render_project(root, plans, output, ffmpeg=ffmpeg)
    assert second.reused_scenes == 1
    assert damaged.read_bytes() == b'Externally changed audio, kept for inspection.'
    assert second.scenes[0].mix.path != damaged
    assert second.scenes[0].mix.sha256 == first.scenes[0].mix.sha256
    assert second.master == first.master


def test_cancellation_after_checkpoint_resumes_without_duplicate_scene(prepared, tmp_path, ffmpeg):
    root, _ = prepared
    output = tmp_path / 'output'
    cancelled = Event()

    def after_first(event):
        if event['state'] == 'rendered' and event['completed'] == 1:
            cancelled.set()

    plans = (scene(), Scene('ending', (), 100))
    with pytest.raises(InterruptedError, match='completed scenes'):
        render_project(root, plans, output, ffmpeg=ffmpeg,
                       cancelled=cancelled.is_set, progress=after_first)
    checkpoint = json.loads((output / studio.MANIFEST).read_bytes())
    assert len(checkpoint['scenes']) == 1 and checkpoint['masters'] == {}
    assert checkpoint['active']['completed'] is False
    previous = {path: path.read_bytes() for path in (output / 'scenes').glob('*.wav')}
    resumed = render_project(root, plans, output, ffmpeg=ffmpeg, progress=None)
    assert resumed.reused_scenes == 1
    assert all(path.read_bytes() == raw for path, raw in previous.items())
    assert not tuple(output.glob('.studio-scene-*'))


def test_cancel_terminates_the_actual_ffmpeg_process(prepared, tmp_path, ffmpeg, monkeypatch):
    root, _ = prepared
    cancelled = Event()
    actual = subprocess.Popen
    processes = []

    def start_then_cancel(*args, **kwargs):
        process = actual(*args, **kwargs)
        processes.append(process)
        cancelled.set()
        return process

    monkeypatch.setattr(studio.subprocess, 'Popen', start_then_cancel)
    output = tmp_path / 'cancelled'
    with pytest.raises(InterruptedError):
        render_project(root, (scene(),), output, ffmpeg=ffmpeg, cancelled=cancelled.is_set)
    assert processes and all(process.poll() is not None for process in processes)
    assert not tuple(output.rglob('*.wav'))
    assert json.loads((output / studio.MANIFEST).read_bytes())['scenes'] == {}


@pytest.mark.parametrize('kwargs', [
    {'path': '../voice.wav'}, {'path': 'https://example.test/voice.wav'},
    {'path': 'C:\\voice.wav'}, {'path': '/voice.wav'}, {'path': 'voice.wav\n'},
    {'path': 'NUL.wav'}, {'path': 'voice.wav', 'pan': float('nan')},
    {'path': 'voice.wav', 'gain': 2}, {'path': 'voice.wav', 'start_ms': True},
])
def test_invalid_paths_and_parameters_do_not_become_commands(kwargs):
    with pytest.raises(ValueError):
        Track(**kwargs)


@pytest.mark.parametrize('damage', ['linked', 'truncated', 'not-wav'])
def test_unsafe_or_incomplete_input_is_rejected_before_publication(prepared, tmp_path, ffmpeg, damage):
    root, _ = prepared
    if damage == 'linked':
        os.link(root / 'center.wav', tmp_path / 'other-link.wav')
    elif damage == 'truncated':
        path = root / 'center.wav'
        path.write_bytes(path.read_bytes()[:-500])
    else:
        (root / 'center.wav').write_bytes(b'This is not an audio file.')
    target = tmp_path / 'output'
    with pytest.raises(ValueError):
        render_project(root, (Scene('scene', (Track('center.wav'),), 500),), target, ffmpeg=ffmpeg)
    assert not target.exists()


def test_no_silent_track_truncation_or_output_folder_overwrite(prepared, tmp_path, ffmpeg):
    root, _ = prepared
    with pytest.raises(ValueError, match='longer scene'):
        render_project(root, (Scene('short', (Track('center.wav', start_ms=1),), 500),),
                       tmp_path / 'short', ffmpeg=ffmpeg)
    output = tmp_path / 'user-files'
    output.mkdir()
    sentinel = output / 'master.wav'
    sentinel.write_bytes(b'An existing user recording.')
    with pytest.raises(FileExistsError):
        render_project(root, (scene(),), output, ffmpeg=ffmpeg)
    assert sentinel.read_bytes() == b'An existing user recording.'
    with pytest.raises(ValueError, match='separate'):
        render_project(root, (scene(),), root / 'output', ffmpeg=ffmpeg)


def test_budgets_and_cooperative_render_lock(prepared, tmp_path, ffmpeg):
    root, _ = prepared
    with pytest.raises(ValueError, match='byte budget'):
        render_project(root, (scene(),), tmp_path / 'budget', ffmpeg=ffmpeg,
                       limits=RenderLimits(max_output_bytes=1000))
    output = tmp_path / 'locked'
    output.mkdir()
    with exclusive_writer(output):
        with pytest.raises(WriterBusy):
            render_project(root, (scene(),), output, ffmpeg=ffmpeg)
    assert not tuple(output.rglob('*.wav'))


def test_malformed_checkpoint_is_kept_without_rendering_over_existing_audio(prepared, tmp_path, ffmpeg):
    root, _ = prepared
    output = tmp_path / 'output'
    result = render_project(root, (scene(),), output, ffmpeg=ffmpeg)
    original = result.master.path.read_bytes()
    damaged = b'{"kind":"carr-local-audio-project","version":99}'
    result.manifest.write_bytes(damaged)
    with pytest.raises(ValueError, match='Unsupported audio-project manifest'):
        render_project(root, (scene(),), output, ffmpeg=ffmpeg)
    assert result.manifest.read_bytes() == damaged and result.master.path.read_bytes() == original


def test_checkpoint_metadata_consumes_the_same_output_budget(tmp_path):
    root = tmp_path / 'output'
    root.mkdir()
    manifest, token = studio._load_manifest(root)
    token = studio._save_manifest(root, manifest, token, 1024)
    original = (root / studio.MANIFEST).read_bytes()
    (root / 'existing-audio.wav').write_bytes(b'x' * (1024 - len(original)))
    manifest['active'] = {'status': 'larger checkpoint'}
    with pytest.raises(ValueError, match='checkpoint exceeds'):
        studio._save_manifest(root, manifest, token, 1024)
    assert (root / studio.MANIFEST).read_bytes() == original


def test_scene_changed_during_assembly_does_not_publish_a_master(prepared, tmp_path, ffmpeg, monkeypatch):
    root, _ = prepared
    output = tmp_path / 'output'
    actual = studio._run

    def changed_after_native_assembly(args, stage, outputs, budget, available):
        actual(args, stage, outputs, budget, available)
        if 'concat' in args:
            # The external writer modifies the real linked scene bytes. The
            # verified output itself must still not be attributed to that scene.
            part = stage / 'part-000000.wav'
            with part.open('r+b') as stream:
                stream.seek(-8, os.SEEK_END)
                stream.write(b'changed!')

    monkeypatch.setattr(studio, '_run', changed_after_native_assembly)
    with pytest.raises(ValueError, match='changed during master assembly'):
        render_project(root, (scene(),), output, ffmpeg=ffmpeg)
    assert not tuple(output.glob('master-*.wav'))
    assert json.loads((output / studio.MANIFEST).read_bytes())['active']['completed'] is False


def test_changed_source_invalidates_only_its_dependent_scene(prepared, tmp_path, ffmpeg):
    root, _ = prepared
    plans = (Scene('left', (Track("ліва роль's.wav"),), 500),
             Scene('right', (Track('right.wav'),), 500))
    first = render_project(root, plans, tmp_path / 'output', ffmpeg=ffmpeg)
    tone(root / 'right.wav', 990, 500)
    second = render_project(root, plans, tmp_path / 'output', ffmpeg=ffmpeg)
    assert second.reused_scenes == 1
    assert second.scenes[0].mix == first.scenes[0].mix
    assert second.scenes[1].mix.path != first.scenes[1].mix.path


def test_one_hour_streaming_silence_has_exact_duration_and_resumes(tmp_path, ffmpeg):
    root = tmp_path / 'prepared'
    root.mkdir()
    limits = RenderLimits(sample_rate=8000, max_output_bytes=512 * 1024**2,
                          max_wall_seconds=60)
    result = render_project(root, (Scene('one-hour-synthetic-silence', (), 3600000),),
                            tmp_path / 'one-hour', ffmpeg=ffmpeg, limits=limits)
    assert result.master.frames == 28800000
    assert result.master.path.stat().st_size < 116 * 1024**2
    with wave.open(str(result.master.path), 'rb') as audio:
        assert audio.getnframes() == 28800000
        audio.setpos(audio.getnframes() - 16)
        assert audio.readframes(16) == b'\0' * 64
    resumed = render_project(root, (Scene('one-hour-synthetic-silence', (), 3600000),),
                             tmp_path / 'one-hour', ffmpeg=ffmpeg, limits=limits)
    assert resumed.reused_scenes == 1 and resumed.master == result.master


def test_rf64_output_verifier_on_real_ffmpeg_file(tmp_path, ffmpeg):
    path = tmp_path / 'actual-rf64.wav'
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=stereo', '-t', '0.1',
        '-c:a', 'pcm_s16le', '-rf64', 'always', '-f', 'wav', '-n', str(path)],
        check=True, timeout=10, stdin=subprocess.DEVNULL, capture_output=True)
    assert path.read_bytes()[:4] == b'RF64'
    checked = studio._output_info(path, 8000, 800,
                                  studio._Budget(RenderLimits(), lambda: False))
    assert checked.frames == 800
    raw = bytearray(path.read_bytes())
    struct.pack_into('<Q', raw, 28, 999)  # ds64 data-byte count cannot be trusted blindly.
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='exact stereo PCM timeline'):
        studio._output_info(path, 8000, 800, studio._Budget(RenderLimits(), lambda: False))
