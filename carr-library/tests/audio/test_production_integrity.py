"""Real production failure/recovery tests, using explicitly synthetic audio."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import pytest

from carr_library.audio import ffmpeg as audio, jobs, production


FFMPEG = Path(os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg') or '/missing-ffmpeg')
pytestmark = pytest.mark.skipif(not FFMPEG.is_file(), reason='Requires installed FFmpeg')


class SyntheticEngine:
    def cache_identity(self, voice):
        return {'fixture': 'constant-pcm-v1'}

    def synthesize(self, text, output, **kwargs):
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 8000, 800, 'NONE', 'not compressed'))
            stream.writeframes(b'\x10\x00' * 800)


def render(plan, root, **kwargs):
    return production.render_project(plan, root, ffmpeg=FFMPEG,
        engine=kwargs.pop('engine', SyntheticEngine()), **kwargs)


def test_builtin_discovery_can_be_cancelled_without_breaking_legacy_engine(tmp_path):
    callbacks = []

    class CancellableEngine(SyntheticEngine):
        def cache_identity(self, voice, *, cancelled):
            callbacks.append(cancelled())
            return super().cache_identity(voice)

    plan = production.parse_script('[output sample_rate=8000 stems=false]\nFixture')
    result = render(plan, tmp_path / 'new', engine=CancellableEngine())
    assert callbacks == [False]
    assert audio.wav_info(result['output']).frames == 800
    legacy = render(plan, tmp_path / 'legacy')
    assert audio.wav_info(legacy['output']).frames == 800


def test_engine_identity_internal_typeerror_is_not_retried(tmp_path):
    calls = []

    class BrokenIdentity(SyntheticEngine):
        def cache_identity(self, voice, *, cancelled=lambda: False):
            calls.append(voice)
            raise TypeError('A genuine adapter defect')

    with pytest.raises(TypeError, match='adapter defect'):
        render(production.parse_script('Fixture'), tmp_path, engine=BrokenIdentity())
    assert len(calls) == 1


def test_changed_source_after_preflight_does_not_seal_wrong_cache_key(tmp_path, monkeypatch):
    assets = tmp_path / 'assets'
    assets.mkdir()
    source = audio.silence(assets / 'door.wav', 800, sample_rate=8000)
    original = audio.process_voice

    def change_before_decode(source, output, **kwargs):
        offset = audio.wav_info(source).data_offset
        with Path(source).open('r+b') as stream:
            stream.seek(offset)
            stream.write(b'\x01\x00' * 1600)
        return original(source, output, **kwargs)

    monkeypatch.setattr(audio, 'process_voice', change_before_decode)
    plan = production.parse_script('[output sample_rate=8000 stems=false]\n[sound door.wav]')
    with pytest.raises(RuntimeError, match='changed'):
        render(plan, tmp_path)
    assert not list((tmp_path / 'cache').glob('*.wav'))
    assert not list((tmp_path / 'renders').iterdir())


def test_mutation_during_real_ffmpeg_processing_is_not_published(tmp_path, monkeypatch):
    source = audio.silence(tmp_path / 'source.wav', 800, sample_rate=8000)
    original = audio.run_ffmpeg

    def change_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        offset = audio.wav_info(source).data_offset
        with source.open('r+b') as stream:
            stream.seek(offset)
            stream.write(b'\x01\x00' * 1600)
        return result

    monkeypatch.setattr(audio, 'run_ffmpeg', change_after_read)
    with pytest.raises(RuntimeError, match='changed'):
        audio.process_voice(source, tmp_path / 'output.wav', ffmpeg=FFMPEG, sample_rate=8000)
    assert not (tmp_path / 'output.wav').exists()


def test_existing_project_storage_is_counted_before_render(tmp_path):
    old = tmp_path / 'music-jobs' / 'old'
    old.mkdir(parents=True)
    (old / 'retained.wav').write_bytes(b'x' * (1024 ** 2))
    plan = replace(production.parse_script('[output sample_rate=8000 stems=false]\n[pause .1]'),
                   disk_budget_bytes=1024 ** 2)
    with pytest.raises(RuntimeError, match='disk budget'):
        render(plan, tmp_path)
    assert not list((tmp_path / 'cache').glob('*.wav'))


def test_final_directory_race_preserves_other_empty_directory(tmp_path, monkeypatch):
    original = production.publish_noreplace
    raced = []

    def race(source, destination, **options):
        destination.mkdir()
        raced.append(destination.stat().st_ino)
        return original(source, destination, **options)

    monkeypatch.setattr(production, 'publish_noreplace', race)
    plan = production.parse_script('[output sample_rate=8000 stems=false]\n[pause .1]')
    with pytest.raises(FileExistsError):
        render(plan, tmp_path)
    output = next((tmp_path / 'renders').iterdir())
    assert output.stat().st_ino == raced[0]
    assert not list(output.iterdir())
    assert json.loads((tmp_path / 'render-journal.json').read_text('utf-8'))['state'] == 'failed'


def test_process_death_after_complete_directory_move_resumes_verified_output(tmp_path):
    code = """
import os, sys
from carr_library.audio import production
class UnusedEngine:
    def synthesize(self, *args, **kwargs):
        raise AssertionError('Silence requires no speech engine')
original = production.publish_noreplace
def stop_after_move(source, destination, **options):
    original(source, destination, **options)
    os._exit(17)
production.publish_noreplace = stop_after_move
plan = production.parse_script('[output sample_rate=8000 stems=false]\\n[pause .1]')
production.render_project(plan, sys.argv[1], engine=UnusedEngine(), ffmpeg=sys.argv[2])
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(production.__file__).parents[2]))
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path), str(FFMPEG)],
                            env=environment, capture_output=True, timeout=30)
    assert result.returncode == 17, result.stderr.decode(errors='replace')
    before = json.loads((tmp_path / 'render-journal.json').read_text('utf-8'))
    assert before['state'] == 'running'
    plan = production.parse_script('[output sample_rate=8000 stems=false]\n[pause .1]')
    completed = render(plan, tmp_path)
    assert audio.wav_info(completed['output']).frames == 800
    assert json.loads((tmp_path / 'render-journal.json').read_text('utf-8'))['state'] == 'complete'
    assert len(list((tmp_path / 'renders').iterdir())) == 1
    assert Path(completed['output']).stat().st_nlink == 1


def test_editing_export_cannot_damage_sealed_scene(tmp_path):
    plan = production.parse_script('[output sample_rate=8000 stems=false]\nFixture')
    completed = render(plan, tmp_path)
    hashes = {path: jobs.sha256_file(path) for path in (tmp_path / 'cache').glob('*.wav')}
    chapter = Path(completed['output']).parent / 'chapter-000001.wav'
    chapter.write_bytes(b'user changed exported chapter')
    assert all(jobs.sha256_file(path) == checksum for path, checksum in hashes.items())
    repaired = render(plan, tmp_path)
    assert audio.wav_info(Path(repaired['output']).parent / 'chapter-000001.wav').frames == 800


def test_inherited_ffreport_does_not_create_unrequested_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FFREPORT', 'file=unrequested-ffmpeg-report.log:level=48')
    report = tmp_path / 'unrequested-ffmpeg-report.log'
    audio.ffmpeg_identity(FFMPEG)
    assert not report.exists()
    source = audio.silence(tmp_path / 'input.wav', 800, sample_rate=8000)
    output = audio.process_voice(source, tmp_path / 'output.wav', ffmpeg=FFMPEG, sample_rate=8000)
    assert audio.wav_info(output).frames == 800
    assert not report.exists()
