"""Explicit native source handoff: real imports, SQLite scope and source bytes."""
from dataclasses import replace
from hashlib import sha256
import os
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.source_file import prepare_source_file, launch_source_file


@pytest.fixture
def source(tmp_path):
    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('Source navigation')
    collection = app.create_collection(workspace, 'Selected sources')
    original = tmp_path / 'Оригінал книги.txt'
    raw = 'Передмова\r\nHistorical railway source.\fSecond logical segment.'
    original.write_bytes(raw.encode('utf-8'))
    imported = app.import_file(workspace, original, title='Source title', collections=(collection,))
    hit, = app.search(workspace, 'railway', collection=collection)
    return app, workspace, collection, original, imported, hit, raw.encode('utf-8')


def test_exact_retained_original_is_opened_as_a_separate_private_copy(source):
    app, workspace, _, original, imported, hit, raw = source
    original.unlink()
    selected = prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert selected.path.read_bytes() == raw
    assert selected.identity.asset_sha256 == sha256(raw).hexdigest()
    assert selected.locator.kind == 'logical_segment'
    assert selected.locator.index == 1
    assert selected.title == 'Source title'
    retained = app.assets.resolve(imported['asset'])
    assert not os.path.samefile(selected.path, retained)
    assert selected.path.parent == app.root / 'source-views'
    assert selected.path == prepare_source_file(app, workspace, hit.ref, hit.revision).path
    launches = []
    launch_source_file(app, selected, lambda path: launches.append(path) is None)
    assert launches == [str(selected.path)]


@pytest.mark.parametrize('edited', [b'An edited viewing copy.', b'\x00\x01\xff'])
def test_viewer_edits_never_change_the_original_and_are_not_overwritten(source, edited):
    app, workspace, _, _, imported, hit, raw = source
    selected = prepare_source_file(app, workspace, hit.ref, hit.revision)
    selected.path.write_bytes(edited)
    replacement = prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert replacement.path != selected.path
    assert selected.path.read_bytes() == edited
    assert replacement.path.read_bytes() == raw
    assert app.assets.resolve(imported['asset']).read_bytes() == raw
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway'


def test_pdf_preview_can_open_original_without_granting_review(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    pdf = tmp_path / 'unverified.pdf'
    canvas = Canvas(str(pdf))
    canvas.drawString(72, 700, 'Synthetic railway source requiring independent review')
    canvas.save()
    app = LibraryApp(tmp_path / 'pdf-library')
    workspace = app.create_workspace('PDF sources')
    result = app.import_file(workspace, pdf)
    assert result['state'] == 'complete', result
    hit, = app.search(workspace, 'railway')
    assert not hit.verified
    selected = prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert selected.mime == 'application/pdf' and selected.locator.index == 1
    assert selected.path.read_bytes() == pdf.read_bytes()
    launches = []
    launch_source_file(app, selected, lambda path: launches.append(path) is None)
    assert launches == [str(selected.path)]
    with pytest.raises(ValueError, match='unverified'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM source_page_review').fetchone()[0] == 0


def test_docx_copy_retains_format_and_is_not_citation_approval(tmp_path):
    from docx import Document
    path = tmp_path / 'book.docx'
    doc = Document()
    doc.add_paragraph('Synthetic railway source')
    doc.save(path)
    app = LibraryApp(tmp_path / 'docx-library')
    workspace = app.create_workspace('DOCX sources')
    assert app.import_file(workspace, path)['state'] == 'complete'
    hit, = app.search(workspace, 'railway')
    selected = prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert selected.path.suffix == '.docx'
    assert selected.path.read_bytes() == path.read_bytes()
    assert selected.locator.kind == 'logical_segment'
    with pytest.raises(ValueError, match='unverified'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')


def test_foreign_scope_or_stale_selection_rejected_before_reading_assets(source, monkeypatch):
    app, workspace, _, _, _, hit, _ = source
    other = app.create_workspace('Another library')
    def forbidden(*args, **kwargs):
        pytest.fail('Unauthorized request reached original bytes')
    monkeypatch.setattr('carr_library.reader.source_file.RetainedAssets.resolve', forbidden)
    for scope, ref, revision in [(other, hit.ref, hit.revision),
                                 (workspace, 'stale-ref', hit.revision),
                                 (workspace, hit.ref, 'another-revision')]:
        with pytest.raises(ValueError):
            prepare_source_file(app, scope, ref, revision)


@pytest.mark.parametrize('revoke', ['membership', 'workspace', 'collection'])
def test_authority_is_rechecked_between_preparation_and_launch(source, revoke):
    app, workspace, collection, _, imported, hit, _ = source
    selected = prepare_source_file(app, workspace, hit.ref, hit.revision)
    if revoke == 'workspace':
        app.archive_workspace(workspace)
    else:
        with app.catalog_transaction(workspace) as catalog:
            if revoke == 'membership':
                catalog.remove(collection, imported['edition'])
            else:
                catalog.archive(collection)
    launched = []
    with pytest.raises(ValueError):
        launch_source_file(app, selected, lambda path: launched.append(path) is None)
    assert launched == []


def test_archived_bound_topic_is_rechecked_before_native_launch(source,monkeypatch):
    app,workspace,_,_,_,hit,_ = source
    topic = app.create_topic(workspace,'Bound research topic')
    selected = prepare_source_file(app,workspace,hit.ref,hit.revision,authorized_topic=topic)
    with app.topic_transaction(workspace) as tree:
        tree.archive(topic)
    launched = []
    def forbidden(*args,**kwargs):
        pytest.fail('An archived topic reached original bytes')
    monkeypatch.setattr('carr_library.reader.source_file._original',forbidden)
    with pytest.raises(ValueError,match='archived'):
        launch_source_file(app,selected,lambda path: launched.append(path) is None,authorized_topic=topic)
    with pytest.raises(ValueError,match='archived'):
        prepare_source_file(app,workspace,hit.ref,hit.revision,authorized_topic=topic)
    assert launched == []


def test_cancel_before_preparation_creates_no_viewing_copy(source):
    app,workspace,_,_,_,hit,_ = source
    with pytest.raises(InterruptedError,match='cancelled'):
        prepare_source_file(app,workspace,hit.ref,hit.revision,cancelled=lambda:True)
    assert not (app.root / 'source-views').exists()


def test_cancel_after_copy_verification_removes_only_the_new_copy(source,monkeypatch):
    from carr_library.reader import source_file
    app,workspace,_,_,imported,hit,raw = source
    cancelled = Event()
    check = source_file._view_matches
    def cancel_after_check(*args, **options):
        result = check(*args, **options)
        cancelled.set()
        return result
    monkeypatch.setattr(source_file,'_view_matches',cancel_after_check)
    with pytest.raises(InterruptedError,match='cancelled'):
        prepare_source_file(app,workspace,hit.ref,hit.revision,cancelled=cancelled.is_set)
    assert not tuple((app.root / 'source-views').iterdir())
    assert app.assets.resolve(imported['asset']).read_bytes() == raw


def test_cancellation_is_rechecked_at_the_native_launch_boundary(source,monkeypatch):
    from carr_library.reader import source_file
    app,workspace,_,_,_,hit,raw = source
    selected = prepare_source_file(app,workspace,hit.ref,hit.revision)
    cancelled = Event()
    check = source_file._view_matches
    def cancel_after_check(*args, **options):
        result = check(*args, **options)
        cancelled.set()
        return result
    monkeypatch.setattr(source_file,'_view_matches',cancel_after_check)
    launched = []
    with pytest.raises(InterruptedError,match='cancelled'):
        launch_source_file(app,selected,lambda path: launched.append(path) is None,cancelled=cancelled.is_set)
    assert launched == [] and selected.path.read_bytes() == raw


def test_metadata_change_preserves_selected_historical_source(source):
    app, workspace, _, _, imported, hit, raw = source
    app.update_metadata(workspace, imported['edition'], title='New title', authors=('New author',))
    selected = prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert selected.identity.revision == hit.revision and selected.title == 'Source title'
    assert selected.path.read_bytes() == raw


@pytest.mark.parametrize('damage', ['suffix', 'corrupt', 'missing', 'symlink', 'hardlink', 'ambiguous'])
def test_invalid_retained_source_is_never_launched(source, tmp_path, damage):
    app, workspace, _, original, imported, hit, _ = source
    path = app.assets.resolve(imported['asset'])
    if damage == 'suffix':
        path.rename(path.with_suffix('.pdf'))
    elif damage == 'corrupt':
        path.write_bytes(b'Tampered text')
    elif damage == 'missing':
        path.unlink()
    elif damage == 'symlink':
        path.unlink()
        path.symlink_to(original)
    elif damage == 'hardlink':
        os.link(path, tmp_path / 'another-link.txt')
    else:
        path.with_suffix('.pdf').write_bytes(path.read_bytes())
    with pytest.raises((ValueError, OSError)):
        prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert not (app.root / 'source-views').exists()


def test_view_directory_symlink_is_rejected(source, tmp_path):
    app, workspace, _, _, _, hit, _ = source
    external = tmp_path / 'external'
    external.mkdir()
    (app.root / 'source-views').symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError):
        prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert list(external.iterdir()) == []


@pytest.mark.parametrize('damage', ['bytes', 'path', 'link'])
def test_changed_view_copy_is_rejected_at_native_launch(source, tmp_path, damage):
    app, workspace, _, _, _, hit, _ = source
    selected = prepare_source_file(app, workspace, hit.ref, hit.revision)
    if damage == 'bytes':
        selected.path.write_bytes(b'Changed viewing bytes')
    elif damage == 'path':
        selected = replace(selected, path=tmp_path / 'elsewhere.txt')
    else:
        selected.path.unlink()
        selected.path.symlink_to(app.assets.resolve(selected.identity.asset_sha256))
    calls = []
    with pytest.raises(ValueError):
        launch_source_file(app, selected, lambda path: calls.append(path) is None)
    assert calls == []


def test_launcher_failure_is_readable_and_keeps_source_unchanged(source):
    app, workspace, _, _, imported, hit, raw = source
    selected = prepare_source_file(app, workspace, hit.ref, hit.revision)
    with pytest.raises(ValueError, match='No application could open this file'):
        launch_source_file(app, selected, lambda path: False)
    assert app.assets.resolve(imported['asset']).read_bytes() == raw
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway'


def test_cancel_cleanup_preserves_a_real_replacement_of_its_created_copy(source, monkeypatch):
    from carr_library.reader import source_file
    app, workspace, _, _, imported, hit, raw = source
    cancelled = Event()
    check = source_file._view_matches
    replacement_bytes = b'Independent editor replacement; do not remove.'
    changed = []

    def replace_after_verification(directory, path, original, **options):
        result = check(directory, path, original, **options)
        assert result
        replacement = directory / 'editor-replacement.txt'
        replacement.write_bytes(replacement_bytes)
        os.replace(replacement, path)
        changed.append(path)
        cancelled.set()
        return result

    monkeypatch.setattr(source_file, '_view_matches', replace_after_verification)
    with pytest.raises(InterruptedError, match='cancelled'):
        prepare_source_file(app, workspace, hit.ref, hit.revision, cancelled=cancelled.is_set)
    assert len(changed) == 1 and changed[0].read_bytes() == replacement_bytes
    assert tuple((app.root / 'source-views').iterdir()) == tuple(changed)
    assert app.assets.resolve(imported['asset']).read_bytes() == raw


def test_exclusive_copy_creation_preserves_a_concurrent_name_collision(source, monkeypatch):
    from pathlib import Path
    app, workspace, _, _, imported, hit, raw = source
    destination = app.root / 'source-views' / (imported['asset'] + '.txt')
    real_open = Path.open
    replacement_bytes = b'Already owned by another viewing operation.'
    observed = []

    def collide(path, mode='r', *args, **kwargs):
        if path == destination and mode == 'xb':
            with real_open(path, 'xb') as stream:
                stream.write(replacement_bytes)
            observed.append(path)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', collide)
    with pytest.raises(FileExistsError):
        prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert observed == [destination] and destination.read_bytes() == replacement_bytes
    assert app.assets.resolve(imported['asset']).read_bytes() == raw
