import hashlib
import json
from pathlib import Path

import pytest

from carr_library.audio.model_manifest import LocalModelManifest, ModelManifestError, digest_file, local_path, read_json, offline_environment


def manifest(tmp_path, **updates):
    asset = tmp_path / 'voice.onnx'
    asset.write_bytes(b'synthetic non-model test fixture')
    value = {'schema_version': 1, 'engine': 'piper', 'model_id': 'synthetic-fixture',
             'revision': 'a' * 40, 'license': 'synthetic-only', 'license_accepted': True,
             'files': {'voice.onnx': hashlib.sha256(asset.read_bytes()).hexdigest()}}
    value.update(updates)
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def test_exact_manifest_hashes_and_mutated_asset(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path))
    assert model.verify()['files'] == 1
    assert model.cache_identity()['revision'] == 'a' * 40
    (tmp_path / 'voice.onnx').write_bytes(b'modified')
    with pytest.raises(ModelManifestError, match='Checksum'):
        model.verify()


def test_moving_revision_and_wrong_engine_fail_closed(tmp_path):
    with pytest.raises(ModelManifestError, match='exact model revision'):
        LocalModelManifest.load(manifest(tmp_path, revision='main'))
    with pytest.raises(ModelManifestError, match='another engine'):
        LocalModelManifest.load(manifest(tmp_path), engine='musicgen')


@pytest.mark.parametrize('name', ['../outside.onnx', '/absolute.onnx', 'C:/model.onnx',
                                 'parent\\file.onnx', 'https://example.test/model', 'a/./b', 'a//b'])
def test_asset_paths_cannot_escape_manifest_root(tmp_path, name):
    with pytest.raises(ModelManifestError):
        LocalModelManifest.load(manifest(tmp_path, files={name: 'a' * 64}))


def test_manifest_duplicate_keys_and_nonfinite_values_rejected(tmp_path):
    path = tmp_path / 'manifest.json'
    path.write_text('{"schema_version":1,"schema_version":2}', encoding='utf-8')
    with pytest.raises(ModelManifestError, match='duplicate'):
        read_json(path)
    path.write_text('{"gain": NaN}', encoding='utf-8')
    with pytest.raises(ModelManifestError, match='Nonfinite'):
        read_json(path)


def test_manifest_changed_between_validation_and_use(tmp_path):
    path = manifest(tmp_path)
    model = LocalModelManifest.load(path)
    path.write_text(path.read_text() + ' ', encoding='utf-8')
    with pytest.raises(ModelManifestError, match='changed'):
        model.verify()


def test_license_and_execution_consent_not_inferred(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path, license_accepted=False))
    with pytest.raises(ModelManifestError, match='accept'):
        model.verify()
    model = LocalModelManifest.load(manifest(tmp_path, engine='musicgen', license='CC-BY-NC-4.0',
                                           personal_noncommercial=True))
    with pytest.raises(ModelManifestError, match='trusted MusicGen checkpoint'):
        model.verify()


def test_musicgen_requires_both_pinned_checkpoints_and_noncommercial_profile(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path, engine='musicgen', license='CC-BY-NC-4.0',
                                           trusted_weights=True, personal_noncommercial=False))
    with pytest.raises(ModelManifestError, match='personal noncommercial'):
        model.verify()
    model = LocalModelManifest.load(manifest(tmp_path, engine='musicgen', license='CC-BY-NC-4.0',
                                           trusted_weights=True, personal_noncommercial=True))
    with pytest.raises(ModelManifestError, match='Both MusicGen checkpoints'):
        model.verify()


def test_linked_asset_is_rejected(tmp_path):
    external = tmp_path / 'external'
    external.write_bytes(b'external')
    linked = tmp_path / 'linked'
    try:
        linked.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip('This filesystem cannot create a test symlink.')
    with pytest.raises(ModelManifestError, match='Linked paths'):
        local_path(linked)


def test_manifest_byte_budget_and_cancellation(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path))
    with pytest.raises(ModelManifestError, match='budget'):
        model.verify(max_asset_bytes=1)
    with pytest.raises(InterruptedError):
        digest_file(model.path, cancelled=lambda: True)


def test_boolean_schema_and_acceptance_strings_are_rejected(tmp_path):
    with pytest.raises(ModelManifestError, match='schema version'):
        LocalModelManifest.load(manifest(tmp_path, schema_version=True))
    with pytest.raises(ModelManifestError, match='booleans'):
        LocalModelManifest.load(manifest(tmp_path, license_accepted='yes'))


def test_complete_runtime_inventory_rejects_unlisted_python_file(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path))
    assert model.verify(require_complete=True)['files'] == 1
    (tmp_path/'unexpected_import.py').write_text('raise RuntimeError()', encoding='utf-8')
    with pytest.raises(ModelManifestError, match='missing from its reviewed manifest'):
        model.verify(require_complete=True)


def test_worker_environment_excludes_host_credentials(monkeypatch):
    for name in ('PROVIDER_API_KEY', 'HF_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'HTTP_PROXY'):
        monkeypatch.setenv(name, 'synthetic-secret')
    environment = offline_environment()
    assert not any(name in environment for name in ('PROVIDER_API_KEY', 'HF_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'HTTP_PROXY'))
    assert environment['HF_HUB_OFFLINE'] == '1'
