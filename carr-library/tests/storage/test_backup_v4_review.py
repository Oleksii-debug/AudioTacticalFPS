"""Independent full-backup publication and source-root rebinding regressions."""
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE

create_legacy_state_backup = runpy.run_path(str(Path(__file__).with_name(
    'legacy_backup_fixture.py')))['create_legacy_state_backup']


def test_full_v4_roundtrip_published_and_pending_assets_and_settings(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Mixed published and pending imports')
    paths = []
    for index in range(3):
        source = tmp_path / f'source-{index}.txt'
        source.write_text(f'Original railway source number {index}.', encoding='utf-8')
        paths.append(source)
    planned = app.plan_import_batch(workspace, paths)
    stop = False
    def progress(value):
        nonlocal stop
        if value['counts']['complete'] == 1:
            stop = True
    partial = app.run_import_batch(workspace, planned['batch_id'], cancelled=lambda: stop, progress=progress)
    assert partial['counts']['complete'] == 1 and partial['counts']['queued'] == 2
    settings = app.root / 'state/ui/progress.json'
    settings.parent.mkdir(parents=True)
    settings.write_text('{"private-setting":"preserved"}\n', encoding='utf-8')
    backup = tmp_path / 'backup'
    with pytest.raises(ValueError, match='backup adapter'):
        create_backup(app, tmp_path / 'new-writer-refuses-unknown')
    assert not (tmp_path / 'new-writer-refuses-unknown').exists()
    saved_hash = create_legacy_state_backup(app, backup, version=4)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert manifest['version'] == 4 and len(manifest['assets']) == 1
    assert len(manifest['pending']['assets']) == 3
    assert saved_hash == sha256((backup / 'manifest.json').read_bytes()).hexdigest()
    for path in paths:
        path.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', saved_hash))
    assert (restored.root / 'state/ui/progress.json').read_bytes() == settings.read_bytes()
    assert len(tuple(restored.assets_dir.iterdir())) == 3
    assert all(path.stat().st_nlink == 1 for path in restored.assets_dir.iterdir())
    final = restored.run_import_batch(workspace, planned['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 3
    for before, after in zip(planned['rows'], final['rows']):
        for key in ('row_id', 'work_id', 'edition_id', 'request_id', 'manifest'):
            assert after[key] == before[key]
    assert len(restored.search(workspace, 'railway')) == 3
    assert not (restored.root / INCOMPLETE).exists()


@pytest.mark.parametrize('selected', ['source', 'restore_target', 'backup_target'])
def test_original_path_ancestor_links_cannot_be_hidden_by_resolution(tmp_path, selected):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Local source')
    app.add_text(workspace, 'Original', 'Original railway document.')
    real_parent = tmp_path / 'real-parent'
    real_parent.mkdir()
    linked_parent = tmp_path / 'linked-parent'
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    backup = real_parent / 'backup'
    digest = create_backup(app, backup)
    with pytest.raises(ValueError, match='links'):
        if selected == 'source':
            restore_backup(linked_parent / 'backup', tmp_path / 'restored', digest)
        elif selected == 'restore_target':
            restore_backup(backup, linked_parent / 'restored', digest)
        else:
            create_backup(app, linked_parent / 'another-backup')
    assert not (tmp_path / 'restored').exists()
    assert not (real_parent / 'restored').exists()
    assert not (real_parent / 'another-backup').exists()


def test_interrupted_pending_journal_publication_retains_incomplete_marker(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pending source')
    source = tmp_path / 'original.txt'
    source.write_text('Original railway source.', encoding='utf-8')
    app.plan_import_batch(workspace, [source])
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    target = tmp_path / 'restored'
    link = os.link
    def interrupted(origin, destination, *args, **kwargs):
        if str(destination).startswith(str(target / 'import-batches')):
            raise OSError('Simulated pending publication interruption')
        return link(origin, destination, *args, **kwargs)
    monkeypatch.setattr(os, 'link', interrupted)
    with pytest.raises(OSError, match='Simulated'):
        restore_backup(backup, target, digest)
    assert (target / INCOMPLETE).exists()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(target)
