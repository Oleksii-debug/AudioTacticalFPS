"""Actual current producer/FFmpeg contracts; synthetic audio, no device claim."""
from hashlib import sha256
from io import BytesIO
from threading import Event
from zipfile import ZipFile
import os
from pathlib import Path
import runpy
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.jobs import file_identity, regular_file
from carr_library.audio.playback import PlaybackStatus
from carr_library.audio.production import render_project
from carr_library.audio.publishing import PublicationService, PublicationCancelled
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.operations import ResourceBroker
from carr_library.reader import media_overlay as module
from carr_library.reader.media_overlay import ReadingBudget, OverlaySession, prepare_overlay
from carr_library.reader.publication_state import PublicationPositions
from carr_library.ui.audio_studio import StudioController


class Synthetic:
    def cache_identity(self, voice, **kwargs):
        return {'fixture': 'publication-reading-v1', 'voice': voice}

    def synthesize(self, text, output, **kwargs):
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 8000, 1600, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 1600)
        return output


@pytest.fixture(scope='module')
def publications(tmp_path_factory):
    root = tmp_path_factory.mktemp('actual-recorded-publications')
    ffmpeg = resolve_ffmpeg()
    assert ffmpeg is not None and ffmpeg.is_file(), 'Actual selected FFmpeg is required; no skip.'
    app = LibraryApp(root / 'library')
    app.create_workspace('Synthetic measured publications')
    studio = StudioController(private_root=app.root, ffmpeg=ffmpeg)
    script = ('[output sample_rate=16000 stems=false]\n# Перша\n'
              'Alice: Text 😀 & <source>.\n[pause 0.1]\n# Second\nBob: Last line.')
    studio.new(script, title='Книжка 🙂', input_kind='script')
    studio.configure(voice='fixture:pcm', language='en')
    studio.save_in_library()
    result = render_project(studio.plan(), studio.output_dir, engine=Synthetic(), ffmpeg=ffmpeg)
    # Exercise the exact native-probe producer, not a more permissive parallel
    # fixture. Both EPUB and DAISY must be admitted before any wx/device proof.
    probe = runpy.run_path(str(Path(__file__).resolve().parents[2]
                              / 'scripts' / 'windows_media_overlay_smoke.py'))
    paths = {'epub': root / 'Recorded book.epub', 'daisy': root / 'Recorded book.zip'}
    probe['export_fixture_publications'](studio.output_dir, app.root, result['run_key'],
                                         paths, ffmpeg=ffmpeg)
    return paths, ffmpeg


@pytest.fixture
def environment(tmp_path, publications):
    root, stage = tmp_path / 'library', tmp_path / 'owned'
    root.mkdir()
    stage.mkdir()
    broker = ResourceBroker.for_app(root)
    documents = []
    def prepare(kind='epub', **options):
        selected = options.pop('publication', publications[0][kind])
        value = prepare_overlay(selected, library_root=root, ffmpeg=publications[1],
            broker=broker, budget=ReadingBudget(max_bytes=16 * 1024**2, reserve_bytes=0),
            temporary_parent=stage, **options)
        documents.append(value)
        return value
    yield prepare, root, stage, broker, publications[0]
    for document in documents:
        document.close()


class Player:
    def __init__(self):
        self.native = object()
        self.state = 'stopped'
        self.frames = 0
        self.accuracy = 'not_started'
        self.close_failure = False
        self.calls = []
        self.path = None

    def status(self):
        return PlaybackStatus(state=self.state, position_frames=self.frames, sample_rate=16000,
                              total_frames=8000, position_accuracy=self.accuracy)

    def play(self, path, *, start_seconds=0, device_id=None):
        self.path, self.state, self.accuracy = path, 'playing', 'sample_frames'
        self.frames = round(start_seconds * 16000)
        self.calls.append((path, start_seconds, device_id))

    def stop(self):
        self.state, self.frames, self.path = 'stopped', 0, None

    def pause(self):
        self.state = 'paused'

    def resume(self):
        self.state = 'playing'

    def close(self):
        if self.close_failure:
            raise RuntimeError('Native output has not released its handle.')
        self.stop()


def alter_wave(path):
    offset = wav_info(path).data_offset
    with path.open('r+b') as stream:
        stream.seek(offset)
        stream.write(b'\x01\x00')


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
def test_actual_publication_prepares_exact_text_timeline_and_only_owned_pcm(environment, kind):
    prepare, root, stage, broker, paths = environment
    original = paths[kind].read_bytes()
    document = prepare(kind)
    assert document.kind == kind and document.title == 'Книжка 🙂'
    assert document.publication_sha256 == sha256(original).hexdigest()
    assert [row['title'] for row in document.chapters] == ['Перша', 'Second']
    assert document.intervals[0]['text'] == 'Text 😀 & <source>.'
    assert document.intervals[-1]['text'] == 'Last line.'
    assert [row['kind'] for row in document.intervals] == ['line', 'audio-interval', 'line']
    info = wav_info(document.audio)
    assert (info.rate, info.frames) == (document.sample_rate, document.frames)
    assert file_identity(regular_file(document.audio)) == document.audio_identity
    assert sha256(document.audio.read_bytes()).hexdigest() == document.audio_sha256
    assert list(document.audio.parent.iterdir()) == [document.audio]
    assert broker.snapshot()['leases'] == [] and paths[kind].read_bytes() == original
    document.close()
    assert list(stage.iterdir()) == []


@pytest.mark.parametrize('phase', ['Capturing', 'Verifying', 'Decoding', 'Publication prepared'])
def test_cancellation_at_each_real_phase_releases_owned_files_and_resources(environment, phase):
    prepare, root, stage, broker, paths = environment
    before = paths['epub'].read_bytes()
    stop = Event()
    def report(message):
        if message.startswith(phase):
            stop.set()
    with pytest.raises(PublicationCancelled):
        prepare(cancelled=stop.is_set, progress=report)
    assert stop.is_set() and list(stage.iterdir()) == []
    assert broker.snapshot()['leases'] == [] and paths['epub'].read_bytes() == before


@pytest.mark.parametrize('options', [
    {'max_bytes': True}, {'max_bytes': 0}, {'timeout': 0},
    {'timeout': float('nan')}, {'reserve_bytes': -1},
])
def test_invalid_budgets_are_rejected_before_runtime_or_directory_creation(options):
    with pytest.raises(ValueError):
        ReadingBudget(**options)


def test_inclusive_global_deadline_rejects_after_verified_graph_before_native_decode(environment, monkeypatch):
    prepare, root, stage, broker, paths = environment
    clock, calls = [0.0], []
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module, 'run_ffmpeg', lambda *args, **kwargs: calls.append(args))
    def report(message):
        if message.startswith('Decoding'):
            clock[0] = 3600.0
    with pytest.raises(TimeoutError):
        prepare(progress=report)
    assert calls == [] and list(stage.iterdir()) == [] and broker.snapshot()['leases'] == []


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
def test_tampered_embedded_audio_is_refused_before_any_native_decode(environment, monkeypatch, tmp_path, kind):
    prepare, root, stage, broker, paths = environment
    tampered = tmp_path / ('tampered.epub' if kind == 'epub' else 'tampered.zip')
    with ZipFile(paths[kind]) as incoming, ZipFile(tampered, 'w') as outgoing:
        for entry in incoming.infolist():
            data = incoming.read(entry.filename)
            if entry.filename.endswith('master.mp3'):
                data = data[:-1] + bytes([data[-1] ^ 1])
            outgoing.writestr(entry, data)
    calls = []
    monkeypatch.setattr(module, 'run_ffmpeg', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        prepare(kind, publication=tampered)
    assert calls == [] and list(stage.iterdir()) == [] and broker.snapshot()['leases'] == []


def test_navigation_observes_driver_frames_without_autoplay_and_stop_preserves_cursor(environment):
    document = environment[0]()
    player = Player()
    session = OverlaySession(document, player)
    row = session.select(2)
    assert row['text'] == 'Last line.' and not player.calls
    with pytest.raises(ValueError, match='Verify'):
        session.play()
    session.verify_audio()
    session.play(device_id=2)
    assert player.calls == [(document.audio, row['start'], 2)] and session._reader is not None
    player.frames = 6400
    assert session.position == .4 and session.index == 2
    session.pause()
    assert player.state == 'paused'
    session.play(device_id=2)
    assert player.state == 'playing' and len(player.calls) == 1
    assert session.stop() == .4 and session.position == .4 and session._reader is None
    session.seek(.1)
    assert session.index == 0 and session.cursor == .1 and len(player.calls) == 1
    session.close()


def test_valid_wave_tamper_before_playback_is_refused_without_advancing_cursor(environment):
    document = environment[0]()
    player = Player()
    session = OverlaySession(document, player)
    alter_wave(document.audio)
    with pytest.raises(RuntimeError, match='changed'):
        session.verify_audio()
    assert not player.calls and session.cursor == 0 and session._reader is None


def test_native_close_refusal_keeps_pcm_and_reader_until_actual_release(environment):
    document = environment[0]()
    player = Player()
    session = OverlaySession(document, player)
    session.verify_audio()
    player.close_failure = True
    with pytest.raises(RuntimeError, match='not released'):
        session.close()
    assert session._reader is not None and document.audio.is_file()
    player.close_failure = False
    session.close()
    assert session._reader is None and not document.audio.exists()


@pytest.mark.parametrize('accuracy', ['external_player', 'not_started', 'unavailable'])
def test_unreported_positions_do_not_invent_playhead_or_overwrite_manual_navigation(environment, accuracy):
    document = environment[0]()
    player = Player()
    session = OverlaySession(document, player)
    session.select(2)
    selected = session.cursor
    session.verify_audio()
    session.play()
    player.accuracy, player.frames = accuracy, 0
    assert session.position == selected and session.index == 2
    session.stop()
    assert session.cursor == selected
    session.close()


@pytest.mark.parametrize('accuracy', ['driver_reported', 'completed_buffers'])
def test_actual_native_position_profiles_update_measured_interval(environment, accuracy):
    document = environment[0]()
    player = Player()
    session = OverlaySession(document, player)
    session.verify_audio()
    session.play()
    player.accuracy, player.frames = accuracy, 6400
    assert session.position == .4 and session.index == 2
    session.stop()
    session.close()


def test_verified_read_cancellation_retains_prepared_pcm_without_publishing_owner(environment):
    document = environment[0]()
    session = OverlaySession(document, Player())
    with pytest.raises(PublicationCancelled):
        session.verify_audio(cancelled=lambda: True)
    assert session._reader is None and document.audio.is_file() and session.cursor == 0
    session.close()


def test_changed_held_pcm_cannot_authorize_saved_position(environment):
    prepare, root, stage, broker, paths = environment
    document = prepare()
    session = OverlaySession(document, Player())
    session.verify_audio()
    store = PublicationPositions(root)
    if os.name == 'nt':
        with pytest.raises(OSError):
            alter_wave(document.audio)
        session.stop()
        session.remember(store)
        assert store.load(document) == 0
    else:
        alter_wave(document.audio)
        with pytest.raises(RuntimeError, match='changed'):
            session.stop()
        assert session.invalid_audio and session._reader is None
        with pytest.raises(ValueError, match='Stop verified'):
            session.remember(store)
        assert not store.path.exists()
    session.close()


def test_zip_directory_read_without_count_is_bounded_before_allocation():
    assert module._DirectoryReader(BytesIO(b'PK test')).read() == b'PK test'
    with pytest.raises(ValueError, match='ZIP directory'):
        module._DirectoryReader(BytesIO(b'x' * (1024**2 + 2))).read()
    with pytest.raises(ValueError):
        module._DirectoryReader(BytesIO(b'x')).read(1024**2 + 1)
