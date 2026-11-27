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
    CopySequence, LiveBudget, LiveReader, SourceSequence, native_text_range,
)
from carr_library.reader.live_state import FILENAME, LiveState, validate_live_state
from carr_library.reader.narration import new_project, revise
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.local import RenderCancelled, SpeechResult, Voice


VOICE = Voice('synthetic:test', 'synthetic', 'Explicit synthetic test voice', 'uk', ())


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
            raise RenderCancelled('Synthetic rendering observed cancellation.')
        with wave.open(str(target), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b'\x01\x00' * 1600)
        return SpeechResult(Path(target), 8000, 1, 1600, .2, VOICE.engine, VOICE.id,
                            hashlib.sha256(Path(target).read_bytes()).hexdigest(), 'synthetic-v1')


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
        options.setdefault('budget', LiveBudget(chunk_characters=48, minimum_free_bytes=1))
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
    assert all(len(request.text) <= 48 and request.rate == .75 and request.volume == .5
               for request in renderer.requests)
    assert all(option['configuration_root'] == app.root and option['timeout_seconds'] == 60
               and option['max_output_bytes'] == 32 * 1024 * 1024 for option in renderer.options)
    assert player.play_count == len(renderer.requests), 'An ended chunk must never restart.'
    assert all(not path.exists() for path in player.paths)
    assert not any(path.is_dir() for path in app.root.glob('.live-reading-*'))
    saved = LiveState(app.root).resume(workspace, book['edition'])
    assert saved.offset == end == saved.end and saved.text_length == len(pages[0])
    assert saved.location == desk.page(1).location


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
        raise RenderCancelled('Stopped synthetic render.')

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
        return (Voice('late', 'synthetic', 'Late voice', None, ()),)

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
    budget = LiveBudget(chunk_characters=48, session_characters=5 if failure == 'budget' else 500000,
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
    project = revise(new_project(page.raw_text, source_location=page.location), 'Edited Україна 🚂.\r\n')
    from carr_library.ui.narration_editor import _validate_studio_source
    sequence = CopySequence(app.root, project, validate_source=lambda: _validate_studio_source(app.root, project))
    reader, render, _player = make_reader(sequence)
    reader.start(1, VOICE.id).result(5)
    assert reader.status().state == 'complete', reader.status()
    assert ''.join(item.text for item in render.requests) == project.current_text
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
