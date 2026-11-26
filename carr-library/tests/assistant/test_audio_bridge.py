"""Actual local speech/DSP callback, durable cancellation and output budgets."""
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.audio_bridge import LocalAudioProduction
from carr_library.audio.engines import FliteEngine, SAPI5Engine, VoiceRegistry
from carr_library.audio.ffmpeg import find_local_tool, wav_info


@pytest.fixture
def production(tmp_path):
    binary = find_local_tool('ffmpeg')
    assert binary is not None, 'This integration gate requires actual installed FFmpeg.'
    engine = SAPI5Engine() if os.name == 'nt' else FliteEngine(binary)
    voices = [voice for voice in engine.voices() if any(code.split('-')[0] == 'en' for code in voice.languages)]
    assert voices, 'This integration gate requires an actual local English voice.'
    app = LibraryApp(tmp_path / 'private')
    output = tmp_path / 'outputs'
    output.mkdir()
    bridge = LocalAudioProduction(app.root, registry=VoiceRegistry((engine,)), ffmpeg=binary)
    service = AssistantService(app, production=bridge.assistant_callback)
    return app, output, engine, voices[0], service


def approved(service, output, voice, text, **options):
    scope = Scope(output_root=output, max_bytes=32 * 1024 ** 2, max_files=20)
    plan = Plan(scope, (Step('audio.produce', {'text': text, 'voice_ids': [voice.id], **options}),))
    return service.submit(plan)


def test_actual_callback_preserves_literal_source_and_returns_every_output(production, tmp_path, monkeypatch):
    app, output, engine, voice, service = production
    original = '# A literal heading.\r\nNarrator: This label is spoken.\n[pause 1]\nEnd.'
    source = tmp_path / 'source.txt'
    source.write_bytes(original.encode('utf-8'))
    checksum = sha256(source.read_bytes()).hexdigest()
    identity = approved(service, output, voice, original, format='flac', stems=True)
    report = service.run(identity)
    assert report.status == 'complete', report
    result = report.results[0]
    assert result['input_kind'] == 'narration'
    assert result['source_sha256'] == checksum
    files = {Path(item['path']).name: Path(item['path']) for item in result['artifacts']}
    assert len(files) == 8 and 'complete.json' in files and 'master.flac' in files
    assert wav_info(files['master.wav']).frames > 0
    timeline = json.loads(files['timeline.json'].read_text(encoding='utf-8'))
    assert ''.join(line['text'] for line in timeline['lines']) == original
    assert all(line['literal_narration'] for line in timeline['lines'])
    assert sha256(source.read_bytes()).hexdigest() == checksum
    monkeypatch.setattr(engine, 'synthesize', lambda *a, **k: pytest.fail('Completed output was resynthesized.'))
    assert service.resume(identity).status == 'complete'


def test_actual_renderer_cancellation_maps_to_cancelled_and_resumes_saved_chunk(production, monkeypatch):
    app, output, engine, voice, service = production
    calls = []
    original = engine.synthesize
    def synthesize(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(engine, 'synthesize', synthesize)
    identity = approved(service, output, voice, 'A short synthetic sentence survives cancellation.', stems=False)
    cancelled = []
    def progress(done, total, message):
        if message == 'Completed and saved an audio step.' and not cancelled:
            cancelled.append(True)
            service.cancel(identity)
    report = service.run(identity, progress=progress)
    assert report.status == 'cancelled', report
    assert len(calls) == 1 and not list(output.glob('*/renders/*/complete.json'))
    accepted = {path: sha256(path.read_bytes()).hexdigest() for path in output.glob('*/cache/*.wav')}
    assert accepted
    resumed = service.resume(identity)
    assert resumed.status == 'complete', resumed
    assert len(calls) == 1
    assert all(sha256(path.read_bytes()).hexdigest() == digest for path, digest in accepted.items())


def test_file_budget_fails_before_generation_or_project_creation(production, monkeypatch):
    app, output, engine, voice, service = production
    monkeypatch.setattr(engine, 'synthesize', lambda *a, **k: pytest.fail('Budget failure reached speech.'))
    scope = Scope(output_root=output, max_files=1, max_bytes=32 * 1024 ** 2)
    plan = Plan(scope, (Step('audio.produce', {'text': 'Synthetic source.', 'voice_ids': [voice.id]}),))
    result = service.run(service.submit(plan))
    assert result.status == 'failed' and 'file budget' in result.message
    assert not list(output.iterdir())


def test_byte_budget_fails_before_generation_or_project_creation(production, monkeypatch):
    app, output, engine, voice, service = production
    monkeypatch.setattr(engine, 'synthesize', lambda *a, **k: pytest.fail('Budget failure reached speech.'))
    scope = Scope(output_root=output, max_bytes=512)
    plan = Plan(scope, (Step('audio.produce', {'text': 'Synthetic source.', 'voice_ids': [voice.id]}),))
    result = service.run(service.submit(plan))
    assert result.status == 'failed' and 'budget' in result.message.lower()
    assert not list(output.iterdir())
