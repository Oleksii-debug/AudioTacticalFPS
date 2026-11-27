"""Real backup/restore keeps dormant voice preferences without engine admission."""
from hashlib import sha256
import json
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.runtime_settings import load_voice_preferences, save_voice_preferences


NAME = 'state/audio/engines.json'


def test_voice_preferences_backup_restore_preserves_exact_bytes_without_discovery(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Voice settings')
    app.add_text(workspace, 'Original', 'Незмінний оригінал.', edition_id='original')
    preferences = {'favorites': ['missing:uk'], 'defaults': {'uk': 'missing:uk'},
                   'profiles': {'Тихий голос': {'voice_id': 'missing:uk', 'language': 'uk',
                                                'rate': -1, 'volume': 60}}}
    save_voice_preferences(app.root / 'state', preferences,
                           expected=load_voice_preferences(app.root / 'state'))
    before = (app.root / NAME).read_bytes()
    monkeypatch.setattr(subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Backup launched a voice engine.'))
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    assert (folder / NAME).read_bytes() == before
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert (target / NAME).read_bytes() == before == (app.root / NAME).read_bytes()
    assert load_voice_preferences(target / 'state') == preferences
    assert LibraryApp(target).edition_metadata(workspace, 'original').title == 'Original'


def test_resigned_invalid_profile_rejects_restore_without_touching_backup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Profile validation')
    initial = load_voice_preferences(app.root / 'state')
    save_voice_preferences(app.root / 'state', initial, expected=initial)
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    value = json.loads((folder / NAME).read_bytes())
    value['voice_preferences']['defaults'] = {'en-US': 'one', 'EN-us': 'another'}
    raw = (json.dumps(value) + '\n').encode()
    (folder / NAME).write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    entry = next(item for item in manifest['sidecars'] if item['filename'] == NAME)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(manifest_raw)
    with pytest.raises(ValueError, match='only one default'):
        restore_backup(folder, tmp_path / 'must-not-exist', sha256(manifest_raw).hexdigest())
    assert not (tmp_path / 'must-not-exist').exists()
    assert (folder / NAME).read_bytes() == raw
    assert (folder / 'manifest.json').read_bytes() == manifest_raw
