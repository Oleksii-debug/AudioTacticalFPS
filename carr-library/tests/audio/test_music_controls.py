"""Finite scene-key controls through the existing score and publication owners.

Actual FFmpeg/MIDI/backup checks are distinct from synthetic model-protocol
fixtures. No MusicGen or ACE-Step weights, inference or listening claim.
"""
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from carr_library.audio import music
from carr_library.audio.engines import VoiceRegistry
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.models import MusicCue, ProductionPlan
from carr_library.audio.production import parse_script, render_project
from carr_library.audio.runtime import resolve_ffmpeg


@pytest.fixture
def ffmpeg():
    selected = resolve_ffmpeg()
    if not selected:
        pytest.skip('Actual local score controls require the selected FFmpeg runtime.')
    return Path(selected).resolve()


def pcm(path):
    with wave.open(str(path), 'rb') as source:
        assert source.getsampwidth() == 2 and source.getnchannels() == 1
        return np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(float), source.getframerate()


def midi_notes(path):
    """Read emitted standard MIDI events independently of the composer."""
    raw = Path(path).read_bytes()
    assert raw[:4] == b'MThd'
    size, kind, tracks, division = struct.unpack('>IHHH', raw[4:14])
    assert (size, kind, tracks, division) == (6, 0, 1, 480)
    assert raw[14:18] == b'MTrk'
    assert int.from_bytes(raw[18:22], 'big') == len(raw) - 22
    cursor, tick, running, result = 22, 0, None, []

    def variable():
        nonlocal cursor
        value = 0
        for _ in range(4):
            byte = raw[cursor]
            cursor += 1
            value = (value << 7) | (byte & 127)
            if not byte & 128:
                return value
        raise AssertionError('MIDI variable-length quantity exceeded four bytes.')

    while cursor < len(raw):
        tick += variable()
        status = raw[cursor]
        if status & 128:
            cursor += 1
            if status < 240:
                running = status
        else:
            assert running is not None
            status = running
        if status == 255:
            meta = raw[cursor]
            cursor += 1
            count = variable()
            cursor += count
            if meta == 47:
                assert count == 0 and cursor == len(raw)
                break
            continue
        assert status < 240
        count = 1 if status >> 4 in (12, 13) else 2
        payload = raw[cursor:cursor + count]
        assert len(payload) == count and all(value < 128 for value in payload)
        cursor += count
        if status >> 4 in (8, 9):
            result.append((tick, status, payload[0], payload[1]))
    assert result
    return result


@pytest.mark.parametrize('mood', ['calm', 'suspense'])
def test_implicit_key_keeps_exact_legacy_cue_document_and_roundtrip(mood):
    script = f'[music mood={mood} tempo=90 gain=0.15 seed=7]\n[pause 0.5]'
    plan = parse_script(script)
    expected = {'mood': mood, 'tempo': 90, 'gain': .15, 'seed': 7,
                'engine': 'procedural', 'model_manifest': None}
    assert plan.scenes[0].music.to_dict() == expected
    legacy = plan.to_dict()
    assert legacy['scenes'][0]['music'] == expected
    assert ProductionPlan.from_dict(legacy).to_dict() == legacy
    assert replace(plan.scenes[0].music, key=None).to_dict() == expected
    assert plan.original_script == script


@pytest.mark.parametrize('mood', ['calm', 'suspense', 'bright', 'hopeful', 'sad', 'neutral', 'energetic', 'dark'])
def test_every_current_mood_renders_actual_bounded_pcm_and_midi(tmp_path, ffmpeg, mood):
    result = music.render_music(tmp_path / 'score.wav', duration=.25, sample_rate=8000,
                                mood=mood, seed=7, ffmpeg=ffmpeg)
    samples, rate = pcm(result['path'])
    assert rate == 8000 and len(samples) == result['frames'] == 2000
    assert 50 < np.max(np.abs(samples)) < 30000
    assert midi_notes(result['midi'])
    assert result['provenance']['mood'] == mood
    assert result['provenance']['sha256'] == sha256(result['path'].read_bytes()).hexdigest()
    assert result['provenance']['listening_acceptance'] == 'NOT_RUN'
    assert 'key' not in result['provenance']


def test_explicit_tonic_changes_actual_pitch_and_midi_without_retiming_notes(tmp_path, ffmpeg):
    settings = dict(duration=.5, sample_rate=8000, mood='calm', tempo=120, seed=7, ffmpeg=ffmpeg)
    implicit = music.render_music(tmp_path / 'implicit.wav', **settings)
    same = music.render_music(tmp_path / 'same.wav', key=None, **settings)
    baseline = music.render_music(tmp_path / 'c-major.wav', key='C', **settings)
    shifted = music.render_music(tmp_path / 'fs-major.wav', key='F#', **settings)
    assert implicit['path'].read_bytes() == same['path'].read_bytes() == baseline['path'].read_bytes()
    assert implicit['midi'].read_bytes() == same['midi'].read_bytes() == baseline['midi'].read_bytes()
    implicit_state = json.loads((tmp_path / 'implicit.music-work' / 'state.json').read_bytes())
    same_state = json.loads((tmp_path / 'same.music-work' / 'state.json').read_bytes())
    assert implicit_state['settings'] == same_state['settings']
    assert implicit_state['settings_sha256'] == same_state['settings_sha256']
    assert 'key' not in implicit_state['settings']
    before, after = midi_notes(baseline['midi']), midi_notes(shifted['midi'])
    assert len(before) == len(after)
    for original, changed in zip(before, after):
        assert original[:2] == changed[:2] and original[3] == changed[3]
        assert changed[2] - original[2] == 6

    def peak(path):
        samples, rate = pcm(path)
        power = np.abs(np.fft.rfft(samples * np.hanning(len(samples)), n=65536))
        frequencies = np.fft.rfftfreq(65536, 1 / rate)
        selected = (frequencies > 40) & (frequencies < 2500)
        return frequencies[selected][np.argmax(power[selected])]

    assert abs(peak(shifted['path']) / peak(baseline['path']) - math.sqrt(2)) < .015
    assert shifted['provenance']['key'] == 'F#' and shifted['provenance']['keyscale'] == 'F# major'
    assert shifted['provenance']['settings_sha256'] != baseline['provenance']['settings_sha256']
    accepted = shifted['path'].read_bytes()
    with pytest.raises(FileExistsError):
        music.render_music(shifted['path'], key='F#', **settings)
    assert shifted['path'].read_bytes() == accepted


@pytest.mark.parametrize('key', ['Db', 'F♯', 'C major', 'C;amovie=file', '', True, 123])
def test_unreviewed_key_is_rejected_before_native_engine_lookup(tmp_path, monkeypatch, key):
    with pytest.raises(ValueError, match='music key'):
        MusicCue(key=key)
    monkeypatch.setattr(music, '_binary', lambda *_: pytest.fail('Invalid key reached native selection.'))
    target = tmp_path / 'refused.wav'
    with pytest.raises(ValueError, match='music key'):
        music.render_music(target, duration=.5, key=key, ffmpeg='never-selected')
    assert not target.exists() and not target.with_suffix('.mid').exists()


def test_keyed_scene_uses_current_director_cache_and_exact_completed_replay(tmp_path, ffmpeg, monkeypatch):
    script = '[output sample_rate=8000 stems=false]\n[music mood=hopeful key=F# tempo=120 seed=7]\n[pause 0.5]'
    plan = parse_script(script)
    assert ProductionPlan.from_dict(plan.to_dict()) == plan
    assert plan.original_script == script and plan.scenes[0].music.key == 'F#'
    calls, original = [], music.generate_music

    def selected(output, **options):
        calls.append(options.get('key'))
        return original(output, **options)

    monkeypatch.setattr(music, 'generate_music', selected)
    engine = VoiceRegistry()
    first = render_project(plan, tmp_path / 'render', engine=engine, ffmpeg=ffmpeg)
    before = Path(first['output']).read_bytes()
    again = render_project(plan, tmp_path / 'render', engine=engine, ffmpeg=ffmpeg)
    assert again == first and calls == ['F#']
    changed = parse_script(script.replace('key=F#', 'key=G'))
    last = render_project(changed, tmp_path / 'render', engine=engine, ffmpeg=ffmpeg)
    assert calls == ['F#', 'G'] and last['run_key'] != first['run_key']
    assert Path(first['output']).read_bytes() == before
    assert wav_info(last['output']).frames == wav_info(first['output']).frames == 4000
    assert Path(last['output']).read_bytes() != before
    assert plan.original_script == script


@pytest.mark.parametrize('mood,key,expected', [('hopeful', 'F#', 'F# major'), ('suspense', 'D#', 'D# minor')])
def test_explicit_key_reaches_existing_isolated_ace_protocol_without_model_weights(tmp_path, ffmpeg, mood, key, expected):
    from tests.audio.test_music_adapters import fake_ace_package
    package = fake_ace_package(tmp_path)
    source = package / 'acestep' / 'inference.py'
    code = source.read_text(encoding='utf-8')
    marker = " assert params.timesignature == '4' and config.audio_format == 'wav'"
    assert code.count(marker) == 1
    source.write_text(code.replace(marker, marker + '\n assert params.keyscale == ' + repr(expected)), encoding='utf-8')
    manifest = SimpleNamespace(root=tmp_path,
        settings={'package_dir': 'packages', 'model_name': 'acestep-v15-turbo',
                  'python': 'explicit-fixture-python', 'threads': 1, 'timeout_seconds': 20},
        directory=lambda name: tmp_path / name,
        asset=lambda name: Path(sys.executable).resolve())
    output = tmp_path / 'synthetic-protocol.wav'
    music._render_acestep_chunk(output, frame_count=2000, sample_rate=8000, mood=mood,
        tempo=90, seed=7, gain=.2, ffmpeg=ffmpeg, manifest=manifest, cancelled=lambda: False, key=key)
    assert wav_info(output).frames == 2000
    assert not list(tmp_path.glob('.ace-step-*'))
    assert not list(package.rglob('__pycache__'))


def test_explicit_musicgen_key_is_parent_request_data_without_an_inference_claim(tmp_path, ffmpeg, monkeypatch):
    (tmp_path / 'hf-cache').mkdir()
    manifest = SimpleNamespace(root=tmp_path, files=(),
        settings={'source_revision': music.MUSICGEN_SOURCE, 'python': 'explicit-fixture-python'},
        asset=lambda name: Path(sys.executable).resolve())
    observed, run = [], music._run

    def selected(command, **options):
        if music._MUSICGEN_WORKER not in command:
            return run(command, **options)
        request = json.loads(options['data'])
        observed.append(request)
        assert request['prompt'].endswith(' Key: F# major.')
        assert request['root'] == str(tmp_path)
        with wave.open(request['output'], 'wb') as output:
            output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\x10\x00' * 2000)
        return b''

    monkeypatch.setattr(music, '_run', selected)
    output = tmp_path / 'synthetic-musicgen-request.wav'
    music._render_musicgen_chunk(output, frame_count=2000, sample_rate=8000, mood='hopeful',
        tempo=90, seed=7, gain=.2, ffmpeg=ffmpeg, manifest=manifest,
        previous=None, cancelled=lambda: False, key='F#')
    assert len(observed) == 1 and wav_info(output).frames == 2000
    assert not output.with_suffix('.neural.wav').exists()


def test_selected_key_survives_current_managed_project_and_typed_full_backup(tmp_path, monkeypatch):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    from carr_library.ui.audio_studio import StudioController

    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Explicit score controls')
    script = '[music mood=hopeful key=F# tempo=120 seed=7]\nNarrator: Exact retained source.'
    studio = StudioController(private_root=app.root)
    studio.new(script, title='Reviewed key', input_kind='script')
    document = studio.save_in_library()
    before = document.read_bytes()
    assert studio.plan().scenes[0].music.key == 'F#'
    monkeypatch.setattr(music, 'generate_music', lambda *a, **k: pytest.fail('Passive backup generated music.'))
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    verify_backup(backup, checksum)
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    reopened = StudioController(private_root=restored).open(restored / document.relative_to(app.root))
    assert reopened.plan().scenes[0].music.key == 'F#'
    assert reopened.script == reopened.draft.source_text == script
    assert document.read_bytes() == before
