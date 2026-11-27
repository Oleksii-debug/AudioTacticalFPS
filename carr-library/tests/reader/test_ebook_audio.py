"""Real retained EPUB/SMIL and FFmpeg preparation; no native acceptance claim."""
from hashlib import sha256
from io import BytesIO
import ast
from concurrent.futures import ThreadPoolExecutor
import inspect
import math
from pathlib import Path
from queue import Queue
import shutil
import struct
import textwrap
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


class WindowsSourceText:
    """UTF-16/CRLF and visual-row XY mapping, as in wxMSW's EDIT control.

    Pinned wx3.2.8 src/msw/textctrl.cpp uses EM_LINEINDEX/EM_LINELENGTH.
    This control double reproduces that contract; it is not native execution.
    """
    def __init__(self, *, wrap, columns):
        self.wrap, self.columns = wrap, columns
        self.value, self.selection = '', (0, 0)

    def ChangeValue(self, value): self.value, self.selection = value, (0, 0)
    def GetValue(self): return self.value
    def SetSelection(self, first, last): self.selection = first, last
    def ShowPosition(self, position): pass
    def GetStringSelection(self): return self.GetRange(*self.selection)

    def GetRange(self, first, last):
        native = self.value.replace('\n', '\r\n').encode('utf-16-le')
        return native[2 * first:2 * last].decode('utf-16-le').replace('\r\n', '\n')

    def XYToPosition(self, column, row):
        lines, start = [], 0
        for line in self.value.split('\n'):
            units = len(line.encode('utf-16-le')) // 2
            if self.wrap:
                for offset in range(0, max(1, units), self.columns):
                    lines.append((start + offset, min(self.columns, units - offset)))
            else:
                lines.append((start, units))
            start += units + 2
        if not 0 <= row < len(lines) or not 0 <= column <= lines[row][1]:
            return -1
        return lines[row][0] + column


def source_view(*, columns, old_wrapped_style=False):
    view = EbookAudioDialog.__new__(EbookAudioDialog)
    view.dialog = object()
    view.player_factory = lambda: SimpleNamespace(state='stopped', play=Mock(), close=Mock())
    view.progress = None
    view.segments, view.timer, view.report = Mock(), Mock(), Mock()
    # Execute the actual constructor's field declaration. A future style
    # regression must fail the real highlight/load behavior below.
    source = ast.parse(textwrap.dedent(inspect.getsource(EbookAudioDialog.__init__)))
    declaration, = [node for node in ast.walk(source) if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Attribute) and target.attr == 'text' for target in node.targets)]
    wx = SimpleNamespace(TE_READONLY=1, TE_MULTILINE=2, TE_DONTWRAP=4,
        TextCtrl=lambda parent, style: WindowsSourceText(wrap=old_wrapped_style or not bool(style & 4), columns=columns))
    exec(compile(ast.Module(body=[declaration], type_ignores=[]), '<actual EPUB text declaration>', 'exec'),
         {'self': view, 'wx': wx})
    return view


@pytest.mark.parametrize('columns', [24, 64, 120])
def test_recorded_highlight_preserves_exact_source_after_long_wrapped_prefix(columns):
    from carr_library.studio.publishing import NOTICE
    prefix = 'Recorded EPUB native fixture\n' + NOTICE + '\nNative source\nNarrator: '
    fragment = 'Exact α 😀 source & words.'
    raw = prefix + fragment + '\nSecond source line.'
    cue = SimpleNamespace(index=0, start=len(prefix), end=len(prefix) + len(fragment))
    session = SimpleNamespace(page=SimpleNamespace(raw_text=raw), chapter=SimpleNamespace(cues=(cue,)))
    old = source_view(columns=columns, old_wrapped_style=True)
    with pytest.raises(ValueError, match='exact extracted-text position'):
        old._loaded(session)
    # This is the exact partial initialization seen in the failed Windows gate.
    assert old.transport is not None and old.player.state == 'stopped'
    assert old.text.GetStringSelection() == ''
    view = source_view(columns=columns)
    view._loaded(session)
    assert view.text.GetValue() == raw and view.text.GetStringSelection() == fragment
    assert view.player.state == 'stopped' and not view.transport.active
    view.player.play.assert_not_called()
    view.timer.Start.assert_called_once_with(100)


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


@pytest.mark.parametrize('failure', ['device', 'source'])
def test_native_start_failure_remains_visible_after_owned_generation_cleanup(imported, failure):
    """Real worker/PCM and dialog dispatch; the device is an explicit boundary double."""
    app, workspace, _, _, desk = imported
    session = EbookAudioSession(desk, 1)
    callbacks, clips = Queue(), []
    player = TrackedPlayer()
    if failure == 'device':
        def failed_play(path):
            assert Path(path).is_file()
            raise RuntimeError('Synthetic native playback rejection (MCI 277).')
        player.play = failed_play
    view = EbookAudioDialog.__new__(EbookAudioDialog)
    view.session, view.player = session, player
    view.transport = OverlayPlayback(session, player)
    view.closed = view.busy = False
    view.cancelled, view._pending = Event(), None
    view.progress, view.guard = None, lambda: None
    view.wx = SimpleNamespace(CallAfter=callbacks.put,
        CallLater=lambda *args: pytest.fail('A ready quiet gate must dispatch without a timer.'))
    view.executor = ThreadPoolExecutor(max_workers=1)
    view._enable, view._highlight = Mock(), Mock()
    view.timer, view.status = Mock(), Mock()
    messages = []
    view.status.ChangeValue.side_effect = messages.append
    prepare = session.prepare
    def capture(index, **kwargs):
        prepared = prepare(index, **kwargs)
        clips.append(prepared)
        return prepared
    session.prepare = capture
    try:
        view.play()
        callback = callbacks.get(timeout=15)
        assert view.busy and len(clips) == 1 and clips[0].path.is_file()
        ticket = view.transport.ticket
        if failure == 'source':
            # Admission must still reject a source archived after worker completion.
            app.archive_workspace(workspace)
        callback()
        expected = 'Synthetic native playback rejection' if failure == 'device' else 'archived'
        assert expected in messages[-1], messages
        assert view.transport.ticket != ticket
        assert not view.busy and not view.transport.active and player.state == 'stopped'
        assert view.transport.prepared is None and view._pending is None
        assert not clips[0].path.exists()
        view.timer.Stop.assert_called_once_with()
        view.status.SetFocus.assert_called_once_with()
        view._highlight.assert_not_called()
        view._enable.assert_called_with(True)
    finally:
        view.cancelled.set()
        view.executor.shutdown(wait=True, cancel_futures=True)
        view.transport.close()


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
    script = '[Scene: Exact original]\nNarrator: Source <words> & repeated α 😀 phrase.\n[Pause: 0.2]\nReader: Source <words> & repeated α 😀 phrase.'
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
        view = source_view(columns=64)
        view._loaded(session)
        for cue, interval in zip(session.chapter.cues, proof['intervals']):
            text = page.raw_text[cue.start:cue.end]
            view.transport.index = cue.index
            view._highlight()
            assert view.text.GetStringSelection() == text
            assert view.text.GetValue() == page.raw_text
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
