"""Offline OCR installation must consume the exact approved manifest snapshot."""
from hashlib import sha256
import json
import os

import pytest

from carr_library.extract import language_manager


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':')) + '\n').encode('utf-8')


def _pack(tmp_path, models=None):
    models = models or {'eng': b'synthetic English model'}
    source = tmp_path / 'Моделі для перевірки'
    source.mkdir()
    for code, data in models.items():
        path = source / (code + '.traineddata')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {'schema': 1, 'upstream': 'synthetic-reviewed-revision',
                'license': 'Synthetic test fixture; not an actual OCR model.',
                'models': {code: {'size': len(data), 'sha256': sha256(data).hexdigest()}
                           for code, data in models.items()}}
    raw = _encode(manifest)
    (source / 'models.json').write_bytes(raw)
    return source, manifest, raw


def test_manifest_replacement_cannot_install_an_unapproved_model(tmp_path, monkeypatch):
    source, approved, raw = _pack(tmp_path)
    other = b'another synthetic model, absent from the approved manifest'
    (source / 'fra.traineddata').write_bytes(other)
    replacement = {'schema': 1, 'upstream': 'unapproved-replacement',
                   'license': approved['license'],
                   'models': {'fra': {'size': len(other), 'sha256': sha256(other).hexdigest()}}}
    expected = sha256(raw).hexdigest()
    replaced = False

    def hash_then_replace(data=b''):
        nonlocal replaced
        digest = sha256(data)
        if data == raw and not replaced:
            replaced = True
            pending = source / 'replacement.json'
            pending.write_bytes(_encode(replacement))
            os.replace(pending, source / 'models.json')
        return digest

    # Replace a real filesystem entry after its bytes were read for the trusted
    # hash. The selected models must still come from that approved snapshot.
    monkeypatch.setattr(language_manager, 'sha256', hash_then_replace)
    destination = tmp_path / 'installed'
    result = language_manager.install_offline_pack(
        source, destination, expected_manifest_sha256=expected)

    assert replaced
    assert json.loads((source / 'models.json').read_text('utf-8')) == replacement
    assert json.loads((destination / 'models.json').read_text('utf-8')) == approved
    assert (destination / 'eng.traineddata').read_bytes() == b'synthetic English model'
    assert not (destination / 'fra.traineddata').exists()
    assert result['manifest_sha256'] == expected


def test_valid_pack_preserves_language_and_script_bytes(tmp_path):
    models = {'eng': b'English fixture', 'script/Latin': b'Latin script fixture'}
    source, manifest, raw = _pack(tmp_path, models)
    destination = tmp_path / 'Встановлені моделі'
    result = language_manager.install_offline_pack(
        source, destination, expected_manifest_sha256=sha256(raw).hexdigest())

    assert json.loads((destination / 'models.json').read_text('utf-8')) == manifest
    for code, data in models.items():
        assert (destination / (code + '.traineddata')).read_bytes() == data
        assert (source / (code + '.traineddata')).read_bytes() == data
    assert (source / 'models.json').read_bytes() == raw
    assert result['models'] == 2
    assert result['size_bytes'] == sum(map(len, models.values()))


def test_unapproved_manifest_checksum_leaves_destination_absent(tmp_path):
    source, _, _ = _pack(tmp_path)
    destination = tmp_path / 'installed'
    with pytest.raises(ValueError, match='checksum'):
        language_manager.install_offline_pack(
            source, destination, expected_manifest_sha256='0' * 64)
    assert not destination.exists()


@pytest.mark.parametrize('malformation', ['duplicate-key', 'non-finite'])
def test_approved_checksum_does_not_relax_strict_json(tmp_path, malformation):
    source, manifest, raw = _pack(tmp_path)
    if malformation == 'duplicate-key':
        raw = b'{"models":{},' + raw[1:]
    else:
        manifest['license'] = float('nan')
        raw = _encode(manifest)
    (source / 'models.json').write_bytes(raw)
    destination = tmp_path / 'installed'
    with pytest.raises(ValueError):
        language_manager.install_offline_pack(
            source, destination, expected_manifest_sha256=sha256(raw).hexdigest())
    assert not destination.exists()


def test_oversized_manifest_cannot_substitute_the_empty_content_checksum(tmp_path):
    source, manifest, _ = _pack(tmp_path)
    manifest['upstream'] = 'x' * (2 * 1024 * 1024)
    (source / 'models.json').write_bytes(_encode(manifest))
    destination = tmp_path / 'installed'
    with pytest.raises(ValueError):
        language_manager.install_offline_pack(
            source, destination, expected_manifest_sha256=sha256(b'').hexdigest())
    assert not destination.exists()


def test_changed_model_bytes_are_not_published(tmp_path):
    source, _, raw = _pack(tmp_path)
    model = source / 'eng.traineddata'
    model.write_bytes(b'x' * model.stat().st_size)
    destination = tmp_path / 'installed'
    with pytest.raises(ValueError, match='checksum'):
        language_manager.install_offline_pack(
            source, destination, expected_manifest_sha256=sha256(raw).hexdigest())
    assert not destination.exists()
