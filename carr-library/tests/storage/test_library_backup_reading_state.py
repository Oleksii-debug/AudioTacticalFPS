"""Full-backup reading recovery against actual LibraryApp and immutable sources."""
from errno import ENOSPC
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library import library_backup
from carr_library.ingest.writer_guard import WriterBusy, exclusive_writer
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import FILENAME, MAX_BYTES, ReadingState


@pytest.fixture
def reading_library(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    a, b = app.create_workspace('Library A'), app.create_workspace('Library B')
    books = [app.add_text(workspace, 'Same title', 'First segment.\fКиїв — čítanie.\n',
                          authors=(author,))
             for workspace, author in ((a, 'Author A'), (b, 'Author B'))]
    pages = [ReadingDesk(app, workspace, book['edition']).page(2)
             for workspace, book in zip((a, b), books)]
    state = ReadingState(app.root)
    bookmarks = []
    for n, page in enumerate(pages):
        state.remember(page.location)
        bookmarks.append(state.add_bookmark(page.location, f'Розділ {n} — Café e\u0301'))
    return app, (a, b), books, pages, bookmarks


def read_manifest(folder):
    return json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))


def write_manifest(folder, document):
    raw = (json.dumps(document, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def reading_entry(manifest):
    return next(entry for entry in manifest['sidecars'] if entry['filename'] == FILENAME)


def seal_reading_bytes(folder, raw):
    """A valid outer digest cannot legitimize invalid or foreign saved places."""
    (folder / FILENAME).write_bytes(raw)
    manifest = read_manifest(folder)
    manifest['sidecars'] = [dict(filename=FILENAME, size_bytes=len(raw),
                                  sha256=sha256(raw).hexdigest())]
    return write_manifest(folder, manifest)


def test_application_backup_restores_exact_bookmarks_and_resume_for_both_libraries(
        reading_library, tmp_path):
    app, workspaces, books, pages, bookmarks = reading_library
    raw = (app.root / FILENAME).read_bytes()
    folder = tmp_path / 'backup'
    digest = app.backup(folder)
    manifest = read_manifest(folder)
    assert manifest['version'] == 3
    assert reading_entry(manifest) == dict(filename=FILENAME, size_bytes=len(raw),
                                             sha256=sha256(raw).hexdigest())
    assert (folder / FILENAME).read_bytes() == raw
    assert {path.name for path in folder.iterdir()} == {
        'manifest.json', 'library.sqlite', 'originals', FILENAME}
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    state = ReadingState(restored.root)
    assert state.path.read_bytes() == raw
    assert state.path.stat().st_nlink == (folder / FILENAME).stat().st_nlink == 1
    for workspace, book, page, bookmark in zip(workspaces, books, pages, bookmarks):
        assert state.resume(workspace, book['edition']) == page.location
        assert state.bookmarks(workspace, book['edition']) == (bookmark,)
        assert ReadingDesk(restored, workspace, book['edition']).validate(bookmark.location) == page
    assert (app.root / FILENAME).read_bytes() == raw


def test_unverified_physical_pdf_bookmark_keeps_location_and_review_status(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    source = tmp_path / 'source.pdf'
    canvas = Canvas(str(source), invariant=1, pageCompression=0)
    for number in (1, 2):
        canvas.drawString(50, 700, f'Synthetic page {number}.')
        canvas.showPage()
    canvas.save()
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('PDF library')
    book = app.import_file(workspace, source)
    page = ReadingDesk(app, workspace, book['edition']).page(2)
    assert page.location.kind == 'physical_page' and not page.verified
    state = ReadingState(app.root)
    state.remember(page.location)
    bookmark = state.add_bookmark(page.location, 'The second physical page')
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    saved, = ReadingState(restored.root).bookmarks(workspace, book['edition'])
    assert saved == bookmark
    after = ReadingDesk(restored, workspace, book['edition']).validate(saved.location)
    assert after == page and not after.locator.verified


def test_historical_and_archived_bindings_are_preserved_without_retargeting(
        reading_library, tmp_path):
    app, (a, b), books, pages, bookmarks = reading_library
    new_revision = app.update_metadata(a, books[0]['edition'], title='Updated title')
    assert new_revision != pages[0].location.revision
    app.archive_workspace(b)
    before = (app.root / FILENAME).read_bytes()
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    state = ReadingState(restored.root)
    assert state.path.read_bytes() == before
    assert state.resume(a, books[0]['edition']) == pages[0].location
    assert state.bookmarks(a, books[0]['edition']) == (bookmarks[0],)
    with pytest.raises(ValueError, match='no longer active'):
        ReadingDesk(restored, a, books[0]['edition'], revision=pages[0].location.revision)
    with pytest.raises(ValueError, match='different library, edition or revision'):
        ReadingDesk(restored, a, books[0]['edition']).validate(pages[0].location)
    assert state.bookmarks(b, books[1]['edition']) == (bookmarks[1],)
    with pytest.raises(ValueError, match='archived'):
        ReadingDesk(restored, b, books[1]['edition'])
    restored.restore_workspace(b)
    assert ReadingDesk(restored, b, books[1]['edition']).validate(pages[1].location) == pages[1]


def test_absent_state_is_explicit_and_unrelated_sidecars_are_not_copied(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Empty reading state')
    # Synthetic credential-shaped data is intentionally not an allowed backup component.
    for name in ('providers.json', 'api-keys.json', 'reading-state.json.old'):
        (app.root / name).write_text('SYNTHETIC DO NOT COPY', encoding='utf-8')
    folder = tmp_path / 'backup'
    digest = app.backup(folder)
    assert read_manifest(folder)['version'] == 2 and 'sidecars' not in read_manifest(folder)
    assert {path.name for path in folder.iterdir()} == {'manifest.json', 'library.sqlite', 'originals'}
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    state = ReadingState(restored.root)
    assert not state.path.exists() and state.resume(workspace, 'unused') is None
    assert all(not (restored.root / name).exists()
               for name in ('providers.json', 'api-keys.json', 'reading-state.json.old'))


@pytest.mark.parametrize('phase', ['backup', 'restore'])
@pytest.mark.parametrize('field,value', [
    ('workspace', 'another-workspace'), ('edition', 'another-edition'),
    ('revision', 'another-revision'), ('kind', 'physical_page'), ('index', 999),
    ('text_sha256', '0' * 64), ('asset_sha256', '0' * 64),
])
def test_saved_location_must_match_the_exact_copied_database(
        reading_library, tmp_path, phase, field, value):
    app, _, _, pages, _ = reading_library
    folder = tmp_path / 'backup'
    if phase == 'restore':
        create_backup(app, folder)
        path = folder / FILENAME
    else:
        path = app.root / FILENAME
    document = json.loads(path.read_text(encoding='utf-8'))
    if field in ('workspace', 'edition', 'revision'):
        current_workspace = document['positions'][0]['location']['workspace']
        other = next(page.location for page in pages if page.location.workspace != current_workspace)
        # Both libraries deliberately contain identical text and asset hashes.
        # Valid IDs from the other real source must still fail this exact binding.
        value = getattr(other, field)
    document['positions'][0]['location'][field] = value
    raw = json.dumps(document, ensure_ascii=False).encode('utf-8')
    destination = tmp_path / 'rejected'
    if phase == 'restore':
        digest = seal_reading_bytes(folder, raw)
        operation = lambda: restore_backup(folder, destination, digest)
    else:
        path.write_bytes(raw)
        operation = lambda: create_backup(app, destination)
    with pytest.raises(ValueError, match='location disagrees'):
        operation()
    assert not destination.exists()
    assert path.read_bytes() == raw


@pytest.mark.parametrize('damage', ['empty', 'truncated', 'invalid_utf8', 'unknown_field'])
def test_invalid_live_reading_state_cannot_be_silently_omitted_from_backup(
        reading_library, tmp_path, damage):
    app, *_ = reading_library
    path = app.root / FILENAME
    if damage == 'empty':
        raw = b''
    elif damage == 'truncated':
        raw = path.read_bytes()[:50]
    elif damage == 'invalid_utf8':
        raw = b'\xff' + path.read_bytes()
    else:
        document = json.loads(path.read_text(encoding='utf-8'))
        document['api_key'] = 'SYNTHETIC DO NOT COPY'
        raw = json.dumps(document).encode('utf-8')
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert path.read_bytes() == raw


@pytest.mark.parametrize('damage', [
    'changed_bytes', 'missing', 'unexpected_file', 'invalid_json',
    'duplicate_key', 'unknown_state_field', 'oversized', 'wrong_size', 'wrong_digest',
])
def test_incomplete_tampered_or_unexpected_state_never_publishes_restore(
        reading_library, tmp_path, damage):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    path = folder / FILENAME
    if damage == 'changed_bytes':
        raw = path.read_bytes().replace(b'Caf', b'New', 1)
        path.write_bytes(raw)
    elif damage == 'missing':
        path.unlink()
    elif damage == 'unexpected_file':
        (folder / 'api-keys.json').write_text('SYNTHETIC DO NOT RESTORE')
    elif damage == 'invalid_json':
        digest = seal_reading_bytes(folder, b'{')
    elif damage == 'duplicate_key':
        digest = seal_reading_bytes(folder, path.read_bytes().replace(
            b'"version": 1', b'"version": 1, "version": 1', 1))
    elif damage == 'unknown_state_field':
        document = json.loads(path.read_text(encoding='utf-8'))
        document['credentials'] = 'SYNTHETIC DO NOT RESTORE'
        digest = seal_reading_bytes(folder, json.dumps(document).encode())
    elif damage == 'oversized':
        digest = seal_reading_bytes(folder, b' ' * (MAX_BYTES + 1))
    else:
        manifest = read_manifest(folder)
        if damage == 'wrong_size':
            reading_entry(manifest)['size_bytes'] += 1
        else:
            reading_entry(manifest)['sha256'] = '0' * 64
        digest = write_manifest(folder, manifest)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, digest)
    assert not destination.exists()


@pytest.mark.parametrize('field,value', [
    ('filename', '../reading-state.json'), ('filename', '/reading-state.json'),
    ('filename', 'reading-state.json:secret'), ('filename', 'api-keys.json'),
    ('size_bytes', True), ('size_bytes', 0), ('sha256', 'not-a-digest'),
])
def test_reading_manifest_is_an_exact_allowlist(reading_library, tmp_path, field, value):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = read_manifest(folder)
    reading_entry(manifest)[field] = value
    digest = write_manifest(folder, manifest)
    with pytest.raises(ValueError, match='backup sidecar'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('change', ['missing_descriptor', 'undeclared_file', 'invalid_descriptor'])
def test_v3_reading_presence_must_be_explicit(reading_library, tmp_path, change):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = read_manifest(folder)
    if change == 'missing_descriptor':
        del manifest['sidecars']
    else:
        manifest['sidecars'] = [] if change == 'undeclared_file' else {}
    with pytest.raises(ValueError):
        restore_backup(folder, tmp_path / 'rejected', write_manifest(folder, manifest))
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('phase', ['backup', 'restore'])
@pytest.mark.parametrize('kind', ['hardlink', 'symlink'])
def test_linked_reading_file_is_rejected_without_touching_external_target(
        reading_library, tmp_path, monkeypatch, phase, kind):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    if phase == 'restore':
        digest = create_backup(app, folder)
        path = folder / FILENAME
    else:
        path = app.root / FILENAME
    raw = path.read_bytes()
    external = tmp_path / 'external-state.json'
    external.write_bytes(raw)
    path.unlink()
    if kind == 'hardlink':
        os.link(external, path)
    else:
        try:
            path.symlink_to(external)
        except OSError as exc:
            if os.name != 'nt' or getattr(exc, 'winerror', None) != 1314:
                raise
            # A standard Windows account may lack symlink privilege. In that
            # case exercise its explicit reparse-point rejection on a real file.
            path.write_bytes(raw)
            original_lstat = Path.lstat

            def reparse_lstat(candidate, *args, **kwargs):
                info = original_lstat(candidate, *args, **kwargs)
                if candidate == path:
                    return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                           st_file_attributes=0x400)
                return info

            monkeypatch.setattr(Path, 'lstat', reparse_lstat)
    with pytest.raises(ValueError):
        if phase == 'restore':
            restore_backup(folder, tmp_path / 'rejected', digest)
        else:
            create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert external.read_bytes() == raw


def test_reading_snapshot_and_database_share_the_existing_writer_guard(
        reading_library, tmp_path, monkeypatch):
    app, _, _, pages, _ = reading_library
    state = ReadingState(app.root)
    with exclusive_writer(app.root):
        with pytest.raises(WriterBusy):
            create_backup(app, tmp_path / 'blocked')
    assert not (tmp_path / 'blocked').exists()
    original = library_backup.copy_catalog_assets
    observed = []

    def while_snapshot_locked(*args, **kwargs):
        with pytest.raises(WriterBusy):
            state.remember(pages[0].location)
        with pytest.raises(WriterBusy):
            app.create_workspace('Must not be included halfway through')
        observed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(library_backup, 'copy_catalog_assets', while_snapshot_locked)
    create_backup(app, tmp_path / 'backup')
    assert observed == [True]


def test_publication_uses_captured_bytes_after_source_path_changes(
        reading_library, tmp_path, monkeypatch):
    app, *_ = reading_library
    raw = (app.root / FILENAME).read_bytes()
    replacement = raw.replace(b'Caf', b'New', 1)
    original = library_backup._sync_file

    def change_source_after_snapshot(path, data):
        result = original(path, data)
        if path.name == 'manifest.json':
            # Deliberate non-cooperating writer, after the one validated read.
            (app.root / FILENAME).write_bytes(replacement)
        return result

    monkeypatch.setattr(library_backup, '_sync_file', change_source_after_snapshot)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    assert (folder / FILENAME).read_bytes() == raw
    assert (app.root / FILENAME).read_bytes() == replacement
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert (target / FILENAME).read_bytes() == raw


@pytest.mark.parametrize('phase', ['backup', 'restore'])
def test_staging_disk_failure_does_not_publish_a_partial_destination(
        reading_library, tmp_path, monkeypatch, phase):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    if phase == 'restore':
        digest = create_backup(app, folder)
    raw = (app.root / FILENAME).read_bytes()
    original = library_backup._copy_sidecar
    interrupted = []

    def full_disk(root, relative, target, identity, cancelled):
        if relative == FILENAME:
            # Exercise failure after private staging began, at the actual
            # streamed sidecar-copy boundary rather than the old byte writer.
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'partially staged reading state')
            interrupted.append(target)
            raise OSError(ENOSPC, 'synthetic disk full')
        return original(root, relative, target, identity, cancelled)

    monkeypatch.setattr(library_backup, '_copy_sidecar', full_disk)
    with pytest.raises(OSError, match='disk full'):
        if phase == 'restore':
            restore_backup(folder, tmp_path / 'rejected', digest)
        else:
            create_backup(app, tmp_path / 'rejected')
    assert interrupted and all(not path.exists() for path in interrupted)
    assert not (tmp_path / 'rejected').exists()
    assert (app.root / FILENAME).read_bytes() == raw
    assert not list(tmp_path.glob('.carr-library-*'))


def test_interruption_while_publishing_reading_state_keeps_restore_unopenable(
        reading_library, tmp_path, monkeypatch):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    destination = tmp_path / 'interrupted'
    original = os.link

    def interrupt(source, target, *args, **kwargs):
        if target == destination / FILENAME:
            raise OSError('synthetic reading-state publication interruption')
        return original(source, target, *args, **kwargs)

    monkeypatch.setattr(os, 'link', interrupt)
    with pytest.raises(OSError, match='publication interruption'):
        restore_backup(folder, destination, digest)
    assert (destination / INCOMPLETE).exists()
    with pytest.raises(ValueError, match='restore is incomplete'):
        LibraryApp(destination)


@pytest.mark.parametrize('version', [1, 2])
def test_legacy_backup_restores_without_fabricating_reading_state(
        reading_library, tmp_path, version):
    app, workspaces, books, *_ = reading_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = read_manifest(folder)
    manifest['version'] = version
    del manifest['sidecars']
    if version == 1:
        del manifest['schema_fingerprint']
    (folder / FILENAME).unlink()
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored',
                                        write_manifest(folder, manifest)))
    state = ReadingState(restored.root)
    for workspace, book in zip(workspaces, books):
        assert state.resume(workspace, book['edition']) is None
        assert state.bookmarks(workspace, book['edition']) == ()
        assert ReadingDesk(restored, workspace, book['edition']).page(1).text == 'First segment.'
    assert not state.path.exists()


@pytest.mark.parametrize('version', [1, 2])
def test_legacy_manifest_cannot_smuggle_an_unbound_reading_file(
        reading_library, tmp_path, version):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = read_manifest(folder)
    manifest['version'] = version
    del manifest['sidecars']
    if version == 1:
        del manifest['schema_fingerprint']
    digest = write_manifest(folder, manifest)
    with pytest.raises(ValueError, match='unexpected reading-state'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()
