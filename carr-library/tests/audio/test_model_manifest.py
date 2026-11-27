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


@pytest.mark.parametrize('expected_version', [None, 'parsed', 'replacement'])
def test_manifest_replacement_cannot_bind_old_consent_to_new_hash(
        tmp_path, monkeypatch, expected_version):
    path = manifest(tmp_path)
    parsed_bytes = path.read_bytes()
    parsed_hash = hashlib.sha256(parsed_bytes).hexdigest()
    replacement = json.loads(parsed_bytes)
    replacement.update(model_id='different-installation', license_accepted=False)
    replacement_bytes = json.dumps(replacement).encode('utf-8')
    replacement_hash = hashlib.sha256(replacement_bytes).hexdigest()
    real_loads = json.loads
    reads = []

    def replace_after_real_parse(data, **kwargs):
        parsed = real_loads(data, **kwargs)
        # Deterministic filesystem race: the genuine JSON read has finished,
        # and the on-disk installation changes before load binds its identity.
        reads.append(data)
        next_path = tmp_path / 'replacement.json'
        next_path.write_bytes(replacement_bytes)
        next_path.replace(path)
        return parsed

    monkeypatch.setattr(json, 'loads', replace_after_real_parse)
    expected_hash = {'parsed': parsed_hash, 'replacement': replacement_hash}.get(expected_version)
    if expected_version == 'replacement':
        with pytest.raises(ModelManifestError, match='manifest has changed'):
            LocalModelManifest.load(path, expected_sha256=expected_hash)
    else:
        model = LocalModelManifest.load(path, expected_sha256=expected_hash)
        assert model.model_id == 'synthetic-fixture'
        assert model.license_accepted is True
        assert model.manifest_sha256 == parsed_hash
        with pytest.raises(ModelManifestError, match='changed after it was loaded'):
            model.verify()
    assert len(reads) == 1
    assert path.read_bytes() == replacement_bytes


def test_manifest_identity_binds_exact_bom_and_whitespace_bytes(tmp_path):
    path = manifest(tmp_path)
    value = json.loads(path.read_bytes())
    value['model_id'] = 'Локальний тестовий голос'
    raw = b'\xef\xbb\xbf' + json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8') + b'\n\n'
    path.write_bytes(raw)
    expected = hashlib.sha256(raw).hexdigest()
    model = LocalModelManifest.load(path, expected_sha256=expected)
    assert model.model_id == value['model_id']
    assert model.manifest_sha256 == expected
    assert model.verify()['manifest_sha256'] == expected


def test_complete_torch_sized_runtime_inventory_and_mutation(tmp_path, record_property):
    """Real regular files and hashes, not weights or a model-inference claim."""
    import time

    started = time.perf_counter()
    empty_digest = hashlib.sha256(b'').hexdigest()
    names = ['state_dict.bin', 'compression_state_dict.bin']
    prefix = 'runtime/lib/python3.11/site-packages/synthetic_dependency_fixture/modules'
    names.extend(f'{prefix}/module_{number:05}.py' for number in range(10701))
    for name in names:
        asset = tmp_path / name
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.touch()
    raw = {'schema_version': 1, 'engine': 'musicgen',
           'model_id': 'synthetic-inventory-only-not-a-model',
           'revision': 'a' * 40, 'license': 'CC-BY-NC-4.0',
           'license_accepted': True, 'trusted_weights': True,
           'personal_noncommercial': True,
           'files': {name: empty_digest for name in names}}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(raw, separators=(',', ':')), encoding='utf-8')
    assert path.stat().st_size > 1024 * 1024
    model = LocalModelManifest.load(path, engine='musicgen')
    before_verify = time.perf_counter()
    report = model.verify(require_complete=True)
    verify_seconds = time.perf_counter() - before_verify
    assert report['files'] == 10703 and report['bytes'] == 0
    assert model.asset(names[-1]) == tmp_path / names[-1]
    with pytest.raises(ModelManifestError, match='relative paths'):
        model.asset('../outside.py')
    with pytest.raises(ModelManifestError, match='not in the verified manifest'):
        model.asset('runtime/unlisted.py')
    # The first sorted checkpoint is checked again rather than trusted from
    # the preceding inventory pass; no model loader executes these fixtures.
    (tmp_path / 'compression_state_dict.bin').write_bytes(b'mutated fixture')
    with pytest.raises(ModelManifestError, match='Checksum mismatch'):
        model.verify()
    record_property('synthetic_inventory_files', len(names))
    record_property('serialized_manifest_bytes', path.stat().st_size)
    record_property('verification_seconds', round(verify_seconds, 6))
    record_property('total_fixture_seconds', round(time.perf_counter() - started, 6))
    record_property('model_inference', 'NOT_RUN')


def test_model_inventory_file_count_remains_bounded(tmp_path):
    from carr_library.audio.model_manifest import MAX_MODEL_FILES
    files = {f'file_{number}.bin': 'a' * 64 for number in range(MAX_MODEL_FILES + 1)}
    with pytest.raises(ModelManifestError, match='List every required local asset'):
        LocalModelManifest.load(manifest(tmp_path, files=files))


def test_model_manifest_bytes_remain_bounded_before_json_parse(tmp_path):
    from carr_library.audio.model_manifest import MAX_MODEL_MANIFEST_BYTES
    path = tmp_path / 'oversized.json'
    with path.open('wb') as stream:
        stream.seek(MAX_MODEL_MANIFEST_BYTES)
        stream.write(b' ')
    with pytest.raises(ModelManifestError, match='bounded regular file'):
        LocalModelManifest.load(path)


def test_complete_inventory_directory_budget_remains_enforced(tmp_path, monkeypatch):
    import carr_library.audio.model_manifest as module
    model = LocalModelManifest.load(manifest(tmp_path))
    for index in range(5):
        (tmp_path / f'empty_{index}').mkdir()
    monkeypatch.setattr(module, 'MAX_MODEL_DIRECTORY_ENTRIES', 3)
    with pytest.raises(ModelManifestError, match='directory entry budget'):
        model.verify(require_complete=True)


def test_complete_inventory_can_be_cancelled_before_hashing_assets(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path))
    for index in range(5):
        (tmp_path / f'empty_{index}').mkdir()
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls == 4
    with pytest.raises(InterruptedError, match='verification cancelled'):
        model.verify(require_complete=True, cancelled=cancelled)
    assert calls == 4
