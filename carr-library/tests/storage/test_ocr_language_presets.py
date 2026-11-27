"""Passive versions 1/2/3 preferences and exact pending-request compatibility."""
from dataclasses import asdict
from hashlib import sha256
import json
import shutil
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import encode
from carr_library.extract.ocr_languages import store_preset, validate_presets_bytes, FILENAME
from carr_library.extract.page_ocr import Config, config_dict
from carr_library.library_backup import create_backup, restore_backup
from carr_library.pending_backup import _config


@pytest.mark.parametrize('automatic', [False, True])
def test_actual_preset_backup_delete_and_relocated_restore_are_passive(tmp_path, monkeypatch, automatic):
    root = tmp_path / 'library'
    app = LibraryApp(root)
    workspace = app.create_workspace('Passive OCR settings')
    app.add_text(workspace, 'Original', 'The source stays unchanged.', edition_id='original')
    store_preset(root, 'Reviewed mixed-language pool',
                 Config(languages='eng+ukr', automatic_languages=automatic))
    raw = (root / FILENAME).read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Passive OCR settings backup must not execute a detector or engine.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr('carr_library.extract.language_detection.Detector', forbidden)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    shutil.rmtree(root)
    restored = restore_backup(folder, tmp_path / 'new-library', digest)
    assert (restored / FILENAME).read_bytes() == raw
    document = validate_presets_bytes(raw)
    settings, = document['presets']
    assert document['version'] == (3 if automatic else 1)
    assert settings.get('automatic_languages', False) is automatic
    assert settings['languages'] == 'eng+ukr'
    assert LibraryApp(restored).edition_metadata(workspace, 'original').title == 'Original'


@pytest.mark.parametrize('version,extra', [(1, {}), (2, {'model_profile': 'fast'})])
def test_legacy_presets_remain_exact_passive_bytes(version, extra):
    row = {'name': 'Legacy', 'languages': 'ukr+eng', 'orientation': False,
           'model_policy': 'default', **extra}
    raw = encode({'version': version, 'presets': [row]})
    assert encode(validate_presets_bytes(raw)) == raw
    assert 'automatic_languages' not in validate_presets_bytes(raw)['presets'][0]


def test_legacy_seven_field_pending_configuration_is_still_the_manual_serialization():
    legacy = asdict(Config(languages='eng+ukr'))
    legacy.pop('automatic_languages')
    assert len(legacy) == 7
    assert config_dict(Config(languages='eng+ukr')) == legacy
    assert _config(legacy) is None
    assert _config({**legacy, 'automatic_languages': False}) is None
    assert _config({**legacy, 'automatic_languages': True}) is None
    assert config_dict(Config(languages='eng+ukr', automatic_languages=True)) == {
        **legacy, 'automatic_languages': True}


def test_old_pending_request_can_be_prepared_again_without_rewrite_or_model_access(tmp_path, monkeypatch):
    from carr_library.ingest.worker import prepare_request
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Retained import')
    source = tmp_path / 'not-opened.pdf'
    metadata = {'title': 'Retained source', 'work_id': 'b' * 32, 'edition_id': 'c' * 32}
    def forbidden(*args, **kwargs):
        pytest.fail('Request preparation must remain passive.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    first = prepare_request(app.root, workspace, source, metadata,
                            Config(languages='ukr+eng'), request_id='a' * 32)
    path = app.root / 'import-requests' / ('a' * 32 + '.json')
    raw = path.read_bytes()
    assert 'automatic_languages' not in first['config']
    repeated = prepare_request(app.root, workspace, source, metadata,
                               Config(**first['config']), request_id='a' * 32)
    assert repeated == first and path.read_bytes() == raw and not source.exists()
    with pytest.raises(ValueError, match='planned identity'):
        prepare_request(app.root, workspace, source, metadata,
                        Config(languages='ukr+eng', automatic_languages=True),
                        request_id='a' * 32)
    assert path.read_bytes() == raw


def test_adding_automatic_preset_preserves_existing_manual_intent(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    store_preset(app.root, 'Manual', Config(languages='ukr+eng', model_profile='best'))
    first = validate_presets_bytes((app.root / FILENAME).read_bytes())
    assert first['version'] == 2
    store_preset(app.root, 'Automatic', Config(languages='eng+ukr', automatic_languages=True))
    value = validate_presets_bytes((app.root / FILENAME).read_bytes())
    assert value['version'] == 3
    manual, automatic = value['presets']
    assert manual == {**first['presets'][0], 'automatic_languages': False}
    assert automatic['automatic_languages'] is True
    assert automatic['languages'] == 'eng+ukr' and automatic['model_profile'] == 'installed'


@pytest.mark.parametrize('mutation', ['unknown', 'boolean', 'orientation', 'duplicate', 'large-pool'],
                         ids=['unknown-field', 'non-boolean-auto', 'non-boolean-orientation',
                              'duplicate-key', 'automatic-pool-budget'])
def test_invalid_or_ambiguous_preset_bytes_never_pass_typed_backup(mutation):
    settings = {'name': 'Test', 'languages': 'eng', 'model_policy': 'default',
                'orientation': False, 'model_profile': 'installed', 'automatic_languages': True}
    value = {'version': 3, 'presets': [settings]}
    if mutation == 'unknown':
        settings['detector_program'] = 'unreviewed'
    elif mutation == 'boolean':
        settings['automatic_languages'] = 'yes'
    elif mutation == 'orientation':
        settings['orientation'] = 1
    elif mutation == 'large-pool':
        settings['languages'] = '+'.join('model' + str(i) for i in range(9))
    raw = encode(value)
    if mutation == 'duplicate':
        raw = raw.rstrip()[:-1] + b',"version":3}\n'
    with pytest.raises(ValueError):
        validate_presets_bytes(raw)


@pytest.mark.parametrize('automatic', [1, None, 'yes', [], {}],
                         ids=['integer', 'null', 'string', 'array', 'object'])
def test_invalid_pending_automatic_value_does_not_restore(automatic):
    with pytest.raises(ValueError):
        _config({**config_dict(Config()), 'automatic_languages': automatic})


def test_unknown_pending_configuration_fields_are_not_implicitly_admitted():
    with pytest.raises(ValueError, match='configuration'):
        _config({**config_dict(Config()), 'detector_program': 'unreviewed'})


def test_invalid_preset_restore_cannot_publish_destination_even_with_updated_manifest(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Preset rejection')
    store_preset(app.root, 'Safe', Config(automatic_languages=True))
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    path = folder / 'sidecars' / FILENAME
    value = json.loads(path.read_bytes())
    value['presets'][0]['automatic_languages'] = 1
    raw = encode(value)
    path.write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == 5
    entry = next(row for row in manifest['sidecars']['files'] if row['path'] == FILENAME)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = encode(manifest)
    (folder / 'manifest.json').write_bytes(manifest_raw)
    destination = tmp_path / 'not-published'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, sha256(manifest_raw).hexdigest())
    assert not destination.exists()
