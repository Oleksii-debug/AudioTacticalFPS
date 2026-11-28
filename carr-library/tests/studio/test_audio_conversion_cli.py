"""Actual installed-origin CLI children use the same FFmpeg batch publisher."""
from array import array
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import wave

import pytest

import carr_library
from bootstrap.vendor.cairn_extractors import hasher as bootstrap_hasher
from carr_library.application import LibraryApp
from carr_library.audio import AudioBudget, Effect
from carr_library.audio.effect_presets import EffectPresetStore
from carr_library.runtime_settings import audio_runtime


def tone(path, frequency):
    values = array('h', (round(2000 * math.sin(2 * math.pi * frequency * index / 24000))
                         for index in range(3000)))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        stream.writeframes(values.tobytes())


@pytest.fixture
def peer(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir(); outputs.mkdir()
    first, second = inputs / 'Перший.wav', inputs / 'second.wav'
    tone(first, 440); tone(second, 880)
    runtime = audio_runtime(outputs, state_root=app.root / 'state', input_roots=(inputs,),
                            budget=AudioBudget(timeout=30, reserve_disk_bytes=0))
    assert Path(runtime.ffmpeg).is_file() and Path(runtime.ffprobe).is_file()
    return app, (first, second), outputs, runtime


def invoke(peer, *options, command='convert-batch', sources=None, name='reviewed'):
    app, selected, outputs, _ = peer
    roots = (Path(carr_library.__file__).resolve().parents[1],
             Path(bootstrap_hasher.__file__).resolve().parents[3])
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, dict.fromkeys(roots))))
    process = subprocess.run([sys.executable, '-B', '-s', '-P', '-X', 'utf8',
        '-m', 'carr_library.studio.cli', '--data-dir', str(app.root), command,
        *map(str, selected if sources is None else sources),
        '--output', str(outputs / name), '--channels', '1', '--timeout', '30',
        '--max-bytes', str(64 * 1024**2), *map(str, options)],
        cwd=outputs, env=environment, capture_output=True, text=True,
        encoding='utf-8', errors='strict', timeout=75)
    return process


def checked(process, expected=0):
    assert process.returncode == expected, (process.stdout, process.stderr)
    return json.loads(process.stdout)


def test_actual_cli_preview_then_publish_and_explicit_resume_keep_exact_bytes(peer):
    app, sources, outputs, _ = peer
    originals = {path: path.read_bytes() for path in sources}
    plan = checked(invoke(peer, '--preview'))['plan']
    assert not (outputs / 'reviewed').exists()
    assert plan['sources'][0]['source'] == str(sources[0])
    assert plan['sources'][0]['information']['frames'] == 3000
    result = checked(invoke(peer))
    assert result['status'] == 'completed' and result['done'] == 2
    paths = [outputs / 'reviewed' / row['output'] for row in result['items']]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    for path, row in zip(paths, result['items']):
        with wave.open(str(path), 'rb') as stream:
            assert (stream.getframerate(), stream.getnchannels(), stream.getnframes()) == (24000, 1, 3000)
        assert sha256(path.read_bytes()).hexdigest() == row['candidate']['sha256']
    refused = invoke(peer)
    assert refused.returncode == 1 and 'already exists' in refused.stderr
    resumed = checked(invoke(peer, '--resume'))
    assert resumed['status'] == 'completed' and resumed['done'] == 2
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before
    assert {path: path.read_bytes() for path in sources} == originals
    assert not (app.root / 'state' / 'audio-batches').exists()


def test_actual_cli_partial_failure_is_visible_without_losing_valid_output(peer):
    _, sources, outputs, _ = peer
    broken = sources[0].parent / 'bad.wav'
    broken.write_bytes(b'No media content.')
    result = checked(invoke(peer, sources=[sources[0], broken, sources[1]]), expected=2)
    assert result['status'] == 'partial' and result['done'] == 2 and result['failed'] == 1
    assert [row['status'] for row in result['items']] == ['completed', 'failed', 'completed']
    assert result['items'][1]['error']
    assert not (outputs / 'reviewed' / result['items'][1]['output']).exists()
    for row in (result['items'][0], result['items'][2]):
        assert sha256((outputs / 'reviewed' / row['output']).read_bytes()).hexdigest() == row['candidate']['sha256']


def test_actual_cli_alias_uses_same_measured_single_item_receipt(peer):
    _, sources, _, _ = peer
    result = checked(invoke(peer, command='convert', sources=[sources[0]]))
    assert result['status'] == 'completed' and result['done'] == 1
    assert result['items'][0]['candidate']['frames'] == 3000
    assert result['plan']['sources'][0]['source'] == str(sources[0])


def test_existing_foreign_folder_cannot_be_adopted_as_resumable_batch(peer):
    _, _, outputs, _ = peer
    destination = outputs / 'reviewed'
    destination.mkdir()
    foreign = destination / 'owner.txt'
    foreign.write_bytes(b'Independent existing output.')
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    process = invoke(peer, '--resume')
    assert process.returncode == 1
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before


def test_saved_effect_preset_is_explicit_and_conflicting_controls_are_refused(peer):
    app, _, outputs, _ = peer
    preset = EffectPresetStore(app.root).save('Reviewed quieter output', (Effect('gain', gain_db=-6),))
    result = checked(invoke(peer, '--preview', '--preset', preset.id))
    assert result['plan']['settings']['effects'] == [effect.to_dict() for effect in preset.effects]
    process = invoke(peer, '--preset', preset.id, '--gain-db', '-2')
    assert process.returncode == 1 and 'cannot be silently combined' in process.stderr
    assert not (outputs / 'reviewed').exists()


def test_changed_original_refuses_resume_and_preserves_completed_results(peer):
    _, sources, outputs, _ = peer
    result = checked(invoke(peer))
    directory = outputs / 'reviewed'
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    sources[1].write_bytes(sources[0].read_bytes())
    process = invoke(peer, '--resume')
    assert process.returncode == 1
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == before
    assert result['done'] == 2
