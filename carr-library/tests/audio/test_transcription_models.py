"""Actual offline manifest publication; fixtures are not acoustic models."""
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from carr_library.audio.model_manifest import LocalModelManifest
from carr_library.audio.transcription_models import MANIFEST_NAME, register_local_model


@pytest.fixture
def model(tmp_path):
    root = tmp_path / 'модель Україна'
    root.mkdir()
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (root / name).write_bytes(('synthetic identity fixture ' + name).encode())
    return root


def register(model, **kwargs):
    return register_local_model(model, model_id='Synthetic fixture', license_name='test-only',
                                license_accepted=True, trusted_weights=True, **kwargs)


def test_registration_pins_existing_bytes_without_copy_or_replacement(model):
    before = {path.name: path.read_bytes() for path in model.iterdir()}
    receipt = register(model)
    assert receipt['network_access'] is False and receipt['weights_copied'] is False
    assert receipt['files'] == 3 and receipt['status'] == 'complete'
    assert {path.name: path.read_bytes() for path in model.iterdir() if path.name != MANIFEST_NAME} == before
    manifest = LocalModelManifest.load(receipt['manifest'], engine='faster-whisper')
    assert manifest.verify(require_complete=True)['files'] == 3
    assert dict(manifest.files) == {name: hashlib.sha256(data).hexdigest() for name, data in before.items()}
    saved = Path(receipt['manifest']).read_bytes()
    with pytest.raises(FileExistsError):
        register(model)
    assert Path(receipt['manifest']).read_bytes() == saved


def test_model_changes_produce_a_distinct_content_revision(model):
    first = register(model)
    (model / MANIFEST_NAME).unlink()
    (model / 'model.bin').write_bytes(b'changed synthetic model bytes')
    second = register(model)
    assert first['model_revision'] != second['model_revision']


def test_registration_requires_explicit_trust_and_licence(model):
    for flags in ({}, {'license_accepted': True}, {'trusted_weights': True}):
        with pytest.raises(ValueError, match='Review'):
            register_local_model(model, model_id='test', license_name='test-only', **flags)
    assert not (model / MANIFEST_NAME).exists()


def test_missing_tokenizer_never_creates_an_active_manifest(model):
    (model / 'tokenizer.json').unlink()
    with pytest.raises(FileNotFoundError):
        register(model)
    assert not (model / MANIFEST_NAME).exists()


def test_cancellation_before_publication_leaves_existing_weights_untouched(model):
    before = {path.name: path.read_bytes() for path in model.iterdir()}
    with pytest.raises(InterruptedError):
        register(model, cancelled=lambda: True)
    assert {path.name: path.read_bytes() for path in model.iterdir()} == before


def test_private_library_model_is_rejected(model, tmp_path):
    with pytest.raises(ValueError, match='private library'):
        register(model, private_root=tmp_path)
    assert not (model / MANIFEST_NAME).exists()


@pytest.mark.parametrize('force_metadata_probe', [False, True])
def test_linked_model_asset_is_rejected(model, tmp_path, force_metadata_probe):
    external = tmp_path / 'other.bin'
    external.write_bytes(b'outside')
    link = model / 'linked.bin'
    try:
        if force_metadata_probe:
            raise OSError('Synthetic missing symlink privilege')
        link.symlink_to(external)
    except OSError:
        # Exercise the same inspection boundary without needing the runner's
        # privilege to create a Windows symlink.
        import stat
        from types import SimpleNamespace

        link.write_bytes(b'synthetic linked entry')
        original = Path.lstat
        def linked_metadata(path, *args, **kwargs):
            if path == link:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', linked_metadata):
            with pytest.raises(ValueError, match='Linked paths'):
                register(model)
    else:
        with pytest.raises(ValueError, match='Linked paths'):
            register(model)
    assert not (model / MANIFEST_NAME).exists()


def test_late_model_mutation_is_not_certified(model):
    from carr_library.audio import transcription_models as module
    original = module.digest_file
    seen = set()
    def changing(path, **kwargs):
        result = original(path, **kwargs)
        path = Path(path)
        if path.name == 'model.bin' and path.name not in seen:
            seen.add(path.name)
            path.write_bytes(b'changed after first hash')
        return result
    with patch.object(module, 'digest_file', side_effect=changing):
        with pytest.raises(ValueError, match='changed'):
            register(model)
    assert not (model / MANIFEST_NAME).exists()
    assert not list(model.glob('.asr-manifest-*'))


def test_cancellation_after_last_verification_prevents_publication(model):
    from carr_library.audio import transcription_models as module
    original = module.digest_file
    calls = [0]
    def hashing(path, **kwargs):
        value = original(path, **kwargs)
        calls[0] += 1
        return value
    with patch.object(module, 'digest_file', side_effect=hashing):
        with pytest.raises(InterruptedError):
            register(model, cancelled=lambda: calls[0] >= 6)
    assert not (model / MANIFEST_NAME).exists()


def test_invalid_manifest_is_rejected_before_its_stable_name_is_written(model):
    from carr_library.audio import transcription_models as module
    with patch.object(module, 'MAX_MODEL_MANIFEST_BYTES', 10):
        with pytest.raises(ValueError, match='byte budget'):
            register(model)
    assert not (model / MANIFEST_NAME).exists()
    with patch.object(module.LocalModelManifest, 'load', side_effect=ValueError('invalid manifest field')):
        with pytest.raises(ValueError, match='invalid manifest'):
            register(model)
    assert not (model / MANIFEST_NAME).exists()
    assert not list(model.glob('.asr-manifest-*'))


def test_registration_reports_unusable_cleanup_state_without_deleting_manifest(model):
    original = Path.unlink
    def blocked(path, *args, **kwargs):
        if path.name.startswith('.asr-manifest-'):
            raise PermissionError('synthetic locked temporary file')
        return original(path, *args, **kwargs)
    with patch.object(Path, 'unlink', blocked):
        receipt = register(model)
    assert receipt['status'] == 'cleanup_required'
    assert Path(receipt['manifest']).is_file()
    assert Path(receipt['cleanup_pending']).is_file()
    Path(receipt['cleanup_pending']).unlink()
    assert LocalModelManifest.load(receipt['manifest']).verify(require_complete=True)['files'] == 3
