from array import array
import hashlib
import json
from pathlib import Path
import shutil
import struct
import sys
import wave

import pytest

from carr_library.audio.engines import EngineUnavailable
from carr_library.audio import music
from carr_library.audio.music import (_wave_frames, generate_music, music_capabilities,
                                      render_music)


@pytest.fixture
def ffmpeg():
    executable = shutil.which('ffmpeg')
    if not executable:
        pytest.skip('Actual local music tests require FFmpeg.')
    return Path(executable).resolve()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('legacy_only', [False, True], ids=['modern', 'legacy'])
def test_filter_file_capability_selects_actual_support_and_cleans_probe(tmp_path, monkeypatch, legacy_only):
    directory = tmp_path / 'Музика з пробілами'
    directory.mkdir()
    calls, scripts = [], []
    cancelled = lambda: False

    def run(command, **options):
        option = next(value for value in command if value in ('-/filter_complex', '-filter_complex_script'))
        script = Path(command[command.index(option) + 1])
        calls.append(option)
        scripts.append(script)
        graph = script.read_text('ascii')
        assert 'atrim=end_sample=1[probe]' in graph
        assert not any('sine=' in str(value) for value in command)
        assert command[-3:] == ['-f', 'null', '-']
        assert options['timeout'] == 15 and options['cancelled'] is cancelled
        assert options['env']['HF_HUB_OFFLINE'] == '1'
        # Reopening for write also catches a still-open NamedTemporaryFile on Windows.
        with script.open('r+b') as stream:
            assert stream.read() == graph.encode('ascii')
        if legacy_only and option == '-/filter_complex':
            raise EngineUnavailable('fixture: unsupported option')
        return b''

    monkeypatch.setattr(music, '_run', run)
    selected = music._filter_file_option(Path('ffmpeg'), directory=directory, cancelled=cancelled)
    expected = '-filter_complex_script' if legacy_only else '-/filter_complex'
    assert selected == expected
    assert calls == (['-/filter_complex', '-filter_complex_script'] if legacy_only else ['-/filter_complex'])
    assert all(not script.exists() for script in scripts)
    assert list(directory.iterdir()) == []


def test_filter_file_capability_rejects_unsupported_engine_without_publication(tmp_path, monkeypatch, ffmpeg):
    original = music._run
    attempts = []

    def reject_graph(command, **options):
        if '-version' in command:
            return original(command, **options)
        attempts.append(command)
        raise EngineUnavailable('fixture: unsupported file graph')

    monkeypatch.setattr(music, '_run', reject_graph)
    with pytest.raises(EngineUnavailable, match='file-based procedural music filters'):
        render_music(tmp_path / 'unsupported.wav', duration=.1, sample_rate=8000, ffmpeg=ffmpeg)
    assert len(attempts) == 2
    assert not list(tmp_path.rglob('*.wav'))
    assert not list(tmp_path.rglob('*.mid'))
    assert not list(tmp_path.rglob('*.music.json'))
    assert not list(tmp_path.rglob('*.filters'))
    assert not list(tmp_path.rglob('.music-filter-probe-*'))


@pytest.mark.parametrize('error', [InterruptedError, TimeoutError, RuntimeError],
                         ids=['cancelled', 'deadline', 'log-budget'])
def test_filter_file_probe_does_not_retry_cancellation_or_resource_failure(tmp_path, monkeypatch, error):
    calls = []

    def stop(command, **options):
        calls.append(command)
        raise error('fixture stopped')

    monkeypatch.setattr(music, '_run', stop)
    with pytest.raises(error, match='fixture stopped'):
        music._filter_file_option(Path('ffmpeg'), directory=tmp_path, cancelled=lambda: False)
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('option', ['-/filter_complex', '-filter_complex_script'], ids=['modern', 'legacy'])
def test_procedural_graph_stays_in_file_and_is_removed_after_render_failure(tmp_path, monkeypatch, option):
    scripts = []

    def failed_render(command, **options):
        script = Path(command[command.index(option) + 1])
        scripts.append(script)
        graph = script.read_text('ascii')
        assert len(graph) > 7000
        assert sum(len(str(value)) for value in command) < 2048
        assert not any('sine=' in str(value) for value in command)
        assert '-filter_complex' not in command
        assert '-map' in command and '[music]' in command
        raise EngineUnavailable('fixture render failure')

    monkeypatch.setattr(music, '_run', failed_render)
    with pytest.raises(EngineUnavailable, match='fixture render failure'):
        music._render_procedural_chunk(tmp_path / 'part.wav', first_bar=0, start_frame=0,
            frame_count=128000, total_frames=128000, sample_rate=8000, tempo=60,
            mood='energetic', seed=1, gain=.18, ffmpeg=Path('ffmpeg'),
            filter_file_option=option, cancelled=lambda: False)
    assert len(scripts) == 1 and not scripts[0].exists()


def test_actual_multichunk_render_probes_once_and_passes_only_file_arguments(tmp_path, monkeypatch, ffmpeg):
    original = music._run
    probe_options, render_options = [], []

    def record(command, **options):
        if '[probe]' in command or '[music]' in command:
            selected = next(value for value in command if value in ('-/filter_complex', '-filter_complex_script'))
            script = Path(command[command.index(selected) + 1])
            assert script.is_file() and script.stat().st_size < 65536
            assert not any('sine=' in str(value) for value in command)
            (probe_options if '[probe]' in command else render_options).append(selected)
        return original(command, **options)

    monkeypatch.setattr(music, '_run', record)
    result = render_music(tmp_path / 'two-parts.wav', duration=9.125, sample_rate=8000,
                          tempo=120, ffmpeg=ffmpeg)
    assert result['frames'] == 73000 and result['provenance']['chunks'] == 2
    assert _wave_frames(result['path'])['frames'] == 73000
    assert probe_options in (['-/filter_complex'], ['-/filter_complex', '-filter_complex_script'])
    assert render_options == [probe_options[-1], probe_options[-1]]
    assert not list(tmp_path.rglob('*.filters'))


def test_actual_procedural_score_is_exact_reproducible_audio_with_midi(tmp_path, ffmpeg):
    settings = dict(duration=3.25, mood='suspense', tempo=91, seed=62, ffmpeg=ffmpeg)
    first = render_music(tmp_path/'first.wav', **settings)
    second = render_music(tmp_path/'second.wav', **settings)
    assert first['frames'] == 156000
    assert digest(first['path']) == digest(second['path'])
    assert first['midi'].read_bytes() == second['midi'].read_bytes()
    midi = first['midi'].read_bytes()
    assert midi[:14] == b'MThd' + struct.pack('>IHHH', 6, 0, 1, 480)
    assert struct.unpack('>I', midi[18:22])[0] == len(midi)-22
    assert midi.endswith(b'\xff\x2f\x00')
    with wave.open(str(first['path']), 'rb') as audio:
        assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
        assert audio.getframerate() == 48000 and audio.getnframes() == 156000
        samples = array('h', audio.readframes(audio.getnframes()))
        if sys.byteorder != 'little':
            samples.byteswap()
        assert 50 < max(abs(sample) for sample in samples) < 16000
    provenance = json.loads(first['manifest'].read_text())
    assert provenance['engine'] == 'procedural'
    assert 'not neural' not in provenance['kind']  # the actual kind is already explicit
    assert provenance['kind'] == 'procedural original notes / FFmpeg sine synthesis'
    assert provenance['sha256'] == digest(first['path'])


def test_actual_changed_seed_changes_composition(tmp_path, ffmpeg):
    first = generate_music(tmp_path/'first.wav', duration=4, seed=1, ffmpeg=ffmpeg)
    second = generate_music(tmp_path/'second.wav', duration=4, seed=2, ffmpeg=ffmpeg)
    assert digest(first) != digest(second)


def test_actual_cancel_resume_reuses_verified_chunk_and_recovers_corruption(tmp_path, ffmpeg):
    target = tmp_path/'resumable.wav'
    cancelled = False
    def progress(message):
        nonlocal cancelled
        cancelled = True
    options = dict(duration=20, sample_rate=8000, tempo=120, seed=13, ffmpeg=ffmpeg)
    with pytest.raises(InterruptedError):
        render_music(target, **options, cancelled=lambda: cancelled, on_status=progress)
    assert not target.exists()
    state = tmp_path/'resumable.music-work'/'state.json'
    value = json.loads(state.read_text())
    assert len(value['chunks']) == 1
    original = (state.parent/'part-00000000.wav').read_bytes()
    finished = render_music(target, **options)
    assert finished['provenance']['reused_chunks'] == 1
    assert (state.parent/'part-00000000.wav').read_bytes() == original
    # Simulate a lost final publication followed by a corrupt accepted chunk.
    target.unlink()
    target.with_suffix('.mid').unlink()
    target.with_suffix('.music.json').unlink()
    (state.parent/'part-00000000.wav').write_bytes(b'corrupted checkpoint')
    repaired = render_music(target, **options)
    assert repaired['provenance']['reused_chunks'] == 2
    assert (state.parent/'part-00000000.wav').read_bytes() == original


def test_actual_more_than_ten_minutes_has_no_total_duration_ceiling(tmp_path, ffmpeg):
    result = render_music(tmp_path/'extended.wav', duration=601.125, sample_rate=8000,
                          mood='calm', tempo=30, seed=91, ffmpeg=ffmpeg)
    assert result['frames'] == 4_809_000
    assert _wave_frames(result['path'])['frames'] == 4_809_000
    assert result['provenance']['chunks'] == 19
    assert 'neural' not in result['provenance']['kind']


def test_actual_output_and_companions_cannot_be_overwritten(tmp_path, ffmpeg):
    target = generate_music(tmp_path/'once.wav', duration=.5, ffmpeg=ffmpeg)
    previous = target.read_bytes()
    with pytest.raises(FileExistsError):
        generate_music(target, duration=.5, ffmpeg=ffmpeg)
    assert target.read_bytes() == previous
    (tmp_path/'other.mid').write_bytes(b'owned by another project')
    with pytest.raises(FileExistsError):
        generate_music(tmp_path/'other.wav', duration=.5, ffmpeg=ffmpeg)
    assert (tmp_path/'other.mid').read_bytes() == b'owned by another project'
    assert not (tmp_path/'other.wav').exists()


def test_changed_checkpoint_settings_need_new_destination(tmp_path, ffmpeg):
    def stop(message):
        raise InterruptedError('synthetic interruption')
    with pytest.raises(InterruptedError):
        render_music(tmp_path/'cue.wav', duration=1, ffmpeg=ffmpeg, on_status=stop)
    with pytest.raises(ValueError, match='another score'):
        render_music(tmp_path/'cue.wav', duration=1, seed=2, ffmpeg=ffmpeg)


@pytest.mark.parametrize('parameter,value', [('duration',float('nan')), ('duration',0),
    ('tempo',0), ('gain',-1), ('seed',True), ('sample_rate',12345), ('mood','sine;amovie=https://invalid.test')])
def test_untrusted_scene_settings_cannot_become_filters(tmp_path, ffmpeg, parameter, value):
    settings = dict(duration=1, ffmpeg=ffmpeg)
    settings[parameter] = value
    with pytest.raises(ValueError):
        render_music(tmp_path/'invalid.wav', **settings)
    assert not (tmp_path/'invalid.wav').exists()


def test_models_and_disk_budgets_fail_before_generation(tmp_path, ffmpeg):
    with pytest.raises(EngineUnavailable, match='manifest'):
        render_music(tmp_path/'neural.wav', duration=1, engine='musicgen', ffmpeg=ffmpeg)
    with pytest.raises(EngineUnavailable, match='not implemented'):
        render_music(tmp_path/'ace.wav', duration=1, engine='ace-step', ffmpeg=ffmpeg)
    with pytest.raises(ValueError, match='disk budget'):
        render_music(tmp_path/'large.wav', duration=100, ffmpeg=ffmpeg, max_output_bytes=10)
    assert not list(tmp_path.glob('*.wav'))
    capability = next(x for x in music_capabilities() if x['engine'] == 'musicgen')
    assert capability['implemented'] is True
    assert 'must be verified' in capability['acceptance']


def test_rf64_parser_validates_large_frame_count_without_reading_audio(tmp_path):
    # Sparse PCM fixture, not a claim of generated multi-hour neural music.
    frames = 2_200_000_000
    data_bytes = frames * 2
    path = tmp_path/'sparse-rf64.wav'
    with path.open('wb') as stream:
        stream.write(b'RF64\xff\xff\xff\xffWAVE')
        stream.write(b'ds64' + struct.pack('<IQQQI',28,data_bytes+72,data_bytes,frames,0))
        stream.write(b'fmt ' + struct.pack('<IHHIIHH',16,1,1,48000,96000,2,16))
        stream.write(b'data\xff\xff\xff\xff')
        stream.seek(data_bytes-1,1)
        stream.write(b'\x00')
    assert _wave_frames(path)['frames'] == frames
    with path.open('r+b') as stream:
        stream.truncate(100)
    with pytest.raises(ValueError, match='truncated'):
        _wave_frames(path)
