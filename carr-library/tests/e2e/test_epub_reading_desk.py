"""EPUB navigation and the general reading desk share exact source locations."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import runpy

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.anchors import Locator
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.ebook import open_ebook, read_chapter
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState


# Reuse the synthetic archive without depending on pytest adding this folder
# to sys.path; installed-package CI collects tests with importlib mode.
synthetic_epub = runpy.run_path(
    str(Path(__file__).with_name('test_epub_library.py')))['synthetic_epub']


def test_epub_desk_bookmark_resume_round_trip_keeps_logical_source_unverified(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('EPUB reading and research')
    topic = app.create_topic(workspace, 'Research evidence')
    source = tmp_path / 'academic.epub'
    original = synthetic_epub()
    source.write_bytes(original)
    imported = app.import_file(workspace, source, title='Academic EPUB',
                               authors=('Catalog author',))
    assert imported['state'] == 'complete', imported
    assert imported['isolated'] is True and imported['verified'] is False
    edition, revision = imported['edition'], imported['id']

    contents = open_ebook(app, workspace, edition, authorized_topic=topic)
    first_link = next(item for item in contents['toc'] if item.get('ordinal') is not None)
    # The empty cover occupies segment 1. A source page-list hint says "7",
    # but neither that hint nor ZIP/manifest ordering may retarget the chapter.
    assert first_link['ordinal'] == 2
    assert contents['page_hints'][0]['title'] == '7'
    chapter = read_chapter(app, workspace, edition, revision, first_link['ordinal'],
                           authorized_topic=topic)
    desk = ReadingDesk(app, workspace, edition, topic=topic, revision=revision)
    page = desk.page(first_link['ordinal'])
    assert page.raw_text == chapter['raw_text']
    assert 'Залізнична історія — Straße.' in page.raw_text
    assert page.locator == Locator('logical_segment', 2)
    assert page.ordinal == 2 and page.total == 4
    assert page.verified is False and 'unverified' in page.status
    assert page.location.text_sha256 == sha256(page.raw_text.encode('utf-8')).hexdigest()
    assert page.location.asset_sha256 == sha256(original).hexdigest()
    assert app._selections == {}

    # A source backup made before personal reading-state changes provides a
    # real, fresh destination for the explicit reading-state export/import.
    backup = tmp_path / 'source-backup'
    manifest = create_backup(app, backup)
    state = ReadingState(app.root)
    state.remember(page.location)
    bookmark = state.add_bookmark(page.location, 'Повернутися до першого розділу')
    exported = state.export_state(workspace)
    document = json.loads(exported)
    expected_location = asdict(page.location)
    assert document['positions'][0]['location'] == expected_location
    assert document['bookmarks'][0]['location'] == expected_location

    source.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', manifest))
    restored_desk = ReadingDesk(restored, workspace, edition, topic=topic, revision=revision)
    restored_state = ReadingState(restored.root)
    assert restored_state.resume(workspace, edition) is None
    assert restored_state.bookmarks(workspace, edition) == ()
    assert restored_state.import_state(workspace, exported, restored_desk.validate) == 2
    assert restored_state.import_state(workspace, exported, restored_desk.validate) == 0

    reopened_state = ReadingState(restored.root)
    resumed = restored_desk.validate(reopened_state.resume(workspace, edition))
    saved, = reopened_state.bookmarks(workspace, edition)
    bookmarked = restored_desk.validate(saved.location)
    assert saved == bookmark
    assert resumed == bookmarked == page
    assert reopened_state.export_state(workspace) == exported
    assert read_chapter(restored, workspace, edition, revision, 2)['raw_text'] == resumed.raw_text
    assert restored._selections == {}

    # Reading, bookmarking and portable state do not confer quotation approval.
    hit, = restored.search(workspace, 'Залізнична історія')
    assert hit.verified is False and hit.revision == revision
    with pytest.raises(ValueError, match='unverified extraction'):
        restored.copy(workspace, hit.ref, hit.revision, 'quote')
    with pytest.raises(ValueError, match='unverified extraction'):
        restored.save_hit(workspace, topic, hit.ref, hit.revision)
    assert restored.evidence(workspace, topic) == ()
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM source_page_review').fetchone()[0] == 0
