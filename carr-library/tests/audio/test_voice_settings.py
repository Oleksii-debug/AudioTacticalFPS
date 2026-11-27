"""File-backed profile contracts; synthetic manifests are never executed."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.audio.voice_settings import (
    FILENAME, VoiceProfileStore, SessionVoiceProfiles, decode_settings, encode_settings,
    prepare_profile, snapshot_voice_settings, validate_voice_settings_payload,
)


def installed(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    models = tmp_path / 'local-model'
    models.mkdir()
    binary = tmp_path / 'synthetic-piper'
    binary.write_bytes(b'SYNTHETIC NONEXECUTABLE FIXTURE')
    files = {'voice.onnx': b'NOT A MODEL', 'voice.json': b'{}'}
    for name, raw in files.items():
        (models / name).write_bytes(raw)
    value = {'schema_version': 1, 'engine': 'piper', 'model_id': 'synthetic',
        'revision': 'a' * 40, 'license': 'synthetic-only', 'license_accepted': True,
        'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        'settings': {'model': 'voice.onnx', 'config': 'voice.json', 'language': 'en', 'name': 'Fixture'}}
    manifest = models / 'manifest.json'
    manifest.write_text(json.dumps(value), encoding='utf-8')
    return root, manifest, binary


def profile(tmp_path):
    root, manifest, binary = installed(tmp_path)
    value, facts = prepare_profile('Reviewed fixture', 'piper', manifest, executable=binary)
    assert facts['files'] == 2
    return root, value


class Registry:
    notices = ()
    def voices(self, **kwargs):
        return ()
    def capabilities(self):
        return ()
    def cache_identity(self, voice):
        return {'voice': voice}
    def synthesize(self, text, output, **kwargs):
        return kwargs['cancelled']()


def test_saving_is_inert_and_a_new_session_never_restores_authority(tmp_path):
    root, item = profile(tmp_path)
    store = VoiceProfileStore(root)
    store.save(item)
    calls = []
    session = SessionVoiceProfiles(root, registry_factory=lambda **kw: calls.append(kw) or Registry())
    session.registry().voices()
    assert session.active_ids() == frozenset()
    assert calls == [{'ffmpeg': None}]
    session.enable(item.id, owner_trusted=True)
    session.registry().voices()
    assert calls[-1] == {'ffmpeg': None, 'piper_manifest': item.manifest,
                         'piper_executable': item.executable}
    assert SessionVoiceProfiles(root).active_ids() == frozenset()


@pytest.mark.parametrize('consent', [False, None, 1, 'yes'])
def test_explicit_execution_review_is_not_inferred(tmp_path, consent):
    root, item = profile(tmp_path)
    VoiceProfileStore(root).save(item)
    session = SessionVoiceProfiles(root)
    with pytest.raises(ValueError, match='Review'):
        session.enable(item.id, owner_trusted=consent)
    assert not session.active_ids()


def test_preparation_and_activation_recheck_actual_model_and_binary_bytes(tmp_path):
    root, item = profile(tmp_path)
    store = VoiceProfileStore(root)
    store.save(item)
    session = SessionVoiceProfiles(root, registry_factory=lambda **kw: Registry())
    session.enable(item.id, owner_trusted=True)
    registry = session.registry()
    Path(item.executable).write_bytes(b'changed')
    with pytest.raises(ValueError, match='executable changed'):
        registry.synthesize('text', root / 'never.wav', voice='piper:fixture')
    assert not (root / 'never.wav').exists()
    with pytest.raises(ValueError, match='executable changed'):
        session.enable(item.id, owner_trusted=True)


def test_mutated_manifest_and_asset_cannot_be_reenabled(tmp_path):
    root, item = profile(tmp_path)
    VoiceProfileStore(root).save(item)
    session = SessionVoiceProfiles(root)
    asset = Path(item.manifest).parent / 'voice.onnx'
    asset.write_bytes(b'altered model')
    with pytest.raises(ValueError, match='Checksum'):
        session.enable(item.id, owner_trusted=True)
    path = Path(item.manifest)
    path.write_text(path.read_text('utf-8') + ' ', encoding='utf-8')
    with pytest.raises(ValueError, match='changed'):
        session.enable(item.id, owner_trusted=True)
    assert not session.active_ids()


def test_removing_or_revising_profile_revokes_a_cached_registry(tmp_path):
    root, item = profile(tmp_path)
    store = VoiceProfileStore(root)
    store.save(item)
    session = SessionVoiceProfiles(root, registry_factory=lambda **kw: Registry())
    session.enable(item.id, owner_trusted=True)
    cached = session.registry()
    cached.voices()
    store.save(replace(item, name='Changed label'), expected=item)
    with pytest.raises(ValueError, match='profile changed'):
        cached.voices()


def test_disable_revokes_an_already_created_registry(tmp_path):
    root, item = profile(tmp_path)
    VoiceProfileStore(root).save(item)
    session = SessionVoiceProfiles(root, registry_factory=lambda **kw: Registry())
    session.enable(item.id, owner_trusted=True)
    cached = session.registry()
    session.disable(item.id)
    with pytest.raises(ValueError, match='authorization changed'):
        cached.cache_identity('piper:fixture')


def test_disabling_during_synthesis_reaches_existing_engine_cancellation(tmp_path):
    root, item = profile(tmp_path)
    VoiceProfileStore(root).save(item)
    class DuringSynthesis(Registry):
        def synthesize(self, text, output, **kwargs):
            session.disable(item.id)
            return kwargs['cancelled']()
    session = SessionVoiceProfiles(root, registry_factory=lambda **kw: DuringSynthesis())
    session.enable(item.id, owner_trusted=True)
    assert session.registry().synthesize('text', root / 'never.wav', voice='piper:fixture') is True


def test_stale_save_cannot_overwrite_another_dialog(tmp_path):
    root, item = profile(tmp_path)
    store = VoiceProfileStore(root)
    store.save(item)
    newer = replace(item, name='Another editor')
    store.save(newer, expected=item)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        store.save(replace(item, name='Stale'), expected=item)
    assert store.path.read_bytes() == before


def test_snapshot_disconnects_external_paths_and_restored_rows_need_review(tmp_path):
    root, item = profile(tmp_path)
    VoiceProfileStore(root).save(item)
    before = Path(item.manifest).read_bytes(), Path(item.executable).read_bytes()
    snapshot = snapshot_voice_settings(root)
    raw = snapshot[FILENAME]
    restored, = validate_voice_settings_payload(raw)
    assert restored == item.disconnected()
    assert item.manifest.encode() not in raw and item.executable.encode() not in raw
    target = tmp_path / 'restored'
    target.mkdir()
    (target / FILENAME).write_bytes(raw)
    session = SessionVoiceProfiles(target)
    assert not session.active_ids()
    with pytest.raises(ValueError, match='no local paths'):
        session.enable(restored.id, owner_trusted=True)
    assert before == (Path(item.manifest).read_bytes(), Path(item.executable).read_bytes())
    with pytest.raises(ValueError, match='disconnected'):
        validate_voice_settings_payload(encode_settings([item]))


def test_snapshot_does_not_follow_missing_external_model_paths(tmp_path):
    root, item = profile(tmp_path)
    VoiceProfileStore(root).save(item)
    Path(item.manifest).unlink()
    Path(item.executable).unlink()
    assert validate_voice_settings_payload(snapshot_voice_settings(root)[FILENAME]) == (item.disconnected(),)


def test_linked_settings_file_is_rejected_without_following_it(tmp_path):
    root, item = profile(tmp_path)
    outside = tmp_path / 'outside.json'
    raw = encode_settings([item])
    outside.write_bytes(raw)
    os.link(outside, root / FILENAME)
    with pytest.raises(ValueError, match='links'):
        VoiceProfileStore(root).load()
    assert outside.read_bytes() == raw


def test_cancellation_does_not_create_session_authority(tmp_path):
    root, item = profile(tmp_path)
    VoiceProfileStore(root).save(item)
    session = SessionVoiceProfiles(root)
    with pytest.raises(InterruptedError):
        session.enable(item.id, owner_trusted=True, cancelled=lambda: True)
    assert not session.active_ids()


@pytest.mark.parametrize('path', ['relative.json', '../outside.json', '/tmp/../outside',
                                 '/tmp/./model', r'C:\models\..\other', r'\\server\share\a',
                                 '//server/share/a', 'https://example.test/a', r'C:\model:stream'])
def test_saved_paths_reject_remote_relative_and_ambiguous_forms(tmp_path, path):
    _, item = profile(tmp_path)
    with pytest.raises(ValueError):
        replace(item, manifest=path)


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(version=True),
    lambda value: value.update(unknown=True),
    lambda value: value['profiles'][0].update(enabled=True),
    lambda value: value['profiles'].append(value['profiles'][0]),
    lambda value: value['profiles'][0].update(manifest_sha256='a' * 63),
])
def test_strict_settings_schema_rejects_unannounced_authority_and_ambiguity(tmp_path, mutate):
    _, item = profile(tmp_path)
    value = json.loads(encode_settings([item]))
    mutate(value)
    with pytest.raises(ValueError):
        decode_settings(json.dumps(value).encode())


def test_duplicate_json_keys_are_rejected(tmp_path):
    _, item = profile(tmp_path)
    raw = encode_settings([item]).replace(b'"version":1', b'"version":1,"version":1')
    with pytest.raises(ValueError, match='Duplicate'):
        decode_settings(raw)


def test_relocating_restored_profile_still_requires_explicit_session_review(tmp_path):
    root, item = profile(tmp_path)
    store = VoiceProfileStore(root)
    restored = item.disconnected()
    store.save(restored)
    candidate, _ = prepare_profile(item.name, item.engine, item.manifest,
                                  executable=item.executable, identity=item.id)
    store.save(candidate, expected=restored)
    session = SessionVoiceProfiles(root)
    assert not session.active_ids()
    session.enable(item.id, owner_trusted=True)
    assert session.active_ids() == {item.id}
