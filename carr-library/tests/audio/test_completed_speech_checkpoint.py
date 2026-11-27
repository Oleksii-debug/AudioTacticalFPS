"""A completed actual speech chunk survives a stop before its cache receipt."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import engines, jobs, production


@pytest.fixture
def actual_engine():
    selected = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    if selected is None or not Path(selected).is_file():
        pytest.skip('This regression requires the selected actual FFmpeg/Flite runtime.')
    engine = engines.FliteEngine(Path(selected))
    assert 'flite:slt' in {voice.id for voice in engine.voices()}
    return Path(selected), engine


class ObservedEngine:
    def __init__(self, engine, stopped, after=None):
        self.engine, self.stopped, self.after = engine, stopped, after
        self.completed = []
        self.forbid = False

    def cache_identity(self, voice, *, cancelled=lambda: False):
        return self.engine.cache_identity(voice, cancelled=cancelled)

    def synthesize(self, text, output, **options):
        assert not self.forbid, 'A verified accepted speech chunk must not be synthesized again.'
        result = self.engine.synthesize(text, output, **options)
        self.completed.append({'sha256': sha256(output.read_bytes()).hexdigest(),
                               'frames': production.audio.wav_info(output).frames})
        if self.after:
            self.after(output)
        return result


def plan():
    return production.parse_script('[output sample_rate=8000 stems=false]\n'
        'One exact completed segment.', voice='flite:slt', language='en')


def journal(root):
    return json.loads((root / 'render-journal.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('boundary', ['synthesize-return', 'cache-hash', 'cache-publication'])
def test_actual_completed_speech_is_sealed_before_stop_and_never_synthesized_again(
        tmp_path, actual_engine, monkeypatch, boundary):
    ffmpeg, native = actual_engine
    root, stopped = tmp_path / 'project', [False]
    engine = ObservedEngine(native, stopped,
        after=(lambda output: stopped.__setitem__(0, True)) if boundary == 'synthesize-return' else None)
    original_hash, original_publish = jobs.sha256_file, jobs.publish_new
    if boundary == 'cache-hash':
        def digest(path, **options):
            result = original_hash(path, **options)
            if Path(path).parent.name.startswith('.work-'):
                stopped[0] = True
            return result
        monkeypatch.setattr(jobs, 'sha256_file', digest)
    elif boundary == 'cache-publication':
        def publish(source, destination, **options):
            result = original_publish(source, destination, **options)
            if Path(destination).parent == root / 'cache':
                stopped[0] = True
            return result
        monkeypatch.setattr(jobs, 'publish_new', publish)
    selected = plan()
    immutable = selected.to_dict()
    with pytest.raises(jobs.RenderCancelled):
        production.render_project(selected, root, engine=engine, ffmpeg=ffmpeg,
                                  cancelled=lambda: stopped[0])
    assert stopped[0] and len(engine.completed) == 1
    assert selected.to_dict() == immutable
    assert journal(root)['state'] == 'cancelled'
    assert not list(root.glob('.work-*')) and not list(root.rglob('complete.json'))
    receipts = list((root / 'cache').glob('*.json'))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding='utf-8'))
    assert receipt['provenance']['kind'] == 'speech'
    assert receipt['provenance']['engine']['voice'] == 'flite:slt'
    assert receipt['sha256'] == engine.completed[0]['sha256']
    assert receipt['frames'] == engine.completed[0]['frames']
    audio = receipts[0].with_suffix('.wav')
    before = (audio.read_bytes(), audio.stat().st_mtime_ns, receipts[0].read_bytes())
    engine.forbid = True
    monkeypatch.setattr(jobs, 'sha256_file', original_hash)
    monkeypatch.setattr(jobs, 'publish_new', original_publish)
    result = production.render_project(selected, root, engine=engine, ffmpeg=ffmpeg)
    assert result['status'] == 'complete' and Path(result['output']).is_file()
    assert len(engine.completed) == 1
    assert (audio.read_bytes(), audio.stat().st_mtime_ns, receipts[0].read_bytes()) == before


@pytest.mark.parametrize('damage', ['truncated', 'not-pcm', 'hardlinked'])
def test_late_stop_never_receipts_corrupt_or_aliased_engine_output(
        tmp_path, actual_engine, damage):
    ffmpeg, native = actual_engine
    root, stopped, foreign = tmp_path / 'project', [False], tmp_path / 'foreign.wav'
    def corrupt(output):
        if damage == 'truncated':
            output.write_bytes(output.read_bytes()[:20])
        elif damage == 'not-pcm':
            output.write_bytes(b'Not a completed PCM container.')
        else:
            foreign.write_bytes(output.read_bytes())
            output.unlink()
            os.link(foreign, output)
        stopped[0] = True
    engine = ObservedEngine(native, stopped, after=corrupt)
    with pytest.raises((ValueError, OSError)):
        production.render_project(plan(), root, engine=engine, ffmpeg=ffmpeg,
                                  cancelled=lambda: stopped[0])
    assert len(engine.completed) == 1
    assert journal(root)['state'] == 'failed'
    assert not list((root / 'cache').glob('*.wav')) and not list((root / 'cache').glob('*.json'))
    assert not list(root.glob('.work-*')) and not list(root.rglob('complete.json'))
    if damage == 'hardlinked':
        assert sha256(foreign.read_bytes()).hexdigest() == engine.completed[0]['sha256']


def test_cancel_before_render_starts_no_engine_or_completed_chunk(tmp_path, monkeypatch):
    class Never:
        def synthesize(self, *args, **options):
            pytest.fail('Pre-start cancellation cannot synthesize speech.')
    monkeypatch.setattr(production.audio, 'ffmpeg_identity',
                        lambda *args, **options: pytest.fail('No native runtime may start.'))
    with pytest.raises(jobs.RenderCancelled):
        production.render_project(plan(), tmp_path, engine=Never(), ffmpeg=tmp_path / 'unused',
                                  cancelled=lambda: True)
    assert not (tmp_path / 'cache').exists() and not (tmp_path / 'render-journal.json').exists()


def test_seal_failure_is_not_masked_by_late_stop_and_restores_original_callback(
        tmp_path, actual_engine, monkeypatch):
    ffmpeg, native = actual_engine
    root, stopped, observed = tmp_path / 'project', [False], []
    engine = ObservedEngine(native, stopped, after=lambda output: stopped.__setitem__(0, True))
    original_seal, original_publish = jobs.ChunkCache.seal, jobs.publish_new
    def seal(self, *args, **options):
        observed.append((self, self.cancelled))
        return original_seal(self, *args, **options)
    def failed(source, destination, **options):
        if Path(destination).parent == root / 'cache':
            raise OSError('Synthetic cache publication failure')
        return original_publish(source, destination, **options)
    monkeypatch.setattr(jobs.ChunkCache, 'seal', seal)
    monkeypatch.setattr(jobs, 'publish_new', failed)
    with pytest.raises(OSError, match='Synthetic cache publication failure'):
        production.render_project(plan(), root, engine=engine, ffmpeg=ffmpeg,
                                  cancelled=lambda: stopped[0])
    assert len(observed) == 1 and observed[0][0].cancelled is not observed[0][1]
    assert observed[0][0].cancelled() is True
    assert journal(root)['state'] == 'failed' and len(engine.completed) == 1
    assert not list((root / 'cache').glob('*.wav')) and not list((root / 'cache').glob('*.json'))
    assert not list(root.glob('.work-*')) and not list(root.rglob('complete.json'))


def pcm(path):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 800, 'NONE', 'not compressed'))
        stream.writeframes(b'\x10\x00' * 800)
    return path


def test_completed_speech_cancel_deferral_has_a_bounded_deadline_and_restores_callback(
        tmp_path, monkeypatch):
    source = pcm(tmp_path / 'closed.wav')
    cache = jobs.ChunkCache(tmp_path / 'cache')
    requested = lambda: True
    cache.cancelled = requested
    observed = []
    ticks = iter((0.0, 0.0, 1.0, 6.0))
    def clock():
        value = next(ticks, 6.0)
        observed.append(value)
        return value
    monkeypatch.setattr(production, 'time', SimpleNamespace(monotonic=clock))
    with pytest.raises(jobs.RenderCancelled):
        production._seal_completed_speech(cache, 'a' * 64, source, provenance={'kind': 'speech'})
    assert cache.cancelled is requested and max(observed) >= 5.0
    assert source.is_file() and not list(cache.root.glob('*.wav')) and not list(cache.root.glob('*.json'))


def test_cache_copy_must_match_the_hash_recorded_before_publication(tmp_path, monkeypatch):
    source = pcm(tmp_path / 'source.wav')
    before = sha256(source.read_bytes()).hexdigest()
    cache = jobs.ChunkCache(tmp_path / 'cache')
    original, calls = jobs.publish_new, []
    def changed(path, destination, **options):
        if Path(path) == source:
            calls.append(options)
            assert options['expected_sha256'] == before
            info = source.stat()
            raw = bytearray(source.read_bytes())
            raw[-1] ^= 1
            source.write_bytes(raw)
            os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        return original(path, destination, **options)
    monkeypatch.setattr(jobs, 'publish_new', changed)
    with pytest.raises((ValueError, RuntimeError)):
        cache.seal('b' * 64, source, provenance={'kind': 'speech'})
    assert len(calls) == 1
    assert not list(cache.root.glob('*.wav')) and not list(cache.root.glob('*.json'))
