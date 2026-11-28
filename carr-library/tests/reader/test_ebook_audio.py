"""Real retained EPUB/SMIL and FFmpeg preparation; no native acceptance claim."""
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock
import wave
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioRuntime
from carr_library.audio.models import AudioCancelled
from carr_library.reader.ebook_audio import EbookAudioSession, _clock, parse_overlay
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.ebook_audio_dialog import EbookAudioDialog, OverlayPlayback


def make_epub(audio, *, body=None, overlay=None, package_edit=lambda text: text, extras=()):
    body = body or '<p>Role: <span id="one">Repeated source phrase.</span></p><p>Role: <span id="two">Repeated source phrase.</span></p>'
    overlay = overlay or ('<seq epub:textref="chapter.xhtml">'
        '<par><text src="chapter.xhtml#one"/><audio src="recording.mp3" clipBegin="100ms" clipEnd="0.3s"/></par>'
        '<seq><par><text src="chapter.xhtml#two"/><audio src="recording.mp3" clipBegin="00:00.500" clipEnd="0.7s"/></par></seq></seq>')
    package = '''<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">synthetic-overlay</dc:identifier>
      <dc:title>Recorded original</dc:title><dc:language>en</dc:language>
      <meta property="dcterms:modified">2026-11-27T00:00:00Z</meta></metadata><manifest>
      <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml" media-overlay="overlay"/>
      <item id="overlay" href="overlay.smil" media-type="application/smil+xml"/>
      <item id="audio" href="recording.mp3" media-type="audio/mpeg"/>
      <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
      </manifest><spine><itemref idref="chapter"/></spine></package>'''
    files = [('mimetype', b'application/epub+zip'),
        ('META-INF/container.xml', b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'),
        ('OPS/package.opf', package_edit(package).encode()),
        ('OPS/chapter.xhtml', ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Recorded source</title></head><body>'+body+'</body></html>').encode()),
        ('OPS/overlay.smil', ('<smil xmlns="http://www.w3.org/ns/SMIL" xmlns:epub="http://www.idpf.org/2007/ops" version="3.0"><body>'+overlay+'</body></smil>').encode()),
        ('OPS/nav.xhtml', b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="chapter.xhtml">Recorded source</a></li></ol></nav></body></html>'),
        ('OPS/recording.mp3', audio), *extras]
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        for name, data in files:
            archive.writestr(name, data, compress_type=ZIP_STORED if name == 'mimetype' else ZIP_DEFLATED)
    return buffer.getvalue()


@pytest.fixture(scope='module')
def audio(tmp_path_factory):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual local FFmpeg/ffprobe required.'
    root = tmp_path_factory.mktemp('epub-overlay-audio')
    source = root / 'tone.wav'
    with wave.open(str(source), 'wb') as out:
        out.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        out.writeframes(b''.join(struct.pack('<h', int(4000*math.sin(2*math.pi*(440 if n < 9600 else 660)*n/24000)))
                                for n in range(19200)))
    output = root / 'tone.mp3'
    AudioRuntime(root).convert(source, output, sample_rate=24000)
    return output.read_bytes()


@pytest.fixture
def imported(tmp_path, audio):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Recorded EPUB')
    source = tmp_path / 'recorded.epub'; source.write_bytes(make_epub(audio))
    book = app.import_file(workspace, source, title='Exact retained recording')
    assert book['state'] == 'complete', book
    return app, workspace, source, book, ReadingDesk(app, workspace, book['edition'])


def test_real_import_isolated_index_and_two_exact_pcm_segments(imported):
    app, _, source, _, desk = imported
    original = source.read_bytes()
    before = desk.page(1)
    source.unlink()
    session = EbookAudioSession(desk, 1)
    root = session.root
    try:
        assert session.chapter.asset_sha256 == sha256(original).hexdigest()
        assert session.page == before and session.page.verified is False
        first, second = session.chapter.cues
        assert first.start < second.start
        assert before.raw_text[first.start:first.end] == 'Repeated source phrase.'
        assert before.raw_text[second.start:second.end] == 'Repeated source phrase.'
        for index in (0, 1):
            prepared = session.prepare(index)
            try:
                session.admit(prepared)
                with wave.open(str(prepared.path), 'rb') as clip:
                    assert (clip.getnframes(), clip.getframerate(), clip.getnchannels()) == (4800,24000,2)
                    assert any(clip.readframes(4800))
                assert prepared.frames == 4800 and prepared.sha256 == sha256(prepared.path.read_bytes()).hexdigest()
            finally:
                prepared.close()
        assert app.assets.resolve(desk.identity.asset_sha256).read_bytes() == original
        assert desk.page(1) == before and app._selections == {}
    finally:
        session.close()
    assert not root.exists()


@pytest.mark.parametrize('value,expected', [('1.25s',1.25), ('250ms',.25), ('2min',120),
    ('1h',3600), ('01:02:03.004',3723.004), ('02:03.5',123.5), ('2',2), (None,None)])
def test_standard_clip_clock_forms(value, expected):
    assert _clock(value) == expected


@pytest.mark.parametrize('value', ['-1s','nan','inf','1e9','01:99:00','60:00','1s;evil',' 1s',1,True,'9'*100])
def test_invalid_clip_clocks_fail(value):
    with pytest.raises(ValueError):
        _clock(value)


@pytest.mark.parametrize('overlay', [
    '<par><text src="https://example.invalid/book.xhtml#one"/><audio src="recording.mp3"/></par>',
    '<par><text src="chapter.xhtml#one"/><audio src="https://example.invalid/audio.mp3"/></par>',
    '<par><text src="chapter.xhtml#one"/><audio src="../../outside.mp3"/></par>',
    '<par><text src="chapter.xhtml#missing"/><audio src="recording.mp3"/></par>',
    '<par><text src="chapter.xhtml#one"/><audio src="recording.mp3" clipBegin="1s" clipEnd=".5s"/></par>',
    '<par><text src="chapter.xhtml#one"/></par>',
    '<seq repeatCount="2"><par><text src="chapter.xhtml#one"/><audio src="recording.mp3"/></par></seq>',
    '<par><text src="other.xhtml#one"/><audio src="recording.mp3"/></par>',
])
def test_unavailable_unsafe_or_unsupported_overlays_fail_explicitly(audio, overlay):
    with pytest.raises(ValueError):
        parse_overlay(make_epub(audio, overlay=overlay), 1)


def test_duplicate_ids_and_hidden_audio_targets_never_guess_source(audio):
    for body in ('<p id="one">First</p><p id="one">Second</p><p id="two">Third</p>',
                 '<p id="one" hidden="">First</p><p id="two">Second</p>'):
        with pytest.raises(ValueError):
            parse_overlay(make_epub(audio, body=body), 1)


def test_nested_visible_markup_uses_the_same_exact_extractor_offsets(audio):
    body = '<div><p>Prefix <span id="one">Words <em>&amp; exact</em> text.</span> tail</p><p id="two">Next<br/>line.</p></div>'
    data = make_epub(audio, body=body)
    parsed = parse_overlay(data, 1)
    from carr_library.extract.epub_ordered import extract_epub
    raw = extract_epub(data)['records'][0]['raw_text']
    assert [raw[cue.start:cue.end] for cue in parsed.cues] == ['Words & exact text.', 'Next\nline.']


def test_encryption_and_aliased_archive_resources_are_rejected(audio):
    for extras in ([('META-INF/encryption.xml', b'<encryption/>')], [('OPS/Recording.mp3',audio)]):
        with pytest.raises(ValueError):
            parse_overlay(make_epub(audio, extras=extras), 1)


def test_absent_overlay_does_not_fall_back_to_speech(audio):
    with pytest.raises(ValueError, match='no local recorded'):
        parse_overlay(make_epub(audio, package_edit=lambda text:text.replace(' media-overlay="overlay"','')), 1)


def test_cancel_before_loading_never_reads_original(imported, monkeypatch):
    app, _, _, _, desk = imported
    monkeypatch.setattr(app.assets,'resolve',lambda _:pytest.fail('Cancelled session read original'))
    with pytest.raises(AudioCancelled):
        EbookAudioSession(desk,1,cancelled=lambda:True)


@pytest.mark.parametrize('change',['archive','metadata','source','private-copy','selection'])
def test_fresh_playback_rejects_changed_source_scope_and_selection(imported,change):
    app,workspace,_,book,desk = imported
    selected = [True]
    def guard():
        if not selected[0]:
            raise ValueError('Selection changed')
    session = EbookAudioSession(desk,1,guard=guard)
    prepared = session.prepare(0)
    try:
        if change == 'archive':
            app.archive_workspace(workspace)
        elif change == 'metadata':
            app.update_metadata(workspace,book['edition'],title='Changed',authors=('Author',))
        elif change == 'source':
            original = app.assets.resolve(desk.identity.asset_sha256)
            raw=original.read_bytes(); original.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        elif change == 'private-copy':
            raw=session.source.read_bytes(); session.source.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        else:
            selected[0] = False
        if change == 'private-copy':
            with pytest.raises(ValueError):
                session.prepare(1)
        else:
            with pytest.raises((ValueError,OSError)):
                session.admit(prepared)
    finally:
        prepared.close(); session.close()


def test_prepared_wave_tampering_fails_admission(imported):
    session=EbookAudioSession(imported[-1],1)
    prepared=session.prepare(0)
    try:
        with prepared.path.open('r+b') as output:
            output.seek(80); output.write(b'tamper')
        with pytest.raises(ValueError,match='changed before playback'):
            session.admit(prepared)
    finally:
        prepared.close(); session.close()


def test_cancellation_during_actual_conversion_discards_clip(imported,monkeypatch):
    session=EbookAudioSession(imported[-1],1)
    cancelled=Event()
    original=AudioRuntime.convert
    def convert(runtime,source,destination,**options):
        cancelled.set()
        return original(runtime,source,destination,**options)
    monkeypatch.setattr(AudioRuntime,'convert',convert)
    try:
        with pytest.raises(AudioCancelled):
            session.prepare(0,cancelled=cancelled.is_set)
    finally:
        session.close()


class TrackedPlayer:
    """Transport boundary double only; parsing and FFmpeg remain real."""
    def __init__(self):
        self.played=[]; self.state='stopped'
    def play(self,path):
        assert Path(path).is_file(); self.played.append(path); self.state='playing'
    def pause(self):
        self.state='paused'; return True
    def resume(self):
        self.state='playing'; return True
    def close(self):
        self.state='stopped'


def test_selection_and_close_discard_late_actual_preparations(imported):
    session=EbookAudioSession(imported[-1],1)
    player=TrackedPlayer(); transport=OverlayPlayback(session,player)
    assert not player.played
    first=session.prepare(0); ticket=transport.ticket
    transport.select(1)
    assert transport.start(first,ticket) is False and not first.path.exists() and not player.played
    second=session.prepare(1); ticket=transport.ticket
    transport.close()
    assert transport.start(second,ticket) is False and not second.path.exists() and not player.played


def test_transport_pause_resume_stop_and_archive_guard_use_existing_player(imported):
    app,workspace,_,_,desk=imported
    session=EbookAudioSession(desk,1)
    player=TrackedPlayer(); transport=OverlayPlayback(session,player)
    prepared=session.prepare(0)
    try:
        assert transport.start(prepared,transport.ticket) and player.state=='playing'
        assert transport.pause() and player.state=='paused'
        assert transport.resume() and player.state=='playing'
        transport.pause(); app.archive_workspace(workspace)
        with pytest.raises(ValueError,match='archived'):
            transport.resume()
        transport.stop()
        assert player.state=='stopped' and not prepared.path.exists()
    finally:
        transport.close()


def test_forced_owned_playback_close_never_becomes_natural_eof(imported):
    session = EbookAudioSession(imported[-1], 1)
    player = TrackedPlayer(); transport = OverlayPlayback(session, player)
    prepared = session.prepare(0)
    dialog = SimpleNamespace(closed=False, transport=transport, session=session, player=player,
        guard=lambda: None, _check_scope=lambda: None, _pending=None, report=Mock(), segments=Mock(), _highlight=Mock(), play=Mock(),
        _error=lambda error: (_ for _ in ()).throw(error))
    try:
        assert transport.start(prepared, transport.ticket)
        # This is the same registered view closed by a recording/quiet owner.
        # The fallback reproduces the original raw-WavePlayer registration.
        observed = getattr(transport, 'observed', player)
        observed.close()
        EbookAudioDialog.tick(dialog)
        assert player.state == 'stopped' and transport.index == 0
        assert not transport.active
        dialog.play.assert_not_called()
        assert not prepared.path.exists()
        assert not session.stopped(), 'A quiet interruption must leave explicit replay available.'
        second = session.prepare(0)
        assert transport.start(second, transport.ticket)
        player.state = 'stopped'  # actual native EOF, with no external close
        EbookAudioDialog.tick(dialog)
        assert transport.index == 1
        dialog.play.assert_called_once_with()
    finally:
        transport.close()


def test_quiet_stop_invalidates_a_prepared_but_not_started_segment(imported):
    session = EbookAudioSession(imported[-1], 1)
    player = TrackedPlayer(); transport = OverlayPlayback(session, player)
    prepared = session.prepare(0); ticket = transport.ticket
    try:
        transport.observed.close()
        assert not transport.start(prepared, ticket)
        assert not player.played and not prepared.path.exists()
        assert transport.index == 0 and transport.next_after_eof() is None
    finally:
        transport.close()


def test_native_start_resume_and_ticks_do_not_rehash_large_files(imported, monkeypatch):
    from carr_library.reader import ebook_audio
    session = EbookAudioSession(imported[-1], 1)
    prepared = session.prepare(0)
    player = TrackedPlayer(); transport = OverlayPlayback(session, player)
    def no_gui_hash(*args, **kwargs):
        pytest.fail('Native dispatch synchronously hashed a large source/PCM file.')
    monkeypatch.setattr(ebook_audio, 'digest_file', no_gui_hash)
    monkeypatch.setattr(session, 'check', no_gui_hash)
    try:
        assert transport.start(prepared, transport.ticket)
        assert transport.next_after_eof() is None
        assert transport.pause() and transport.resume()
        assert transport.next_after_eof() is None
    finally:
        transport.close()


def test_stop_defers_windows_file_cleanup_until_background_verification_releases_it(imported, monkeypatch):
    from carr_library.reader import ebook_audio
    session = EbookAudioSession(imported[-1], 1)
    prepared = session.prepare(0)
    player = TrackedPlayer(); transport = OverlayPlayback(session, player)
    assert transport.start(prepared, transport.ticket) and transport.pause()
    entered, release, finished = Event(), Event(), Event()
    failures = []
    digest = ebook_audio.digest_file
    def held_hash(path):
        if path == prepared.path:
            # Keep the real stream open across Stop, as a Windows rehash does.
            with path.open('rb') as stream:
                entered.set()
                assert release.wait(5), 'The test failed to release its worker.'
                return sha256(stream.read()).hexdigest()
        return digest(path)
    monkeypatch.setattr(ebook_audio, 'digest_file', held_hash)
    def verify():
        try:
            session.admit(prepared)
        except Exception as exc:
            failures.append(exc)
        finally:
            finished.set()
    worker = Thread(target=verify)
    worker.start()
    try:
        assert entered.wait(5)
        transport.stop()
        assert player.state == 'stopped' and not transport.active
        assert prepared.path.exists(), 'Stop removed a WAV that the verifier still owns.'
    finally:
        release.set()
        assert finished.wait(5), 'Background verification did not finish after Stop.'
        worker.join(1)
        transport.close()
    assert len(failures) == 1 and isinstance(failures[0], AudioCancelled)
    assert not prepared.path.exists()


@pytest.mark.parametrize('change', ['archive', 'metadata', 'source', 'prepared'])
def test_lightweight_native_admission_still_rejects_changes_after_worker(imported, change):
    app, workspace, _, book, desk = imported
    session = EbookAudioSession(desk, 1)
    prepared = session.prepare(0)
    player = TrackedPlayer(); transport = OverlayPlayback(session, player)
    try:
        if change == 'archive':
            app.archive_workspace(workspace)
        elif change == 'metadata':
            app.update_metadata(workspace, book['edition'], title='Changed after worker', authors=('Author',))
        else:
            path = session.original if change == 'source' else prepared.path
            with path.open('r+b') as stream:
                stream.seek(80); stream.write(b'changed')
        with pytest.raises((ValueError, OSError)):
            transport.start(prepared, transport.ticket)
        assert not player.played and not prepared.path.exists()
    finally:
        transport.close()


@pytest.mark.parametrize('overlay', [
    '<par><text src="chapter.xhtml#one"/><audio src="recording.mp3" dur="0.1s"/></par>',
    '<par><text src="chapter.xhtml#one"/><audio src="recording.mp3" repeatCount="2"/></par>',
    '<par><text src="chapter.xhtml#one" begin="2s"/><audio src="recording.mp3"/></par>',
    '<seq epub:textref="chapter.xhtml#missing"><par><text src="chapter.xhtml#one"/><audio src="recording.mp3"/></par></seq>',
])
def test_unsupported_timing_and_missing_sequence_fragments_are_not_silently_ignored(audio, overlay):
    with pytest.raises(ValueError):
        parse_overlay(make_epub(audio, overlay=overlay), 1)


def test_actual_studio_publication_import_and_recorded_overlay_roundtrip(tmp_path):
    from carr_library.studio.render import ProductionService
    from carr_library.studio.publishing import PublicationService, inspect_epub
    class SyntheticTones:
        def voices(self):
            return (SimpleNamespace(id='synthetic-tone', name='Synthetic fixture', language='und', engine='test'),)
        def synthesize(self, text, voice_id, destination, *, settings, cancelled):
            assert not cancelled()
            with wave.open(str(destination), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b''.join(struct.pack('<h', int(4000 * math.sin(2 * math.pi * 440 * n / 24000)))
                                           for n in range(4800)))
    app = LibraryApp(tmp_path / 'library'); workspace = app.create_workspace('Publication roundtrip')
    service = ProductionService(app.root / 'state' / 'studio', voices=SyntheticTones())
    script = '[Scene: Exact original]\nNarrator: Source <words> & repeated phrase.\n[Pause: 0.2]\nReader: Source <words> & repeated phrase.'
    project = service.create(script, title='Recorded roundtrip', voice_ids=('synthetic-tone',))
    original_lines = {line.id: line.text for scene in project.scenes for line in scene.lines}
    identity = service.submit(project, tmp_path, stems=False)
    completed = service.run(identity)
    assert completed['status'] == 'complete'
    publication = tmp_path / 'published.epub'
    publisher = PublicationService(service)
    result = publisher.export_epub(identity, publication, language='en')
    original = publication.read_bytes(); proof = inspect_epub(publication)
    assert proof['original_script'] == script
    imported = app.import_file(workspace, publication)
    assert imported['state'] == 'complete', imported
    desk = ReadingDesk(app, workspace, imported['edition'])
    page = desk.page(1)
    session = EbookAudioSession(desk, 1)
    try:
        assert len(session.chapter.cues) == len(proof['intervals'])
        for cue, interval in zip(session.chapter.cues, proof['intervals']):
            text = page.raw_text[cue.start:cue.end]
            assert sha256(text.encode()).hexdigest() == interval['text_sha256']
            if interval['kind'] == 'line':
                assert text == original_lines[interval['line']]
            assert (cue.begin, cue.finish) == (interval['start'], interval['end'])
            prepared = session.prepare(cue.index)
            try:
                with wave.open(str(prepared.path), 'rb') as stream:
                    assert (stream.getframerate(), stream.getnchannels()) == (24000, 2)
                    assert stream.getnframes() == round((cue.finish - cue.begin) * 24000)
            finally:
                prepared.close()
        assert publisher.verify(result['id']) == result
        assert publication.read_bytes() == original
        assert app.assets.resolve(desk.identity.asset_sha256).read_bytes() == original
        assert not page.verified and app._selections == {}
    finally:
        session.close(); service.close()


def test_canonical_backup_restores_recorded_epub_without_original_source_or_old_root(imported, tmp_path, monkeypatch):
    import builtins
    import io
    from carr_library.library_backup import create_backup, restore_backup
    app, workspace, original, book, desk = imported
    raw = original.read_bytes()
    old_root = app.root
    backup = tmp_path / 'full-backup'
    expected = create_backup(app, backup)
    original.unlink()
    shutil.rmtree(old_root)
    restored = tmp_path / 'restored-library'
    restore_backup(backup, restored, expected)
    reopened = LibraryApp(restored)
    restored_desk = ReadingDesk(reopened, workspace, book['edition'])

    def guarded(opener):
        def open_checked(file, *args, **kwargs):
            if isinstance(file, (str, Path)):
                selected = Path(file).absolute()
                assert selected != original and not selected.is_relative_to(old_root), selected
            return opener(file, *args, **kwargs)
        return open_checked
    monkeypatch.setattr(builtins, 'open', guarded(builtins.open))
    monkeypatch.setattr(io, 'open', guarded(io.open))
    session = EbookAudioSession(restored_desk, 1)
    try:
        assert session.chapter.asset_sha256 == sha256(raw).hexdigest()
        prepared = session.prepare(1)
        try:
            session.admit(prepared)
            assert prepared.frames == 4800 and prepared.sample_rate == 24000
            assert any(prepared.path.read_bytes())
        finally:
            prepared.close()
        assert reopened.assets.resolve(restored_desk.identity.asset_sha256).read_bytes() == raw
        assert not restored_desk.page(1).verified and reopened._selections == {}
    finally:
        session.close()


def _resource_broker(app, *, cpu_slots=2):
    from carr_library.operations import ResourceBroker, ResourceLimits
    broker = ResourceBroker.for_app(app.root)
    broker.configure(ResourceLimits(cpu_slots=cpu_slots, min_free_disk_bytes=0))
    return broker


def _assert_capacity_held(broker):
    from carr_library.operations import ResourceRequest, ResourceTimeout
    with pytest.raises(ResourceTimeout):
        with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='epub-test-competitor', timeout=0):
            pytest.fail('The real native reservation was released before its child exited.')


@contextmanager
def _pending_real_child(tmp_path):
    from carr_library.audio.cleanup import CleanupOwner
    ready, release = tmp_path / 'child-ready', tmp_path / 'child-release'
    code = ("import os,sys,time; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(str(os.getpid()),encoding='ascii'); "
            "release=Path(sys.argv[2]); "
            "exec('while not release.exists():\\n time.sleep(.01)')")
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code, str(ready), str(release)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    def cleaned():
        if child.poll() is None:
            return False
        child.wait()
        return True
    owner = CleanupOwner(cleaned)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.is_file() and child.poll() is None, 'The controlled real child did not become ready.'
        yield child, owner, release
    finally:
        release.write_bytes(b'release')
        child.wait(timeout=10)
        assert owner.close()


def test_parser_cleanup_owner_retains_source_until_actual_quiescence(imported, monkeypatch, tmp_path):
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.operations import ResourceRequest
    import carr_library.reader.ebook_audio as module
    broker = _resource_broker(imported[0])
    held, errors, entered = {}, [], Event()
    with _pending_real_child(tmp_path) as (child, owner, release):
        pending = AudioCleanupPending('controlled parser cleanup', cleanup_owner=owner)
        def interrupted(command, **options):
            source = Path(command[-3])
            assert source.name == 'source.epub' and source.is_file()
            assert options['owned_windows_job'] is (module.os.name == 'nt')
            held['root'] = source.parent
            entered.set()
            raise pending
        monkeypatch.setattr(module, 'run_process', interrupted)
        def load():
            try:
                EbookAudioSession(imported[-1], 1)
            except BaseException as error:
                errors.append(error)
        worker = Thread(target=load)
        worker.start()
        try:
            assert entered.wait(10)
            assert child.poll() is None and worker.is_alive()
            assert held['root'].is_dir() and not owner.close()
            _assert_capacity_held(broker)
            release.write_bytes(b'release')
            worker.join(10)
            assert not worker.is_alive()
            assert errors == [pending] and owner.quiescent_event.is_set()
            assert child.poll() == 0
            assert not held['root'].exists()
            with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='epub-after-parser', timeout=0):
                pass
        finally:
            release.write_bytes(b'release')
            worker.join(10)


def test_decoder_cleanup_owner_retains_pcm_staging_before_session_close(imported, monkeypatch, tmp_path):
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.operations import ResourceRequest
    broker = _resource_broker(imported[0])
    session = EbookAudioSession(imported[-1], 1)
    held, errors, entered = {}, [], Event()
    with _pending_real_child(tmp_path) as (child, owner, release):
        pending = AudioCleanupPending('controlled decoder cleanup', cleanup_owner=owner)
        def interrupted(runtime, source, destination, **options):
            Path(destination).write_bytes(b'controlled-incomplete-pcm')
            held['root'] = Path(destination).parent
            entered.set()
            raise pending
        monkeypatch.setattr(AudioRuntime, 'convert', interrupted)
        def prepare():
            try:
                session.prepare(0)
            except BaseException as error:
                errors.append(error)
        worker = Thread(target=prepare)
        worker.start()
        try:
            assert entered.wait(10)
            assert child.poll() is None and worker.is_alive()
            assert held['root'].is_dir() and not owner.quiescent
            session.close()
            assert held['root'].is_dir(), 'Session close cannot discard an unreaped decoder staging directory.'
            assert session.root.is_dir() and not owner.close()
            _assert_capacity_held(broker)
            release.write_bytes(b'release')
            worker.join(10)
            assert not worker.is_alive()
            assert errors == [pending] and owner.quiescent and child.poll() == 0
            assert not held['root'].exists() and not session.root.exists()
            with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='epub-after-decoder', timeout=0):
                pass
        finally:
            release.write_bytes(b'release')
            worker.join(10)
            session.close()


def test_recorded_preparation_uses_current_configured_runtime_factory(imported, monkeypatch):
    import carr_library.reader.ebook_audio as module
    from carr_library.runtime_settings import audio_runtime
    calls = []
    def configured(root, **options):
        calls.append((Path(root), options['state_root']))
        assert options['state_root'] == imported[0].root / 'state'
        return audio_runtime(root, **options)
    monkeypatch.setattr(module, 'audio_runtime', configured)
    session = EbookAudioSession(imported[-1], 1)
    try:
        prepared = session.prepare(0)
        try:
            assert len(calls) == 1 and calls[0][0] == prepared.path.parent
            assert prepared.frames == 4800
        finally:
            prepared.close()
    finally:
        session.close()


@pytest.mark.parametrize('stage', ['parse', 'decode'])
def test_cancellation_while_waiting_for_shared_capacity_starts_no_source_or_decoder_work(imported, monkeypatch, stage):
    from carr_library.operations import ResourceBroker, ResourceRequest
    import carr_library.reader.ebook_audio as module
    app, _, _, _, desk = imported
    broker = _resource_broker(app)
    session = EbookAudioSession(desk, 1) if stage == 'decode' else None
    entered, cancelled = Event(), Event()
    work, errors, results = [], [], []
    reserve = ResourceBroker.reserve
    @contextmanager
    def observed(self, request, **options):
        if options['operation_id'].startswith('epub-audio:'):
            entered.set()
        with reserve(self, request, **options) as lease:
            yield lease
    monkeypatch.setattr(ResourceBroker, 'reserve', observed)
    original_asset, original_parser, original_inspect = app._asset_bytes, module.run_process, AudioRuntime.inspect
    def asset(*args, **kwargs):
        work.append('asset')
        return original_asset(*args, **kwargs)
    def parser(*args, **kwargs):
        work.append('parser')
        return original_parser(*args, **kwargs)
    def inspect(*args, **kwargs):
        work.append('decoder')
        return original_inspect(*args, **kwargs)
    monkeypatch.setattr(app, '_asset_bytes', asset)
    monkeypatch.setattr(module, 'run_process', parser)
    monkeypatch.setattr(AudioRuntime, 'inspect', inspect)
    def waiting():
        try:
            results.append(EbookAudioSession(desk, 1, cancelled=cancelled.is_set) if session is None
                           else session.prepare(0, cancelled=cancelled.is_set))
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=waiting)
    try:
        with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='epub-blocking-work', timeout=0):
            worker.start()
            assert entered.wait(10)
            assert worker.is_alive() and work == []
            cancelled.set()
            worker.join(10)
            assert not worker.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], AudioCancelled)
            assert results == [] and work == []
        assert broker.snapshot()['leases'] == []
    finally:
        cancelled.set()
        if worker.ident is not None:
            worker.join(10)
        for result in results:
            result.close()
        if session is not None:
            session.close()


@pytest.mark.parametrize('stage', ['parse', 'decode'])
def test_changed_selection_while_waiting_is_rechecked_before_admitted_heavy_work(imported, monkeypatch, stage):
    from carr_library.operations import ResourceBroker, ResourceRequest
    import carr_library.reader.ebook_audio as module
    app, _, _, _, desk = imported
    broker = _resource_broker(app)
    selected = [True]
    def guard():
        if not selected[0]:
            raise ValueError('Controlled source selection changed while queued.')
    session = EbookAudioSession(desk, 1, guard=guard) if stage == 'decode' else None
    entered, errors, results, work = Event(), [], [], []
    reserve = ResourceBroker.reserve
    @contextmanager
    def observed(self, request, **options):
        if options['operation_id'].startswith('epub-audio:'):
            entered.set()
        with reserve(self, request, **options) as lease:
            yield lease
    monkeypatch.setattr(ResourceBroker, 'reserve', observed)
    def forbidden(*args, **kwargs):
        work.append('unreviewed source or decoder work')
        raise AssertionError(work[-1])
    monkeypatch.setattr(app, '_asset_bytes', forbidden)
    monkeypatch.setattr(module, 'run_process', forbidden)
    monkeypatch.setattr(AudioRuntime, 'inspect', forbidden)
    def waiting():
        try:
            results.append(EbookAudioSession(desk, 1, guard=guard) if session is None else session.prepare(0))
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=waiting)
    try:
        with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='epub-blocking-selection', timeout=0):
            worker.start()
            assert entered.wait(10)
            assert worker.is_alive() and work == []
            selected[0] = False
        worker.join(10)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], ValueError)
        assert 'selection changed' in str(errors[0])
        assert results == [] and work == [] and broker.snapshot()['leases'] == []
    finally:
        selected[0] = False
        if worker.ident is not None:
            worker.join(10)
        for result in results:
            result.close()
        if session is not None:
            session.close()


def test_configured_two_thread_decoder_is_refused_by_explicit_one_slot_policy(imported, monkeypatch):
    from carr_library.operations import ResourceUnavailable
    broker = _resource_broker(imported[0], cpu_slots=1)
    session = EbookAudioSession(imported[-1], 1)
    def no_decoder(*args, **kwargs):
        pytest.fail('The explicitly refused two-thread decoder was launched.')
    monkeypatch.setattr(AudioRuntime, 'inspect', no_decoder)
    try:
        with pytest.raises(ResourceUnavailable):
            session.prepare(0)
        assert broker.limits.cpu_slots == 1
        assert broker.snapshot()['leases'] == []
    finally:
        session.close()


def test_actual_parser_decoder_and_passive_pcm_use_one_shared_resource_coordinator(imported, monkeypatch):
    from carr_library.operations import ResourceRequest
    import carr_library.reader.ebook_audio as module
    broker = _resource_broker(imported[0])
    seen = []
    run, convert = module.run_process, AudioRuntime.convert
    def parser(command, **options):
        lease, = broker.snapshot()['leases']
        request = lease['request']
        assert lease['operation_id'].endswith(':parse') and request['cpu_slots'] == 1
        assert request['memory_bytes'] >= 2 * module.epub.MAX_SOURCE_BYTES
        assert Path(request['disk_path']) == Path(command[-3]).parent
        assert request['disk_bytes'] >= Path(command[-3]).stat().st_size + 2 * options['budget'].max_log_bytes
        seen.append('parser')
        _assert_capacity_held(broker)
        return run(command, **options)
    def decoder(runtime, source, destination, **options):
        lease, = broker.snapshot()['leases']
        request = lease['request']
        assert lease['operation_id'].endswith(':decode')
        assert request['cpu_slots'] == runtime.budget.threads == 2
        assert request['memory_bytes'] >= 2 * runtime.budget.max_alloc_bytes
        pcm = math.ceil(options['duration'] * 24000) * 4 + 65536
        assert request['disk_bytes'] >= Path(source).stat().st_size + pcm + runtime.budget.reserve_disk_bytes
        assert Path(request['disk_path']) == Path(destination).parent
        seen.append('decoder')
        _assert_capacity_held(broker)
        return convert(runtime, source, destination, **options)
    monkeypatch.setattr(module, 'run_process', parser)
    monkeypatch.setattr(AudioRuntime, 'convert', decoder)
    session = EbookAudioSession(imported[-1], 1)
    try:
        prepared = session.prepare(0)
        try:
            assert seen == ['parser', 'decoder'] and prepared.frames == 4800
            assert prepared.path.is_file() and broker.snapshot()['leases'] == []
            # Playback keeps its verified file but never keeps decoder CPU.
            with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='epub-passive-playback', timeout=0):
                assert prepared.path.is_file()
        finally:
            prepared.close()
    finally:
        session.close()
