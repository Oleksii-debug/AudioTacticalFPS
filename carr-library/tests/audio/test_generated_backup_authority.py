"""Passive backup and completed-media boundaries; all audio here is synthetic."""
from hashlib import sha256
import json
import os
from pathlib import Path
import wave

import pytest

from carr_library.audio import ffmpeg as audio, jobs, production
from carr_library.audio.models import FORMATS


KEY = 'a' * 64
FORMATS_TO_CHECK = ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'aac', 'wma')


def _wave(path, *, frames=80, rate=8000, channels=2, sample=b'\x10\x00'):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(sample * frames * channels)
    return path


def _write_receipt(directory, result):
    result = dict(result)
    result['files'] = [
        {'name': path.name, 'size': path.stat().st_size, 'sha256': sha256(path.read_bytes()).hexdigest()}
        for path in sorted(directory.iterdir()) if path.is_file() and path.name != 'complete.json']
    (directory / 'complete.json').write_text(
        json.dumps(result, sort_keys=True, allow_nan=False), encoding='utf-8')
    return result


def _completed(directory, *, frames=80, rate=8000):
    """Minimal legacy-compatible receipt for the existing read-only consumer."""
    directory.mkdir(parents=True)
    _wave(directory / 'master.wav', frames=frames, rate=rate)
    (directory / 'subtitles.srt').write_text('1\n00:00:00,000 --> 00:00:00,010\nSynthetic.\n',
                                            encoding='utf-8')
    return _write_receipt(directory, {
        'schema_version': 1, 'status': 'complete', 'run_key': KEY,
        'output_name': 'master.wav', 'stem_names': [],
        'frames': frames, 'sample_rate': rate, 'duration_seconds': frames / rate})


def _files(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob('*') if path.is_file() and not path.name.endswith('.lock')}


@pytest.fixture
def ffmpeg():
    from carr_library.audio.runtime import resolve_ffmpeg
    selected = resolve_ffmpeg()
    if selected is None:
        pytest.skip('The actual selected FFmpeg is required for the real-format controls.')
    return selected


@pytest.mark.parametrize('format', FORMATS_TO_CHECK)
def test_resealed_wrong_container_is_a_refusal_not_a_regeneration_request(tmp_path, monkeypatch, format):
    assert set(FORMATS_TO_CHECK) == set(FORMATS)
    directory = tmp_path / KEY
    result = _completed(directory)
    name = 'master.' + format
    (directory / name).write_bytes(b'Not an audio container, with a self-consistent retained checksum.' * 2)
    result['output_name'] = name
    _write_receipt(directory, result)
    before = _files(directory)
    monkeypatch.setattr(audio, 'ffmpeg_identity', lambda *a, **k: pytest.fail('inspection activated FFmpeg'))
    monkeypatch.setattr(audio, 'run_ffmpeg', lambda *a, **k: pytest.fail('inspection started a decoder'))
    with pytest.raises(ValueError, match='completed production'):
        production._verified_output(directory, KEY, lambda: False)
    assert _files(directory) == before
    assert not (directory.parent / 'quarantine').exists()


@pytest.mark.parametrize('damage', [
    'frames', 'sample-rate', 'duration', 'frames-bool', 'duration-bool',
    'mono-master', 'stem-frames', 'stem-rate', 'stem-not-pcm', 'output-not-audio',
])
def test_completed_receipt_must_match_actual_pcm_and_stems(tmp_path, damage):
    directory = tmp_path / KEY
    frames = 1 if damage == 'frames-bool' else 8000 if damage == 'duration-bool' else 80
    result = _completed(directory, frames=frames)
    if damage == 'frames':
        result['frames'] += 1
    elif damage == 'sample-rate':
        result['sample_rate'] = 16000
    elif damage == 'duration':
        result['duration_seconds'] += 0.5
    elif damage == 'frames-bool':
        result['frames'] = True
    elif damage == 'duration-bool':
        result['duration_seconds'] = True
    elif damage == 'mono-master':
        _wave(directory / 'master.wav', frames=frames, channels=1)
    elif damage in ('stem-frames', 'stem-rate'):
        _wave(directory / 'stem-001.wav',
              frames=frames - 1 if damage == 'stem-frames' else frames,
              rate=16000 if damage == 'stem-rate' else 8000)
        result['stem_names'] = ['stem-001.wav']
    elif damage == 'stem-not-pcm':
        result['stem_names'] = ['subtitles.srt']
    elif damage == 'output-not-audio':
        result['output_name'] = 'subtitles.srt'
    _write_receipt(directory, result)
    before = _files(directory)
    with pytest.raises(ValueError, match='completed'):
        production._verified_output(directory, KEY, lambda: False)
    assert _files(directory) == before


def test_pcm_inspection_keeps_the_checksum_file_identity_held(tmp_path, monkeypatch):
    directory = tmp_path / KEY
    _completed(directory)
    replacement = _wave(tmp_path / 'replacement.wav', sample=b'\x20\x00')
    actual = audio.wav_info
    observed = {}
    def replace_after_hash(path, **options):
        if Path(path) == directory / 'master.wav' and not observed:
            try:
                os.replace(replacement, path)
            except PermissionError:
                observed['writer_blocked'] = True
            else:
                observed['foreign_replacement'] = True
        return actual(path, **options)
    monkeypatch.setattr(audio, 'wav_info', replace_after_hash)
    try:
        result = production._verified_output(directory, KEY, lambda: False)
    except RuntimeError as error:
        assert observed == {'foreign_replacement': True}
        assert 'changed' in str(error)
    else:
        assert observed == {'writer_blocked': True}
        assert result['status'] == 'complete'
    assert not (directory.parent / 'quarantine').exists()


@pytest.mark.parametrize('format', FORMATS_TO_CHECK)
def test_real_encoder_outputs_have_passive_runtime_free_completion_checks(tmp_path, monkeypatch, ffmpeg, format):
    """Actual FFmpeg containers; this is not an ASR/TTS or perceptual-quality claim."""
    directory = tmp_path / KEY
    result = _completed(directory, frames=800)
    if format != 'wav':
        audio.transcode(directory / 'master.wav', directory / ('master.' + format),
                        ffmpeg=ffmpeg, format=format)
        result['output_name'] = 'master.' + format
    _write_receipt(directory, result)
    before = _files(directory)
    monkeypatch.setattr(audio, 'ffmpeg_identity', lambda *a, **k: pytest.fail('passive inspection rediscovered a runtime'))
    monkeypatch.setattr(audio, 'run_ffmpeg', lambda *a, **k: pytest.fail('passive inspection executed a runtime'))
    verified = production._verified_output(directory, KEY, lambda: False)
    assert verified['status'] == 'complete' and verified['output_name'] == 'master.' + format
    assert 'plan_sha256' not in verified  # Legacy plan binding remains the existing renderer's job.
    assert _files(directory) == before
    if format != 'wav':
        (directory / 'master.wav').unlink()
        _write_receipt(directory, result)
        remaining = _files(directory)
        with pytest.raises(ValueError, match='actual PCM master'):
            production._verified_output(directory, KEY, lambda: False)
        assert _files(directory) == remaining


def _managed(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.ui.audio_studio import StudioController
    app = LibraryApp(tmp_path / 'library')
    studio = StudioController(private_root=app.root)
    studio.new('[output sample_rate=8000 stems=false]\nRetained original.', input_kind='script')
    studio.configure(voice='fixture:local')
    source = studio.save_in_library()
    return app, studio, source


@pytest.mark.parametrize('state', ['missing-marker', 'old-marker', 'bad-pcm', 'bad-companion'])
def test_old_or_incomplete_cache_backup_stays_passive_until_explicit_reuse(tmp_path, monkeypatch, state):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    from carr_library.ui.audio_studio import StudioController
    app, studio, source = _managed(tmp_path)
    cache = jobs.ChunkCache(studio.output_dir / 'cache')
    staged = _wave(tmp_path / 'source.wav')
    companions = ()
    if state == 'bad-companion':
        staged.with_suffix('.mid').write_bytes(b'MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0')
        staged.with_suffix('.music.json').write_text('{"fixture":"synthetic"}', encoding='utf-8')
        companions = ('.mid', '.music.json')
    cached, metadata = cache.seal(KEY, staged, companions=companions)
    marker = cache.root / (KEY + '.json')
    if state == 'missing-marker':
        marker.unlink()
    elif state == 'old-marker':
        marker.write_text('{"source":"retained old inert cache"}', encoding='utf-8')
    elif state == 'bad-pcm':
        from carr_library.ingest.restore import RestoreError
        original = cached.read_bytes()
        # The existing backup envelope guard must still reject wrong magic.
        cached.write_bytes(b'NOPE' + original[4:])
        with pytest.raises(RestoreError, match='not a WAV or RF64'):
            create_backup(app, tmp_path / 'wrong-envelope-backup')
        # A valid RIFF/RF64 + WAVE envelope is not a valid PCM size table.
        # Preserve byte length and reseal its hash so explicit reuse must
        # actually inspect the deeper container instead of a hash mismatch.
        cached.write_bytes(original[:4] + b'\0' * 4 + original[8:])
        with pytest.raises(ValueError, match='size declaration'):
            audio.wav_info(cached)
        metadata['sha256'] = sha256(cached.read_bytes()).hexdigest()
        marker.write_text(json.dumps(metadata), encoding='utf-8')
    else:
        cached.with_suffix('.mid').write_bytes(b'changed old retained companion')
    retained = {path.name: path.read_bytes() for path in cache.root.iterdir() if path.is_file()}
    original_source = source.read_bytes()
    def forbidden(*args, **options):
        pytest.fail('Passive backup, verification, restore and open may not activate generated media.')
    with monkeypatch.context() as blocked:
        blocked.setattr(jobs.ChunkCache, 'get', forbidden)
        blocked.setattr(production, '_verified_output', forbidden)
        blocked.setattr(production, 'render_project', forbidden)
        blocked.setattr(audio, 'ffmpeg_identity', forbidden)
        backup = tmp_path / 'backup'
        digest = create_backup(app, backup)
        verify_backup(backup, digest)
        restored = restore_backup(backup, tmp_path / 'restored', digest)
        reopened = StudioController(private_root=restored)
        reopened.open(restored / source.relative_to(app.root))
        assert reopened.result is None
        rebackup = tmp_path / 'rebackup'
        second = create_backup(LibraryApp(restored), rebackup)
        verify_backup(rebackup, second)
    restored_cache = reopened.output_dir / 'cache'
    assert {path.name: path.read_bytes() for path in restored_cache.iterdir() if path.is_file()} == retained
    assert source.read_bytes() == original_source
    assert not (restored_cache / 'quarantine').exists()
    # Only this new explicit consumer call may quarantine an invalid cache.
    assert jobs.ChunkCache(restored_cache).get(KEY) is None
    assert {path.name.rsplit('.', 1)[0]: path.read_bytes()
            for path in (restored_cache / 'quarantine').iterdir()} == retained
    assert {path.name: path.read_bytes() for path in cache.root.iterdir() if path.is_file()} == retained


def test_restored_resealed_bad_completion_refuses_before_any_fresh_work(tmp_path, monkeypatch, ffmpeg):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    from carr_library.security import admission
    from carr_library.ui.audio_studio import StudioController

    class SyntheticEngine:
        def __init__(self):
            self.calls = 0
        def cache_identity(self, voice):
            return {'fixture': 'restored-completion-boundary-v1', 'voice': voice}
        def synthesize(self, content, output, **options):
            self.calls += 1
            return _wave(output, frames=800, channels=1)

    app = LibraryApp(tmp_path / 'library')
    engine = SyntheticEngine()
    studio = StudioController(private_root=app.root, registry=engine, ffmpeg=ffmpeg)
    studio.new('[output sample_rate=8000 stems=false]\nExact synthetic original.', input_kind='script')
    studio.configure(voice='fixture:local')
    source = studio.save_in_library()
    before = studio.render()
    assert engine.calls == 1
    directory = Path(before['output']).parent
    master = directory / 'master.wav'
    original = master.read_bytes()
    master.write_bytes(original[:4] + b'\0' * 4 + original[8:])
    with pytest.raises(ValueError, match='size declaration'):
        audio.wav_info(master)
    receipt = json.loads((directory / 'complete.json').read_text(encoding='utf-8'))
    _write_receipt(directory, receipt)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    reopened = StudioController(private_root=restored, registry=engine, ffmpeg=ffmpeg)
    reopened.open(restored / source.relative_to(app.root))
    assert reopened.result is None
    saved = _files(reopened.output_dir)
    def forbidden(*args, **options):
        pytest.fail('A malformed completed claim must refuse before admission, cache work or publication.')
    monkeypatch.setattr(admission, 'admit_new_operation', forbidden)
    monkeypatch.setattr(jobs.ChunkCache, 'get', forbidden)
    monkeypatch.setattr(production, 'publish_noreplace', forbidden)
    with pytest.raises(ValueError, match='invalid PCM'):
        reopened.render()
    assert engine.calls == 1 and reopened.result is None
    assert _files(reopened.output_dir) == saved
    assert not (reopened.output_dir / 'quarantine').exists()
