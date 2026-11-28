"""Existing OCR preset schema remains passive and round-trips exact bytes."""
from dataclasses import asdict
from hashlib import sha256
import json
import shutil
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import encode
from carr_library.extract.language_manager import LanguageModel, save_preset
from carr_library.extract.ocr_languages import validate_presets_bytes
from carr_library.extract.page_ocr import Config
from carr_library.library_backup import create_backup, restore_backup
from carr_library.pending_backup import _config


NAME = 'ocr-language-presets.json'


@pytest.mark.parametrize('automatic', [False, True])
def test_actual_preset_backup_delete_and_relocated_restore_are_passive(tmp_path, monkeypatch, automatic):
    root = tmp_path / 'library'
    app = LibraryApp(root)
    workspace = app.create_workspace('Passive OCR settings')
    app.add_text(workspace, 'Original', 'The source stays unchanged.', edition_id='original')
    result = save_preset(root / NAME, 'Reviewed mixed-language pool', ('eng', 'ukr'),
        available=(LanguageModel('eng', 'English', 'language'),
                   LanguageModel('ukr', 'Ukrainian', 'language')),
        automatic_languages=automatic)
    assert result.automatic_languages is automatic
    raw = (root / NAME).read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Passive OCR settings backup must not execute a detector or engine.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr('carr_library.extract.language_detection.Detector', forbidden)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    shutil.rmtree(root)
    restored = restore_backup(folder, tmp_path / 'new-library', digest)
    assert (restored / NAME).read_bytes() == raw
    settings = validate_presets_bytes(raw)['presets']['Reviewed mixed-language pool']
    assert settings['automatic_languages'] is automatic
    assert LibraryApp(restored).edition_metadata(workspace, 'original').title == 'Original'


def test_legacy_seven_field_presets_and_pending_config_are_valid_without_rewriting(tmp_path):
    settings = asdict(Config(languages='eng+ukr'))
    assert 'automatic_languages' not in settings
    raw = encode({'schema': 1, 'presets': {'Legacy': settings}})
    assert validate_presets_bytes(raw)['presets']['Legacy'] == settings
    assert 'automatic_languages' not in settings
    assert _config(settings) is None
    assert _config({**settings, 'automatic_languages': True}) is None
    assert encode(validate_presets_bytes(raw)) == raw


@pytest.mark.parametrize('mutation', ['unknown', 'boolean', 'integer', 'duplicate', 'large-pool'])
def test_invalid_or_ambiguous_preset_bytes_never_pass_typed_backup(mutation):
    settings = asdict(Config())
    value = {'schema': 1, 'presets': {'Test': settings}}
    if mutation == 'unknown': settings['detector_program'] = 'unreviewed'
    elif mutation == 'boolean': settings['automatic_languages'] = 'yes'
    elif mutation == 'integer': settings['dpi'] = True
    elif mutation == 'large-pool':
        settings.update(automatic_languages=True, languages='+'.join('model'+str(i) for i in range(9)))
    raw = encode(value)
    if mutation == 'duplicate': raw = raw.rstrip()[:-1] + b',"schema":1}\n'
    with pytest.raises(ValueError):
        validate_presets_bytes(raw)


def test_invalid_preset_restore_cannot_publish_destination_even_with_updated_manifest(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Preset rejection')
    path = app.root / NAME
    path.write_bytes(encode({'schema': 1, 'presets': {'Safe': asdict(Config())}}))
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    value = json.loads((folder / NAME).read_bytes())
    value['presets']['Safe']['automatic_languages'] = 1
    raw = encode(value)
    (folder / NAME).write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    entry = next(row for row in manifest['sidecars'] if row['filename'] == NAME)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = encode(manifest)
    (folder / 'manifest.json').write_bytes(manifest_raw)
    destination = tmp_path / 'not-published'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, sha256(manifest_raw).hexdigest())
    assert not destination.exists()
