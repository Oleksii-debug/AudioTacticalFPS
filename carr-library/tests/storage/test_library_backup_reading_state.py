"""Full-backup reading recovery against actual LibraryApp and immutable sources."""
from errno import ENOSPC
from hashlib import sha256
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_sidecars, library_backup
from carr_library.ingest.writer_guard import WriterBusy, exclusive_writer
from carr_library.library_backup import create_backup, restore_backup, verify_backup, INCOMPLETE
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


def reading_entry(manifest):
    return next(entry for entry in manifest['sidecars'] if entry['filename'] == FILENAME)


def legacy_reading_manifest(folder):
    document = read_manifest(folder)
    assert document['version'] == 4
    entries = document.pop('sidecars')
    assert not entries or (len(entries) == 1 and entries[0]['filename'] == FILENAME)
    document.pop('generated_audio_included')
    if document.pop('pending', None) is not None:
        shutil.rmtree(folder / 'pending')
    document.update(version=3, reading_state=entries[0] if entries else None)
    return document


def write_manifest(folder, document):
    raw = (json.dumps(document, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def seal_reading_bytes(folder, raw):
    """A valid outer digest cannot legitimize invalid or foreign saved places."""
    (folder / FILENAME).write_bytes(raw)
    manifest = read_manifest(folder)
    reading_entry(manifest).update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    return write_manifest(folder, manifest)


def test_application_backup_restores_exact_bookmarks_and_resume_for_both_libraries(
        reading_library, tmp_path):
    app, workspaces, books, pages, bookmarks = reading_library
    raw = (app.root / FILENAME).read_bytes()
    folder = tmp_path / 'backup'
    digest = app.backup(folder)
    manifest = read_manifest(folder)
    assert manifest['version'] == 4 and manifest['generated_audio_included'] is False
    assert reading_entry(manifest) == dict(filename=FILENAME, size_bytes=len(raw),
                                             sha256=sha256(raw).hexdigest())
    assert (folder / FILENAME).read_bytes() == raw
    assert {path.name for path in folder.iterdir()} == {
        'manifest.json', 'library.sqlite', 'originals', 'pending', FILENAME}
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
    assert read_manifest(folder)['sidecars'] == []
    assert {path.name for path in folder.iterdir()} == {'manifest.json', 'library.sqlite', 'originals'}
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    state = ReadingState(restored.root)
    assert not state.path.exists() and state.resume(workspace, 'unused') is None
    assert all(not (restored.root / name).exists()
               for name in ('providers.json', 'api-keys.json', 'reading-state.json.old'))


@pytest.mark.parametrize('phase', ['backup', 'restore', 'verify'])
@pytest.mark.parametrize('field,value', [
    ('workspace', 'another-workspace'), ('edition', 'another-edition'),
    ('revision', 'another-revision'), ('kind', 'physical_page'), ('index', 999),
    ('text_sha256', '0' * 64), ('asset_sha256', '0' * 64),
])
def test_saved_location_must_match_the_exact_copied_database(
        reading_library, tmp_path, phase, field, value):
    app, _, _, pages, _ = reading_library
    folder = tmp_path / 'backup'
    if phase in ('restore', 'verify'):
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
    if phase in ('restore', 'verify'):
        digest = seal_reading_bytes(folder, raw)
        operation = (lambda: restore_backup(folder, destination, digest)) if phase == 'restore' else (
            lambda: verify_backup(folder, digest))
    else:
        path.write_bytes(raw)
        operation = lambda: create_backup(app, destination)
    with pytest.raises(ValueError, match='exact saved source'):
        operation()
    assert not destination.exists()
    assert path.read_bytes() == raw


def test_public_verification_checks_real_backup_without_changing_original_files(
        reading_library, tmp_path):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    before = {str(path.relative_to(folder)): (path.read_bytes(), path.stat().st_mtime_ns)
              for path in folder.rglob('*') if path.is_file()}
    assert verify_backup(folder, digest) == read_manifest(folder)
    assert {str(path.relative_to(folder)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in folder.rglob('*') if path.is_file()} == before
    assert {path.name for path in tmp_path.iterdir()} == {'data', 'backup'}


@pytest.mark.parametrize('damage', [
    'database', 'asset', 'extra_asset', 'missing_asset', 'extra_file',
    'reading_bytes', 'missing_reading',
])
def test_public_verification_rejects_corruption_and_unexpected_files_without_mutation(
        reading_library, tmp_path, damage):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    if damage == 'database':
        path = folder / 'library.sqlite'
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 1
        path.write_bytes(raw)
    elif damage in ('asset', 'missing_asset'):
        path = next((folder / 'originals').iterdir())
        path.write_bytes(b'x' * path.stat().st_size) if damage == 'asset' else path.unlink()
    elif damage == 'extra_asset':
        (folder / 'originals' / 'unregistered.txt').write_bytes(b'Unexpected original')
    elif damage == 'extra_file':
        (folder / 'unregistered.txt').write_bytes(b'Unexpected sidecar')
    elif damage == 'reading_bytes':
        path = folder / FILENAME
        path.write_bytes(path.read_bytes().replace(b'Caf', b'New', 1))
    else:
        (folder / FILENAME).unlink()
    before = {str(path.relative_to(folder)): path.read_bytes()
              for path in folder.rglob('*') if path.is_file()}
    with pytest.raises(ValueError):
        verify_backup(folder, digest)
    assert {str(path.relative_to(folder)): path.read_bytes()
            for path in folder.rglob('*') if path.is_file()} == before


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
@pytest.mark.parametrize('version', [3, 4])
def test_reading_manifest_is_an_exact_allowlist(reading_library, tmp_path, field, value, version):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = read_manifest(folder)
    if version == 3:
        manifest = legacy_reading_manifest(folder)
        manifest['reading_state'][field] = value
    else:
        reading_entry(manifest)[field] = value
    digest = write_manifest(folder, manifest)
    with pytest.raises(ValueError, match='manifest|filename|validated backup adapter'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('change', ['missing_descriptor', 'undeclared_file', 'array_descriptor'])
def test_v3_reading_presence_must_be_explicit(reading_library, tmp_path, change):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = legacy_reading_manifest(folder)
    if change == 'missing_descriptor':
        del manifest['reading_state']
    else:
        manifest['reading_state'] = None if change == 'undeclared_file' else []
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
    original = library_backup._copy_originals
    observed = []

    def while_snapshot_locked(*args, **kwargs):
        with pytest.raises(WriterBusy):
            state.remember(pages[0].location)
        with pytest.raises(WriterBusy):
            app.create_workspace('Must not be included halfway through')
        observed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(library_backup, '_copy_originals', while_snapshot_locked)
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
    original = backup_sidecars._write

    def full_disk(stage, name, data):
        if name == FILENAME:
            raise OSError(ENOSPC, 'synthetic disk full')
        return original(stage, name, data)

    monkeypatch.setattr(backup_sidecars, '_write', full_disk)
    with pytest.raises(OSError, match='disk full'):
        if phase == 'restore':
            restore_backup(folder, tmp_path / 'rejected', digest)
        else:
            create_backup(app, tmp_path / 'rejected')
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
    if manifest.pop('pending', None) is not None:
        shutil.rmtree(folder / 'pending')
    manifest['version'] = version
    del manifest['sidecars']
    del manifest['generated_audio_included']
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
    if manifest.pop('pending', None) is not None:
        shutil.rmtree(folder / 'pending')
    manifest['version'] = version
    del manifest['sidecars']
    del manifest['generated_audio_included']
    if version == 1:
        del manifest['schema_fingerprint']
    digest = write_manifest(folder, manifest)
    with pytest.raises(ValueError, match='unexpected files.*reading-state'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


def test_exact_v3_reading_shape_remains_read_only_compatible(reading_library, tmp_path):
    app, _, _, pages, _ = reading_library
    folder = tmp_path / 'legacy-v3'
    create_backup(app, folder)
    manifest = legacy_reading_manifest(folder)
    checksum = write_manifest(folder, manifest)
    before = {p.relative_to(folder): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in folder.rglob('*') if p.is_file()}
    assert verify_backup(folder, checksum) == manifest
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    state = ReadingState(restored)
    for page in pages:
        assert state.resume(page.location.workspace, page.location.edition) == page.location
    assert {p.relative_to(folder): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in folder.rglob('*') if p.is_file()} == before

# Independent legacy-v5 and source-attestation regression coverage.
"""Whole-library backup retains exact personal locations without retargeting.

Real SQLite, retained originals and reading-state files are used throughout.
The failure hooks model interruption/competing writers; they do not mock the
source resolver or permit a new quotation to bypass its existing attestors.
"""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.writer_guard import WriterBusy
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.reader import reading_state
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.storage.revisions import Page, Storage


@pytest.fixture
def populated(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    first = app.create_workspace('Research one')
    second = app.create_workspace('Research two')
    books = [app.add_text(workspace, 'Same title', 'Перший розділ.\fSecond section.',
                          authors=(author,), edition='First')
             for workspace, author in ((first, 'Author A'), (second, 'Author B'))]
    pages = [ReadingDesk(app, workspace, book['edition']).page(2)
             for workspace, book in zip((first, second), books)]
    state = ReadingState(app.root)
    bookmarks = []
    for number, page in enumerate(pages, 1):
        state.remember(page.location)
        bookmarks.append(state.add_bookmark(page.location, f'Читати далі — čítanie {number}'))
    topic = app.create_topic(first, 'Evidence remains separate')
    hit, = app.search(first, 'Перший')
    app.save_hit(first, topic, hit.ref, hit.revision, 'An independent research note.')
    return app, (first, second), books, pages, state, tuple(bookmarks), topic


def manifest_at(folder):
    return json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))


def replace_manifest(folder, manifest):
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def reseal_state(folder, raw):
    """A caller-supplied outer digest still cannot authorize invalid locations."""
    (folder / reading_state.FILENAME).write_bytes(raw)
    manifest = manifest_at(folder)
    manifest['reading_state']['size_bytes'] = len(raw)
    manifest['reading_state']['sha256'] = sha256(raw).hexdigest()
    return replace_manifest(folder, manifest)


def test_full_backup_keeps_bookmarks_resume_and_archived_library(populated, tmp_path):
    app, workspaces, books, pages, state, bookmarks, topic = populated
    before = state.path.read_bytes()
    evidence = app.evidence(workspaces[0], topic)
    app.archive_workspace(workspaces[1])
    folder = tmp_path / 'backup'
    digest = create_legacy_v5_backup(app, folder)
    manifest = manifest_at(folder)
    assert manifest['version'] == 5
    assert manifest['reading_state'] == dict(filename=reading_state.FILENAME,
        size_bytes=len(before), sha256=sha256(before).hexdigest())
    assert (folder / reading_state.FILENAME).read_bytes() == before
    # Later reading activity cannot change the already sealed backup.
    state.rename_bookmark(workspaces[0], books[0]['edition'], bookmarks[0].id, 'Later name')
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    copied = ReadingState(restored.root)
    assert copied.path.read_bytes() == before
    assert copied.path.stat().st_nlink == 1
    assert (folder / reading_state.FILENAME).stat().st_nlink == 1
    assert restored.evidence(workspaces[0], topic) == evidence
    for workspace, book, page, bookmark in zip(workspaces, books, pages, bookmarks):
        assert copied.resume(workspace, book['edition']) == page.location
        assert copied.bookmarks(workspace, book['edition']) == (bookmark,)
    with pytest.raises(ValueError, match='archived'):
        ReadingDesk(restored, workspaces[1], books[1]['edition'])
    restored.restore_workspace(workspaces[1])
    for workspace, book, page in zip(workspaces, books, pages):
        assert ReadingDesk(restored, workspace, book['edition']).validate(page.location) == page
    assert (folder / reading_state.FILENAME).read_bytes() == before
    assert state.bookmarks(workspaces[0], books[0]['edition'])[0].label == 'Later name'


def test_historical_position_survives_without_becoming_a_new_revision(populated, tmp_path):
    app, workspaces, books, pages, state, bookmarks, _ = populated
    location = pages[0].location
    new_revision = app.update_metadata(workspaces[0], books[0]['edition'], title='Revised title')
    assert location.revision != new_revision
    folder = tmp_path / 'backup'
    digest = create_legacy_v5_backup(app, folder)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    saved = ReadingState(restored.root)
    assert saved.resume(workspaces[0], books[0]['edition']) == location
    assert saved.bookmarks(workspaces[0], books[0]['edition']) == (bookmarks[0],)
    with pytest.raises(ValueError, match='no longer active'):
        ReadingDesk(restored, workspaces[0], books[0]['edition'], revision=location.revision)
    current = ReadingDesk(restored, workspaces[0], books[0]['edition'])
    assert current.revision == new_revision
    with pytest.raises(ValueError, match='different library, edition or revision'):
        current.validate(location)


def test_sparse_physical_pdf_bookmark_keeps_its_actual_page_and_unverified_state(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    source = tmp_path / 'source.pdf'
    canvas = Canvas(str(source), invariant=1, pageCompression=0)
    for number in (1, 2):
        canvas.drawString(50, 700, f'Synthetic physical PDF page {number}.')
        canvas.showPage()
    canvas.save()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Physical-page reading')
    book = app.import_file(workspace, source, title='PDF reading fixture')
    assert book['state'] == 'complete'
    # A retained partial extraction can have reading ordinal 1 at PDF page 2.
    with closing(Storage(app.database)) as storage:
        with storage.transaction():
            raw = storage.db.execute('SELECT raw FROM page WHERE revision=? AND physical=2',
                                     (book['id'],)).fetchone()[0]
            storage.stage_revision(workspace, book['edition'], 'sparse-pdf-revision', book['asset'],
                                   'synthetic-sparse-extraction', [Page(raw, 2)])
            storage.db.execute('''INSERT INTO source_metadata
                SELECT workspace,edition,?,author,title,volume,edition_label
                FROM source_metadata WHERE workspace=? AND edition=? AND revision=?''',
                ('sparse-pdf-revision', workspace, book['edition'], book['id']))
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    assert page.location.kind == 'physical_page' and page.location.index == 2
    assert page.ordinal == 1 and page.verified is False
    state = ReadingState(app.root)
    state.remember(page.location)
    bookmark = state.add_bookmark(page.location, 'Actual PDF page two')
    folder = tmp_path / 'backup'
    digest = create_legacy_v5_backup(app, folder)
    source.unlink()
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    copied = ReadingState(restored.root)
    assert copied.bookmarks(workspace, book['edition']) == (bookmark,)
    assert ReadingDesk(restored, workspace, book['edition']).validate(
        copied.resume(workspace, book['edition'])) == page
    hit, = restored.search(workspace, 'Synthetic')
    with pytest.raises(ValueError, match='unverified'):
        restored.copy(workspace, hit.ref, hit.revision, 'quote')


def test_epub_bookmarks_restore_logical_sections_without_quotation_authority(tmp_path):
    synthetic_epub = runpy.run_path(str(
        Path(__file__).parents[1] / 'e2e' / 'test_epub_library.py'))['synthetic_epub']
    source = tmp_path / 'source.epub'
    source.write_bytes(synthetic_epub())
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Logical EPUB reading')
    book = app.import_file(workspace, source, title='EPUB reading fixture')
    assert book['state'] == 'complete'
    page = ReadingDesk(app, workspace, book['edition']).page(2)
    assert page.location.kind == 'logical_segment' and page.location.index == 2
    assert page.verified is False
    state = ReadingState(app.root)
    state.remember(page.location)
    bookmark = state.add_bookmark(page.location, 'Exact chapter after the cover')
    folder = tmp_path / 'backup'
    digest = create_legacy_v5_backup(app, folder)
    source.unlink()
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    copied = ReadingState(restored.root)
    assert copied.bookmarks(workspace, book['edition']) == (bookmark,)
    assert ReadingDesk(restored, workspace, book['edition']).validate(
        copied.resume(workspace, book['edition'])) == page
    hit, = restored.search(workspace, 'Залізнична історія')
    with pytest.raises(ValueError, match='unverified'):
        restored.copy(workspace, hit.ref, hit.revision, 'quote')


@pytest.mark.parametrize('exists', [False, True])
def test_absent_and_explicit_empty_state_remain_distinct(tmp_path, exists):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Empty personal state')
    path = app.root / reading_state.FILENAME
    if exists:
        path.write_text(reading_state._encode({}, {}), encoding='utf-8')
    folder = tmp_path / 'backup'
    digest = create_legacy_v5_backup(app, folder)
    manifest = manifest_at(folder)
    assert (manifest.get('reading_state') is not None) == exists
    if exists:
        assert manifest['version'] == 5 and manifest['pending'] is None
        assert manifest['state'] == [] and (folder / 'state').is_dir()
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert (target / reading_state.FILENAME).exists() == exists
    if exists:
        assert (target / reading_state.FILENAME).read_bytes() == path.read_bytes()


@pytest.mark.parametrize('include_generated', [False, True])
def test_v5_restores_reading_state_settings_and_resumable_originals_together(
        populated, tmp_path, include_generated):
    app, workspaces, books, pages, state, bookmarks, topic = populated
    settings = retained_studio_project(app.root)
    cache = retained_generated_cache(app.root)
    sources = [tmp_path / f'pending-{number}.txt' for number in range(2)]
    for number, source in enumerate(sources):
        source.write_text(f'Original pending railway source {number}.', encoding='utf-8')
    planned = app.plan_import_batch(workspaces[0], sources)
    stop = False
    def progress(value):
        nonlocal stop
        stop = value['counts']['complete'] == 1
    partial = app.run_import_batch(workspaces[0], planned['batch_id'],
        cancelled=lambda: stop, progress=progress)
    assert partial['counts']['complete'] == 1 and partial['counts']['queued'] == 1
    before = state.path.read_bytes()
    evidence = app.evidence(workspaces[0], topic)
    folder = tmp_path / 'backup'
    digest = create_legacy_v5_backup(app, folder, include_generated=include_generated)
    manifest = manifest_at(folder)
    assert manifest['version'] == 5
    assert manifest['generated_audio_included'] is include_generated
    assert manifest['reading_state']['sha256'] == sha256(before).hexdigest()
    assert manifest['pending'] is not None
    originals = {entry['filename']: entry for entry in
                 manifest['assets'] + manifest['pending']['assets']}
    assert len(originals) == 3  # the two initial editions share one original
    for source in sources:
        source.unlink()
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    assert restored.evidence(workspaces[0], topic) == evidence
    assert (restored.root / reading_state.FILENAME).read_bytes() == before
    assert (restored.root / settings.relative_to(app.root)).read_bytes() == settings.read_bytes()
    restored_cache = restored.root / cache.relative_to(app.root)
    assert restored_cache.exists() is include_generated
    if include_generated:
        assert restored_cache.read_bytes() == cache.read_bytes()
    assert not (restored.root / 'pending').exists()
    assert {path.name for path in restored.assets_dir.iterdir()} == set(originals)
    for name, entry in originals.items():
        retained = restored.assets_dir / name
        assert sha256(retained.read_bytes()).hexdigest() == entry['sha256']
        assert retained.stat().st_size == entry['size_bytes'] and retained.stat().st_nlink == 1
    saved = ReadingState(restored.root)
    for workspace, book, page, bookmark in zip(workspaces, books, pages, bookmarks):
        assert saved.resume(workspace, book['edition']) == page.location
        assert saved.bookmarks(workspace, book['edition']) == (bookmark,)
        assert ReadingDesk(restored, workspace, book['edition']).validate(page.location) == page
    final = restored.run_import_batch(workspaces[0], planned['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 2
    for previous, current in zip(planned['rows'], final['rows']):
        for key in ('row_id', 'work_id', 'edition_id', 'request_id', 'manifest'):
            assert current[key] == previous[key]
    assert not (restored.root / INCOMPLETE).exists()
    assert (folder / reading_state.FILENAME).read_bytes() == before


@pytest.mark.parametrize('component', ['reading', 'state'])
def test_distinct_historical_v3_shapes_restore_their_exact_component(
        populated, tmp_path, component):
    app, workspaces, books, pages, state, bookmarks, _ = populated
    settings = retained_studio_project(app.root)
    folder = tmp_path / 'historical-v3'
    create_legacy_v5_backup(app, folder)
    manifest = manifest_at(folder)
    manifest['version'] = 3
    manifest.pop('pending')
    shutil.rmtree(folder / 'pending')
    if component == 'reading':
        for name in ('state', 'generated_audio_included'):
            manifest.pop(name)
        shutil.rmtree(folder / 'state')
    else:
        manifest.pop('reading_state')
        (folder / reading_state.FILENAME).unlink()
    digest = replace_manifest(folder, manifest)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    if component == 'reading':
        saved = ReadingState(restored.root)
        assert saved.path.read_bytes() == state.path.read_bytes()
        for workspace, book, page, bookmark in zip(workspaces, books, pages, bookmarks):
            assert saved.resume(workspace, book['edition']) == page.location
            assert saved.bookmarks(workspace, book['edition']) == (bookmark,)
        assert not (restored.root / 'state').exists()
    else:
        assert (restored.root / settings.relative_to(app.root)).read_bytes() == settings.read_bytes()
        assert not (restored.root / reading_state.FILENAME).exists()
    assert restored.search(workspaces[0], 'Перший')[0].quote == 'Перший'


def test_legacy_v3_absent_reading_state_remains_absent(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Historical empty reading state')
    folder = tmp_path / 'legacy'
    create_legacy_v5_backup(app, folder)
    manifest = manifest_at(folder)
    assert manifest['version'] == 2
    manifest.update(version=3, reading_state=None)
    restored = restore_backup(folder, tmp_path / 'restored', replace_manifest(folder, manifest))
    assert not (restored / reading_state.FILENAME).exists()
    assert not (restored / 'state').exists()


@pytest.mark.parametrize('component', ['state', 'pending'])
def test_v5_missing_declared_component_never_publishes_partial_reading_restore(
        populated, tmp_path, component):
    app, _, _, _, state, _, _ = populated
    folder = tmp_path / 'backup'
    digest = create_legacy_v5_backup(app, folder)
    shutil.rmtree(folder / component)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='incomplete'):
        restore_backup(folder, target, digest)
    assert not target.exists()
    assert (folder / reading_state.FILENAME).read_bytes() == state.path.read_bytes()


def test_cancellation_after_valid_reading_snapshot_never_publishes_v5(
        populated, tmp_path, monkeypatch):
    from carr_library import backup_sidecars
    app, _, _, _, state, _, _ = populated
    original = backup_sidecars.read_regular
    stop = False
    def checked_snapshot(*args, **kwargs):
        nonlocal stop
        result = original(*args, **kwargs)
        if Path(args[0]).name == reading_state.FILENAME:
            stop = True
        return result
    monkeypatch.setattr(backup_sidecars, 'read_regular', checked_snapshot)
    target = tmp_path / 'cancelled'
    before = state.path.read_bytes()
    with pytest.raises(InterruptedError):
        create_legacy_v5_backup(app, target, cancelled=lambda: stop)
    assert stop and not target.exists() and state.path.read_bytes() == before


@pytest.mark.parametrize('operation', ['backup', 'restore'])
@pytest.mark.parametrize('field,value', [
    ('workspace', 'unrelated-library'), ('edition', 'unrelated-edition'),
    ('revision', 'unrelated-revision'), ('asset_sha256', '0' * 64),
    ('text_sha256', '0' * 64), ('kind', 'physical_page'), ('index', 999),
])
def test_invalid_source_binding_never_publishes_or_retargets(
        populated, tmp_path, operation, field, value):
    app, _, _, pages, state, _, _ = populated
    invalid = replace(pages[0].location, **{field: value})
    target = tmp_path / 'rejected'
    if operation == 'backup':
        state.add_bookmark(invalid, 'Wrong location must remain an error')
        before = state.path.read_bytes()
        with pytest.raises(ValueError, match='exact saved source'):
            create_legacy_v5_backup(app, target)
        assert state.path.read_bytes() == before
    else:
        folder = tmp_path / 'backup'
        create_legacy_v5_backup(app, folder)
        path = folder / reading_state.FILENAME
        data = json.loads(path.read_bytes())
        data['bookmarks'][0]['location'][field] = value
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        digest = reseal_state(folder, raw)
        before = state.path.read_bytes()
        with pytest.raises(ValueError, match='exact saved source'):
            restore_backup(folder, target, digest)
        assert path.read_bytes() == raw
        assert state.path.read_bytes() == before
    assert not target.exists()


@pytest.mark.parametrize('damage', ['truncated', 'duplicate-key', 'invalid-utf8', 'oversized'])
def test_corrupt_personal_state_cannot_be_omitted_from_backup(populated, tmp_path, damage):
    app, _, _, _, state, _, _ = populated
    raw = state.path.read_bytes()
    if damage == 'truncated':
        raw = raw[:len(raw) // 2]
    elif damage == 'duplicate-key':
        raw = raw.replace(b'"version": 1', b'"version": 1, "version": 1', 1)
    elif damage == 'invalid-utf8':
        raw += b'\xff'
    else:
        raw = b' ' * (reading_state.MAX_BYTES + 1)
    state.path.write_bytes(raw)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_legacy_v5_backup(app, target)
    assert not target.exists()
    assert state.path.read_bytes() == raw


def test_restore_rejects_changed_bytes_even_when_state_still_parses(populated, tmp_path):
    app, _, _, _, state, _, _ = populated
    folder = tmp_path / 'backup'
    digest = create_legacy_v5_backup(app, folder)
    path = folder / reading_state.FILENAME
    path.write_bytes(path.read_bytes() + b' ')
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='sidecar digest or size'):
        restore_backup(folder, target, digest)
    assert not target.exists()


def test_valid_outer_hash_does_not_skip_reading_state_validation(populated, tmp_path):
    app, *_ = populated
    folder = tmp_path / 'backup'
    create_legacy_v5_backup(app, folder)
    raw = b'{"kind":"carr-reading-state","version":99,"positions":[],"bookmarks":[]}'
    digest = reseal_state(folder, raw)
    with pytest.raises(ValueError, match='reading state'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('part,value', [
    ('filename', '../reading-state.json'), ('filename', '/tmp/reading-state.json'),
    ('filename', 'library.sqlite'), ('size_bytes', True), ('size_bytes', 0),
    ('size_bytes', reading_state.MAX_BYTES + 1), ('sha256', 'invalid'),
])
def test_reading_state_manifest_never_selects_an_untrusted_path(populated, tmp_path, part, value):
    app, *_ = populated
    folder = tmp_path / 'backup'
    create_legacy_v5_backup(app, folder)
    manifest = manifest_at(folder)
    manifest['reading_state'][part] = value
    digest = replace_manifest(folder, manifest)
    with pytest.raises(ValueError, match='manifest|sidecar'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('damage', ['missing', 'unexpected', 'legacy-unbound'])
def test_sidecar_presence_must_agree_with_versioned_manifest(populated, tmp_path, damage):
    app, *_ = populated
    folder = tmp_path / 'backup'
    create_legacy_v5_backup(app, folder)
    manifest = manifest_at(folder)
    if damage == 'missing':
        (folder / reading_state.FILENAME).unlink()
    elif damage == 'unexpected':
        manifest['reading_state'] = None
    else:
        manifest['version'] = 2
        del manifest['reading_state']
        # Keep a real legacy envelope while leaving the unbound reader file.
        for name in ('state', 'generated_audio_included', 'pending'):
            manifest.pop(name)
        shutil.rmtree(folder / 'state')
        shutil.rmtree(folder / 'pending')
    digest = replace_manifest(folder, manifest)
    expected = 'reading-state manifest' if damage == 'unexpected' else 'incomplete backup'
    with pytest.raises(ValueError, match=expected):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_linked_reading_state_is_not_read_as_a_private_snapshot(populated, tmp_path, operation):
    app, _, _, _, state, _, _ = populated
    target = tmp_path / 'rejected'
    if operation == 'backup':
        os.link(state.path, tmp_path / 'external-link.json')
        with pytest.raises(ValueError, match='namespace entry|hard links'):
            create_legacy_v5_backup(app, target)
    else:
        folder = tmp_path / 'backup'
        digest = create_legacy_v5_backup(app, folder)
        os.link(folder / reading_state.FILENAME, tmp_path / 'external-link.json')
        with pytest.raises(ValueError, match='namespace entry|hard links'):
            restore_backup(folder, target, digest)
    assert not target.exists()


def test_database_and_personal_state_share_one_writer_snapshot(populated, tmp_path, monkeypatch):
    app, _, _, pages, state, _, _ = populated
    original = Storage.backup
    calls = []

    def competing(storage, target, *, cancelled, maximum_bytes=None):
        assert callable(cancelled)
        with pytest.raises(WriterBusy):
            ReadingState(app.root).add_bookmark(pages[0].location, 'Concurrent update')
        calls.append(True)
        return original(storage, target, cancelled=cancelled, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(Storage, 'backup', competing)
    before = state.path.read_bytes()
    folder = tmp_path / 'backup'
    create_legacy_v5_backup(app, folder)
    assert calls == [True]
    assert state.path.read_bytes() == (folder / reading_state.FILENAME).read_bytes() == before


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_reading_state_publication_failure_never_reports_a_complete_library(
        populated, tmp_path, monkeypatch, operation):
    app, _, _, _, state, _, _ = populated
    folder = tmp_path / 'backup'
    digest = create_legacy_v5_backup(app, folder)
    target = tmp_path / 'interrupted'
    before = state.path.read_bytes()
    original = os.link

    def interrupted(source, destination, *args, **kwargs):
        if Path(destination) == target / reading_state.FILENAME:
            raise OSError('simulated reading-state publication failure')
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, 'link', interrupted)
    with pytest.raises(OSError, match='publication failure'):
        if operation == 'backup':
            create_legacy_v5_backup(app, target)
        else:
            restore_backup(folder, target, digest)
    assert state.path.read_bytes() == before
    if operation == 'backup':
        assert not (target / 'manifest.json').exists()
        with pytest.raises(ValueError, match='incomplete backup'):
            restore_backup(target, tmp_path / 'cannot-restore', digest)
    else:
        assert (target / INCOMPLETE).is_file()
        with pytest.raises(ValueError, match='incomplete'):
            LibraryApp(target)



def create_legacy_v5_backup(app, folder, **kwargs):
    """Derive a historical envelope from a real current typed producer snapshot.

    Current production continues to emit common format4. These independent
    compatibility cases preserve the exact old v5/state/reading discriminator.
    """
    create_backup(app, folder, **kwargs)
    manifest = manifest_at(folder)
    sidecars = manifest.pop('sidecars')
    reading = [entry for entry in sidecars if entry['filename'] == reading_state.FILENAME]
    state = [dict(entry, filename=entry['filename'][6:]) for entry in sidecars
             if entry['filename'].startswith('state/')]
    assert len(reading) + len(state) == len(sidecars)
    if reading:
        manifest.update(version=5, state=state, reading_state=reading[0])
        manifest.setdefault('pending', None)
        (folder / 'state').mkdir(exist_ok=True)
    else:
        assert not sidecars and 'pending' not in manifest
        manifest['version'] = 2
        manifest.pop('generated_audio_included')
    return replace_manifest(folder, manifest)


def retained_studio_project(root):
    from carr_library.studio.project import ProjectStore, parse_script
    store = ProjectStore(root / 'state/studio/projects')
    project = parse_script('Narrator: Retained private production.', project_id='private')
    store.save(project)
    return store.directory(project.id) / 'project.json'


def retained_generated_cache(root):
    import wave
    from carr_library.durable_state import atomic_json
    key = sha256(b'Historical v5 completed PCM fixture').hexdigest()
    path = root / 'state/studio/cache' / (key + '-0123456789.wav')
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 800)
    atomic_json(path.with_name(key + '.json'), {
        'schema': 1, 'key': key, 'file': path.name, 'sha256': sha256(path.read_bytes()).hexdigest(),
        'duration': .1, 'sample_rate': 8000, 'channels': 1})
    return path
