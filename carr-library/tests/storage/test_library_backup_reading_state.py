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
    digest = create_backup(app, folder)
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
    digest = create_backup(app, folder)
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
    digest = create_backup(app, folder)
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
    digest = create_backup(app, folder)
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
    digest = create_backup(app, folder)
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
    settings = app.root / 'state/studio/projects/private.json'
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"roles":["Narrator"],"revision":3}\n')
    cache = app.root / 'state/studio/cache/chunk.wav'
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'synthetic-generated-audio-cache')
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
    digest = create_backup(app, folder, include_generated=include_generated)
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
    assert (restored.root / 'state/studio/projects/private.json').read_bytes() == settings.read_bytes()
    restored_cache = restored.root / 'state/studio/cache/chunk.wav'
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
    settings = app.root / 'state/ui/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"private-setting":"preserved"}\n')
    folder = tmp_path / 'historical-v3'
    create_backup(app, folder)
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
        assert (restored.root / 'state/ui/settings.json').read_bytes() == settings.read_bytes()
        assert not (restored.root / reading_state.FILENAME).exists()
    assert restored.search(workspaces[0], 'Перший')[0].quote == 'Перший'


def test_legacy_v3_absent_reading_state_remains_absent(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Historical empty reading state')
    folder = tmp_path / 'legacy'
    create_backup(app, folder)
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
    digest = create_backup(app, folder)
    shutil.rmtree(folder / component)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='incomplete'):
        restore_backup(folder, target, digest)
    assert not target.exists()
    assert (folder / reading_state.FILENAME).read_bytes() == state.path.read_bytes()


def test_cancellation_after_valid_reading_snapshot_never_publishes_v5(
        populated, tmp_path, monkeypatch):
    from carr_library import library_backup
    app, _, _, _, state, _, _ = populated
    original = library_backup._reading_snapshot
    stop = False
    def checked_snapshot(*args, **kwargs):
        nonlocal stop
        result = original(*args, **kwargs)
        stop = True
        return result
    monkeypatch.setattr(library_backup, '_reading_snapshot', checked_snapshot)
    target = tmp_path / 'cancelled'
    before = state.path.read_bytes()
    with pytest.raises(InterruptedError):
        create_backup(app, target, cancelled=lambda: stop)
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
        with pytest.raises(ValueError, match='exact library source snapshot'):
            create_backup(app, target)
        assert state.path.read_bytes() == before
    else:
        folder = tmp_path / 'backup'
        create_backup(app, folder)
        path = folder / reading_state.FILENAME
        data = json.loads(path.read_bytes())
        data['bookmarks'][0]['location'][field] = value
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        digest = reseal_state(folder, raw)
        before = state.path.read_bytes()
        with pytest.raises(ValueError, match='exact library source snapshot'):
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
        create_backup(app, target)
    assert not target.exists()
    assert state.path.read_bytes() == raw


def test_restore_rejects_changed_bytes_even_when_state_still_parses(populated, tmp_path):
    app, _, _, _, state, _, _ = populated
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    path = folder / reading_state.FILENAME
    path.write_bytes(path.read_bytes() + b' ')
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='reading-state digest or size'):
        restore_backup(folder, target, digest)
    assert not target.exists()


def test_valid_outer_hash_does_not_skip_reading_state_validation(populated, tmp_path):
    app, *_ = populated
    folder = tmp_path / 'backup'
    create_backup(app, folder)
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
    create_backup(app, folder)
    manifest = manifest_at(folder)
    manifest['reading_state'][part] = value
    digest = replace_manifest(folder, manifest)
    with pytest.raises(ValueError, match='reading-state manifest'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('damage', ['missing', 'unexpected', 'legacy-unbound'])
def test_sidecar_presence_must_agree_with_versioned_manifest(populated, tmp_path, damage):
    app, *_ = populated
    folder = tmp_path / 'backup'
    create_backup(app, folder)
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
    expected = 'missing reading-state' if damage == 'unexpected' else 'incomplete backup'
    with pytest.raises(ValueError, match=expected):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_linked_reading_state_is_not_read_as_a_private_snapshot(populated, tmp_path, operation):
    app, _, _, _, state, _, _ = populated
    target = tmp_path / 'rejected'
    if operation == 'backup':
        os.link(state.path, tmp_path / 'external-link.json')
        with pytest.raises(ValueError, match='namespace entry'):
            create_backup(app, target)
    else:
        folder = tmp_path / 'backup'
        digest = create_backup(app, folder)
        os.link(folder / reading_state.FILENAME, tmp_path / 'external-link.json')
        with pytest.raises(ValueError, match='namespace entry'):
            restore_backup(folder, target, digest)
    assert not target.exists()


def test_database_and_personal_state_share_one_writer_snapshot(populated, tmp_path, monkeypatch):
    app, _, _, pages, state, _, _ = populated
    original = Storage.backup
    calls = []

    def competing(storage, target):
        with pytest.raises(WriterBusy):
            ReadingState(app.root).add_bookmark(pages[0].location, 'Concurrent update')
        calls.append(True)
        return original(storage, target)

    monkeypatch.setattr(Storage, 'backup', competing)
    before = state.path.read_bytes()
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    assert calls == [True]
    assert state.path.read_bytes() == (folder / reading_state.FILENAME).read_bytes() == before


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_reading_state_publication_failure_never_reports_a_complete_library(
        populated, tmp_path, monkeypatch, operation):
    app, _, _, _, state, _, _ = populated
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
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
            create_backup(app, target)
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
