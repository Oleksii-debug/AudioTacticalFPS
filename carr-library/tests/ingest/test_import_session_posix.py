"""Actual POSIX ancestor substitutions around a live accounting observation."""
import os
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import session as module
from carr_library.ingest.resource_budget import ImportBudget, MIB


@pytest.mark.parametrize('change', ['root-link', 'ancestor-link', 'root-directory'])
def test_changed_actual_ancestor_after_free_space_probe_is_refused_before_grant(
        tmp_path, monkeypatch, change):
    assert os.name == 'posix', 'This actual directory-rename proof belongs to the Linux gate.'
    container = tmp_path / 'selected-parent'
    app = LibraryApp(container / 'library')
    workspace = app.create_workspace('Actual ancestor replacement')
    policy = ImportBudget(source_bytes=MIB, segment_bytes=MIB,
        memory_bytes=128*MIB, library_bytes=16*MIB, temporary_bytes=16*MIB)
    selected = container if change == 'ancestor-link' else app.root
    moved = tmp_path / 'held-original-parent'
    before = selected.lstat()
    actual_disk_usage = module.shutil.disk_usage
    armed = False
    changed = []

    def observed_space(path):
        nonlocal armed
        actual = actual_disk_usage(path)
        if armed:
            armed = False
            assert Path(path) == app.root
            selected.rename(moved)
            if change.endswith('link'):
                selected.symlink_to(moved, target_is_directory=True)
                # The alias still reaches the original directory identity:
                # only a real ancestor lstat rejects this substituted path.
                reached = selected.stat()
                assert (reached.st_dev, reached.st_ino) == (before.st_dev, before.st_ino)
            else:
                selected.mkdir()
                replaced = selected.lstat()
                assert (replaced.st_dev, replaced.st_ino) != (before.st_dev, before.st_ino)
            changed.append(change)
        return actual

    with module.import_session(app, workspace, policy) as owner:
        with monkeypatch.context() as observation:
            observation.setattr(module.shutil, 'disk_usage', observed_space)
            armed = True
            try:
                with pytest.raises(ValueError, match='regular local files|root was replaced'):
                    owner.check()
            finally:
                if changed:
                    selected.unlink() if selected.is_symlink() else selected.rmdir()
                    moved.rename(selected)
        assert changed == [change]
        restored = selected.lstat()
        assert (restored.st_dev, restored.st_ino) == (before.st_dev, before.st_ino)
        assert owner.check() == 0
        owner.storage.check()
    with app.read(workspace) as db:
        assert not db.execute('SELECT id FROM catalog_edition WHERE workspace=?', (workspace,)).fetchall()
