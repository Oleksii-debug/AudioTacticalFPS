"""Exact historical settings remain passive beside current typed preferences."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.keyboard_preferences import KeymapStore
from carr_library.library_backup import create_backup, restore_backup
from carr_library.progress_preferences import ProgressPreferenceStore


SHORTCUTS = {'schema': 1, 'overrides': {'options/historical task': 'Ctrl+Shift+J'},
             'originals': {'options/historical task': 'Ctrl+J'}}
PROGRESS = {'schema': 1, 'preferences': {'mode': 'both', 'interval_seconds': 90,
    'volume': .23, 'quiet': True, 'background': True, 'mute_during_audio': False}}


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + '\n', encoding='utf-8')
    return path.read_bytes()


def test_old_menu_paths_and_dialog_preferences_survive_beside_current_settings(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Settings coexistence')
    values = {
        'state/ui/shortcuts.json': SHORTCUTS,
        'state/ui/progress.json': PROGRESS,
        'keyboard-shortcuts.json': {'version': 1, 'bindings': {'future.explicit_id': 'Ctrl+Shift+K'}},
        'progress-preferences.json': {'version': 1, 'preferences': {'mode': 'off',
            'interval_seconds': 5, 'volume': 30, 'muted': False,
            'allow_background': False, 'quiet_during_media': True}},
    }
    originals = {name: _save(app.root / name, value) for name, value in values.items()}
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == 4 and 'state' not in manifest
    rows = {row['filename']: row for row in manifest['sidecars']}
    for name, raw in originals.items():
        assert rows[name] == {'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
    target = restore_backup(folder, tmp_path / 'restored', digest)
    for name, raw in originals.items():
        assert (target / name).read_bytes() == raw
    # Current stores only read their own current paths. Old menu text is never
    # interpreted as a stable command ID, and old sound settings are not enabled.
    assert KeymapStore(target / 'keyboard-shortcuts.json').load() == {'future.explicit_id': 'Ctrl+Shift+K'}
    current = ProgressPreferenceStore(target / 'progress-preferences.json').load()
    assert current.mode == 'off' and current.volume == 30 and not current.allow_background
    assert (app.root / 'state/ui/shortcuts.json').read_bytes() == originals['state/ui/shortcuts.json']


def test_historical_settings_do_not_create_current_authority(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Historical settings only')
    _save(app.root / 'state/ui/shortcuts.json', SHORTCUTS)
    _save(app.root / 'state/ui/progress.json', PROGRESS)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert KeymapStore(target / 'keyboard-shortcuts.json').load() == {}
    assert ProgressPreferenceStore(target / 'progress-preferences.json').load().mode == 'off'
    assert not (target / 'keyboard-shortcuts.json').exists()
    assert not (target / 'progress-preferences.json').exists()


@pytest.mark.parametrize('damage', ['extra', 'missing-original', 'bad-key', 'duplicate-key',
                                   'wrong-mode', 'boolean-volume', 'too-long-interval'])
def test_known_historical_filename_still_requires_exact_bounded_schema(tmp_path, damage):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Invalid old settings')
    shortcuts, progress = deepcopy(SHORTCUTS), deepcopy(PROGRESS)
    if damage == 'extra':
        shortcuts['apply_to_current_commands'] = True
    elif damage == 'missing-original':
        shortcuts['originals'].clear()
    elif damage == 'bad-key':
        shortcuts['overrides']['options/historical task'] = 'Ctrl+Insert'
    elif damage == 'duplicate-key':
        shortcuts['overrides']['options/another'] = 'Ctrl+Shift+J'
        shortcuts['originals']['options/another'] = 'Ctrl+K'
    elif damage == 'wrong-mode':
        progress['preferences']['mode'] = 'beep_speech'
    elif damage == 'boolean-volume':
        progress['preferences']['volume'] = True
    else:
        progress['preferences']['interval_seconds'] = 121
    _save(app.root / 'state/ui/shortcuts.json', shortcuts)
    _save(app.root / 'state/ui/progress.json', progress)
    folder = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, folder)
    assert not folder.exists()
