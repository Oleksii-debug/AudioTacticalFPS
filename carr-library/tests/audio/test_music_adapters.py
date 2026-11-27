"""Actual installed FluidSynth audio; ACE-Step transport doubles are not inference."""
from array import array
import ctypes
import hashlib
import json
from pathlib import Path
import shutil
import sys
import wave

import pytest

from carr_library.audio import music
from carr_library.audio.engines import EngineUnavailable, _run
from carr_library.audio.model_manifest import LocalModelManifest, ModelManifestError


@pytest.fixture
def ffmpeg():
    from carr_library.audio.runtime import resolve_ffmpeg
    executable = resolve_ffmpeg()
    if executable is None:
        pytest.skip('Actual music adapter conversion requires installed FFmpeg.')
    return executable


def profile(folder, engine, assets, settings, *, license='synthetic-fixture', **flags):
    folder.mkdir(parents=True, exist_ok=True)
    for name, value in assets.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    files = {name: hashlib.sha256(value).hexdigest() for name, value in assets.items()}
    data = dict(schema_version=1, engine=engine, model_id='explicit-local-test-fixture',
                revision='a' * 40, license=license, license_accepted=True, files=files,
                settings=settings, **flags)
    path = folder / 'manifest.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


@pytest.fixture
def actual_fluid_profile(tmp_path):
    library = Path('/usr/lib/x86_64-linux-gnu/libfluidsynth.so.3.3.4')
    soundfont = Path('/usr/share/sounds/sf2/TimGM6mb.sf2')
    if not library.is_file() or not soundfont.is_file():
        pytest.skip('Actual installed FluidSynth 2.4.4 / reviewed TimGM6mb fixture is unavailable.')
    font_bytes = soundfont.read_bytes()
    assert hashlib.sha256(font_bytes).hexdigest() == 'c5378b62028c920cb11e4803327983fee2f2cdff5dc89c708e39da417e51c854'
    settings = dict(api_source_revision=music.FLUIDSYNTH_API_SOURCE, backend='library',
                    library='libfluidsynth.so', soundfont='instrument.sf2', runtime_version='2.4.4',
                    runtime_license='LGPL-2.1-or-later', timeout_seconds=60,
                    output_rights_notice='TimGM6mb GPL-2.0-only. Private local fixture; asset is not published.')
    return profile(tmp_path / 'fluid-profile', 'fluidsynth',
        {'libfluidsynth.so': library.read_bytes(), 'instrument.sf2': font_bytes,
         'soundfont-license.txt': Path('/usr/share/doc/timgm6mb-soundfont/copyright').read_bytes()},
        settings, license='GPL-2.0-only')


def test_actual_fluidsynth_renders_original_midi_and_resumes(tmp_path, actual_fluid_profile, ffmpeg):
    target = tmp_path / 'instruments.wav'
    settings = dict(duration=16.125, sample_rate=8000, tempo=120, seed=73, engine='fluidsynth',
                    manifest=actual_fluid_profile, ffmpeg=ffmpeg)
    def interrupt(message):
        raise InterruptedError('Stop after a verified real instrumental chunk.')
    with pytest.raises(InterruptedError):
        music.render_music(target, **settings, on_status=interrupt)
    chunk = tmp_path / 'instruments.music-work/part-00000000.wav'
    first = chunk.read_bytes()
    assert not target.exists()
    result = music.render_music(target, **settings)
    assert result['frames'] == 129000 and result['provenance']['reused_chunks'] == 1
    assert result['provenance']['chunks'] == 3 and chunk.read_bytes() == first
    with wave.open(str(target), 'rb') as source:
        assert source.getnframes() == 129000 and source.getframerate() == 8000
        samples = array('h', source.readframes(129000))
        if sys.byteorder != 'little':
            samples.byteswap()
        assert 50 < max(abs(value) for value in samples) < 32767
        assert any(samples[8 * 8000:9 * 8000])
    assert 'SoundFont' in result['provenance']['kind']
    assert result['provenance']['rights'] == 'GPL-2.0-only'
    assert result['provenance']['listening_acceptance'] == 'NOT_RUN'
    sine = music.render_music(tmp_path / 'basic.wav', **{**settings, 'engine':'procedural', 'manifest':None})
    assert result['midi'].read_bytes() == sine['midi'].read_bytes()
    assert target.read_bytes() != sine['path'].read_bytes()


def test_actual_production_scene_selects_fluidsynth_and_rechecks_cached_assets(tmp_path, actual_fluid_profile, ffmpeg):
    from carr_library.audio.production import parse_script, render_project
    from carr_library.audio.ffmpeg import wav_info
    project = tmp_path / 'project'
    (project / 'models').mkdir(parents=True)
    shutil.copytree(actual_fluid_profile.parent, project / 'models/fluid')
    plan = parse_script('[output sample_rate=8000 stems=false]\n'
        '[music engine=fluidsynth model_manifest=fluid/manifest.json gain=0.2]\n[pause 1.25]')
    class NoSpeech:
        def cache_identity(self, voice): return {'fixture': 'no-speech'}
        def synthesize(self, *args, **kwargs): pytest.fail('This instrumental scene contains no speech.')
    result = render_project(plan, project, engine=NoSpeech(), ffmpeg=ffmpeg)
    assert result['status'] == 'complete' and wav_info(result['output']).frames == 10000
    font = project / 'models/fluid/instrument.sf2'
    with font.open('r+b') as stream:
        stream.write(b'changed')
    with pytest.raises(ModelManifestError, match='Checksum'):
        render_project(plan, project, engine=NoSpeech(), ffmpeg=ffmpeg)


def test_soundfont_profile_requires_review_before_starting_worker(tmp_path, ffmpeg, monkeypatch):
    path = profile(tmp_path/'assets', 'fluidsynth', {'font.sf2':b'fixture'},
                   {'api_source_revision':music.FLUIDSYNTH_API_SOURCE})
    monkeypatch.setattr(music, '_run', lambda *a, **k: pytest.fail('Invalid profile must fail before execution.'))
    with pytest.raises(ModelManifestError, match='runtime license'):
        music.render_music(tmp_path/'result.wav', duration=1, engine='fluidsynth', manifest=path, ffmpeg=ffmpeg)


def test_ace_step_requires_trusted_code_and_exact_reviewed_entrypoints(tmp_path, ffmpeg, monkeypatch):
    path = profile(tmp_path/'assets', 'ace-step', {'python':b'fixture'},
                   {'source_revision':music.ACESTEP_SOURCE})
    with pytest.raises(ModelManifestError, match='checkpoints and installed code'):
        music.render_music(tmp_path/'result.wav', duration=1, engine='ace-step', manifest=path, ffmpeg=ffmpeg)
    value = json.loads(path.read_text()); value['trusted_weights'] = True
    value['settings'].update(python='python', package_dir='packages', output_rights_notice='Synthetic contract only')
    code = path.parent / 'packages/acestep/inference.py'; code.parent.mkdir(parents=True); code.write_bytes(b'changed code')
    value['files']['packages/acestep/inference.py'] = hashlib.sha256(code.read_bytes()).hexdigest()
    path.write_text(json.dumps(value))
    with pytest.raises(ModelManifestError, match='entrypoints differ'):
        music.render_music(tmp_path/'result.wav', duration=1, engine='ace-step', manifest=path, ffmpeg=ffmpeg)


def fake_ace_package(folder, *, malicious=None):
    """Small protocol doubles. No model, Torch implementation or inference claim."""
    package = folder / 'packages'; ace = package / 'acestep'; ace.mkdir(parents=True)
    (ace/'__init__.py').write_text('')
    (package/'torch.py').write_text('def manual_seed(seed): pass\ndef set_num_threads(count): pass\n')
    (ace/'model_downloader.py').write_text('def check_model_exists(name, root): return True\n'
                                        'def _check_code_mismatch(name, root): return []\n')
    (ace/'handler.py').write_text('class AceStepHandler:\n'
        ' def initialize_service(self, **kwargs):\n'
        '  assert kwargs["vae_checkpoint"] == "official"\n'
        '  assert self._ensure_models_present(checkpoint_path=None, config_path="model", prefer_source=None) is None\n'
        '  self._sync_model_code_if_needed("model", None)\n'
        '  return "ready", True\n')
    body = '''import pathlib, wave
from types import SimpleNamespace
class GenerationParams:
 def __init__(self, **kwargs): self.__dict__.update(kwargs)
class GenerationConfig:
 def __init__(self, **kwargs): self.__dict__.update(kwargs)
def generate_music(handler, lm, params, config, save_dir):
 assert lm is None and params.instrumental and params.lyrics == '[Instrumental]'
 assert not params.thinking and not params.use_cot_metas and config.batch_size == 1
 assert config.seeds == [params.seed] and not config.use_random_seed
 assert params.timesignature == '4' and config.audio_format == 'wav'
 path = pathlib.Path(save_dir) / 'synthetic.wav'
 with wave.open(str(path), 'wb') as output:
  output.setparams((1,2,8000,0,'NONE','not compressed'))
  output.writeframes(b'\\x10\\x00' * round(params.duration * 8000))
 return SimpleNamespace(success=True, audios=[{'path':str(path)}])
'''
    if malicious == 'network':
        body = 'import socket\nsocket.create_connection(("127.0.0.1", 9))\n' + body
    elif malicious == 'outside':
        body = body.replace("'path':str(path)", "'path':str(pathlib.Path(save_dir).parent / 'outside.wav')")
    (ace/'inference.py').write_text(body)
    return package


def request_for_worker(tmp_path, package):
    stage = tmp_path / 'stage'; stage.mkdir()
    return dict(root=str(tmp_path), stage=str(stage), output=str(stage/'selected.wav'),
                package_dir=str(package), model_name='acestep-v15-turbo', device='cpu',
                duration=10, seed=42, threads=1, tempo=90, inference_steps=8,
                keyscale='C major', prompt='Original instrumental fixture; not real inference.')


def test_isolated_ace_step_contract_and_pcm_receipt_without_weights(tmp_path):
    package = fake_ace_package(tmp_path)
    request = request_for_worker(tmp_path, package)
    result = _run([sys.executable, '-I', '-S', '-B', '-c', music._ACESTEP_WORKER],
                  data=json.dumps(request).encode(), timeout=10)
    assert json.loads(result) == {'engine':'ace-step', 'frames':80000, 'sample_rate':8000, 'channels':1}
    assert Path(request['output']).is_file()
    assert not list(package.rglob('__pycache__'))


@pytest.mark.parametrize('malicious', ['network', 'outside'])
def test_isolated_ace_step_worker_blocks_egress_and_escaped_results(tmp_path, malicious):
    package = fake_ace_package(tmp_path, malicious=malicious)
    request = request_for_worker(tmp_path, package)
    (tmp_path/'outside.wav').write_bytes(b'external unrelated artifact')
    with pytest.raises(EngineUnavailable):
        _run([sys.executable, '-I', '-S', '-B', '-c', music._ACESTEP_WORKER],
             data=json.dumps(request).encode(), timeout=10)
    assert not Path(request['output']).exists()
    assert (tmp_path/'outside.wav').read_bytes() == b'external unrelated artifact'


def test_new_music_engines_are_typed_explicit_and_round_trip():
    from carr_library.audio.models import ProductionPlan
    from carr_library.audio.production import parse_script
    for engine in ('fluidsynth', 'ace-step'):
        plan = parse_script(f'[music engine={engine} model_manifest={engine}/manifest.json]\n[pause 2]')
        assert ProductionPlan.from_dict(plan.to_dict()) == plan
        with pytest.raises(ValueError, match='manifest'):
            parse_script(f'[music engine={engine}]\n[pause 2]')
