"""Real composite and foreign-flat backup acceptance for reading appearance."""
from hashlib import sha256
import json
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import encode_preferences
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.speech.presentation import ReadingAppearance, ReadingAppearanceStore, ReadingFont
from carr_library.state_recovery import validate_state_documents


def _raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _seal(folder, document):
    raw = _raw(document)
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _fixture(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reader appearance')
    app.add_text(workspace, 'Exact source', 'Unchanged Україна source 🧭.')
    (app.root / 'keybindings.json').write_bytes(encode_preferences({}))
    store = ReadingAppearanceStore(app.root)
    value = ReadingAppearance(ReadingFont('Segoe UI', 16), '#FFFFFF', '#101010', 175)
    store.save(value, expected=ReadingAppearance())
    return app, workspace, store, value


def _backup(app, destination, shape):
    checksum = create_backup(app, destination)
    manifest = json.loads((destination / 'manifest.json').read_bytes())
    assert manifest['version'] == 5
    if shape == 'flat':
        rows = [{**row, 'filename': 'state/' + row['filename']} for row in manifest['state']]
        for row in manifest['sidecars']['files']:
            name = row['path']
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(destination / 'sidecars' / name, target)
            rows.append({'filename': name, 'size_bytes': row['size_bytes'], 'sha256': row['sha256']})
        shutil.rmtree(destination / 'sidecars')
        flat = {key: manifest[key] for key in ('kind', 'schema_version', 'schema_fingerprint', 'database', 'assets')}
        flat.update(version=4, sidecars=sorted(rows, key=lambda row: row['filename']), generated_audio_included=False)
        if 'pending' in manifest:
            flat['pending'] = manifest['pending']
        manifest, checksum = flat, _seal(destination, flat)
    return manifest, checksum


@pytest.mark.parametrize('shape', ['composite', 'flat'])
def test_saved_appearance_survives_real_backup_without_resolving_font(tmp_path, monkeypatch, shape):
    app, workspace, store, appearance = _fixture(tmp_path)
    original = store.path.read_bytes()
    # Pure backup validation must not create wx or ask this machine for a font.
    import sys
    monkeypatch.setitem(sys.modules, 'wx', None)
    backup, restored = tmp_path / 'backup', tmp_path / 'restored'
    manifest, checksum = _backup(app, backup, shape)
    restore_backup(backup, restored, checksum)
    assert ReadingAppearanceStore(restored).load() == appearance
    assert (restored / 'state/speech/presentation.json').read_bytes() == original
    assert LibraryApp(restored).search(workspace, 'Unchanged')
    assert not (restored / INCOMPLETE).exists()


@pytest.mark.parametrize('shape', ['composite', 'flat'])
@pytest.mark.parametrize('bad', [{'schema': 'future'}, [], {'zoom': 900}])
def test_resealed_invalid_settings_cannot_be_restored(tmp_path, shape, bad):
    app, _, _, _ = _fixture(tmp_path)
    backup, target = tmp_path / 'backup', tmp_path / 'rejected'
    manifest, _ = _backup(app, backup, shape)
    relative = 'state/speech/presentation.json'
    raw = _raw(bad)
    (backup / relative).write_bytes(raw)
    collection = manifest['state'] if shape == 'composite' else manifest['sidecars']
    selected = relative.removeprefix('state/') if shape == 'composite' else relative
    for item in collection:
        if item['filename'] == selected:
            item.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    checksum = _seal(backup, manifest)
    with pytest.raises((ValueError, RestoreError)):
        restore_backup(backup, target, checksum)
    assert not target.exists()


@pytest.mark.parametrize('name', ['speech/presentation.json', 'Speech/Presentation.JSON'])
@pytest.mark.parametrize('raw', [b'{', b'[]', b' ' * 8193])
def test_typed_recovery_rejects_malformed_or_oversized_case_alias(tmp_path, name, raw):
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        validate_state_documents(tmp_path, [{'filename': name, 'size_bytes': len(raw),
                                             'sha256': sha256(raw).hexdigest()}])
    assert path.read_bytes() == raw


def test_invalid_live_appearance_prevents_incomplete_backup(tmp_path):
    app, _, store, _ = _fixture(tmp_path)
    atomic_json(store.path, {'schema': 'carr.reading-appearance.v1', 'zoom': 200})
    target = tmp_path / 'rejected'
    with pytest.raises((ValueError, RestoreError)):
        create_backup(app, target)
    assert not target.exists()
