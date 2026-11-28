"""Unified backup keeps typed preparation provenance without activating optional code."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, verify_backup, restore_backup
from carr_library.reader.narration import NarrationStore, apply_preparation, new_project
from carr_library.reader.reading_desk import ReadingDesk

_spec = importlib.util.spec_from_file_location('_backup_preparation_fixture',
    Path(__file__).parents[1] / 'reader/preparation_fixture.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


def prepared_library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source-bound preparation')
    book = app.add_text(workspace, 'Exact original', 'Source 42 remains evidence.')
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    project = new_project(page.raw_text, source_location=page.location)
    project = apply_preparation(project, _fixture.preview(project, 7, 9, 'forty-two'))
    NarrationStore(app.root).save(project)
    return app, workspace, book, project


def test_unified_backup_restore_and_rebackup_preserve_exact_preparation_provenance(tmp_path):
    app, workspace, book, project = prepared_library(tmp_path)
    before = (app.root / 'narration-projects' / (project.id + '.json')).read_bytes()
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    manifest = verify_backup(backup, digest)
    assert any(row['path'] == 'narration-projects/' + project.id + '.json' for row in manifest['sidecars'])
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    saved, _ = NarrationStore(restored.root).load(project.id)
    assert saved == project and saved.revisions[-1].preparation == project.revisions[-1].preparation
    assert (restored.root / 'narration-projects' / (project.id + '.json')).read_bytes() == before
    assert ReadingDesk(restored, workspace, book['edition']).page(1).raw_text == project.original_text
    assert not (restored.root / 'reading-preparation').exists()
    assert not (restored.root / 'hunspell').exists()
    again = tmp_path / 'again'
    verify_backup(again, create_backup(restored, again))


@pytest.mark.parametrize('field', ['input_sha256', 'output_sha256', 'source_sha256'])
def test_outer_checksum_cannot_authorize_broken_inner_preparation_history(tmp_path, field):
    app, _, _, project = prepared_library(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    name = 'narration-projects/' + project.id + '.json'
    path = backup / 'sidecars' / name
    value = json.loads(path.read_bytes())
    value['revisions'][1]['preparation'][field] = '0' * 64
    path.write_bytes((json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode())
    row = next(row for row in manifest['sidecars'] if row['path'] == name)
    row['size_bytes'] = path.stat().st_size
    row['sha256'] = sha256(path.read_bytes()).hexdigest()
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n').encode()
    manifest_path.write_bytes(raw)
    with pytest.raises(ValueError, match='preparation|invalid'):
        verify_backup(backup, sha256(raw).hexdigest())
