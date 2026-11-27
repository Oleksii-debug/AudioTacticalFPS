"""Canonical registry/Studio integration using an explicitly synthetic worker.

Real pipes, PCM publication and FFmpeg are exercised. No private neural model,
pronunciation, physical playback or native accessibility claim is made here.
"""
from hashlib import sha256
import os
from pathlib import Path
import sys
from time import monotonic

import pytest

from carr_library.integrations import dordiai_voices as bridge
from carr_library.speech import SpeechSettings, Voice, VoiceRegistry
from carr_library.speech import dordiai, dordiai_settings, live_adapter
from carr_library.studio.render import ProductionService
from carr_library.ui.dordiai_settings import connect_runtime, disable_runtime
# Load the existing synthetic peer by exact local path in both ordinary and
# isolated importlib collection; installed package imports remain untouched.
import importlib.util
_fixture_spec = importlib.util.spec_from_file_location(
    'resonance_dordiai_protocol_fixture', Path(__file__).with_name('test_dordiai_voices.py'))
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
ACTOR, runtime, requests = (_fixture_module.ACTOR, _fixture_module.runtime, _fixture_module.requests)


@pytest.fixture
def configured(runtime, tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    connect_runtime(root, str(runtime), python_executable=str(Path(sys.executable).resolve()),
                    expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
    return root


_REGISTRIES = []


@pytest.fixture(autouse=True)
def close_created_registries():
    yield
    while _REGISTRIES:
        assert _REGISTRIES.pop().close() is True


def registry(root):
    value = VoiceRegistry([dordiai.DordiaiProvider(root)])
    _REGISTRIES.append(value)
    return value


def selected(value, language='uk'):
    return next(voice for voice in value.voices()
                if voice.language == language and voice.name.startswith('Fixture One'))


def test_disabled_library_ignores_ambient_enable_and_never_probes(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    monkeypatch.setenv('CARR_DORDIAI_ENABLED', '1')
    monkeypatch.setenv('CARR_DORDIAI_RUNTIME', str(tmp_path / 'missing-runtime'))
    monkeypatch.setattr(bridge, 'DordiaiVoicesAdapter', lambda *a, **k: pytest.fail('Disabled runtime probe'))
    assert dordiai.configuration(root)['enabled'] is False
    assert dordiai.configuration()['enabled'] is False


def test_native_connection_checks_worker_without_launching(runtime, tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    # filelock imports asyncio, whose Windows Popen subclasses the real class.
    # Initialize that dependency before forbidding private process launches.
    import filelock
    monkeypatch.setattr(bridge.subprocess, 'Popen', lambda *a, **k: pytest.fail('Connection launched private code'))
    value = connect_runtime(root, str(runtime), python_executable=str(Path(sys.executable).resolve()),
                            expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
    assert dordiai_settings.load_settings(root) == value
    before = (root / dordiai_settings.FILENAME).read_bytes()
    with pytest.raises(bridge.DordiaiProtocolError, match='fingerprint'):
        connect_runtime(root, str(runtime), python_executable=str(Path(sys.executable).resolve()), expected_worker_sha256='0' * 64)
    assert (root / dordiai_settings.FILENAME).read_bytes() == before


def test_disable_missing_runtime_is_pure_and_preserves_declaration(configured, monkeypatch):
    value = dordiai_settings.load_settings(configured)
    value['runtime_directory'] = str(configured / 'missing-runtime')
    dordiai_settings.save_settings(configured, value)
    monkeypatch.setattr(bridge, 'DordiaiVoicesAdapter', lambda *a, **k: pytest.fail('Disable probed private code'))
    assert disable_runtime(configured) == dict(value, enabled=False)


def test_disable_signals_owned_work_even_when_preference_save_fails(configured, monkeypatch):
    import carr_library.ui.dordiai_settings as controls
    def blocked(*args, **kwargs):
        raise PermissionError('Synthetic blocked writer')
    monkeypatch.setattr(controls, 'save_settings', blocked)
    with dordiai._cancellation(configured) as check:
        with pytest.raises(PermissionError):
            controls.disable_runtime(configured)
        with pytest.raises(InterruptedError):
            check()
    assert dordiai_settings.load_settings(configured)['enabled'] is True


def test_disable_remains_effective_between_identity_check_and_render(configured, tmp_path, monkeypatch):
    import carr_library.ui.dordiai_settings as controls
    value = registry(configured)
    voice = selected(value)
    original = value.fingerprint
    def blocked(*args, **kwargs):
        raise PermissionError('Synthetic blocked writer')
    monkeypatch.setattr(controls, 'save_settings', blocked)
    def disabling(*args, **kwargs):
        identity = original(*args, **kwargs)
        with pytest.raises(PermissionError):
            controls.disable_runtime(configured)
        return identity
    monkeypatch.setattr(value, 'fingerprint', disabling)
    with pytest.raises(InterruptedError):
        value.synthesize('Must not begin after Disable.', voice.id, tmp_path / 'disabled.wav')
    assert not (tmp_path / 'disabled.wav').exists()


@pytest.mark.parametrize('change', [
    {'expected_worker_sha256': None}, {'expected_worker_sha256': 'A' * 64},
    {'enabled': 1}, {'version': True}, {'unexpected': 'argument'},
    {'runtime_directory': '../runtime'}, {'runtime_directory': 'https://example.invalid/runtime'},
    {'runtime_directory': '//server/share/runtime'}, {'python_executable': 'python.exe'},
])
def test_invalid_declarations_are_rejected_without_runtime_io(change, monkeypatch):
    value = dordiai_settings.default_settings()
    value.update(change)
    monkeypatch.setattr(Path, 'stat', lambda *a, **k: pytest.fail('Pure validation inspected a path'))
    with pytest.raises(ValueError):
        dordiai_settings.settings_bytes(value)


def test_restored_settings_are_disabled_without_inspecting_declared_paths(configured, monkeypatch):
    raw = (configured / dordiai_settings.FILENAME).read_bytes()
    original = dordiai_settings.validate_settings_bytes(raw)
    monkeypatch.setattr(Path, 'stat', lambda *a, **k: pytest.fail('Restore probed the runtime'))
    assert dordiai_settings.validate_settings_bytes(dordiai_settings.restored_settings_bytes(raw)) == dict(original, enabled=False)
    assert dordiai_settings.validate_settings_bytes(raw)['enabled'] is True


def test_foreign_os_path_cannot_become_a_current_directory_lookup(monkeypatch):
    value = dordiai_settings.default_settings()
    value.update(enabled=True, runtime_directory='/other/runtime' if os.name == 'nt' else r'C:\Other\runtime')
    monkeypatch.setattr(bridge, 'DordiaiVoicesAdapter', lambda *a, **k: pytest.fail('Foreign path reached bridge'))
    with pytest.raises(ValueError, match=r'absolute (?:local )?runtime path'):
        dordiai.validate_connection(value)


def test_inventory_uses_canonical_voice_type_and_actual_installed_language_pairs(configured):
    voices = registry(configured).voices()
    assert len(voices) == len({voice.id for voice in voices}) == 5
    assert all(type(voice) is Voice and voice.engine == 'dordiai-voices' for voice in voices)
    assert {voice.language for voice in voices} == {'uk', 'en', 'sk', 'ja'}
    assert all(voice.capabilities == ('rate', 'volume', 'pitch') for voice in voices)
    assert all('word timings unavailable' in voice.timing for voice in voices)


def test_engine_settings_registry_binds_to_selected_library_root(configured, monkeypatch):
    from carr_library.runtime_settings import voice_registry
    seen = []
    def discover(cls, **kwargs):
        seen.extend(kwargs['extra_providers'])
        return cls(kwargs['extra_providers'], cancelled=kwargs['cancelled'])
    monkeypatch.setattr(VoiceRegistry, 'discover', classmethod(discover))
    value = voice_registry(configured / 'state')
    assert len(value.voices()) == 5
    assert len(seen) == 1 and seen[0].configuration_root == configured


def test_registry_discovery_cancellation_propagates_from_real_private_probe(configured, runtime):
    (runtime / 'windows_runtime.py').write_text(
        'import time\ndef reset_inherited_dll_directory(): time.sleep(15)\n'
        'def configure_worker_import_paths(): return {}\n')
    provider = dordiai.DordiaiProvider(configured)
    started = monotonic()
    with pytest.raises(InterruptedError):
        VoiceRegistry([provider], cancelled=lambda: monotonic() - started > .15)
    assert monotonic() - started < 3


def test_registry_discovery_enforces_one_overall_deadline(configured, runtime):
    (runtime / 'windows_runtime.py').write_text(
        'import time\ndef reset_inherited_dll_directory(): time.sleep(15)\n'
        'def configure_worker_import_paths(): return {}\n')
    provider = dordiai.DordiaiProvider(configured)
    started = monotonic()
    with pytest.raises(TimeoutError, match='overall time budget'):
        VoiceRegistry([provider], timeout_seconds=.15)
    assert monotonic() - started < 3


def test_canonical_registry_publishes_exact_chunk_stream_and_native_controls(configured, runtime, tmp_path):
    value = registry(configured)
    voice = selected(value)
    text = 'Точний синтетичний текст.\r\n' * 220
    result = value.synthesize(text, voice.id, tmp_path / 'speech.wav',
        settings=SpeechSettings(rate=4, volume=70, pitch=51))
    sent = [row for row in requests(runtime) if row['mode'] == 'reference']
    assert ''.join(row['text'] for row in sent) == text
    assert all(row['speed'] == 2 ** .4 and row['language'] == 'uk' for row in sent)
    assert all(row['controls'] == {'pitch': 51, 'timbre': 50, 'volume': 70} for row in sent)
    assert result.chunks == len(sent) >= 2
    assert result.frames == len(sent) * 6 and result.sample_rate == 24000
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
    assert result.text_sha256 == sha256(text.encode()).hexdigest()


def test_same_size_model_change_invalidates_identity_even_with_preserved_mtime(configured, runtime):
    model = runtime / 'models' / 'fixture.bin'
    model.write_bytes(b'model-A')
    value = registry(configured)
    voice = selected(value)
    original = value.fingerprint(voice.id)
    before = model.stat()
    model.write_bytes(b'model-B')
    os.utime(model, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert value.fingerprint(voice.id) != original


def test_fingerprint_cancellation_is_checked_during_large_model_hash(configured, runtime, monkeypatch):
    model = runtime / 'models' / 'fixture.bin'
    model.write_bytes(b'x' * (3 * 1024 * 1024))
    value = registry(configured)
    voice = selected(value)
    opened = [False]
    original = Path.open
    def observed(path, *args, **kwargs):
        if path == model:
            opened[0] = True
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', observed)
    with pytest.raises(InterruptedError):
        value.fingerprint(voice.id, cancelled=lambda: opened[0])


def test_asset_change_during_render_prevents_final_publication(configured, runtime, tmp_path, monkeypatch):
    model = runtime / 'models' / 'fixture.bin'
    model.write_bytes(b'old')
    value = registry(configured)
    voice = selected(value)
    provider = value._owners[voice.id]
    original = provider.render
    def changing(*args, **kwargs):
        original(*args, **kwargs)
        model.write_bytes(b'new')
    monkeypatch.setattr(provider, 'render', changing)
    target = tmp_path / 'changed.wav'
    with pytest.raises(ValueError, match='assets changed'):
        value.synthesize('Synthetic text.', voice.id, target)
    assert not target.exists() and not list(tmp_path.glob('.speech-*'))


def test_changed_plan_fingerprint_fails_before_render(configured, tmp_path, monkeypatch):
    value = registry(configured)
    voice = selected(value)
    monkeypatch.setattr(value._owners[voice.id], 'render', lambda *a, **k: pytest.fail('Stale plan rendered'))
    with pytest.raises(ValueError, match='production plan'):
        value.synthesize('Synthetic text.', voice.id, tmp_path / 'stale.wav', expected_fingerprint='0' * 64)


def test_disable_invalidates_a_previously_discovered_registry(configured, tmp_path):
    value = registry(configured)
    voice = selected(value)
    disable_runtime(configured)
    with pytest.raises(ValueError, match='configuration changed'):
        value.synthesize('Synthetic text.', voice.id, tmp_path / 'disabled.wav')
    assert not (tmp_path / 'disabled.wav').exists()


def test_registry_synthesis_timeout_includes_private_worker_and_cleanup(configured, tmp_path):
    value = registry(configured)
    voice = selected(value)
    target = tmp_path / 'timeout.wav'
    started = monotonic()
    with pytest.raises(TimeoutError):
        value.synthesize('fixture:timeout', voice.id, target, settings=SpeechSettings(timeout_seconds=1))
    assert monotonic() - started < 4
    assert not target.exists() and not list(tmp_path.glob('.speech-*'))


def test_live_pcm_budget_is_enforced_before_publication(configured, tmp_path, monkeypatch):
    value = registry(configured)
    voice = selected(value)
    closed = []
    def oversized(*args, **kwargs):
        try:
            yield bridge.PCMChunk(b'\0' * 6000)
        finally:
            closed.append(True)
    monkeypatch.setattr(bridge.DordiaiVoicesAdapter, 'stream_pcm', oversized)
    with pytest.raises(ValueError, match='audio-output budget'):
        value.synthesize('Synthetic oversized stream.', voice.id, tmp_path / 'large.wav',
                         settings=SpeechSettings(max_output_bytes=4096))
    assert closed == [True]
    assert not (tmp_path / 'large.wav').exists()


def test_disable_is_scoped_to_one_library(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    with dordiai._cancellation(first) as one, dordiai._cancellation(second) as two:
        dordiai.cancel_active(first)
        assert two() is False
        with pytest.raises(InterruptedError):
            one()


@pytest.mark.parametrize('ratio,expected', [(.5, -10), (1, 0), (2, 10), (2 ** .4, 4)])
def test_reader_ratio_maps_explicitly_to_existing_native_controls(ratio, expected):
    assert live_adapter.native_rate(ratio) == expected


def test_reader_adapter_uses_same_registry_and_explicit_root(configured, tmp_path, monkeypatch):
    value = registry(configured)
    voice = selected(value)
    seen = []
    obtained = []
    def obtain(root, cancelled, *, timeout_seconds=60):
        seen.append(root)
        assert cancelled() is False
        current = registry(root)
        obtained.append(current)
        return current
    monkeypatch.setattr(live_adapter, '_registry', obtain)
    result = live_adapter.render_speech(live_adapter.SpeechRequest('A synthetic reading chunk.', voice.id,
        language='uk', rate=2, volume=.6), tmp_path / 'read.wav', configuration_root=configured)
    assert seen == [configured] and result.frames == 6
    assert not obtained[0].voices()
    with pytest.raises(ValueError, match='language differs'):
        live_adapter.render_speech(live_adapter.SpeechRequest('Wrong language.', voice.id, language='ja'),
            tmp_path / 'wrong.wav', configuration_root=configured)
    assert seen == [configured, configured]
    assert not obtained[1].voices() and not (tmp_path / 'wrong.wav').exists()


def test_canonical_production_cache_changes_with_private_model_bytes(configured, runtime, tmp_path):
    model = runtime / 'models' / 'fixture.bin'
    model.write_bytes(b'model-A')
    voices = registry(configured)
    voice = selected(voices)
    service = ProductionService(configured / 'state' / 'studio', voices=voices)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    project = service.create('A synthetic production line.', voice_ids=(voice.id,))
    first = service.run(service.submit(project, outputs, stems=False))
    count = len([row for row in requests(runtime) if row['mode'] == 'reference'])
    second = service.run(service.submit(project, outputs, stems=False))
    assert len([row for row in requests(runtime) if row['mode'] == 'reference']) == count
    before = model.stat()
    model.write_bytes(b'model-B')
    os.utime(model, ns=(before.st_atime_ns, before.st_mtime_ns))
    third = service.run(service.submit(project, outputs, stems=False))
    assert len([row for row in requests(runtime) if row['mode'] == 'reference']) == count + 1
    assert len({item['destination'] for item in (first, second, third)}) == 3
    assert all(item['status'] == 'complete' for item in (first, second, third))
    for state in (first, second, third):
        for artifact in state['artifacts']:
            assert sha256(Path(artifact['path']).read_bytes()).hexdigest() == artifact['sha256']
