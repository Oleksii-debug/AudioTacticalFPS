"""Real WAV, exact-source and durable recovery tests for the shared reader."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio.engines import Voice as CoreVoice, VoiceRegistry as CoreRegistry, FliteEngine, SAPI5Engine
from carr_library.audio.ffmpeg import wav_info
from carr_library.speech import ReadingDocument, SpeechSettings, VoiceRegistry
from carr_library.speech._io import local_file
from carr_library.speech.session import ReaderDraftStore, ReaderResume, validate_sessions_bytes, session_source_bindings
from carr_library.speech.source import read_source_range, source_key
from carr_library.ui.voice_reader import VoiceReaderActions


class PCMEngine:
    notices = []

    def __init__(self, after=lambda: None, frames=800):
        self.calls, self.after, self.frames = [], after, frames

    def voices(self):
        return (CoreVoice('fixture:local', 'Synthetic PCM', ('uk',), 'fixture'),)

    def synthesize(self, text, output, **kwargs):
        self.calls.append((text, kwargs))
        with wave.open(str(output), 'wb') as audio:
            audio.setparams((1, 2, 8000, self.frames, 'NONE', 'not compressed'))
            audio.writeframes(b'\x01\x02' * self.frames)
        self.after()


def test_literal_text_uses_existing_engine_contract_and_streams_exact_chunks(tmp_path):
    engine = PCMEngine()
    registry = VoiceRegistry(engine)
    text = '[music on]\nNarrator: literal text.\r\n' + ('Довгий текст. ' * 700)
    result = registry.synthesize(text, 'fixture:local', tmp_path / 'spoken.wav')
    assert ''.join(call[0] for call in engine.calls) == text
    assert all(len(call[0]) <= 7000 for call in engine.calls)
    assert engine.calls[0][1]['language'] == 'uk'
    assert engine.calls[0][1]['volume'] == 0.8
    assert result.chunks >= 2 and result.frames == len(engine.calls) * 800
    assert result.text_sha256 == sha256(text.encode()).hexdigest()
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
    assert wav_info(result.path).frames == result.frames
    assert not list(tmp_path.glob('.live-reading-*'))


@pytest.mark.parametrize('failure', ['cancel', 'source', 'budget'])
def test_stopped_or_changed_source_never_publishes_partial_audio(tmp_path, failure):
    changed = []
    engine = PCMEngine(after=lambda: changed.append(True), frames=5000 if failure == 'budget' else 800)
    registry = VoiceRegistry(engine)
    def guard():
        if failure == 'source' and changed:
            raise ValueError('The source revision changed.')
    with pytest.raises((InterruptedError, ValueError)):
        registry.synthesize('Synthetic source', 'fixture:local', tmp_path / 'output.wav', guard=guard,
            cancelled=lambda: failure == 'cancel' and bool(changed), settings=SpeechSettings(max_output_bytes=4096))
    assert not (tmp_path / 'output.wav').exists()
    assert not list(tmp_path.glob('.live-reading-*'))


def test_missing_voice_or_existing_output_never_changes_voice_or_overwrites(tmp_path):
    engine = PCMEngine()
    registry = VoiceRegistry(engine)
    with pytest.raises(ValueError, match='unavailable'):
        registry.synthesize('Synthetic', 'fixture:removed', tmp_path / 'missing.wav')
    existing = tmp_path / 'existing.wav'
    existing.write_bytes(b'Keep unchanged')
    with pytest.raises(FileExistsError):
        registry.synthesize('Synthetic', 'fixture:local', existing)
    assert engine.calls == [] and existing.read_bytes() == b'Keep unchanged'


def test_real_installed_local_synthesis_uses_same_registry_as_studio(tmp_path):
    if os.name == 'nt':
        # SAPI is the actual installed Windows voice provider. The reviewed
        # Windows FFmpeg build does not promise the optional Flite filter.
        engine = SAPI5Engine()
    else:
        ffmpeg = shutil.which('ffmpeg')
        if ffmpeg is None:
            pytest.skip('An actual FFmpeg build with Flite is required.')
        engine = FliteEngine(Path(ffmpeg).resolve())
    voices = engine.voices()
    registry = VoiceRegistry(CoreRegistry((engine,)))
    result = registry.synthesize('A real local voice reads this synthetic sentence.', voices[0].id,
                                 tmp_path / 'actual.wav')
    assert result.frames > 800 and result.duration_seconds > 0.1
    assert any(result.path.read_bytes()[44:])


def test_system_engine_hardlink_is_not_confused_with_untrusted_reading_input(tmp_path):
    system_binary = tmp_path / 'powershell.exe'
    system_binary.write_bytes(b'Synthetic executable identity; never executed')
    second = tmp_path / 'system-component.exe'
    os.link(system_binary, second)
    # SAPI5Engine is the existing explicit engine adapter, not the strict
    # reading-input validator that broke PR103's native discovery.
    assert SAPI5Engine(system_binary).binary == system_binary
    with pytest.raises(ValueError, match='without links'):
        local_file(system_binary)


def test_reading_copy_uses_narration_draft_and_preserves_source_on_edit_save_reload(tmp_path):
    private = tmp_path / 'library'
    private.mkdir()
    original = tmp_path / 'source.txt'
    original.write_bytes('Original\r\nОригінал'.encode('utf-8'))
    document = ReadingDocument('Original\r\nОригінал', source_path=original, private_root=private)
    document.set_text('Spoken\r\nОригіна́л')
    path = document.save(tmp_path / 'reading.json')
    loaded = ReadingDocument.load(path, private_root=private)
    assert loaded.source_text == 'Original\r\nОригінал'
    assert loaded.text == 'Spoken\r\nОригіна́л'
    assert loaded.draft.spans() and loaded.undo()
    assert loaded.text == loaded.source_text
    assert loaded.redo() and loaded.text == document.text
    with pytest.raises(ValueError, match='immutable'):
        loaded.save(original)
    path.write_bytes(b'Externally changed')
    with pytest.raises(ValueError, match='changed outside'):
        loaded.save(path)
    assert original.read_bytes() == 'Original\r\nОригінал'.encode('utf-8')


def test_manual_reader_recovers_exact_edit_cursor_voice_and_audio_hash(tmp_path):
    document = ReadingDocument('Original\r\nText', private_root=tmp_path)
    document.set_text('Spoken\r\nText')
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(document, 'fixture:local', 0, 80, 4, 2.5, 'a' * 64, 'b' * 64))
    restored = ReaderDraftStore(tmp_path).load()
    assert restored.document.source_text == 'Original\r\nText'
    assert restored.document.text == 'Spoken\r\nText'
    assert (restored.cursor, restored.audio_seconds, restored.audio_key, restored.audio_sha256) == (4, 2.5, 'a' * 64, 'b' * 64)
    assert restored.document.undo() and restored.document.text == 'Original\r\nText'
    assert session_source_bindings(store.path.read_bytes()) == ()


def test_library_sessions_bind_exact_revision_and_positions_without_retarget(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source workspace')
    imported = app.add_text(workspace, 'Source', 'Exact source\r\nOne page.')
    source = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 1)
    document = ReadingDocument(source.text, source_id=source.identity, source_binding=source.binding,
                               private_root=app.root)
    document.set_text('A separate spoken derivative.')
    store = ReaderDraftStore(app.root, slot=source.identity)
    store.save(ReaderResume(document, 'fixture:local'))
    bindings = session_source_bindings(store.path.read_bytes())
    assert bindings == (source.binding,)
    assert bindings[0]['positions'][0]['asset_sha256'] == imported['asset']
    changed_slot = source_key(workspace, imported['edition'], 'another-revision', 1, 1)
    assert ReaderDraftStore(app.root, slot=changed_slot).load() is None
    with pytest.raises(ValueError, match='different source'):
        ReaderDraftStore(app.root, slot=changed_slot).save(ReaderResume(document))
    assert store.load().document.source_id == source.identity


@pytest.mark.parametrize('mutation', ['unknown', 'source', 'hash', 'cursor', 'duplicate'])
def test_reader_recovery_rejects_unrecognized_or_corrupt_state_without_overwrite(tmp_path, mutation):
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(ReadingDocument('Original')))
    before = store.path.read_bytes()
    value = json.loads(before)
    resume = value['sessions'][store.key]['resume']
    if mutation == 'unknown':
        resume['api_key'] = 'synthetic forbidden field'
    elif mutation == 'source':
        resume['document']['draft']['source_text'] = 'Changed'
    elif mutation == 'hash':
        resume['audio_key'] = 'a' * 64
    elif mutation == 'cursor':
        resume['cursor'] = 99999
    else:
        store.path.write_bytes(b'{"kind":1,"kind":2}')
    if mutation != 'duplicate':
        store.path.write_text(json.dumps(value))
    damaged = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save(ReaderResume(ReadingDocument('New')))
    assert store.path.read_bytes() == damaged


def test_source_identity_is_unambiguous_when_catalog_ids_contain_slashes():
    assert source_key('a/b', 'c', 'd', 1, 2) != source_key('a', 'b/c', 'd', 1, 2)


def test_voice_reader_menu_action_rechecks_root_and_blocks_busy_open():
    calls, messages = [], []
    frame = SimpleNamespace(library=object(), working=False, busy=False, report=messages.append)
    def show(parent, *, source_guard):
        source_guard()
        calls.append(True)
        parent.library = object()
        source_guard()
    actions = VoiceReaderActions(frame, reader=show)
    actions.open()
    assert calls == [True] and 'changed' in messages[-1]
    frame.working = True
    actions.open()
    assert len(calls) == 1 and 'operation' in messages[-1]
