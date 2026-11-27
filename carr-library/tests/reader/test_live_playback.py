"""Real file/resource ownership for prepared tempo/pitch; synthetic PCM only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import math
import os
from pathlib import Path
import struct
from threading import Event
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio.engines import Voice
from carr_library.audio.jobs import file_identity, regular_file
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.reader import live_playback
from carr_library.reader.live_playback import PlaybackRuntime, PlaybackSettings, playback_request
from carr_library.reader.live_speech import (
    SpeechOperation, SpeechSelection, SpeechSession, SpeechSettings, text_hash)


def tone(path, *, frames=16000, rate=8000, frequency=440):
    samples = b''.join(struct.pack('<h', int(5000 * math.sin(2 * math.pi * frequency * i / rate)))
                       for i in range(frames))
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, frames, 'NONE', 'not compressed'))
        output.writeframes(samples)


class ToneEngine:
    def __init__(self):
        self.calls = []
        self.bytes = []

    def voices(self, *, cancelled):
        cancelled()
        return (Voice('fixture:voice', 'Synthetic tone fixture', ('en',), 'fixture'),)

    def cache_identity(self, voice, *, cancelled):
        cancelled()
        return {'voice': voice, 'fixture': 'playback-tone-v1'}

    def synthesize(self, text, output, *, cancelled, **settings):
        cancelled()
        tone(output)
        self.calls.append((text, settings))
        self.bytes.append(Path(output).read_bytes())
        cancelled()
        return output


@pytest.fixture
def speech(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    engine = ToneEngine()
    broker = ResourceBroker.for_app(root,
        limits=ResourceLimits(cpu_slots=1, memory_bytes=64 * 1024**2))
    binary = tmp_path / 'synthetic-processor'
    binary.write_bytes(b'Owned processor identity fixture.')
    runtime = PlaybackRuntime(binary, file_identity(regular_file(binary)),
                              sha256(binary.read_bytes()).hexdigest(), 'synthetic identity')
    sessions, operations = [], []
    def create(playback=PlaybackSettings(), *, selected_runtime=runtime, operation=None):
        operation = operation or SpeechOperation(playback=playback, playback_runtime=selected_runtime)
        session = SpeechSession(root, 'First. Second!', SpeechSettings('fixture:voice', 'en'),
            SpeechSelection(0, 14), registry=engine, broker=broker,
            temporary_parent=tmp_path, operation=operation)
        sessions.append(session)
        operations.append(operation)
        return session
    yield SimpleNamespace(create=create, root=root, engine=engine, broker=broker,
                          runtime=runtime, binary=binary)
    for session in reversed(sessions):
        session.close()
    for operation in operations:
        operation.close()


@pytest.mark.parametrize('values', [
    {'tempo': 0}, {'tempo': .249}, {'tempo': 4.01}, {'tempo': True}, {'tempo': float('nan')},
    {'pitch': -12.01}, {'pitch': 12.01}, {'pitch': True}, {'pitch': float('inf')}, {'pitch': '2'},
])
def test_playback_accepts_only_existing_finite_effect_bounds(values):
    with pytest.raises(ValueError):
        PlaybackSettings(**values)


@pytest.mark.parametrize('tempo,pitch', [(.25, -12), (4, 12)])
def test_extreme_playback_settings_use_only_the_existing_typed_effects(tempo, pitch):
    settings = PlaybackSettings(tempo, pitch)
    assert [(effect.kind, effect.parameters) for effect in settings.effects] == [
        ('pitch', (('semitones', pitch),)), ('tempo', (('factor', tempo),))]


def test_default_playback_keeps_exact_synthesized_bytes_without_runtime_or_processing(speech, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Default playback attempted processing or runtime discovery.')
    monkeypatch.setattr(live_playback, 'resolve_ffmpeg', forbidden)
    monkeypatch.setattr(live_playback.processing, 'process_voice', forbidden)
    session = speech.create(selected_runtime=None)
    excerpt = session.render_next()
    assert excerpt.path.read_bytes() == speech.engine.bytes[0]
    assert excerpt.sha256 == sha256(speech.engine.bytes[0]).hexdigest()
    assert excerpt.frames == excerpt.synthesis_frames == 16000
    assert excerpt.sample_rate == excerpt.synthesis_sample_rate == 8000
    assert excerpt.playback == PlaybackSettings() and excerpt.playback.effects == ()


def test_nondefault_playback_requires_explicit_verified_runtime_before_any_owner(speech):
    with pytest.raises(ValueError, match='verified local FFmpeg'):
        SpeechOperation(playback=PlaybackSettings(1.5, 0))
    assert speech.engine.calls == [] and speech.broker.snapshot()['leases'] == []


def test_request_captures_immutable_playback_settings(speech):
    settings = PlaybackSettings(1.5, 3)
    operation = SpeechOperation(playback=settings, playback_runtime=speech.runtime)
    with pytest.raises(FrozenInstanceError):
        settings.tempo = 2
    with pytest.raises(AttributeError):
        operation.playback = PlaybackSettings(2, 4)
    assert operation.playback == PlaybackSettings(1.5, 3)
    operation.close()


def test_transform_reuses_same_reservation_and_does_not_change_voice_or_source(speech, monkeypatch):
    observed = []
    def process(source, output, **options):
        assert len(speech.broker.snapshot()['leases']) == 1
        observed.append((source, output, options))
        options['cancelled']()
        tone(output, frames=32000)
        return output
    monkeypatch.setattr(live_playback.processing, 'process_voice', process)
    session = speech.create(PlaybackSettings(.5, 3))
    excerpt = session.render_next()
    assert [(effect.kind, effect.parameters) for effect in observed[0][2]['effects']] == [
        ('pitch', (('semitones', 3),)), ('tempo', (('factor', .5),))]
    assert speech.engine.calls[0][1] == dict(voice='fixture:voice', language='en',
                                           rate=0, pitch=0, volume=.8)
    assert excerpt.frames == 32000 and excerpt.synthesis_frames == 16000
    assert excerpt.text_sha256 == text_hash('First.')
    assert excerpt.sha256 == sha256(excerpt.path.read_bytes()).hexdigest()
    assert not observed[0][0].exists() and list(session.directory.iterdir()) == [excerpt.path]
    assert session.cursor == 0 and session.text == 'First. Second!'
    assert speech.broker.snapshot()['leases'] == []
    session.release(completed=True)
    assert session.cursor == 6 and not list(session.directory.iterdir())


def test_slower_playback_expands_only_existing_declared_disk_reservation(speech):
    from carr_library.audio.production import plan_narration
    from carr_library.operations.admission import production_request
    request = production_request(plan_narration('Original.', voice='fixture:voice', language='en'),
                                 speech.root, speech.broker.limits)
    assert playback_request(request, PlaybackSettings()) is request
    slower = playback_request(request, PlaybackSettings(.25, 0))
    assert slower.disk_bytes == 4 * request.disk_bytes
    assert replace(slower, disk_bytes=request.disk_bytes) == request


def test_changed_runtime_bytes_cannot_borrow_current_metadata(speech, monkeypatch):
    speech.binary.write_bytes(b'Changed processor identity bytes.')
    runtime = replace(speech.runtime, identity=file_identity(regular_file(speech.binary)))
    def forbidden(*args, **kwargs):
        pytest.fail('Changed runtime was executed.')
    monkeypatch.setattr(live_playback.processing, 'process_voice', forbidden)
    session = speech.create(PlaybackSettings(2, 0), selected_runtime=runtime)
    with pytest.raises(ValueError, match='runtime changed'):
        session.render_next()
    assert session.current is None and session.cursor == 0
    assert not list(session.directory.iterdir()) and speech.broker.snapshot()['leases'] == []


def test_processing_retains_source_reader_until_actual_process_return(speech, monkeypatch):
    def changed_input(source, output, **options):
        with Path(source).open('r+b') as stream:
            stream.seek(44)
            stream.write(b'\x01\x00')
        tone(output)
        return output
    monkeypatch.setattr(live_playback.processing, 'process_voice', changed_input)
    session = speech.create(PlaybackSettings(2, 0))
    # Windows refuses the writer; POSIX detects the changed leased input.
    with pytest.raises(OSError if os.name == 'nt' else RuntimeError):
        session.render_next()
    assert session.current is None and session.cursor == 0
    assert not list(session.directory.iterdir()) and speech.broker.snapshot()['leases'] == []


def test_cancel_close_retains_processor_inputs_and_resource_owner_until_settled(speech, monkeypatch):
    entered, release = Event(), Event()
    paths = []
    def processing(source, output, **options):
        paths.extend((source, output))
        tone(output, frames=8000)
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Processor fixture was not released.')
        options['cancelled']()
        return output
    monkeypatch.setattr(live_playback.processing, 'process_voice', processing)
    session = speech.create(PlaybackSettings(2, 0))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(session.render_next)
        try:
            assert entered.wait(5)
            assert len(speech.broker.snapshot()['leases']) == 1
            with pytest.raises(RuntimeError, match='Wait for speech cancellation'):
                session.close()
            assert all(path.exists() for path in paths) and session.directory.exists()
        finally:
            release.set()
        with pytest.raises(InterruptedError):
            future.result(timeout=5)
    session.close()
    assert not session.directory.exists() and session.cursor == 0
    assert speech.broker.snapshot()['leases'] == []


def test_changed_transformed_pcm_is_rejected_before_player_handoff(speech, monkeypatch):
    def process(source, output, **options):
        tone(output, frames=8000)
        return output
    monkeypatch.setattr(live_playback.processing, 'process_voice', process)
    session = speech.create(PlaybackSettings(2, 0))
    excerpt = session.render_next()
    with excerpt.path.open('r+b') as stream:
        stream.seek(44)
        stream.write(b'\x01\x00')
    session.current = replace(excerpt, identity=file_identity(regular_file(excerpt.path)))
    with pytest.raises(ValueError, match='changed before playback'):
        session.pin_playback()
    assert session._playback_reader is None and session.cursor == 0


def test_one_operation_keeps_playback_selection_through_new_page_sessions(speech, monkeypatch):
    seen = []
    def process(source, output, **options):
        seen.append(options['effects'])
        tone(output, frames=8000)
        return output
    monkeypatch.setattr(live_playback.processing, 'process_voice', process)
    operation = SpeechOperation(playback=PlaybackSettings(2, 3), playback_runtime=speech.runtime)
    first = speech.create(operation=operation)
    first.render_next()
    first.release(completed=True)
    first.close()
    following = speech.create(operation=operation)
    following.render_next()
    assert seen == [operation.playback.effects, operation.playback.effects]
    assert not operation.closed
