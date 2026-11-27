"""Actual source/state/WAV boundaries with an explicitly synthetic audio device.

These tests do not establish Windows playback, listening quality or word timing.
The controllable device makes revocation and pause races reproducible.
"""
from dataclasses import replace
from concurrent.futures import Future
import hashlib
from pathlib import Path
import threading
import time
import wave
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.live_reading import (
    DocumentSequence, LiveBudget, LiveReader, SourceSequence, native_text_range,
)
from carr_library.reader.live_state import FILENAME, LiveState, validate_live_state
from carr_library.speech.reading import ReadingDocument
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.runtime import SpeechResult, Voice


VOICE = Voice('synthetic:test', 'Explicit synthetic test voice', 'synthetic', 'uk', ('rate', 'volume'))


def until(predicate, *, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('The bounded live-reading operation did not reach the expected state.')
        threading.Event().wait(.01)


class SyntheticPlayer:
    """PCM-checking controllable player; never pretends to be an actual device."""
    def __init__(self, *, automatic=True, available=True):
        self.automatic, self.enabled = automatic, available
        self.state, self.position, self.duration = 'closed', 0., 0.
        self.paths, self.play_count, self.pause_count = [], 0, 0
        self.resume_count, self.close_count, self.handle = 0, 0, None
        self.device_lost = False

    def available(self):
        return self.enabled

    def status(self):
        if self.device_lost:
            raise RuntimeError('Synthetic device disappeared.')
        if self.automatic and self.state == 'playing':
            self.state, self.position = 'ended', self.duration
        return dict(state=self.state, position_seconds=self.position,
                    duration_seconds=self.duration, reason='No synthetic test device.')

    def open(self, path):
        assert self.handle is None
        with wave.open(str(path)) as stream:
            assert stream.getsampwidth() == 2 and stream.getnchannels() == 1
            self.duration = stream.getnframes() / stream.getframerate()
        self.handle = Path(path).open('rb')
        self.paths.append(Path(path))
        self.state, self.position = 'stopped', 0.
        return self.status()

    def play(self):
        self.play_count += 1
        self.state = 'playing'
        return dict(state=self.state, position_seconds=self.position, duration_seconds=self.duration)

    def pause(self):
        self.pause_count += 1
        if self.state == 'playing':
            self.state = 'paused'
        return self.status()

    def resume(self):
        self.resume_count += 1
        return self.play()

    def advance(self, seconds):
        if self.state == 'playing':
            self.position = min(self.duration, self.position + seconds)
            if self.position == self.duration:
                self.state = 'ended'

    def seek(self, seconds):
        self.position = seconds
        return self.status()

    def stop(self):
        self.state, self.position = 'stopped', 0.
        return self.status()

    def close(self):
        self.close_count += 1
        if self.handle is not None:
            assert self.paths[-1].is_file(), 'The WAV was deleted before closing its playback handle.'
            self.handle.close()
            self.handle = None
        self.state = 'closed'


class SyntheticRender:
    def __init__(self, before=None):
        self.requests, self.options = [], []
        self.before = before

    def __call__(self, request, target, **options):
        self.requests.append(request)
        self.options.append(options)
        if self.before:
            self.before(request, target, options)
        if options['cancelled']():
            raise InterruptedError('Synthetic rendering observed cancellation.')
        with wave.open(str(target), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b'\x01\x00' * 1600)
        return SpeechResult(Path(target), hashlib.sha256(Path(target).read_bytes()).hexdigest(),
                            .2, 8000, 1, 1600, VOICE.id,
                            hashlib.sha256(request.text.encode('utf-8')).hexdigest(), 1)


@pytest.fixture
def source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Live source tests')
    raw = ('Початок 🚂.\r\nRepeat repeat. Literal <speak>& text. ' * 3
           + '\f \r\n\fДруга сторінка.\r\nEnd.')
    book = app.add_text(workspace, 'Actual retained source', raw, language='uk')
    desk = ReadingDesk(app, workspace, book['edition'])
    return app, workspace, book, desk, raw.split('\f')


@pytest.fixture
def make_reader():
    readers = []

    def make(sequence, *, renderer=None, player=None, **options):
        renderer = renderer or SyntheticRender()
        player = player or SyntheticPlayer()
        options.setdefault('budget', LiveBudget(chunk_characters=64, minimum_free_bytes=1))
        reader = LiveReader(sequence, discover=lambda **kwargs: (VOICE,), render=renderer,
                            player_factory=lambda: player, **options)
        readers.append(reader)
        reader.discover_voices().result(5)
        assert reader.status().voices == (VOICE,)
        return reader, renderer, player

    yield make
    for reader in readers:
        reader.close()
        if reader._future:
            reader._future.result(5)


def test_exact_selection_is_chunked_without_drops_normalization_or_ssml(source, make_reader):
    app, workspace, book, desk, pages = source
    reader, renderer, player = make_reader(SourceSequence(desk))
    start, end = pages[0].index('🚂'), len(pages[0]) - 4
    reader.start(1, VOICE.id, start=start, end=end, rate=.75, volume=.5).result(5)
    assert reader.status().state == 'complete', reader.status()
    assert ''.join(request.text for request in renderer.requests) == pages[0][start:end]
    assert all(len(request.text) <= 64 and request.rate == .75 and request.volume == .5
               for request in renderer.requests)
    assert all(option['configuration_root'] == app.root and option['timeout_seconds'] == 60
               and option['max_output_bytes'] == 32 * 1024 * 1024 for option in renderer.options)
    assert player.play_count == len(renderer.requests), 'An ended chunk must never restart.'
    assert all(not path.exists() for path in player.paths)
    assert not any(path.is_dir() for path in app.root.glob('.live-reading-*'))
    saved = LiveState(app.root).resume(workspace, book['edition'])
    assert saved.offset == end == saved.end and saved.text_length == len(pages[0])
    assert saved.location == desk.page(1).location


@pytest.mark.parametrize('unit', ['word', 'sentence', 'paragraph'])
@pytest.mark.parametrize('reading_copy', [False, True])
def test_current_unit_uses_owned_live_chunks_and_preserves_source_and_reading_map(source, make_reader,
                                                                              unit, reading_copy):
    from carr_library.reader.reading_units import reading_unit_range
    app, workspace, book, desk, pages = source
    original = desk.page(1)
    document = ReadingDocument(pages[0])
    if reading_copy:
        document.replace(0, 0, 'An explicit reading edit. ')
        sequence = DocumentSequence(app.root, document)
        raw = document.text
    else:
        sequence, raw = SourceSequence(desk), pages[0]
    saved_document = document.to_dict()
    start, end = reading_unit_range(raw, raw.index('Literal') + 2, unit)
    reader, renderer, player = make_reader(sequence)
    reader.start(1, VOICE.id, start=start, end=end, continuous=False).result(5)
    assert reader.status().state == 'complete', reader.status()
    assert ''.join(request.text for request in renderer.requests) == raw[start:end]
    assert reader.status().section.ordinal == 1
    assert reader.status().range_end == end
    assert player.play_count == len(renderer.requests) >= 1
    if unit == 'paragraph':
        assert len(renderer.requests) > 1, 'A long selected paragraph must keep the existing bounded chunker.'
    assert all(len(request.text) <= 64 for request in renderer.requests)
    assert all(not path.exists() for path in player.paths)
    assert document.to_dict() == saved_document
    assert desk.page(1).raw_text == original.raw_text and desk.page(1).location == original.location


def test_stop_during_current_paragraph_render_cannot_play_or_continue_and_next_word_is_explicit(source,
                                                                                            make_reader):
    from carr_library.reader.reading_units import reading_unit_range
    app, _workspace, _book, desk, pages = source
    entered, release = threading.Event(), threading.Event()
    def hold_first(request, target, options):
        if not entered.is_set():
            entered.set()
            assert release.wait(5), 'The controlled render was not released.'
    renderer, player = SyntheticRender(hold_first), SyntheticPlayer()
    reader, _renderer, _player = make_reader(SourceSequence(desk), renderer=renderer, player=player)
    start, end = reading_unit_range(pages[0], pages[0].index('Literal'), 'paragraph')
    future = reader.start(1, VOICE.id, start=start, end=end)
    try:
        assert entered.wait(5)
        reader.stop()
    finally:
        release.set()
    future.result(5)
    assert reader.status().state == 'stopped' and player.play_count == 0
    assert len(renderer.requests) == 1 and not player.paths
    assert not any(app.root.glob('.live-reading-*'))
    first_count = len(renderer.requests)
    start, end = reading_unit_range(pages[0], pages[0].index('Literal') + 2, 'word')
    reader.start(1, VOICE.id, start=start, end=end).result(5)
    assert reader.status().state == 'complete'
    assert [request.text for request in renderer.requests[first_count:]] == ['Literal']
    assert player.play_count == 1 and all(not path.exists() for path in player.paths)


@pytest.mark.parametrize('unit', ['word', 'sentence', 'paragraph'])
def test_closed_reader_never_plays_a_pending_unit_after_its_native_display_owner_is_gone(source,
                                                                                       make_reader, unit):
    from carr_library.reader.reading_units import reading_unit_range
    _app, _workspace, _book, desk, pages = source
    reader, renderer, player = make_reader(SourceSequence(desk), require_display_ack=True)
    start, end = reading_unit_range(pages[0], pages[0].index('Literal'), unit)
    future = reader.start(1, VOICE.id, start=start, end=end)
    until(lambda: reader.status().section is not None)
    pending_section = reader.status().section
    completion = reader.close()
    # A callback already queued by the departed native owner is harmless.
    reader.acknowledge(pending_section)
    if completion is not None:
        completion.result(5)
    future.result(5)
    assert not reader.busy and renderer.requests == [] and player.play_count == 0
    assert player.handle is None


def test_native_probe_stages_do_not_reuse_a_closed_readers_cancelled_desk(source):
    import importlib.util
    app, _workspace, _book, desk, pages = source
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_live_reading_smoke.py'
    spec = importlib.util.spec_from_file_location('_native_live_probe_cancellation', path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    stopped = threading.Event()
    desk.cancelled = stopped.is_set
    stopped.set()
    with pytest.raises(TimeoutError):
        desk.page(1)
    independent = probe._independent_probe_desk(app, desk)
    assert type(independent) is ReadingDesk and independent is not desk
    assert independent.identity == desk.identity
    assert independent.page(1).raw_text == pages[0]
    assert desk.cancelled() and stopped.is_set(), 'Do not clear or replace the previous reader stop state.'
    with pytest.raises(TimeoutError):
        desk.page(1)
    later_stop = threading.Event()
    independent.cancelled = later_stop.is_set
    later_stop.set()
    with pytest.raises(TimeoutError):
        independent.page(1)


def test_continuous_reading_is_explicit_and_skips_only_unspoken_whitespace(source, make_reader):
    _app, _workspace, _book, desk, pages = source
    reader, render, player = make_reader(SourceSequence(desk))
    reader.start(1, VOICE.id).result(5)
    assert ''.join(item.text for item in render.requests) == pages[0]
    assert reader.status().section.ordinal == 1
    render.requests.clear()
    reader.start(1, VOICE.id, continuous=True).result(8)
    assert reader.status().state == 'complete', reader.status()
    assert ''.join(item.text for item in render.requests) == pages[0] + pages[2]
    assert reader.status().section.ordinal == 3


def test_selection_end_never_spills_into_next_location(source, make_reader):
    *_, desk, pages = source
    reader, render, _player = make_reader(SourceSequence(desk))
    reader.start(1, VOICE.id, start=0, end=10, continuous=True).result(5)
    assert ''.join(item.text for item in render.requests) == pages[0][:10]
    assert reader.status().section.ordinal == 1


def test_no_voice_is_implicitly_selected_or_substituted(source, make_reader):
    reader, render, player = make_reader(SourceSequence(source[3]))
    with pytest.raises(ValueError, match='explicitly'):
        reader.start(1, '')
    with pytest.raises(ValueError, match='explicitly'):
        reader.start(1, 'missing:voice')
    assert render.requests == [] and player.paths == []


def test_stop_during_renderer_prevents_player_open_and_cleans_partial(source, make_reader):
    entered = threading.Event()

    def blocking(_request, target, options):
        Path(target).write_bytes(b'explicit synthetic incomplete WAV')
        entered.set()
        until(options['cancelled'])
        raise InterruptedError('Stopped synthetic render.')

    reader, _render, player = make_reader(SourceSequence(source[3]), renderer=SyntheticRender(blocking))
    future = reader.start(1, VOICE.id)
    assert entered.wait(5)
    reader.stop()
    future.result(5)
    assert reader.status().state == 'stopped' and reader.status().section is None
    assert player.paths == []
    assert not any(path.is_dir() for path in source[0].root.glob('.live-reading-*'))


def test_workspace_revoked_during_render_never_reaches_playback(source, make_reader):
    app, workspace, _book, desk, _pages = source
    reader, _render, player = make_reader(SourceSequence(desk), renderer=SyntheticRender(
        lambda *_: app.archive_workspace(workspace)))
    reader.start(1, VOICE.id).result(5)
    assert reader.status().state == 'error' and 'archived' in reader.status().message.lower()
    assert player.paths == []


def test_revocation_during_playback_stops_actual_chunk_and_preserves_start(source, make_reader):
    app, workspace, book, desk, _pages = source
    reader, _render, player = make_reader(SourceSequence(desk), player=SyntheticPlayer(automatic=False))
    future = reader.start(1, VOICE.id)
    until(lambda: reader.status().state == 'playing')
    player.advance(.07)
    app.archive_workspace(workspace)
    future.result(5)
    assert reader.status().state == 'error' and player.state == 'closed'
    assert LiveState(app.root).resume(workspace, book['edition']).offset == 0
    assert all(not path.exists() for path in player.paths)


def test_pause_keeps_measured_position_and_resume_does_not_rewind(source, make_reader):
    reader, _render, player = make_reader(SourceSequence(source[3]), player=SyntheticPlayer(automatic=False))
    future = reader.start(1, VOICE.id, end=15)
    until(lambda: reader.status().state == 'playing')
    player.advance(.06)
    reader.toggle_pause()
    until(lambda: reader.status().state == 'paused')
    before = reader.status().audio_seconds
    for _ in range(3):
        player.advance(.08)
        threading.Event().wait(.05)
    assert player.position == before == .06
    assert reader.status().audio_seconds == before
    reader.toggle_pause()
    until(lambda: player.resume_count == 1)
    assert player.position == before
    player.advance(.2)
    future.result(5)
    assert reader.status().state == 'complete' and player.resume_count == 1


@pytest.mark.parametrize('stop_fails', [False, True])
def test_failed_native_close_retains_owned_pcm_and_reports_unconfirmed_silence(source, make_reader, stop_fails):
    from carr_library.reader.live_reading import _RETAINED_MEDIA

    class FailingPlayer(SyntheticPlayer):
        failed = True
        def stop(self):
            if self.failed and stop_fails:
                raise RuntimeError('Native stop failed.')
            return super().stop()
        def close(self):
            if self.failed and self.handle is not None:
                raise RuntimeError('Native close failed.')
            return super().close()

    player = FailingPlayer(automatic=False)
    reader, _render, _player = make_reader(SourceSequence(source[3]), player=player)
    future = reader.start(1, VOICE.id, end=15)
    until(lambda: reader.status().state == 'playing')
    reader.close()
    future.result(5)
    try:
        assert player.handle is not None and player.paths[-1].is_file()
        assert reader._retained_resources in _RETAINED_MEDIA
        assert reader.quiescent is not stop_fails
        assert reader.silence_uncertain is stop_fails
        assert 'retained' in reader.status().warning and 'Native close failed' in reader.status().warning
    finally:
        player.failed = False
        player.close()
        retained = reader._retained_resources
        retained[1].cleanup()
        _RETAINED_MEDIA.remove(retained)
        reader._retained_resources = None
        reader._possibly_audible = False


def test_stop_and_explicit_resume_restarts_exact_chunk_without_auto_play(source, make_reader):
    app, workspace, book, desk, pages = source
    reader, render, player = make_reader(SourceSequence(desk), player=SyntheticPlayer(automatic=False))
    future = reader.start(1, VOICE.id, start=7, end=37)
    until(lambda: reader.status().state == 'playing')
    player.advance(.12)
    reader.stop()
    future.result(5)
    saved = LiveState(app.root).resume(workspace, book['edition'])
    assert saved.offset == 7 and saved.end == 37
    player.automatic = True
    reader.start(1, VOICE.id, resume=True).result(5)
    assert [item.text for item in render.requests] == [pages[0][7:37]] * 2
    assert reader.status().state == 'complete'


def test_seek_stays_in_measured_paused_chunk_and_rejects_stale_chunk_dialog(source, make_reader):
    app, workspace, book, desk, _pages = source
    reader, _render, player = make_reader(SourceSequence(desk), player=SyntheticPlayer(automatic=False))
    future = reader.start(1, VOICE.id, start=7, end=30)
    until(lambda: reader.status().state == 'playing')
    reader.toggle_pause()
    until(lambda: reader.status().state == 'paused')
    view = reader.status()
    reader.seek(.15, expected=view)
    until(lambda: reader.status().audio_seconds == .15)
    assert player.state == 'paused'
    player.advance(.1)
    assert player.position == .15
    with pytest.raises(ValueError, match='chunk changed'):
        reader.seek(.05, expected=replace(view, chunk_start=0))
    saved = LiveState(app.root).resume(workspace, book['edition'])
    assert saved.offset == 7, 'Audio seconds must never become a guessed source-word offset.'
    reader.toggle_pause()
    until(lambda: player.resume_count == 1)
    assert player.position == .15
    reader.stop()
    future.result(5)
    assert reader.status().state == 'stopped' and player.handle is None


def test_document_snapshot_keeps_canonical_edit_map_and_rejects_foreign_source(source):
    app, _workspace, _book, desk, _pages = source
    page = desk.page(1)
    document = ReadingDocument(page.raw_text)
    document.replace(0, 7, 'Edited')
    sequence = DocumentSequence(app.root, document, source_locations=(page.location,))
    captured = sequence.document.to_dict()
    document.set_text('A later editor revision.')
    section = sequence.load(1, lambda: False)
    assert section.text == sequence.document.text and captured == sequence.document.to_dict()
    assert sequence.document.undo() and sequence.document.text == page.raw_text
    foreign = DocumentSequence(app.root, ReadingDocument('Foreign copied source.'),
                               source_locations=(page.location,))
    with pytest.raises(ValueError, match='authenticated source'):
        foreign.load(1, lambda: False)


@pytest.mark.parametrize('outcome', ['complete', 'revoked_after_render', 'cancelled_after_render', 'already_exists'])
def test_wav_export_stays_private_until_final_auth_and_never_clobbers(source, tmp_path, outcome):
    from carr_library.reader.live_export import export_document_speech
    app, workspace, _book, desk, _pages = source
    page = desk.page(1)
    document = ReadingDocument(page.raw_text)
    document.replace(0, 7, 'An edited reading copy')
    sequence = DocumentSequence(app.root, document, source_locations=(page.location,))
    destination = tmp_path / 'published.wav'
    renderer = SyntheticRender()
    cancelled = threading.Event()

    def render(*args, **kwargs):
        result = renderer(*args, **kwargs)
        assert not destination.exists(), 'Synthesis published before final source validation.'
        if outcome == 'revoked_after_render':
            app.archive_workspace(workspace)
        elif outcome == 'cancelled_after_render':
            cancelled.set()
        return result

    if outcome == 'already_exists':
        destination.write_bytes(b'Existing user audio must remain intact.')
    arguments = dict(start=3, end=25, voice_id=VOICE.id, render=render, cancelled=cancelled.is_set)
    if outcome == 'complete':
        result = export_document_speech(sequence, destination, **arguments)
        assert result.path == destination and result.sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
        assert renderer.requests[0].text == document.text[3:25]
        assert renderer.options[0]['configuration_root'] == app.root
        assert desk.page(1).raw_text == page.raw_text
    else:
        error = {'revoked_after_render': ValueError, 'cancelled_after_render': InterruptedError,
                 'already_exists': FileExistsError}[outcome]
        with pytest.raises(error):
            export_document_speech(sequence, destination, **arguments)
        if outcome == 'already_exists':
            assert destination.read_bytes() == b'Existing user audio must remain intact.'
            assert renderer.requests == []
        else:
            assert not destination.exists()
    assert not list(tmp_path.glob('.speech-*'))
    assert not (app.root / FILENAME).exists(), 'Derivative export must not create source voice-resume positions.'


def test_resume_rejects_new_revision_before_any_renderer_call(source, make_reader):
    app, workspace, book, desk, pages = source
    LiveState(app.root).remember(desk.page(1).location, 3, len(pages[0]), len(pages[0]), VOICE.id)
    app.update_metadata(workspace, book['edition'], title='Changed revision')
    reader, render, player = make_reader(SourceSequence(ReadingDesk(app, workspace, book['edition'])))
    reader.start(1, VOICE.id, resume=True).result(5)
    assert reader.status().state == 'error'
    assert render.requests == [] and player.paths == []


def test_corrupt_resume_file_is_preserved_and_reading_reports_save_warning(source, make_reader):
    app, _workspace, _book, desk, _pages = source
    path = app.root / FILENAME
    original = b'{invalid prior state'
    path.write_bytes(original)
    reader, _render, _player = make_reader(SourceSequence(desk))
    reader.start(1, VOICE.id, end=15).result(5)
    assert reader.status().state == 'complete'
    assert 'could not be saved' in reader.status().warning
    assert path.read_bytes() == original


def test_close_during_discovery_discards_result_without_late_state(source, make_reader):
    entered, release = threading.Event(), threading.Event()
    reader, _render, _player = make_reader(SourceSequence(source[3]))

    def delayed(**kwargs):
        entered.set()
        assert release.wait(5)
        return (Voice('late', 'Late voice', 'synthetic', 'und', ()),)

    reader.discover = delayed
    future = reader.discover_voices()
    assert entered.wait(5)
    reader.close()
    closed = reader.status()
    release.set()
    future.result(5)
    assert reader.status() == closed and closed.state == 'closed'


def test_queued_discovery_is_visible_before_worker_and_survives_page_acceptance(source, make_reader):
    from carr_library.ui.reading_desk import ReadingDialog
    reader, _render, _player = make_reader(SourceSequence(source[3]))
    reader._executor.shutdown(wait=True)

    class HeldExecutor:
        def submit(self, operation, *args):
            self.operation, self.args, self.future = operation, args, Future()
            return self.future

        def run(self):
            assert self.future.set_running_or_notify_cancel()
            self.future.set_result(self.operation(*self.args))

        def shutdown(self, **kwargs):
            self.future.cancel()

    reader._executor = held = HeldExecutor()
    future = reader.discover_voices()
    pending = reader.status()
    assert reader.busy and pending.state == 'discovering'
    with pytest.raises(ValueError, match='current reading operation'):
        reader.discover_voices()
    assert reader.status() == pending, 'Rejected work changed the active operation state.'
    dialog = object.__new__(ReadingDialog)
    dialog.closed = False
    dialog.live = SimpleNamespace(core=reader, stop=reader.stop)
    dialog._stop_live()
    assert not reader._cancel.is_set(), 'Source acceptance cancelled queued discovery.'
    held.run()
    future.result(1)
    assert reader.status().state == 'idle' and reader.status().voices == (VOICE,)


def test_no_audio_device_reports_error_without_rendering_or_fallback(source, make_reader):
    reader, render, player = make_reader(SourceSequence(source[3]), player=SyntheticPlayer(available=False))
    reader.start(1, VOICE.id).result(5)
    assert reader.status().state == 'error' and 'device' in reader.status().message
    assert render.requests == [] and player.paths == []


def test_display_must_accept_exact_section_before_playback(source, make_reader):
    reader, render, player = make_reader(SourceSequence(source[3]), require_display_ack=True)
    future = reader.start(1, VOICE.id, end=15)
    until(lambda: reader.status().section is not None)
    assert render.requests == [] and player.paths == []
    reader.acknowledge(replace(reader.status().section))
    assert player.paths == []
    reader.acknowledge(reader.status().section)
    future.result(5)
    assert reader.status().state == 'complete' and len(player.paths) == 1


@pytest.mark.parametrize('failure', ['digest', 'duration', 'budget', 'device'])
def test_render_integrity_budget_and_device_failures_end_bounded_session(source, make_reader, failure):
    render = SyntheticRender()
    def corrupted(*args, **kwargs):
        result = render(*args, **kwargs)
        return replace(result, sha256='0' * 64) if failure == 'digest' else (
            replace(result, duration_seconds=999) if failure == 'duration' else result)
    budget = LiveBudget(chunk_characters=64, session_characters=5 if failure == 'budget' else 500000,
                        minimum_free_bytes=1)
    player = SyntheticPlayer(automatic=failure != 'device')
    reader, _render, _player = make_reader(SourceSequence(source[3]), renderer=corrupted,
                                          player=player, budget=budget)
    future = reader.start(1, VOICE.id, end=15)
    if failure == 'device':
        until(lambda: reader.status().state == 'playing')
        player.device_lost = True
    future.result(5)
    assert reader.status().state == 'error', reader.status()
    assert player.handle is None
    assert not any(path.is_dir() for path in source[0].root.glob('.live-reading-*'))


def test_source_bound_derivative_is_reauthorized_and_has_no_source_resume(source, make_reader):
    app, workspace, _book, desk, _pages = source
    page = desk.page(1)
    project = ReadingDocument(page.raw_text)
    project.set_text('Edited Україна 🚂.\r\n')
    sequence = DocumentSequence(app.root, project, source_locations=(page.location,))
    reader, render, _player = make_reader(sequence)
    reader.start(1, VOICE.id).result(5)
    assert reader.status().state == 'complete', reader.status()
    assert ''.join(item.text for item in render.requests) == project.text
    assert not (app.root / FILENAME).exists()
    app.archive_workspace(workspace)
    render.requests.clear()
    reader.start(1, VOICE.id).result(5)
    assert reader.status().state == 'error' and render.requests == []


def test_native_prefix_mapping_handles_crlf_emoji_and_repeated_text():
    raw = 'Є 🚂\r\nrepeat\r\nrepeat Ω'
    displayed = raw.replace('\r\n', '\n')
    prefix = 'Є 🚂\nrepeat\n'
    start, end = native_text_range(raw, displayed, prefix, prefix + 'repeat')
    assert raw[start:end] == 'repeat' and start == raw.rindex('repeat')
    with pytest.raises(ValueError, match='complete Unicode'):
        native_text_range(raw, displayed, 'Є \ud83d', prefix)
    with pytest.raises(ValueError, match='changed'):
        native_text_range(raw, displayed + 'changed', '', '')


def test_live_state_strict_roundtrip_cancellation_and_no_clobber(source):
    app, workspace, book, desk, pages = source
    state = LiveState(app.root)
    state.remember(desk.page(1).location, 7, 25, len(pages[0]), VOICE.id, rate=.75)
    original = state.snapshot()
    assert validate_live_state(original)[0] == state.resume(workspace, book['edition'])
    with pytest.raises(InterruptedError):
        state.remember(desk.page(1).location, 9, 25, len(pages[0]), VOICE.id, cancelled=lambda: True)
    with pytest.raises(ValueError):
        state.remember(desk.page(1).location, True, 25, len(pages[0]), VOICE.id)
    assert state.snapshot() == original
    forged = original.replace(b'"version": 1', b'"version": 1, "version": 1')
    with pytest.raises(ValueError):
        validate_live_state(forged)


@pytest.mark.parametrize('field', ['text_sha256', 'sha256', 'voice_id', 'path'])
def test_wav_export_rejects_mismatched_render_receipt(source, tmp_path, field):
    from carr_library.reader.live_export import export_document_speech
    app, _, _, desk, _ = source
    page = desk.page(1)
    document = ReadingDocument(page.raw_text)
    sequence = DocumentSequence(app.root, document, source_locations=(page.location,))
    destination = tmp_path / 'mismatch.wav'
    renderer = SyntheticRender()

    def render(*args, **kwargs):
        result = renderer(*args, **kwargs)
        incorrect = {'text_sha256': '0' * 64, 'sha256': '0' * 64,
                     'voice_id': 'wrong:voice', 'path': destination}
        return replace(result, **{field: incorrect[field]})

    with pytest.raises(ValueError, match='receipt'):
        export_document_speech(sequence, destination, start=0, end=10,
                               voice_id=VOICE.id, render=render)
    assert not destination.exists()
    assert not list(tmp_path.glob('.speech-*'))


def test_wav_export_rechecks_full_source_after_staging_fsync(source, tmp_path, monkeypatch):
    import os
    from carr_library.reader.live_export import export_document_speech
    app, _, _, desk, _ = source
    page = desk.page(1)
    sequence = DocumentSequence(app.root, ReadingDocument(page.raw_text),
                                source_locations=(page.location,))
    destination = tmp_path / 'drift.wav'
    renderer = SyntheticRender()
    actual_check = sequence.check
    actual_fsync = os.fsync
    flushed = False
    staged_path = None
    observed = []

    def render(*args, **kwargs):
        nonlocal staged_path
        result = renderer(*args, **kwargs)
        staged_path = result.path
        return result

    def check(section, cancelled, *, full=False):
        observed.append((flushed, full))
        if flushed and full:
            raise ValueError('Injected retained-byte drift detectable only by full attestation.')
        return actual_check(section, cancelled, full=full)

    def flush(fd):
        nonlocal flushed
        actual_fsync(fd)
        if staged_path is not None and staged_path.exists():
            opened, staged = os.fstat(fd), staged_path.stat()
            if (opened.st_dev, opened.st_ino) == (staged.st_dev, staged.st_ino):
                flushed = True

    monkeypatch.setattr(sequence, 'check', check)
    monkeypatch.setattr(os, 'fsync', flush)
    with pytest.raises(ValueError, match='retained-byte drift'):
        export_document_speech(sequence, destination, start=0, end=10,
                               voice_id=VOICE.id, render=render)
    assert renderer.requests and (True, True) in observed
    assert not destination.exists()
    assert not list(tmp_path.glob('.speech-*'))


def test_native_copy_probe_loads_actual_source_keyed_draft_without_using_legacy_slot(source):
    import importlib.util
    from carr_library.speech.session import ReaderDraftStore, ReaderResume, open_source_draft
    from carr_library.speech.source import read_source_range

    app, _workspace, _book, desk, _pages = source
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_live_reading_smoke.py'
    spec = importlib.util.spec_from_file_location('_native_live_probe_draft', path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    slot = 'native-copy-recovery-boundary'
    legacy = ReaderDraftStore(app.root, slot=slot)
    loaded = read_source_range(desk, 1, 1, cancelled=lambda: False)
    resumed, owned, detached = open_source_draft(loaded, legacy,
        choose=lambda: pytest.fail('No older draft exists before the actual source is opened.'))
    assert not detached and owned.path != legacy.path
    original = resumed.document.text
    resumed.document.set_text('Edited ' + original)
    owned.save(replace(resumed, source_locations=loaded.locations))
    legacy.save(ReaderResume(ReadingDocument('Unrelated legacy reading copy')))
    old_bytes = legacy.path.read_bytes()

    _, recovered_legacy, recovered_store, recovered = probe._source_probe_draft(app, desk, slot)
    assert recovered_store.path == owned.path and recovered_legacy.path == legacy.path
    assert recovered is not None and recovered.source_locations == (desk.page(1).location,)
    assert recovered.document.source_text == original
    assert recovered.document.text == 'Edited ' + original
    assert recovered.audio_seconds == 0 and recovered.document.undo()
    assert recovered.document.text == original
    assert legacy.path.read_bytes() == old_bytes
