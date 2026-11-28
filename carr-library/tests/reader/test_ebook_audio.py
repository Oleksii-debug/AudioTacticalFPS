"""Real retained EPUB/SMIL and FFmpeg preparation; no native acceptance claim."""
from hashlib import sha256
from io import BytesIO
import math
from pathlib import Path
import shutil
import struct
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
        guard=lambda: None, _pending=None, report=Mock(), segments=Mock(), _highlight=Mock(), play=Mock(),
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


def test_parser_policy_rejection_precedes_isolated_work_and_preserves_source(imported, monkeypatch):
    from dataclasses import replace
    from carr_library.operations import ResourceBroker, ResourceUnavailable
    from carr_library.reader import ebook_audio
    app, _, source, _, desk = imported
    before = source.read_bytes()
    writes = []
    actual_open = Path.open
    def observed_open(path, mode='r', *args, **kwargs):
        if path.name == 'source.epub' and any(flag in mode for flag in 'wax'):
            writes.append(path)
        return actual_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', observed_open)
    broker = ResourceBroker.for_app(app.root)
    broker.configure(replace(broker.limits, memory_bytes=0, min_free_disk_bytes=0))
    monkeypatch.setattr(ebook_audio, 'run_process',
                        lambda *args, **kwargs: pytest.fail('Denied EPUB parser was started.'))
    with pytest.raises(ResourceUnavailable, match='memory_bytes'):
        EbookAudioSession(desk, 1)
    assert not broker.snapshot()['leases']
    assert source.read_bytes() == before and not app._selections
    assert not writes, 'A rejected parser wrote a private source snapshot before admission.'


def test_actual_parser_and_converter_reserve_the_same_saved_policy(imported, monkeypatch):
    from dataclasses import replace
    from carr_library.operations import ResourceBroker
    from carr_library.reader import ebook_audio
    app, _, _, _, desk = imported
    broker = ResourceBroker.for_app(app.root)
    broker.configure(replace(broker.limits, cpu_slots=1, min_free_disk_bytes=0))
    seen = []
    actual_process, actual_convert, actual_open = ebook_audio.run_process, AudioRuntime.convert, Path.open
    def open_staged(path, mode='r', *args, **kwargs):
        if any(flag in mode for flag in 'wax') and path.name in ('source.epub', 'recording.mp3'):
            stage = 'snapshot' if path.name == 'source.epub' else 'member'
            seen.append((stage, broker.snapshot(), 1))
        return actual_open(path, mode, *args, **kwargs)
    def parser(*args, **kwargs):
        seen.append(('parser', broker.snapshot(), kwargs['budget'].threads))
        return actual_process(*args, **kwargs)
    def convert(runtime, *args, **kwargs):
        seen.append(('convert', broker.snapshot(), runtime.budget.threads))
        return actual_convert(runtime, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', open_staged)
    monkeypatch.setattr(ebook_audio, 'run_process', parser)
    monkeypatch.setattr(AudioRuntime, 'convert', convert)
    session = EbookAudioSession(desk, 1)
    try:
        prepared = session.prepare(0)
        try:
            assert prepared.frames == 4800 and prepared.path.is_file()
        finally:
            prepared.close()
    finally:
        session.close()
    assert [row[0] for row in seen] == ['snapshot', 'parser', 'member', 'convert']
    for _, snapshot, threads in seen:
        assert threads == snapshot['usage']['cpu_slots'] == 1
        assert len(snapshot['leases']) == 1
        lease = snapshot['leases'][0]
        assert lease['operation_id'].startswith('epub-audio:')
        assert lease['request']['memory_bytes'] >= 512 * 1024**2
        assert lease['request']['disk_bytes'] > 0
    assert not broker.snapshot()['leases']


@pytest.mark.parametrize('cancel_wait', [False, True])
def test_waiting_clip_uses_real_shared_lease_and_cancellation_preserves_other_owner(
        imported, monkeypatch, cancel_wait):
    from contextlib import contextmanager
    from dataclasses import replace
    from carr_library.operations import ResourceRequest
    session = EbookAudioSession(imported[-1], 1)
    broker = session.broker
    broker.configure(replace(broker.limits, cpu_slots=1, min_free_disk_bytes=0))
    entered, cancel, finished = Event(), Event(), Event()
    results, errors, conversions = [], [], []
    actual_resources, actual_convert = session._resources, AudioRuntime.convert
    @contextmanager
    def observed_resources(*args, **kwargs):
        entered.set()
        with actual_resources(*args, **kwargs) as lease:
            yield lease
    def convert(runtime, *args, **kwargs):
        conversions.append(runtime.budget.threads)
        return actual_convert(runtime, *args, **kwargs)
    monkeypatch.setattr(session, '_resources', observed_resources)
    monkeypatch.setattr(AudioRuntime, 'convert', convert)
    def prepare():
        try:
            results.append(session.prepare(0, cancelled=cancel.is_set))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
    worker = Thread(target=prepare)
    try:
        with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='unrelated-actual-worker', timeout=0):
            worker.start()
            assert entered.wait(5), 'Clip did not reach shared admission.'
            assert not finished.is_set() and not conversions
            if cancel_wait:
                cancel.set()
                assert finished.wait(5), 'Cancelled admission did not finish.'
                assert len(errors) == 1 and isinstance(errors[0], AudioCancelled)
            assert [lease['operation_id'] for lease in broker.snapshot()['leases']] == ['unrelated-actual-worker']
        assert finished.wait(20), 'Clip did not finish after capacity was released.'
        if cancel_wait:
            assert not results and not conversions
        else:
            assert not errors and len(results) == 1 and conversions == [1]
            assert results[0].frames == 4800
        assert not broker.snapshot()['leases']
    finally:
        cancel.set()
        if worker.ident is not None:
            worker.join(10)
        for prepared in results:
            prepared.close()
        session.close()
        assert not worker.is_alive()


def test_released_clip_metadata_warning_does_not_discard_valid_pcm(imported, monkeypatch):
    session = EbookAudioSession(imported[-1], 1)
    actual_unlink = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.parent == session.broker.leases_root and path.suffix == '.json':
            raise OSError('Synthetic coordination metadata cleanup failure')
        return actual_unlink(path, *args, **kwargs)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(Path, 'unlink', unlink)
            prepared = session.prepare(0)
        try:
            assert prepared.frames == 4800 and prepared.path.is_file()
            assert 'ownership was released' in session.resource_notice
            assert not session.broker.snapshot()['leases']
        finally:
            prepared.close()
    finally:
        session.close()


def test_pending_parser_keeps_its_exact_source_and_reservation_until_native_exit(imported, monkeypatch):
    from carr_library.audio.cleanup import CleanupOwner
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.operations import ResourceBroker
    from carr_library.reader import ebook_audio
    app, _, original, _, desk = imported
    before = original.read_bytes()
    permitted, streams, staged = Event(), [], []
    def finish():
        if not permitted.is_set():
            return False
        for stream in streams:
            stream.close()
        return True
    owner = CleanupOwner(finish)
    pending = AudioCleanupPending('Synthetic parser still owns its EPUB stream', cleanup_owner=owner)
    broker = ResourceBroker.for_app(app.root)
    def parser(arguments, **options):
        source = Path(arguments[-3])
        streams.append(source.open('rb'))
        staged.append(source)
        active = broker.snapshot()['leases']
        assert len(active) == 1
        assert active[0]['request']['memory_bytes'] >= 3 * 1024**3
        raise pending
    monkeypatch.setattr(ebook_audio, 'run_process', parser)
    try:
        with pytest.raises(AudioCleanupPending) as error:
            EbookAudioSession(desk, 1)
        assert error.value is pending
        assert staged and staged[0].read_bytes() == before
        assert not streams[0].closed and len(broker.snapshot()['leases']) == 1
        assert owner.close() is False and staged[0].is_file()
        assert original.read_bytes() == before and not app._selections
    finally:
        permitted.set()
        assert owner.close() is True
    assert staged and not staged[0].parent.exists()
    assert streams[0].closed and not broker.snapshot()['leases']


@pytest.mark.parametrize('boundary', ['inspect', 'convert'])
def test_pending_clip_keeps_source_stage_lock_and_reservation_until_native_exit(imported, monkeypatch, boundary):
    from carr_library.audio.cleanup import CleanupOwner
    from carr_library.audio.models import AudioCleanupPending
    app, _, original, _, desk = imported
    before = original.read_bytes()
    session = EbookAudioSession(desk, 1)
    permitted, streams, staged = Event(), [], []
    def finish():
        if not permitted.is_set():
            return False
        for stream in streams:
            stream.close()
        return True
    owner = CleanupOwner(finish)
    pending = AudioCleanupPending('Synthetic clip worker still owns its recording', cleanup_owner=owner)
    def native(runtime, selected, *args, **options):
        source = Path(selected)
        streams.append(source.open('rb'))
        staged.append(source)
        raise pending
    monkeypatch.setattr(AudioRuntime, boundary, native)
    root = session.root
    try:
        with pytest.raises(AudioCleanupPending) as error:
            session.prepare(0)
        assert error.value is pending
        assert staged and staged[0].is_file() and not streams[0].closed
        assert root.is_dir() and len(session.broker.snapshot()['leases']) == 1
        with pytest.raises(ValueError, match='Wait for the current EPUB audio preparation'):
            session.prepare(1)
        session.close()
        assert root.is_dir() and staged[0].is_file()
        assert owner.close() is False and len(session.broker.snapshot()['leases']) == 1
        assert original.read_bytes() == before and not app._selections
    finally:
        permitted.set()
        assert owner.close() is True
        session.close()
    assert streams[0].closed and not staged[0].parent.exists() and not root.exists()
    assert not session.broker.snapshot()['leases']
    with pytest.raises(AudioCancelled):
        session.prepare(0)  # Cleanup released the busy lock; a closed source still cannot restart.


def test_failed_native_wave_close_does_not_forget_the_device_before_retry(monkeypatch):
    from carr_library.speech import player as native
    player = native.WavePlayer()
    calls, permitted = [], [False]
    def command(text):
        calls.append(text)
        if text.startswith('close ') and not permitted[0]:
            raise RuntimeError('Synthetic native close still owns its WAV')
        return ''
    monkeypatch.setattr(native, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(player, '_mci', command)
    player._open = True
    with pytest.raises(RuntimeError, match='still owns'):
        player.close()
    assert player._open is True
    permitted[0] = True
    player.close()
    assert player._open is False
    assert [text for text in calls if text.startswith('close ')] == [
        f'close {player._alias}', f'close {player._alias}']


def test_owned_overlay_retry_keeps_actual_prepared_pcm_until_native_close_ack(imported, monkeypatch):
    from carr_library.audio.cleanup import CleanupOwner
    from carr_library.speech import player as native
    session = EbookAudioSession(imported[-1], 1)
    prepared = session.prepare(0)
    player = native.WavePlayer()
    permitted = [False]
    def command(text):
        if text.startswith('close ') and not permitted[0]:
            raise RuntimeError('Synthetic native close still owns its PCM')
        return ''
    monkeypatch.setattr(native, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(player, '_mci', command)
    transport = OverlayPlayback(session, player)
    owner = CleanupOwner(transport.close)
    try:
        assert transport.start(prepared, transport.ticket)
        with pytest.raises(RuntimeError, match='still owns'):
            transport.stop()
        assert prepared.path.is_file() and transport.prepared is prepared and player._open
        assert owner.close() is False
        assert prepared.path.is_file() and session.root.is_dir() and player._open
    finally:
        permitted[0] = True
        assert owner.close() is True
    assert not prepared.path.exists() and not session.root.exists() and not player._open


def test_detached_overlay_observation_keeps_silence_latch_without_native_polling():
    class NativePlayer:
        @property
        def state(self):
            pytest.fail('Detached progress polled a native device while its cleanup owner was running.')
    transport = OverlayPlayback(object(), NativePlayer())
    transport.closed = True
    assert transport.observed.state == 'stopping'


@pytest.mark.parametrize('stage', ['parser', 'inspection', 'decoder'])
def test_actual_worker_uncertain_cleanup_retains_source_clip_lock_and_admission(
        imported, monkeypatch, stage):
    import os
    from dataclasses import replace
    from carr_library.audio import process
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.audio.windows_job import JobProcess
    from carr_library.operations import ResourceBroker, ResourceRequest, ResourceTimeout

    app, _workspace, original, _book, desk = imported
    before = original.read_bytes()
    broker = ResourceBroker.for_app(app.root)
    broker.configure(replace(broker.limits, cpu_slots=1, min_free_disk_bytes=0))
    session = None if stage == 'parser' else EbookAudioSession(desk, 1)
    permitted, released = Event(), Event()
    attempts, files = [], []
    target, name = ((JobProcess, 'close') if stage == 'parser' and os.name == 'nt'
                    else (process, '_wait_posix_group') if stage == 'parser'
                    else (process, '_terminate'))
    actual = getattr(target, name)
    launched = {}
    if stage == 'parser' and os.name != 'nt':
        original_popen = process.subprocess.Popen
        def observe_start(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            launched[child.pid] = child
            return child
        monkeypatch.setattr(process.subprocess, 'Popen', observe_start)

    def uncertain(first, *args_after_child, **kwargs):
        child = launched[first] if stage == 'parser' and os.name != 'nt' else first
        args = child.args
        selected = (stage == 'parser' and len(args) >= 3 and Path(args[-3]).name == 'source.epub'
                    or stage == 'inspection' and Path(args[-1]).name == 'recording.mp3'
                    or stage == 'decoder' and Path(args[-1]).name == 'render.wav'
                    and Path(args[-1]).parent.name.startswith('.audio-'))
        if selected:
            attempts.append(child)
            files.append(Path(args[-3] if stage == 'parser' else args[-1]))
            if not permitted.is_set():
                raise OSError('Synthetic refusal after the actual native worker ran')
        return actual(first, *args_after_child, **kwargs)

    monkeypatch.setattr(target, name, uncertain)
    owner = None
    try:
        with pytest.raises(AudioCleanupPending) as failure:
            if session is None:
                EbookAudioSession(desk, 1)
            else:
                session.prepare(0)
        owner = failure.value.cleanup_owner
        owner.add_quiescent_callback(released.set)
        def exited_successfully(child):
            if child.returncode == 0:
                return True
            if stage == 'parser' and os.name != 'nt':
                observed = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
                return (observed is not None and observed.si_code == os.CLD_EXITED
                        and observed.si_status == 0)
            return False
        assert attempts and files and all(exited_successfully(child) for child in attempts)
        retained = files[0]
        assert retained.is_file(), 'Native-owned input/output was removed before shutdown confirmation.'
        assert owner.close() is False and not owner.quiescent
        assert len(broker.snapshot()['leases']) == 1
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='competing-reader', timeout=0):
                pytest.fail('Unconfirmed native ownership released its shared admission.')
        if session is None:
            assert retained.read_bytes() == before
            source_root = retained.parent
        else:
            source_root = session.root
            assert session.source.read_bytes() == before and session._lock.locked()
            with pytest.raises(ValueError, match='Wait for the current'):
                session.prepare(1)
            if stage == 'decoder':
                with wave.open(str(retained), 'rb') as rendered:
                    assert rendered.getnframes() == 4800 and rendered.getframerate() == 24000
            session.close()
            assert session.source.is_file() and retained.is_file() and session._lock.locked()
        assert original.read_bytes() == before and not app._selections
        permitted.set()
        owner.close()
        assert released.wait(10), 'Native completion did not release all retained consumers.'
        assert owner.quiescent and not retained.exists() and not source_root.exists()
        assert not broker.snapshot()['leases']
        if session is not None:
            assert not session._lock.locked()
        assert original.read_bytes() == before and not app._selections
    finally:
        permitted.set()
        if owner is not None:
            owner.close()
            assert released.wait(10)
        if session is not None:
            session.close()


def test_explicit_unconfirmed_player_close_retains_prepared_recording(imported):
    session = EbookAudioSession(imported[-1], 1)
    prepared = session.prepare(0)
    player = TrackedPlayer()
    transport = OverlayPlayback(session, player)
    transport.start(prepared, transport.ticket)
    actual = player.close
    player.close = lambda: False
    try:
        with pytest.raises(RuntimeError, match='not confirmed'):
            transport.stop()
        assert transport.prepared is prepared and prepared.path.is_file()
        assert not transport.closed and not transport.active
        player.close = actual
        transport.close()
        assert transport.closed and not prepared.path.exists() and not session.root.exists()
    finally:
        player.close = actual
        transport.close()


def test_close_at_preparation_unlock_cannot_leave_source_snapshot_retained(imported):
    session = EbookAudioSession(imported[-1], 1)
    actual = session._lock
    closing = []

    class ClosingLock:
        def acquire(self, *args, **kwargs):
            return actual.acquire(*args, **kwargs)

        def release(self):
            if not closing:
                closing.append(True)
                session.close()
                assert actual.locked() and session.source.is_file()
            actual.release()

    session._lock = ClosingLock()
    prepared = None
    try:
        prepared = session.prepare(0)
        assert closing == [True] and session.stopped() and not actual.locked()
        assert not session.root.exists(), 'A close arriving at unlock lost its source cleanup request.'
        assert prepared.path.is_file(), 'The separately owned prepared result belongs to its caller.'
        assert not session.broker.snapshot()['leases']
    finally:
        if prepared is not None:
            prepared.close()
        session.close()


@pytest.mark.parametrize('failure', ['native-open', 'archived-source'])
def test_dialog_reports_start_failure_after_transport_invalidates_its_ticket(
        imported, failure):
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from carr_library.ui.progress_media import ProgressMedia

    app, workspace, original, _book, desk = imported
    original_bytes = original.read_bytes()
    session = EbookAudioSession(desk, 1)
    player = TrackedPlayer()
    native_attempts = []

    def rejected_native_open(path):
        native_attempts.append(path)
        raise RuntimeError('Authored native-open refusal')

    if failure == 'native-open':
        player.play = rejected_native_open

    callbacks = Queue()
    feedback = SimpleNamespace(quiescent=True, suspend=Mock())
    panel = SimpleNamespace(closed=False, preferences=SimpleNamespace(quiet_during_media=True),
        feedback=feedback, active_task=None, _flag=lambda _name: False,
        wx=SimpleNamespace(CallAfter=lambda callback: callbacks.put(callback)))
    media = ProgressMedia(panel)
    class Progress:
        def hold_audio(self):
            return media.hold_audio()
        def refresh_audio_state(self):
            return media.refresh()
    progress = Progress()

    dialog = object.__new__(EbookAudioDialog)
    dialog.closed, dialog.busy = False, False
    dialog.cancelled = Event()
    dialog.session, dialog.player = session, player
    dialog.transport = OverlayPlayback(session, player)
    media.register_player(dialog.transport.observed)
    dialog.progress, dialog.guard = progress, lambda: None
    dialog._pending = None
    dialog.cleanup_warning, dialog._cleanup_owners = '', []
    dialog.timer, dialog._enable, dialog._highlight = Mock(), Mock(), Mock()
    messages = []
    dialog.status = SimpleNamespace(ChangeValue=messages.append, SetFocus=Mock())
    dialog.wx = SimpleNamespace(
        CallAfter=lambda callback: callbacks.put(callback),
        CallLater=lambda *_args: pytest.fail('A silent real progress owner unexpectedly deferred start.'))
    dialog.executor = ThreadPoolExecutor(max_workers=1)
    actual_prepare, prepared_results = session.prepare, []

    def prepare(index, *, cancelled):
        result = actual_prepare(index, cancelled=cancelled)
        prepared_results.append(result)
        return result

    session.prepare = prepare
    try:
        # Actual owned executor prepares a real source-bound PCM segment. The
        # GUI completion alone is held so the final admission can change.
        dialog.play()
        apply = callbacks.get(timeout=30)
        assert len(prepared_results) == 1 and prepared_results[0].path.is_file()
        ticket = dialog.transport.ticket
        if failure == 'archived-source':
            app.archive_workspace(workspace)
        apply()
        assert dialog.transport.ticket != ticket and not dialog.transport.active
        assert not dialog.busy and dialog._pending is None
        assert dialog.cancelled.is_set() and not prepared_results[0].path.exists()
        assert not media.hold_snapshot(), 'Failed start leaked a shared audio quiet hold.'
        assert not callbacks.qsize()
        assert messages[-1] and 'Preparing the exact' not in messages[-1]
        if failure == 'native-open':
            assert 'Authored native-open refusal' in messages[-1]
            assert native_attempts == [prepared_results[0].path]
        else:
            assert 'archived' in messages[-1].casefold()
            assert not player.played and not native_attempts
        dialog._highlight.assert_not_called()
        assert original.read_bytes() == original_bytes
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)
        dialog.transport.close()
        if dialog._pending is not None:
            dialog._pending.close()


def test_cancelled_pending_dialog_start_never_reports_or_plays_a_late_result(imported):
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue

    session = EbookAudioSession(imported[-1], 1)
    player = TrackedPlayer()
    callbacks, prepared_results = Queue(), []
    dialog = object.__new__(EbookAudioDialog)
    dialog.closed, dialog.busy = False, False
    dialog.cancelled = Event()
    dialog.session, dialog.player = session, player
    dialog.transport = OverlayPlayback(session, player)
    dialog.progress, dialog.guard = None, lambda: None
    dialog._pending = None
    dialog.cleanup_warning, dialog._cleanup_owners = '', []
    dialog.timer, dialog._enable, dialog._highlight = Mock(), Mock(), Mock()
    messages = []
    dialog.status = SimpleNamespace(ChangeValue=messages.append, SetFocus=Mock())
    dialog.wx = SimpleNamespace(
        CallAfter=lambda callback: callbacks.put(callback),
        CallLater=lambda *_args: pytest.fail('A cancelled start scheduled native work.'))
    dialog.executor = ThreadPoolExecutor(max_workers=1)
    actual_prepare = session.prepare

    def prepare(index, *, cancelled):
        result = actual_prepare(index, cancelled=cancelled)
        prepared_results.append(result)
        return result

    session.prepare = prepare
    try:
        dialog.play()
        apply = callbacks.get(timeout=30)
        assert len(prepared_results) == 1 and prepared_results[0].path.is_file()
        dialog.stop()
        stopped_messages = list(messages)
        apply()
        assert messages == stopped_messages and not player.played
        assert not prepared_results[0].path.exists()
        assert dialog._pending is None and not dialog.busy
        dialog._highlight.assert_not_called()
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)
        dialog.transport.close()
