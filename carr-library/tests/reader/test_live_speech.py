"""Exact source selection, real files/admission and saved reading positions.

The tiny engine produces synthetic PCM only. Actual local voice execution and
native controls have separate Windows/installed probes; these are not quality
or physical playback assertions.
"""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path

import pytest

from carr_library.audio.engines import Voice
from carr_library.audio.ffmpeg import silence
from carr_library.audio.jobs import read_json
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.reader.live_speech import (
    SpeechPositionStore, SpeechSelection, SpeechSession, SpeechSettings,
    decode_positions, next_utterance, saved_locations, select_range, text_hash)
from carr_library.reader.reading_state import ReadingLocation


TEXT = 'Перший рядок. Друге речення!\nAnother line\n\nLast paragraph.'


@pytest.mark.parametrize('mode,cursor,selected,expected', [
    ('page', 0, None, TEXT), ('cursor', 14, None, TEXT[14:]),
    ('selection', 0, (0, 6), TEXT[:6]), ('word', 8, None, 'рядок.'),
    ('line', 18, None, TEXT.splitlines()[0]),
    ('sentence', 16, None, ' Друге речення!'),
    ('sentence', 30, None, 'Another line'),
    ('paragraph', 30, None, TEXT[:TEXT.index('\n\n')]),
    ('paragraph', len(TEXT), None, '\n\nLast paragraph.'),
])
def test_reading_units_are_exact_unmodified_source_slices(mode, cursor, selected, expected):
    span = select_range(TEXT, mode, cursor=cursor, selection=selected)
    assert TEXT[span.start:span.end] == expected


@pytest.mark.parametrize('arguments', [
    {'mode': 'selection'}, {'mode': 'selection', 'selection': (3, 3)},
    {'mode': 'selection', 'selection': (-1, 3)}, {'mode': 'cursor', 'cursor': len(TEXT)},
    {'mode': 'word', 'cursor': True}, {'mode': 'shell'},
])
def test_empty_or_untyped_selections_do_not_start_reading(arguments):
    with pytest.raises(ValueError):
        select_range(TEXT, **arguments)


@pytest.mark.parametrize('text', ['第一句。第二句！', 'one\ntwo', 'x' * 3100, 'word ' * 900])
def test_utterance_continuation_reaches_every_non_whitespace_character(text):
    cursor, pieces, previous = 0, [], 0
    while (span := next_utterance(text, cursor, len(text))) is not None:
        assert span.end > span.start >= previous
        assert span.end - span.start <= 1200
        assert not text[previous:span.start].strip()
        pieces.append(text[span.start:span.end])
        cursor = previous = span.end
    assert ''.join(''.join(pieces).split()) == ''.join(text.split())


class PcmEngine:
    def __init__(self):
        self.calls = []
        self.revision = 'synthetic-revision-1'
        self.hook = lambda: None

    def voices(self, *, cancelled):
        cancelled()
        return (Voice('fixture:voice', 'Synthetic PCM fixture', ('en',), 'fixture'),)

    def cache_identity(self, voice, *, cancelled):
        cancelled()
        return {'engine': 'fixture', 'revision': self.revision, 'voice': voice}

    def synthesize(self, text, output, *, cancelled, **settings):
        cancelled()
        self.calls.append((text, settings))
        silence(output, 800, sample_rate=8000)
        self.hook()
        cancelled()
        return output


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    engine = PcmEngine()
    broker = ResourceBroker.for_app(root, limits=ResourceLimits(cpu_slots=1, memory_bytes=64*1024**2))
    settings = SpeechSettings('fixture:voice', 'en')
    session = SpeechSession(root, 'First. Second!', settings, SpeechSelection(0, 14),
                            registry=engine, broker=broker, temporary_parent=tmp_path)
    yield session, engine, broker
    session.close()


def test_one_measured_utterance_then_explicit_completed_playback_advances(prepared):
    session, engine, broker = prepared
    first = session.render_next()
    assert engine.calls[0][0] == 'First.'
    assert (first.frames, first.sample_rate) == (800, 8000)
    assert first.text_sha256 == text_hash('First.')
    assert session.cursor == 0 and first.path.is_file()
    assert broker.snapshot()['leases'] == []
    with pytest.raises(RuntimeError, match='release'):
        session.render_next()
    session.release(completed=True)
    assert not first.path.exists() and session.cursor == 6
    session.checkpoint()
    second = session.render_next()
    assert engine.calls[1][0] == 'Second!'
    assert second.selection == SpeechSelection(7, 14)
    session.release(completed=True)
    assert session.render_next() is None
    assert session.text == 'First. Second!'


def test_stop_without_observed_completion_repeats_utterance_instead_of_skipping(prepared):
    session, engine, broker = prepared
    first = session.render_next()
    session.release(completed=False)
    assert session.cursor == 0
    second = session.render_next()
    assert first.path != second.path and engine.calls[0][0] == engine.calls[1][0]


def test_cancellation_retains_no_partial_audio_or_resource_reservation(prepared):
    session, engine, broker = prepared
    engine.hook = session.cancel.set
    with pytest.raises(InterruptedError):
        session.render_next()
    assert list(session.directory.iterdir()) == [] and session.current is None
    assert broker.snapshot()['leases'] == []
    assert session.cursor == 0


def test_changed_voice_identity_cannot_continue_an_existing_session(prepared):
    session, engine, broker = prepared
    session.render_next()
    session.release(completed=True)
    engine.revision = 'different runtime'
    with pytest.raises(ValueError, match='changed'):
        session.render_next()
    assert len(engine.calls) == 1 and broker.snapshot()['leases'] == []


def test_voice_that_changes_during_render_never_becomes_a_playable_result(prepared):
    session, engine, broker = prepared
    engine.hook = lambda: setattr(engine, 'revision', 'changed inside render')
    with pytest.raises(ValueError, match='changed'):
        session.render_next()
    assert session.current is None and not list(session.directory.iterdir())
    assert broker.snapshot()['leases'] == []


def test_revoked_source_does_not_synthesize_or_save_another_position(prepared):
    session, engine, broker = prepared
    def revoked():
        raise PermissionError('Selected workspace access was revoked.')
    session.guard = revoked
    with pytest.raises(PermissionError):
        session.render_next()
    with pytest.raises(PermissionError):
        session.checkpoint()
    assert engine.calls == [] and broker.snapshot()['leases'] == []
    assert not (session.root / 'reading-speech.json').exists()


@pytest.mark.parametrize('control', ['pitch', 'rate', 'volume'])
def test_missing_voice_control_is_rejected_without_silent_fallback(control):
    options = {'pitch': 1, 'rate': 1, 'volume': .5}
    settings = SpeechSettings('fixture:voice', 'en', **{control: options[control]})
    with pytest.raises(ValueError, match=control):
        settings.check_voice(Voice('fixture:voice', 'Fixture', ('en',), 'fixture', controls=()))


def test_exact_source_position_is_kept_separate_from_an_independent_copy(tmp_path):
    text = 'Exact source text.'
    location = ReadingLocation('workspace', 'edition', 'revision', 'physical_page', 1,
                               text_hash(text), 'a' * 64)
    store = SpeechPositionStore(tmp_path)
    settings = SpeechSettings('fixture:voice', 'en')
    store.remember(text, 5, settings, location)
    store.remember(text, 2, settings)
    assert store.load(text, location)['cursor'] == 5
    assert store.load(text)['cursor'] == 2
    assert store.load(text + ' changed') is None
    assert saved_locations(store.path.read_bytes()) == (location,)
    assert read_json(store.path)['schema'] == 'carr-live-reading/1'


@pytest.mark.parametrize('field,value', [
    ('cursor', -1), ('cursor', True), ('cursor', 10000), ('characters', 0),
    ('key', 'forged'), ('updated_at', 'today'), ('extra', 'unknown'),
])
def test_invalid_saved_positions_are_never_silently_reset(tmp_path, field, value):
    store = SpeechPositionStore(tmp_path)
    store.remember('Exact source text.', 0, SpeechSettings('fixture:voice', 'en'))
    before = json.loads(store.path.read_text())
    before['positions'][0][field] = value
    raw = json.dumps(before).encode()
    store.path.write_bytes(raw)
    with pytest.raises(ValueError):
        store.load('Exact source text.')
    assert store.path.read_bytes() == raw


def test_invalid_source_binding_never_replaces_a_good_resume(tmp_path):
    store = SpeechPositionStore(tmp_path)
    settings = SpeechSettings('fixture:voice', 'en')
    store.remember('Exact source text.', 1, settings)
    previous = store.path.read_bytes()
    location = ReadingLocation('workspace', 'edition', 'revision', 'physical_page', 1,
                               text_hash('Different source'), 'a' * 64)
    with pytest.raises(ValueError, match='exact source'):
        store.remember('Exact source text.', 2, settings, location)
    assert store.path.read_bytes() == previous


@pytest.mark.parametrize('failure', [PermissionError, NotADirectoryError])
def test_inaccessible_saved_position_is_never_empty_or_replaced(tmp_path, monkeypatch, failure):
    store = SpeechPositionStore(tmp_path)
    text, settings = 'Exact saved source.', SpeechSettings('fixture:voice', 'en')
    store.remember(text, 3, settings)
    previous = store.path.read_bytes()
    lstat, exists, is_symlink = Path.lstat, Path.exists, Path.is_symlink
    def denied(path, *args, **kwargs):
        if path == store.path:
            raise failure('saved reading position lookup unavailable')
        return lstat(path, *args, **kwargs)
    from carr_library.ingest.restore import RestoreError
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'lstat', denied)
        # Some pathlib versions suppress lookup failures in convenience probes.
        patch.setattr(Path, 'exists', lambda path, *a, **kw:
                      False if path == store.path else exists(path, *a, **kw))
        patch.setattr(Path, 'is_symlink', lambda path:
                      False if path == store.path else is_symlink(path))
        with pytest.raises(RestoreError, match='could not be inspected') as load_error:
            store.load(text)
        assert isinstance(load_error.value.__cause__, failure)
        with pytest.raises(RestoreError, match='could not be inspected') as save_error:
            store.remember(text, 6, settings)
        assert isinstance(save_error.value.__cause__, failure)
    assert store.path.read_bytes() == previous


def test_a_long_book_replaces_one_resume_row_instead_of_accumulating_pages(tmp_path):
    store = SpeechPositionStore(tmp_path)
    settings = SpeechSettings('fixture:voice', 'en')
    first = 'First exact page.'
    location = ReadingLocation('workspace', 'edition', 'revision', 'physical_page', 1,
                               text_hash(first), 'a' * 64)
    store.remember(first, 3, settings, location)
    second = 'Second exact page.'
    following = replace(location, index=2, text_sha256=text_hash(second))
    store.remember(second, 7, settings, following)
    assert len(decode_positions(store.path.read_bytes())) == 1
    assert store.load(first, location) is None
    assert store.load(second, following)['cursor'] == 7
    assert store.load(second, replace(following, revision='new-revision')) is None
    assert store.load(second, replace(following, index=3)) is None
    assert saved_locations(store.path.read_bytes()) == (following,)


@pytest.mark.parametrize('phase', ['before', 'after'])
def test_full_source_revalidation_prevents_speaking_changed_source(prepared, phase):
    session, engine, broker = prepared
    valid = [phase != 'before']
    def validate():
        if not valid[0]:
            raise ValueError('The original source changed.')
    session.validate_source = validate
    engine.hook = lambda: valid.__setitem__(0, False)
    with pytest.raises(ValueError, match='source changed'):
        session.render_next()
    assert len(engine.calls) == (0 if phase == 'before' else 1)
    assert session.current is None and session.cursor == 0
    assert not list(session.directory.iterdir()) and broker.snapshot()['leases'] == []


def test_inflight_cancellation_must_settle_before_audio_directory_is_removed(prepared):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    session, engine, broker = prepared
    entered, release = Event(), Event()
    def barrier():
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Test owner did not release the speech barrier.')
    engine.hook = barrier
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(session.render_next)
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError, match='Wait for speech cancellation'):
                session.close()
            assert session.directory.exists() and session.cancel.is_set()
        finally:
            release.set()
        with pytest.raises(InterruptedError):
            future.result(timeout=5)
    session.close()
    assert not session.directory.exists() and broker.snapshot()['leases'] == []


def test_playback_digest_is_required_even_when_metadata_is_presented_as_current(prepared):
    from carr_library.audio.jobs import file_identity, regular_file
    from carr_library.audio.ffmpeg import wav_info
    session, engine, broker = prepared
    excerpt = session.render_next()
    offset = wav_info(excerpt.path).data_offset
    with excerpt.path.open('r+b') as stream:
        stream.seek(offset)
        stream.write(b'\x01\x00')
    # Model the weak-metadata boundary independently of timestamp precision.
    # Actual bytes change; only the expected metadata is replaced for this test.
    session.current = replace(excerpt, identity=file_identity(regular_file(excerpt.path)))
    with pytest.raises(ValueError, match='changed before playback'):
        session.pin_playback()
    assert session._playback_reader is None and session.cursor == 0


@pytest.mark.parametrize('phase', ['before', 'after'])
def test_playback_source_is_rechecked_before_and_after_payload_verification(prepared, phase):
    session, engine, broker = prepared
    session.render_next()
    calls = []
    def validate():
        calls.append(True)
        if phase == 'before' or len(calls) == 2:
            raise PermissionError('Selected source was revoked before playback.')
    session.validate_source = validate
    with pytest.raises(PermissionError, match='revoked'):
        session.pin_playback()
    assert session._playback_reader is None and session.cursor == 0


def test_retained_payload_lease_prevents_false_completion_after_a_write(prepared):
    from carr_library.audio.ffmpeg import wav_info
    session, engine, broker = prepared
    excerpt = session.render_next()
    offset = wav_info(excerpt.path).data_offset
    session.pin_playback()
    session.check_playback()
    if os.name == 'nt':
        with pytest.raises(OSError):
            excerpt.path.open('r+b')
        session.release(completed=True)
        assert session.cursor == 6
    else:
        with excerpt.path.open('r+b') as stream:
            stream.seek(offset)
            stream.write(b'\x01\x00')
        with pytest.raises(RuntimeError, match='changed'):
            session.release(completed=True)
        assert session.cursor == 0
        session.checkpoint()
        assert SpeechPositionStore(session.root).load(session.text)['cursor'] == 0
    session.close()
    assert not excerpt.path.exists()
