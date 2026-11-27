"""Full backup and source-bound recovery of completed live-reading cursors."""
from dataclasses import replace

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.reader.live_speech import SpeechPositionStore, SpeechSelection, SpeechSession, SpeechSettings
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState


def test_live_reading_checkpoint_roundtrips_source_page_and_exact_text_cursor(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact live reading')
    book = app.add_text(workspace, 'Original', 'First page.\fДругий текст 😀. Still original.')
    page = ReadingDesk(app, workspace, book['edition']).page(2)
    session = SpeechSession(app.root, page.raw_text, SpeechSettings('fixture:voice', 'en'),
                            SpeechSelection(0, len(page.raw_text)), location=page.location)
    try:
        session.cursor = 14
        session.checkpoint()
    finally:
        session.close()
    before = SpeechPositionStore(app.root).path.read_bytes()
    backup = tmp_path / 'snapshot'
    digest = create_backup(app, backup)
    verify_backup(backup, digest)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    store = SpeechPositionStore(restored.root)
    assert store.path.read_bytes() == before and store.path.stat().st_nlink == 1
    assert store.load(page.raw_text, page.location)['cursor'] == 14
    assert ReadingState(restored.root).resume(workspace, book['edition']) == page.location
    assert ReadingDesk(restored, workspace, book['edition']).validate(page.location) == page
    assert not (backup / 'carr-live-reading').exists()
    assert ReadingDesk(app, workspace, book['edition']).page(2) == page


@pytest.mark.parametrize('field,value', [('workspace', 'another-library'), ('edition', 'another-book'),
                                        ('revision', 'another-revision'), ('index', 999),
                                        ('asset_sha256', '0' * 64)])
def test_outer_backup_rejects_typed_but_foreign_live_reading_source_binding(tmp_path, field, value):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact live reading')
    book = app.add_text(workspace, 'Original', 'Exact source text.')
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    SpeechPositionStore(app.root).remember(page.raw_text, 1, SpeechSettings('fixture:voice', 'en'),
                                          replace(page.location, **{field: value}))
    with pytest.raises(ValueError, match='location disagrees'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected' / 'manifest.json').exists()


def test_new_revision_does_not_retarget_a_restored_historical_cursor(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact live reading')
    book = app.add_text(workspace, 'Original', 'Exact source text.')
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    SpeechPositionStore(app.root).remember(page.raw_text, 5, SpeechSettings('fixture:voice', 'en'), page.location)
    app.update_metadata(workspace, book['edition'], title='Changed title')
    digest = create_backup(app, tmp_path / 'snapshot')
    restored = LibraryApp(restore_backup(tmp_path / 'snapshot', tmp_path / 'restored', digest))
    new_page = ReadingDesk(restored, workspace, book['edition']).page(1)
    assert new_page.location.revision != page.location.revision
    store = SpeechPositionStore(restored.root)
    assert store.load(new_page.raw_text, new_page.location) is None
    assert store.load(page.raw_text, page.location)['cursor'] == 5
