"""Managed OCR choices survive save/reopen/backup without profile substitution."""
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.ocr_languages import (
    FILENAME, load_presets, store_preset, validate_presets_bytes,
)
from carr_library.extract.page_ocr import Config
from carr_library.extract.pinned_tessdata import POLICY
from carr_library.library_backup import create_backup, restore_backup


@pytest.mark.parametrize('profile', ['installed', 'fast', 'best'])
def test_ordered_script_and_language_profile_survive_library_backup(tmp_path, profile):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('OCR research')
    app.add_text(workspace, 'Unchanged original', 'Unchanged original source text.')
    choice = Config(languages='script/Latin+fra', model_profile=profile, orientation=True)
    store_preset(app.root, 'Ordered script and French', choice)
    raw = (app.root / FILENAME).read_bytes()
    assert json.loads(raw)['version'] == 2
    row, = load_presets(app.root)
    assert Config(**{key: value for key, value in row.items() if key != 'name'}) == choice
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    assert (folder / FILENAME).read_bytes() == raw
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    restored_row, = load_presets(restored)
    restored_choice = Config(**{key: value for key, value in restored_row.items() if key != 'name'})
    assert restored_choice == choice
    assert restored_choice.model_profile == profile
    assert restored_choice.languages == 'script/Latin+fra'
    assert (restored / FILENAME).read_bytes() == raw
    assert (app.root / FILENAME).read_bytes() == raw


def test_legacy_preset_is_read_unchanged_then_upgraded_with_explicit_installed_profile(tmp_path):
    legacy = {'version': 1, 'presets': [dict(name='Legacy', languages='ukr+eng',
        model_policy='default', orientation=False)]}
    path = tmp_path / FILENAME
    raw = json.dumps(legacy).encode()
    path.write_bytes(raw)
    assert load_presets(tmp_path) == tuple(legacy['presets'])
    assert path.read_bytes() == raw
    store_preset(tmp_path, 'Best script', Config(languages='script/Cyrillic+ukr', model_profile='best'))
    upgraded = json.loads(path.read_bytes())
    assert upgraded['version'] == 2
    assert upgraded['presets'][0] == {**legacy['presets'][0], 'model_profile': 'installed'}
    assert upgraded['presets'][1]['model_profile'] == 'best'
    assert upgraded['presets'][1]['languages'] == 'script/Cyrillic+ukr'


@pytest.mark.parametrize('damage', ['missing-profile', 'unknown-profile', 'best-pinned-fast',
                                  'v1-with-profile', 'extra-path'])
def test_preset_format_never_guesses_or_drops_a_profile(tmp_path, damage):
    store_preset(tmp_path, 'Managed', Config(languages='eng', model_profile='best'))
    data = json.loads((tmp_path / FILENAME).read_bytes())
    item = data['presets'][0]
    if damage == 'missing-profile':
        del item['model_profile']
    elif damage == 'unknown-profile':
        item['model_profile'] = 'automatic'
    elif damage == 'best-pinned-fast':
        item['model_policy'] = POLICY
    elif damage == 'v1-with-profile':
        data['version'] = 1
    else:
        item['model_path'] = '/arbitrary/external/model'
    with pytest.raises(ValueError):
        validate_presets_bytes(json.dumps(data).encode())
