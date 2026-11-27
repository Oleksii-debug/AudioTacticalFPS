"""Reuse local engine registrations and cancellation without a second renderer."""
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.production import LocalProduction
from carr_library.audio.engines import Voice, VoiceRegistry


def test_assistant_discovers_the_current_registered_engine_path_lazily(tmp_path, monkeypatch):
    from carr_library import runtime_settings
    app = LibraryApp(tmp_path / 'library')
    calls = []
    voice = Voice('fixture:voice', 'Synthetic voice', ('uk',), 'fixture')
    class Engine:
        id = 'fixture'
        def voices(self, *, cancelled):
            assert not cancelled()
            calls.append('voices')
            return (voice,)
    def configured(root, *, ffmpeg):
        assert root == app.root and ffmpeg is None
        calls.append('configured')
        return VoiceRegistry((Engine(),))
    monkeypatch.setattr(runtime_settings, 'configured_voice_registry', configured)
    producer = LocalProduction(app.root)
    assert calls == []
    plan = producer.plan({'text': 'Literal fixture', 'voice_ids': [voice.id]}, max_bytes=2 * 1024**2)
    assert plan.original_script == 'Literal fixture'
    assert producer.voices() == (voice,)
    assert calls == ['configured', 'voices']


def test_voice_discovery_forwards_and_honors_cancellation(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    cancelled = Event()
    calls = []
    class Registry:
        def voices(self, *, cancelled):
            calls.append(True)
            assert not cancelled()
            return ()
    producer = LocalProduction(app.root, registry=Registry(), cancelled=cancelled.is_set)
    cancelled.set()
    with pytest.raises(InterruptedError):
        producer.voices()
    assert calls == []
    cancelled.clear()
    assert producer.voices() == () and calls == [True]


def test_existing_custom_registry_without_cancel_keyword_is_compatible(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    class Registry:
        def voices(self):
            return ()
    assert LocalProduction(app.root, registry=Registry()).voices() == ()


@pytest.mark.parametrize('default', [True, False], ids=['shared-admission', 'legacy-injected-renderer'])
def test_only_default_renderer_uses_shared_resource_and_music_admission(tmp_path, monkeypatch, default):
    from hashlib import sha256
    from types import SimpleNamespace
    from uuid import uuid4
    from carr_library import runtime_settings
    from carr_library.assistant import production as bridge
    from carr_library.operations import ResourceBroker

    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'output'
    output.mkdir()
    binary = tmp_path / 'ffmpeg-fixture'
    binary.write_text('Never executed in this handler fixture.', encoding='utf-8')
    identity = uuid4().hex
    cancelled = lambda: False
    progress = lambda message: None
    registry = SimpleNamespace(voices=lambda: ())
    admitted, model = object(), object()
    admissions, resolutions = [], []
    def admit(root, *, cancelled):
        assert default, 'An explicit custom renderer unexpectedly acquired a second admission.'
        assert root == app.root
        admissions.append(cancelled)
        return admitted
    def resolve(reference, engine, *, cancelled):
        resolutions.append((reference, engine, cancelled))
        return model
    monkeypatch.setattr(ResourceBroker, 'for_app', admit)
    monkeypatch.setattr(runtime_settings, 'EngineProfileStore',
                        lambda root: SimpleNamespace(music_manifest=resolve))
    def publish(folder):
        folder.mkdir()
        master = folder / 'master.wav'
        raw = b'Synthetic checked artifact; no WAV or voice qualification.'
        master.write_bytes(raw)
        return {'status': 'complete', 'output': str(master),
                'files': [{'name': master.name, 'size': len(raw), 'sha256': sha256(raw).hexdigest()}]}
    def managed(plan, folder, *, engine, ffmpeg, cancelled, on_status,
                broker, operation_id, music_manifest_resolver):
        assert broker is admitted and operation_id == 'assistant:' + identity
        assert engine is registry and ffmpeg == binary and on_status is progress
        assert music_manifest_resolver('profiles/' + 'a' * 32 + '.json', 'fluidsynth',
                                       cancelled=cancelled) is model
        return publish(folder)
    def legacy(plan, folder, *, engine, ffmpeg, cancelled, on_status):
        assert engine is registry and ffmpeg == binary
        return publish(folder)
    if default:
        monkeypatch.setattr(bridge, 'render_project', managed)
    producer = LocalProduction(app.root, registry=registry, ffmpeg=binary,
                               **({} if default else {'renderer': legacy}))
    result = producer({'operation_id': identity, '_artifact_budget_bytes': 2 * 1024**2,
                       'text': 'Literal fixture', 'language': 'en'}, output, cancelled, progress)
    assert result['operation_id'] == identity and len(result['artifacts']) == 1
    assert admissions == ([cancelled] if default else [])
    assert resolutions == ([('profiles/' + 'a' * 32 + '.json', 'fluidsynth', cancelled)] if default else [])
