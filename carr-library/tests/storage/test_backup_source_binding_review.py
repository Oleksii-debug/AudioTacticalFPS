"""Independent backup review: narration sidecars cannot silently retarget sources."""
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.narration import NarrationStore, new_project
from carr_library.reader.reading_desk import ReadingDesk


@pytest.mark.parametrize('damage', ['workspace', 'original-text', 'narration-directory-link'])
def test_trusted_manifest_does_not_replace_source_binding_checks(tmp_path, damage):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('First library')
    other = app.create_workspace('Second library')
    book = app.add_text(workspace, 'Source', 'Exact original text.')
    # Identical edition names in another library must not authorize a revision
    # whose original workspace is different.
    app.add_text(other, 'Source', 'Exact original text.', edition_id=book['edition'])
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    project = new_project(page.raw_text, source_location=page.location)
    NarrationStore(app.root).save(project)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    relative = f'narration-projects/{project.id}.json'
    path = backup / relative
    if damage == 'narration-directory-link':
        moved = tmp_path / 'external-copy'
        (backup / 'narration-projects').rename(moved)
        try:
            (backup / 'narration-projects').symlink_to(moved, target_is_directory=True)
        except OSError:
            pytest.skip('Host does not permit creating directory symbolic links')
    else:
        value = json.loads(path.read_bytes())
        if damage == 'workspace':
            value['source_location']['workspace'] = other
        else:
            value['original_text'] = 'Forged source wording.'
            value['original_sha256'] = sha256(value['original_text'].encode()).hexdigest()
            value['revisions'][0]['text'] = value['original_text']
            value['source_location']['text_sha256'] = value['original_sha256']
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        manifest_path = backup / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        entry = next(item for item in manifest['sidecars'] if item['filename'] == relative)
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
        manifest_path.write_bytes(json.dumps(manifest).encode())
        digest = sha256(manifest_path.read_bytes()).hexdigest()
    destination = tmp_path / 'restored'
    with pytest.raises((ValueError, OSError)):
        restore_backup(backup, destination, digest)
    assert not destination.exists()
