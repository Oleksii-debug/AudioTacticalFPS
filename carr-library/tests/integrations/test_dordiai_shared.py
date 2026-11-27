"""Shared production rendering with an explicitly synthetic private-protocol actor.

These checks exercise real subprocess pipes and FFmpeg, not a neural model or
an acoustic-quality claim.
"""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import monotonic

import pytest

from carr_library.integrations import dordiai_voices as bridge
from carr_library.speech import local, dordiai, dordiai_settings
from carr_library.studio import Line, Project, ProjectStore, Role, Scene
from carr_library.studio.director import Director
from carr_library.ui.dordiai_settings import connect_runtime, disable_runtime
from test_dordiai_voices import ACTOR, runtime, requests


@pytest.fixture
def configured(runtime, tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    monkeypatch.setenv('CARR_DORDIAI_ENABLED', '0')
    connect_runtime(root, str(runtime), python_executable=sys.executable,
                    expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
    return root


def selected(configured, language='uk'):
    return next(entry for entry in dordiai.entries(configuration_root=configured)
                if entry.native_id == 'fixture_one' and entry.voice.language == language)


def test_default_off_and_explicit_disabled_library_never_probe(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    monkeypatch.setenv('CARR_DORDIAI_ENABLED', '1')
    monkeypatch.setenv('CARR_DORDIAI_RUNTIME', str(tmp_path / 'not-installed'))
    monkeypatch.setattr(bridge, 'DordiaiVoicesAdapter',
                        lambda *a, **k: pytest.fail('Disabled library tried the private runtime'))
    assert dordiai.entries(configuration_root=root) == ()
    monkeypatch.setenv('CARR_DORDIAI_ENABLED', '0')
    assert dordiai.entries() == ()
    with pytest.raises(local.SpeechUnavailable, match='Connect or disable'):
        local.engine_fingerprint('dordiai-voices:missing', configuration_root=root)


def test_native_connect_saves_reviewed_declaration_without_launching(runtime, tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    monkeypatch.setattr(bridge.subprocess, 'Popen', lambda *a, **k: pytest.fail('Connect launched private code'))
    configured = connect_runtime(root, str(runtime), python_executable=sys.executable,
                                 expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
    assert configured['enabled'] is True
    assert dordiai_settings.load_settings(root) == configured
    assert not (runtime / 'requests.jsonl').exists()
    before = (root / dordiai_settings.FILENAME).read_bytes()
    with pytest.raises(bridge.DordiaiProtocolError, match='fingerprint'):
        connect_runtime(root, str(runtime), python_executable=sys.executable, expected_worker_sha256='0' * 64)
    assert (root / dordiai_settings.FILENAME).read_bytes() == before


def test_native_disable_preserves_unavailable_declaration_and_never_probes(configured, monkeypatch):
    original = dordiai_settings.load_settings(configured)
    original['runtime_directory'] = str(configured / 'removed-runtime')
    dordiai_settings.save_settings(configured, original)
    monkeypatch.setattr(bridge, 'DordiaiVoicesAdapter',
                        lambda *a, **k: pytest.fail('Disable probed missing private code'))
    result = disable_runtime(configured)
    assert result == dict(original, enabled=False)
    assert dordiai.entries(configuration_root=configured) == ()


def test_native_disable_stops_owned_work_even_if_saving_is_blocked(configured, monkeypatch):
    import carr_library.ui.dordiai_settings as controls
    def cannot_save(*args, **kwargs):
        raise PermissionError('Synthetic blocked writer')
    monkeypatch.setattr(controls, 'save_settings', cannot_save)
    with dordiai._cancellation(configured, None) as check:
        with pytest.raises(PermissionError):
            controls.disable_runtime(configured)
        with pytest.raises(local.RenderCancelled):
            check()
    assert dordiai_settings.load_settings(configured)['enabled'] is True


@pytest.mark.parametrize('change', [
    {'expected_worker_sha256': None}, {'expected_worker_sha256': 'A' * 64},
    {'enabled': 1}, {'version': True}, {'unexpected': 'argument'},
    {'runtime_directory': '../runtime'}, {'runtime_directory': 'https://example.invalid/runtime'},
    {'runtime_directory': '//server/share/runtime'}, {'python_executable': 'python.exe'},
], ids=['null-hash', 'uppercase-hash', 'integer-enabled', 'boolean-version', 'unknown-field',
        'relative-runtime', 'url-runtime', 'network-runtime', 'relative-interpreter'])
def test_settings_invalid_declaration_fails_before_runtime_io(change, monkeypatch):
    value = dordiai_settings.default_settings()
    value.update(change)
    monkeypatch.setattr(Path, 'stat', lambda *a, **k: pytest.fail('Pure validation performed filesystem IO'))
    with pytest.raises(ValueError):
        dordiai_settings.settings_bytes(value)


def test_restored_declaration_is_disabled_without_runtime_access(configured, monkeypatch):
    raw = (configured / dordiai_settings.FILENAME).read_bytes()
    original = dordiai_settings.validate_settings_bytes(raw)
    monkeypatch.setattr(Path, 'stat', lambda *a, **k: pytest.fail('Restore inspected private path'))
    restored = dordiai_settings.validate_settings_bytes(dordiai_settings.restored_settings_bytes(raw))
    assert restored == dict(original, enabled=False)
    assert dordiai_settings.validate_settings_bytes(raw)['enabled'] is True


def test_foreign_os_declaration_cannot_resolve_against_current_directory(monkeypatch):
    value = dordiai_settings.default_settings()
    value.update(enabled=True, runtime_directory='/other-host/runtime' if os.name == 'nt' else r'C:\OtherHost\runtime')
    assert dordiai_settings.validate_settings(value) == value
    monkeypatch.setattr(bridge, 'DordiaiVoicesAdapter',
                        lambda *a, **k: pytest.fail('Foreign path reached runtime discovery'))
    with pytest.raises(local.SpeechUnavailable, match='absolute runtime path'):
        dordiai.validate_connection(value)


def test_shared_voice_inventory_uses_real_probe_and_per_profile_language_ids(configured, monkeypatch):
    monkeypatch.setattr(local, '_powershell_candidates', lambda: ())
    monkeypatch.setattr(local, '_espeak_runtime', lambda: None)
    voices = local.discover_voices(configuration_root=configured)
    assert len(voices) == 5 and len({voice.id for voice in voices}) == 5
    assert {voice.language for voice in voices} == {'uk', 'en', 'sk', 'ja'}
    assert all(voice.engine == 'dordiai-voices' and 'neutral-style' in voice.capabilities for voice in voices)
    assert not any(voice.name.startswith('Excluded') or voice.language == 'ru' for voice in voices)


def test_cancellable_discovery_stops_private_probe(configured, runtime):
    (runtime / 'windows_runtime.py').write_text(
        'import time\ndef reset_inherited_dll_directory(): time.sleep(15)\n'
        'def configure_worker_import_paths(): return {}\n')
    started = monotonic()
    with pytest.raises(local.RenderCancelled):
        dordiai.entries(configuration_root=configured, cancelled=lambda: monotonic() - started > .15)
    assert monotonic() - started < 4
    assert not (runtime / 'requests.jsonl').exists()


def test_full_model_and_helper_bytes_invalidate_identity_with_preserved_mtime(configured, runtime):
    entry = selected(configured)
    asset = runtime / 'models/Holos/weights.bin'
    asset.write_bytes(b'one')
    original_time = asset.stat()
    first = local.engine_fingerprint(entry.voice.id, configuration_root=configured)
    asset.write_bytes(b'two')
    os.utime(asset, ns=(original_time.st_atime_ns, original_time.st_mtime_ns))
    second = local.engine_fingerprint(entry.voice.id, configuration_root=configured)
    assert second != first
    helper = runtime / 'wide_languages.py'
    helper.write_bytes(helper.read_bytes() + b'\n# protocol dependency revision\n')
    third = local.engine_fingerprint(entry.voice.id, configuration_root=configured)
    assert third != second


def test_all_host_adapter_dependency_bytes_participate_in_identity(configured, tmp_path, monkeypatch):
    entry = selected(configured)
    shadow = tmp_path / 'settings-source.py'
    shadow.write_bytes(Path(dordiai_settings.__file__).read_bytes())
    monkeypatch.setattr(dordiai_settings, '__file__', str(shadow))
    first = dordiai.fingerprint(entry)
    shadow.write_bytes(shadow.read_bytes() + b'\n# source identity revision\n')
    assert dordiai.fingerprint(entry) != first


def test_asset_hashing_is_cancellable_and_inventory_is_bounded(configured, runtime, monkeypatch):
    entry = selected(configured)
    (runtime / 'models/Holos/weights.bin').write_bytes(b'0' * (3 * 1024 * 1024))
    with pytest.raises(local.RenderCancelled):
        dordiai.fingerprint(entry, cancelled=lambda: True)
    monkeypatch.setattr(dordiai, '_MAX_ASSETS', 2)
    with pytest.raises(local.SpeechUnavailable, match='inventory budget'):
        dordiai.fingerprint(entry)


def test_shared_renderer_preserves_literal_text_controls_and_checked_pcm(configured, runtime, tmp_path):
    entry = selected(configured, 'sk')
    text = 'Doslovný text.\r\nУкраїнський рядок. <speak>plain</speak> $(no-command)'
    result = local.render_speech(local.SpeechRequest(text, entry.voice.id, language='sk',
                                rate=1.2, pitch=.4, volume=.8),
                                tmp_path / 'speech.wav', configuration_root=configured)
    assert (result.engine, result.sample_rate, result.channels, result.frames) == ('dordiai-voices', 24000, 1, 6)
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
    assert result.engine_fingerprint == local.engine_fingerprint(entry.voice.id, configuration_root=configured)
    message = next(row for row in requests(runtime) if row['mode'] == 'reference')
    assert message['text'] == text and message['language'] == 'sk' and message['nativeLocale'] is True
    assert message['speed'] == 1.2 and message['controls'] == {'pitch': 70, 'timbre': 50, 'volume': 80}
    assert not list(tmp_path.glob('.speech-*'))
    before = result.path.read_bytes()
    with pytest.raises(FileExistsError):
        local.render_speech(local.SpeechRequest('replacement', entry.voice.id), result.path, configuration_root=configured)
    assert result.path.read_bytes() == before


def test_shared_long_chunk_splits_exact_text_at_public_worker_budget(configured, runtime, tmp_path):
    entry = selected(configured)
    text = ('Український текст.\r\n' * 1000) + 'Фініш.'
    result = local.render_speech(local.SpeechRequest(text, entry.voice.id),
                                tmp_path / 'long.wav', configuration_root=configured)
    messages = [row for row in requests(runtime) if row['mode'] == 'reference']
    assert len(messages) > 1 and all(len(row['text']) <= bridge.MAX_TEXT_CHARACTERS for row in messages)
    assert ''.join(row['text'] for row in messages) == text
    assert result.frames == 6 * len(messages)


@pytest.mark.parametrize('text', ['fixture:empty', 'fixture:malformed', 'fixture:odd', 'fixture:error'],
                         ids=['empty', 'malformed-base64', 'odd-pcm', 'worker-error'])
def test_shared_failure_leaves_no_published_or_partial_audio(configured, tmp_path, text):
    entry = selected(configured)
    target = tmp_path / 'failure.wav'
    with pytest.raises(local.SpeechError) as error:
        local.render_speech(local.SpeechRequest(text, entry.voice.id), target, configuration_root=configured)
    assert 'PRIVATE-TRANSCRIPT' not in str(error.value)
    assert not target.exists() and not list(tmp_path.glob('.speech-*'))


def test_shared_live_byte_allowance_stops_before_publication(configured, tmp_path):
    entry = selected(configured)
    target = tmp_path / 'budget.wav'
    with pytest.raises(local.SpeechBudgetExceeded):
        local.render_speech(local.SpeechRequest('budget', entry.voice.id), target,
                            configuration_root=configured, max_output_bytes=50)
    assert not target.exists() and not list(tmp_path.glob('.speech-*'))


def test_shared_timeout_includes_discovery_and_asset_identity(configured, tmp_path):
    entry = selected(configured)
    target = tmp_path / 'timeout.wav'
    started = monotonic()
    with pytest.raises(local.SpeechError, match='execution time budget'):
        local.render_speech(local.SpeechRequest('fixture:timeout', entry.voice.id), target,
                            configuration_root=configured, timeout_seconds=.3)
    assert monotonic() - started < 5 and not target.exists()
    assert not list(tmp_path.glob('.speech-*'))


def test_native_disable_cancels_owned_active_worker_without_audio_publication(configured, runtime, tmp_path):
    entry = selected(configured)
    target = tmp_path / 'cancel.wav'
    disabled = []
    def disable_after_request():
        log = runtime / 'requests.jsonl'
        if not disabled and log.exists() and 'fixture:delay' in log.read_text():
            disable_runtime(configured)
            disabled.append(True)
        return False
    with pytest.raises(local.RenderCancelled):
        local.render_speech(local.SpeechRequest('fixture:delay', entry.voice.id), target,
                            configuration_root=configured, cancelled=disable_after_request)
    assert disabled == [True] and dordiai_settings.load_settings(configured)['enabled'] is False
    assert not target.exists() and not list(tmp_path.glob('.speech-*'))


def test_changed_or_disabled_configuration_cannot_reuse_selected_entry(configured):
    entry = selected(configured)
    disable_runtime(configured)
    with pytest.raises(local.SpeechUnavailable, match='configuration changed'):
        dordiai.fingerprint(entry)


def test_actual_studio_uses_shared_backend_and_model_identity_controls_resume(configured, runtime, tmp_path):
    entry = selected(configured)
    asset = runtime / 'models/Holos/weights.bin'
    asset.write_bytes(b'one')
    project = Project('Explicit synthetic bridge', (Role('n', entry.voice.id, language='uk'),),
                      (Scene('one', 'Chapter', (Line('line', 'n', 'Точний текст.\r\nДругий рядок.', pause_after=.2),)),))
    engine = Director(ProjectStore(tmp_path / 'studio'), configuration_root=configured)
    first = engine.render(project)
    assert first['master']['frames'] > 0
    assert first['captions'][0]['text'] == project.scenes[0].lines[0].text
    previous_manifest = Path(first['manifest_path']).read_bytes()
    count = sum(row['mode'] == 'reference' for row in requests(runtime))
    again = engine.render(project)
    assert again['completed_chunks'] == 0 and again['master']['sha256'] == first['master']['sha256']
    assert sum(row['mode'] == 'reference' for row in requests(runtime)) == count
    asset.write_bytes(b'two')
    changed = engine.render(project)
    assert changed['completed_chunks'] > 0
    assert sum(row['mode'] == 'reference' for row in requests(runtime)) == count + 1
    assert changed['manifest_path'] != first['manifest_path']
    assert Path(first['manifest_path']).read_bytes() == previous_manifest
    assert changed['project_revision'] == first['project_revision'] == project.revision
    from carr_library.studio.backup import backup_project, restore_project
    snapshot = tmp_path / 'snapshot'
    digest = backup_project(engine.store, snapshot)
    restored = restore_project(snapshot, tmp_path / 'restored', expected_manifest_sha256=digest)
    completed = [job['result'] for job in restored.jobs() if job['state'] == 'complete']
    assert len({result['manifest_path'] for result in completed}) == 2
    assert all(Path(result['manifest_path']).is_file() for result in completed)
    resumed = Director(restored, configuration_root=configured).render(restored.load())
    assert resumed['completed_chunks'] == 0
    assert sum(row['mode'] == 'reference' for row in requests(runtime)) == count + 1

