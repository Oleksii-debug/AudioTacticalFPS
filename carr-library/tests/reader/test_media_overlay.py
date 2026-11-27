"""Actual FFmpeg decode and bounded navigation of generated synchronized EPUB."""
from hashlib import sha256
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import wave

import pytest

from carr_library.audio.models import AudioBudget, AudioCancelled
from carr_library.reader import media_overlay
from carr_library.reader.media_overlay import prepare_overlay, OverlaySession
from carr_library.studio.publishing import PublicationService

_spec = importlib.util.spec_from_file_location('overlay_production_fixtures',
    Path(__file__).parents[1] / 'studio' / 'test_publishing.py')
_fixtures = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_fixtures)
actual_job = _fixtures.actual_job


@pytest.fixture(scope='module')
def publication(actual_job, tmp_path_factory):
    service, state = actual_job
    root = tmp_path_factory.mktemp('verified-reading-publication')
    path = root / 'source.epub'
    receipt = PublicationService(service).export_epub(state['id'], path)
    return service.root.parent.parent, path, receipt


class Player:
    available = True
    position_is_approximate = True
    def __init__(self):
        self.state, self.position_seconds, self.path = 'stopped', 0.0, None
        self.calls = []
    def play(self, path, *, start_seconds=0):
        self.calls.append(('play', Path(path), start_seconds))
        self.path, self.position_seconds, self.state = Path(path), start_seconds, 'playing'
    def pause(self):
        self.calls.append(('pause',)); self.state = 'paused'
    def resume(self):
        self.calls.append(('resume',)); self.state = 'playing'
    def stop(self):
        self.calls.append(('stop',)); self.state, self.position_seconds = 'stopped', 0.0
    def close(self):
        self.calls.append(('close',)); self.stop()


def test_load_captures_exact_epub_and_decodes_actual_pcm_without_playback(publication):
    root, path, receipt = publication
    prepared = prepare_overlay(path, library_root=root)
    try:
        assert prepared.publication_sha256 == receipt['artifact']['sha256']
        assert prepared.provenance['source'] == receipt['source']
        assert prepared.chapters and prepared.intervals
        with wave.open(str(prepared.audio), 'rb') as audio:
            assert audio.getcomptype() == 'NONE' and audio.getsampwidth() == 2
            assert abs(audio.getnframes() / audio.getframerate() - prepared.duration) < 1 / audio.getframerate()
        assert sha256(prepared.audio.read_bytes()).hexdigest() == prepared.audio_identity['sha256']
        assert not any(p.name.endswith(('.epub', '.mp3')) for p in prepared.audio.parent.iterdir())
        session = OverlaySession(prepared, Player())
        assert session.player.calls == [] and session.player.state == 'stopped'
        target = prepared.audio.parent
    finally:
        prepared.close()
    assert not target.exists()


def test_navigation_seek_and_explicit_play_preserve_measured_intervals(publication):
    root, path, _ = publication
    prepared = prepare_overlay(path, library_root=root)
    player = Player(); session = OverlaySession(prepared, player)
    try:
        session.select(len(prepared.intervals) - 1)
        assert session.cursor == prepared.intervals[-1]['start']
        assert not any(call[0] == 'play' for call in player.calls)
        with pytest.raises(ValueError, match='Verify'):
            session.play()
        session.verify_audio(); session.play()
        assert player.calls[-1][0] == 'play' and player.calls[-1][2] == session.cursor
        player.position_seconds = min(prepared.duration, session.cursor + .02)
        session.pause()
        assert session.position == player.position_seconds
        session.verify_audio(); session.play()
        assert player.calls[-1] == ('resume',)
        session.seek(.05)
        assert player.state == 'stopped' and session.cursor == .05
        assert session.index == 0
        session.verify_audio(); session.play()
        player.position_seconds = prepared.duration; player.state = 'stopped'
        assert session.position == prepared.duration and session.index == len(prepared.intervals) - 1
    finally:
        session.close()


@pytest.mark.parametrize('value', [True, float('nan'), -1, 10**9])
def test_invalid_seek_never_reaches_native_playback(publication, value):
    root, path, _ = publication
    prepared = prepare_overlay(path, library_root=root)
    player = Player()
    try:
        with pytest.raises(ValueError):
            OverlaySession(prepared, player).seek(value)
        assert player.calls == []
    finally:
        prepared.close()


def test_private_pcm_tampering_is_rejected_before_native_play(publication):
    root, path, _ = publication
    prepared = prepare_overlay(path, library_root=root)
    player = Player(); session = OverlaySession(prepared, player)
    try:
        with prepared.audio.open('r+b') as output:
            output.seek(-1, 2); output.write(b'X')
        with pytest.raises(ValueError, match='changed'):
            session.verify_audio()
        assert player.calls == []
    finally:
        prepared.close()


def test_captured_publication_remains_exact_after_original_file_changes(publication, tmp_path):
    root, original, receipt = publication
    selected = tmp_path / 'selected.epub'; selected.write_bytes(original.read_bytes())
    changed = False
    def progress(message):
        nonlocal changed
        if message.startswith('Verifying text'):
            selected.write_bytes(b'Changed after immutable capture.'); changed = True
    prepared = prepare_overlay(selected, library_root=root, progress=progress)
    try:
        assert changed and prepared.publication_sha256 == receipt['artifact']['sha256']
        assert selected.read_bytes() == b'Changed after immutable capture.'
        assert prepared.provenance['source'] == receipt['source']
    finally:
        prepared.close()


def test_changed_file_between_hash_and_capture_is_rejected(publication, tmp_path, monkeypatch):
    root, original, _ = publication
    selected = tmp_path / 'selected.epub'; selected.write_bytes(original.read_bytes())
    real_read = media_overlay._read
    def replacing(path, *args, **kwargs):
        selected.write_bytes(b'Changed while selected file was captured.')
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(media_overlay, '_read', replacing)
    with pytest.raises(ValueError, match='changed|receipt|size'):
        prepare_overlay(selected, library_root=root)


@pytest.mark.parametrize('phase', ['Capturing', 'Verifying text', 'Preparing the verified', 'Decoding verified'])
def test_cancel_cleans_staging_after_real_worker_finishes(publication, tmp_path, monkeypatch, phase):
    root, path, _ = publication
    folders, stopped = [], False
    def temporary(**kwargs):
        handle = TemporaryDirectory(dir=tmp_path, **kwargs)
        folders.append(Path(handle.name)); return handle
    def progress(message):
        nonlocal stopped
        if message.startswith(phase):
            stopped = True
    monkeypatch.setattr(media_overlay, 'TemporaryDirectory', temporary)
    with pytest.raises(AudioCancelled):
        prepare_overlay(path, library_root=root, cancelled=lambda: stopped, progress=progress)
    assert folders and not any(folder.exists() for folder in folders)


def test_decode_respects_total_capture_and_pcm_budget(publication, monkeypatch):
    from zipfile import ZipFile
    from carr_library.audio import AudioRuntime
    root, path, _ = publication
    with ZipFile(path) as archive:
        unpacked = sum(member.file_size for member in archive.infolist())
        captured = path.stat().st_size + archive.getinfo('EPUB/audio/master.mp3').file_size
    # Both the compressed capture and the verified expanded graph must fit.
    # The remaining byte cannot accommodate the actual decoded PCM, so the
    # rejection exercises the playback staging guard, not ZIP size validation.
    maximum = max(4096, unpacked, captured) + 1
    monkeypatch.setattr(AudioRuntime, 'convert',
                        lambda *a, **k: pytest.fail('Over-budget playback reached the decoder.'))
    with pytest.raises(ValueError, match='Decoded PCM and captured EPUB.*budget'):
        prepare_overlay(path, library_root=root,
                        budget=AudioBudget(max_output_bytes=maximum))


def test_unknown_epub_graph_is_rejected_before_audio_runtime(publication, tmp_path, monkeypatch):
    from zipfile import ZipFile
    root, _, _ = publication
    unknown = tmp_path / 'generic.epub'
    with ZipFile(unknown, 'x') as archive:
        archive.writestr('untrusted.xml', '<!DOCTYPE x SYSTEM "https://example.invalid/"><x/>')
    monkeypatch.setattr('carr_library.runtime_settings.audio_runtime',
                        lambda *a, **k: pytest.fail('Unsupported graph reached an audio decoder.'))
    with pytest.raises(ValueError, match='container graph'):
        prepare_overlay(unknown, library_root=root)


@pytest.mark.parametrize('rate,channels', [(8000, 1), (24000, 2), (48000, 2), (192000, 1)])
def test_riff32_limit_rejects_the_first_unplayable_frame_without_large_decode(rate, channels):
    from carr_library.reader.media_overlay import pcm_playback_bytes, MAX_RIFF_BYTES, RIFF_HEADER_RESERVE
    last_frame = (MAX_RIFF_BYTES - RIFF_HEADER_RESERVE) // (channels * 2)
    assert pcm_playback_bytes(last_frame / rate, rate, channels) <= MAX_RIFF_BYTES
    with pytest.raises(ValueError, match='RIFF32 PCM limit'):
        pcm_playback_bytes((last_frame + 1) / rate, rate, channels)


@pytest.mark.parametrize('duration,rate,channels', [(float('nan'), 24000, 2), (1, True, 2), (1, 24000, True)])
def test_playback_size_preflight_rejects_invalid_numeric_declarations(duration, rate, channels):
    with pytest.raises(ValueError):
        media_overlay.pcm_playback_bytes(duration, rate, channels)
