"""Current managed backend approval, real pipes and the existing shared registry.

The owner metadata and waveform fixtures are synthetic. Real current manifest
CAS, version/reference identity, OS ownership, subprocess retirement and WAV
publication are exercised. No model inference or acoustic quality is claimed.
"""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library.audio.voice_ownership import capture_voice_ownership, hold_voice_ownership
from carr_library.integrations import dordiai_voices as api
from carr_library.integrations.dordiai_speech import DordiaiSpeechProvider
from carr_library.runtime_settings import load_settings, save_settings
from carr_library.speech.runtime import VoiceRegistry

from .test_dordiai_reference_backends import BACKENDS, lines, runtime_files


def _metadata_fixture():
    path = Path(__file__).resolve().parents[1] / 'audio' / 'voice_management_fixtures.py'
    spec = importlib.util.spec_from_file_location('_managed_reference_metadata_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=BACKENDS)
def managed_backend(request, tmp_path, monkeypatch):
    backend = request.param
    fixture = _metadata_fixture()
    library, manifest_path, manager, owner_calls = fixture.installed.__wrapped__(tmp_path, monkeypatch)
    root = manifest_path.parent
    requests, starts = tmp_path / 'speech-requests.jsonl', tmp_path / 'speech-starts.jsonl'
    additions = runtime_files(backend, requests, starts)
    additions['voice_policy.py'] = (fixture.POLICY +
        '\ndef filter_voice_catalog(values): return {key:value for key,value in values.items() if is_voice_allowed(key)}\n').encode('utf-8')
    # Both metadata protocols retain the existing selected owner API modules.
    # Only its inert interpreter is substituted, exactly as the core fixture.
    document = json.loads(manifest_path.read_text(encoding='utf-8'))
    for name, data in additions.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        document['files'][name] = sha256(data).hexdigest()
    fixture.write_json(manifest_path, document)
    settings = load_settings(library / 'state')
    previous = deepcopy(settings['dordiai'])
    settings['dordiai']['expected_worker_sha256'] = sha256(additions['nvda_worker.py']).hexdigest()
    save_settings(library / 'state', settings, expected_dordiai=previous)
    assert manager.store.read()['restored'] is True
    manager.register_manifest(manifest_path, expected_revision=manager.store.read()['revision'])
    fixture.configure(manager, backend=backend)
    package = tmp_path / 'selected-reference.dordiai-voice'
    selected_document = fixture.package(package, backend=backend)
    manager.apply(manager.inspect(package), rights_approved=True, supported_languages=['uk'])
    actual_process, children, providers = api._JsonLinesProcess, [], []
    def selected_interpreter(command, cwd, **kwargs):
        assert Path(command[0]) == root / 'python/python.exe'
        assert command[1:4] == ['-I', '-B', '-u']
        child = actual_process([sys.executable, *command[1:]], cwd, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(api, '_JsonLinesProcess', selected_interpreter)
    def provider():
        row = manager.store.read()['runtime_configuration']
        value = DordiaiSpeechProvider(root, python_executable=row['python_executable'],
            expected_worker_sha256=row['expected_worker_sha256'],
            configuration=row, state_root=library / 'state')
        providers.append(value)
        return value
    case = SimpleNamespace(backend=backend, root=root, library=library, fixture=fixture,
        manifest=manifest_path, manager=manager, document=selected_document, provider=provider,
        children=children, requests=requests, starts=starts, owner_calls=owner_calls)
    try:
        yield case
    finally:
        try:
            for value in reversed(providers):
                value.close()
        finally:
            for child in children:
                child.close()


def chosen(provider):
    voices = provider.voices()
    assert len(voices) == 1
    value = voices[0]
    assert provider._tokens[value.id] == ('fixture', 'uk')
    return value


def test_current_managed_reference_backend_preserves_version_identity_capabilities_registry_pcm_and_lease(managed_backend, tmp_path):
    from carr_library.audio.voice_packages import _hash
    case = managed_backend
    provider = case.provider()
    voice = chosen(provider)
    approval = provider._token_authority[voice.id]
    assert approval[0] == '1.0.0' and approval[1] == _hash(case.document)
    assert approval[2] == _hash({row['role']: row['sha256'] for row in case.document['files'].values()})
    assert len(approval[3]) == 64
    capabilities = provider.adapter.capabilities()
    assert capabilities.streaming_pcm is (case.backend != 'omnivoice')
    assert capabilities.loading_progress is False
    assert capabilities.managed_authority_sha256 == approval[3]
    assert capabilities.managed_asset_versions == (('fixture', *approval[:3]),)
    assert capabilities.pronunciation_verified is False
    registry = VoiceRegistry([provider])
    result = registry.synthesize('Synthetic current reference.', voice.id, tmp_path / 'reference.wav')
    expected = 3 if case.backend == 'omnivoice' else 6
    assert result.frames == expected and result.sample_rate == 24000
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
    modes = [row['mode'] for row in lines(case.requests)]
    assert modes == (['reference'] if case.backend == 'omnivoice' else ['reference', 'next', 'next'])
    starts = lines(case.starts)
    assert len(starts) == 1 and starts[0]['reference_engine'] == case.backend
    assert starts[0]['reference_model'] == str(case.root / 'models' / (
        'OmniVoice' if case.backend == 'omnivoice' else 'OpenVoice'))
    assert provider.adapter._worker is None
    assert all(child.process.poll() is not None and child._cleanup_done for child in case.children)
    with hold_voice_ownership(capture_voice_ownership(case.root), timeout=0):
        pass


def test_current_managed_reference_reply_still_holds_real_lock_and_rechecks_document_before_completion(managed_backend):
    case = managed_backend
    provider = case.provider()
    chosen(provider)
    stream = provider.adapter.stream_pcm('Current approved reference.', 'fixture', 'uk')
    assert next(stream).frames == 3
    child = provider.adapter._worker
    ticket = capture_voice_ownership(case.root)
    try:
        assert child.process.poll() is None
        with pytest.raises(TimeoutError):
            with hold_voice_ownership(ticket, timeout=0):
                pytest.fail('A live managed stream released its owner lock.')
        document = case.root / 'models/voices/packages/fixture/1.0.0/voice-package.json'
        document.write_bytes(document.read_bytes() + b' ')
        with pytest.raises(ValueError, match='document changed|approval changed|contents|fingerprint'):
            next(stream)
        assert child.process.poll() is not None and child._cleanup_done
        assert provider.adapter._worker is None
        assert all(row['mode'] != 'next' for row in lines(case.requests))
        assert [row['mode'] for row in lines(case.requests)].count('reference') == 1
    finally:
        stream.close()
    with hold_voice_ownership(ticket, timeout=0):
        pass


def test_current_backend_configuration_change_retires_saved_authority_without_retargeting_approved_voice(managed_backend, tmp_path):
    case = managed_backend
    provider = case.provider()
    voice = chosen(provider)
    fingerprint = provider.fingerprint(voice.id)
    registry = VoiceRegistry([provider])
    other = 'openvoice' if case.backend == 'omnivoice' else 'omnivoice'
    case.fixture.configure(case.manager, backend=other)
    before = len(case.children)
    with pytest.raises(ValueError, match='configuration changed'):
        provider.fingerprint(voice.id)
    with pytest.raises(ValueError):
        registry.synthesize('A previously reviewed selection.', voice.id, tmp_path / 'stale.wav')
    assert not (tmp_path / 'stale.wav').exists() and not case.requests.exists()
    assert len(case.children) == before and provider.adapter._worker is None
    assert len(fingerprint) == 64
    fresh = case.provider()
    assert fresh.voices() == ()
    assert all(child.process.poll() is not None and child._cleanup_done for child in case.children)
