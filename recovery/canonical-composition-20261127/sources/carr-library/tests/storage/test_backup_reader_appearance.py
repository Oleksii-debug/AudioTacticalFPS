"""Actual persisted reader appearance remains passive, bounded backup state."""
from hashlib import sha256
import json
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup, verify_backup


def resign(folder, manifest):
    raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


@pytest.fixture
def appearance(tmp_path):
    from carr_library.reader.appearance import (
        ReaderAppearancePreferences, ReaderFont, save_appearance,
    )
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Presentation preferences only')
    preferences = ReaderAppearancePreferences(
        font=ReaderFont('Synthetic declared font', 14, weight=600, style='italic'),
        foreground='#123456', background='#FEDCBA', zoom_percent=175)
    snapshot = save_appearance(app.root, preferences, expected_token=None)
    return app, preferences, snapshot


@pytest.mark.parametrize('envelope', ['typed', 'canonical-v4'])
def test_reader_appearance_roundtrips_exact_bytes_without_resolving_live_settings(
        appearance, tmp_path, monkeypatch, envelope):
    from carr_library.reader import appearance as model
    app, preferences, snapshot = appearance
    relative = 'state/ui/reader-appearance.json'
    original = (app.root / relative).read_bytes()
    folder = tmp_path / 'backup'

    def forbidden(*args, **kwargs):
        pytest.fail('Passive appearance backup must not inspect live settings or launch an engine.')

    with monkeypatch.context() as guarded:
        guarded.setattr(model, '_path', forbidden)
        guarded.setattr(subprocess, 'Popen', forbidden)
        checksum = create_backup(app, folder)
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        assert manifest['version'] == 5 and manifest['extensions']['version'] == 4
        assert manifest['state'] == [dict(filename='ui/reader-appearance.json',
            size_bytes=len(original), sha256=sha256(original).hexdigest())]
        assert manifest['extensions']['files'] == []
        if envelope == 'canonical-v4':
            manifest.pop('extensions')
            (folder / 'extensions').rmdir()
            manifest['sidecars'] = [{**row, 'filename': 'state/' + row['filename']}
                                    for row in manifest.pop('state')]
            manifest['version'] = 4
            checksum = resign(folder, manifest)
        before = files(folder)
        assert verify_backup(folder, checksum) == manifest
        target = restore_backup(folder, tmp_path / 'restored', checksum)
    recovered = model.load_appearance(target)
    assert recovered.preferences == preferences
    assert recovered.token == snapshot.token
    assert (target / relative).read_bytes() == original
    assert (app.root / relative).read_bytes() == original
    assert files(folder) == before


@pytest.mark.parametrize('change', ['zoom-bool', 'unknown-schema', 'duplicate-key', 'oversized'])
def test_current_appearance_requires_its_bounded_producer_schema_before_backup(
        appearance, tmp_path, change):
    from carr_library.reader.appearance import MAX_APPEARANCE_BYTES
    app, *_ = appearance
    path = app.root / 'state/ui/reader-appearance.json'
    value = json.loads(path.read_bytes())
    if change == 'zoom-bool':
        value['preferences']['zoom_percent'] = True
        raw = json.dumps(value).encode()
    elif change == 'unknown-schema':
        value['schema'] = 2
        raw = json.dumps(value).encode()
    elif change == 'duplicate-key':
        raw = b'{"schema":1,' + json.dumps(value).encode()[1:]
    else:
        raw = b' ' * (MAX_APPEARANCE_BYTES + 1)
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='appearance|sidecar'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert path.read_bytes() == raw


def test_resealed_invalid_appearance_is_rejected_before_restore_publication(appearance, tmp_path):
    app, *_ = appearance
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    entry, = manifest['state']
    path = folder / 'state' / entry['filename']
    value = json.loads(path.read_bytes())
    value['preferences']['zoom_percent'] = 401
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    checksum = resign(folder, manifest)
    before = files(folder)
    with pytest.raises(ValueError, match='appearance|sidecar'):
        verify_backup(folder, checksum)
    with pytest.raises(ValueError, match='appearance|sidecar'):
        restore_backup(folder, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()
    assert files(folder) == before
