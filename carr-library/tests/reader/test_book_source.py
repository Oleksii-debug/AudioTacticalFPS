"""Shared source authority on actual retained originals and current SQLite."""
from dataclasses import asdict
from hashlib import sha256

import pytest

from carr_library.application import LibraryApp
from carr_library.reader import book_source as source
from carr_library.reader.reading_desk import ReadingDesk
from tests.reader.test_reading_desk import _mutate_page
from tests.reader.test_ebook_contents import _published


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Книжки ü')
    raw = 'Перша\r\nсторінка [sound unsafe.wav]\f\fОстання сторінка. 字。'
    first = app.add_text(workspace, 'Перша книжка', raw, authors=('Автор',), language='uk')
    second = app.add_text(workspace, 'Second book', 'Second exact text.', authors=('Other',), language='en')
    assert first['state'] == second['state'] == 'complete'
    return app, workspace, first, second, raw


def test_exact_pages_order_blank_segments_and_non_authorizing_lineage(library):
    app, workspace, first, _, raw = library
    captured = source.capture_source(app, workspace, first['edition'])
    pages = source.source_pages(captured)
    assert [row['raw_text'] for row in pages] == raw.split('\f')
    assert [row['ordinal'] for row in pages] == [1, 2, 3]
    assert captured['text'] == '\n\n'.join(raw.split('\f'))
    assert captured['source_anchor']['quotation_authority'] is False
    assert app._selections == {}
    for row in pages:
        actual = ReadingDesk(app, workspace, first['edition']).page(row['ordinal'])
        assert row['location'] == asdict(actual.location)
        assert captured['text'][row['source_start']:row['source_end']] == row['raw_text']
    assert source.validate_source(app, captured['text'], captured['source_anchor'],
                                  captured['source_metadata']) == captured


def test_selected_order_and_exact_ranges_are_not_sorted_or_deduplicated(library):
    app, workspace, first, second, _ = library
    result = source.capture_sources(app, [
        dict(workspace=workspace, edition=second['edition'], kind='document'),
        dict(workspace=workspace, edition=first['edition'], first=2, last=3,
             revision=ReadingDesk(app, workspace, first['edition']).revision)])
    assert [row['title'] for row in result] == ['Second book', 'Перша книжка']
    assert [row['ordinal'] for row in source.source_pages(result[1])] == [2, 3]
    with pytest.raises(ValueError, match='repeats'):
        source.capture_sources(app, [dict(workspace=workspace, edition=first['edition'])] * 2)


def test_selected_chapter_requires_real_epub_inventory(tmp_path):
    app, workspace, revision, _, extracted, _ = _published(tmp_path, enhanced=True)
    result, = source.capture_sources(app, [dict(workspace=workspace, edition='edition',
        kind='chapter', first=2, revision=revision)])
    assert result['text'] == extracted['records'][1]['raw_text']
    assert result['source_anchor']['positions'][0]['ordinal'] == 2
    assert not result['source_anchor']['quotation_authority']
    assert result['warnings']


def test_plain_text_segment_is_not_mislabelled_an_epub_chapter(library):
    app, workspace, first, _, _ = library
    with pytest.raises(ValueError):
        source.capture_sources(app, [dict(workspace=workspace, edition=first['edition'],
                                          kind='chapter', first=1)])


@pytest.mark.parametrize('change', ['revision', 'original', 'workspace', 'topic'])
def test_current_source_revalidation_refuses_drift_and_revocation(library, change):
    app, workspace, first, _, _ = library
    topic = app.create_topic(workspace, 'Explicit scope')
    value = source.capture_source(app, workspace, first['edition'], topic=topic)
    if change == 'revision':
        app.update_metadata(workspace, first['edition'], title='Changed title')
    elif change == 'original':
        path = app.assets.resolve(first['asset'])
        path.write_bytes(path.read_bytes() + b'changed')
    elif change == 'workspace':
        app.archive_workspace(workspace)
    else:
        with app.topic_transaction(workspace) as tree:
            tree.archive(topic)
    with pytest.raises((ValueError, RuntimeError)):
        source.validate_source(app, value['text'], value['source_anchor'], value['source_metadata'], topic)
    assert app._selections == {}


def test_earlier_source_is_rechecked_after_last_capture(library, monkeypatch):
    app, workspace, first, second, _ = library
    original, changed = ReadingDesk.page, [False]
    def change_first_after_second(desk, ordinal):
        page = original(desk, ordinal)
        if desk.edition == second['edition'] and not changed[0]:
            changed[0] = True
            app.update_metadata(workspace, first['edition'], title='Changed while other source opened')
        return page
    monkeypatch.setattr(ReadingDesk, 'page', change_first_after_second)
    with pytest.raises(ValueError, match='active|revision'):
        source.capture_sources(app, [dict(workspace=workspace, edition=book['edition'])
                                     for book in (first, second)])
    assert changed[0]


def test_combined_character_limit_is_complete_or_fail(library):
    app, workspace, first, second, _ = library
    before = app.assets.resolve(first['asset']).read_bytes()
    with pytest.raises(ValueError, match='character budget'):
        source.capture_sources(app, [dict(workspace=workspace, edition=book['edition'])
            for book in (first, second)], max_total_characters=20)
    assert app.assets.resolve(first['asset']).read_bytes() == before
    assert app._selections == {}


@pytest.mark.parametrize('bad', [0, True, 33, 1.5], ids=['zero', 'boolean', 'over-limit', 'fraction'])
def test_document_count_rejected_before_reading(bad):
    with pytest.raises(ValueError):
        source.capture_sources(None, [{}], max_documents=bad)


def test_cancellation_after_a_page_never_returns_partial_capture(library, monkeypatch):
    app, workspace, first, _, _ = library
    original, stopped = ReadingDesk.page, [False]
    def cancel(desk, ordinal):
        page = original(desk, ordinal)
        stopped[0] = True
        return page
    monkeypatch.setattr(ReadingDesk, 'page', cancel)
    with pytest.raises(InterruptedError):
        source.capture_source(app, workspace, first['edition'], cancelled=lambda: stopped[0])


def test_guard_time_is_inside_inclusive_shared_deadline(monkeypatch):
    clock = [0]
    monkeypatch.setattr(source, 'monotonic_ns', lambda: clock[0])
    def consume():
        clock[0] = 1_000_000
    with pytest.raises(TimeoutError):
        source.capture_sources(None, [dict(workspace='w', edition='e')],
                               timeout=.001, guard=consume)


def test_fragments_validate_exact_page_offsets_without_whole_snapshot_repetition(library):
    app, workspace, first, _, _ = library
    captured = source.capture_source(app, workspace, first['edition'])
    pages = source.source_pages(captured)
    origins = [dict(location=pages[2]['location'], ordinal=3, start=0, end=8),
               dict(location=pages[0]['location'], ordinal=1, start=1, end=5)]
    key = (workspace, first['edition'], captured['source_anchor']['revision'])
    assert source.validate_fragments(app, origins, metadata={key: captured['source_metadata']}) == (
        pages[2]['raw_text'][:8], pages[0]['raw_text'][1:5])
    assert app._selections == {}
    with pytest.raises(ValueError, match='differs'):
        source.validate_fragments(app, [dict(origins[0], text='A changed claimed fragment')])
    with pytest.raises(ValueError, match='metadata'):
        source.validate_fragments(app, origins, metadata={key: {**captured['source_metadata'], 'title': 'Other'}})


def test_fragment_final_recheck_checks_every_selected_page(library, monkeypatch):
    app, workspace, first, _, _ = library
    captured = source.capture_source(app, workspace, first['edition'])
    pages = source.source_pages(captured)
    origins = [dict(location=row['location'], ordinal=row['ordinal'], start=0, end=len(row['raw_text']))
               for row in (pages[0], pages[2])]
    original, changed = ReadingDesk.page, [False]
    def change_first_after_last(desk, ordinal):
        page = original(desk, ordinal)
        if ordinal == 3 and not changed[0]:
            changed[0] = True
            _mutate_page(app, desk.revision, 'UPDATE page SET raw=? WHERE ordinal=1 AND revision=?',
                         ('Different stored bytes',))
        return page
    monkeypatch.setattr(ReadingDesk, 'page', change_first_after_last)
    with pytest.raises(ValueError, match='damaged|changed'):
        source.validate_fragments(app, origins)


def test_unselected_oversized_page_is_not_an_arbitrary_revalidation_surrogate(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected fragments')
    book = app.add_text(workspace, 'Two pages', 'x' * 100 + '\fshort')
    monkeypatch.setattr('carr_library.reader.reading_desk.MAX_PAGE_CHARACTERS', 5)
    captured = source.capture_source(app, workspace, book['edition'], first=2, last=2)
    page, = source.source_pages(captured)
    origin = dict(location=page['location'], ordinal=2, start=0, end=5)
    assert source.validate_fragments(app, [origin]) == ('short',)


def test_captured_approval_flag_never_becomes_quotation_authority(library):
    app, workspace, first, _, _ = library
    captured = source.capture_source(app, workspace, first['edition'])
    captured['source_anchor']['positions'][0]['text_verified_at_capture'] = False
    current = source.validate_source(app, captured['text'], captured['source_anchor'],
                                     captured['source_metadata'])
    assert current['source_anchor']['quotation_authority'] is False
    forged = {**captured['source_anchor'], 'quotation_authority': True}
    with pytest.raises(ValueError):
        source.source_context(captured['text'], forged, captured['source_metadata'])


def test_explicit_source_location_range_is_distinct_from_epub_chapter(library):
    app, workspace, first, _, raw = library
    selected, = source.capture_sources(app, [dict(workspace=workspace, edition=first['edition'],
        kind='range', first=2, last=3)])
    assert selected['text'] == '\n\n'.join(raw.split('\f')[1:])
    assert [row['ordinal'] for row in source.source_pages(selected)] == [2, 3]
    with pytest.raises(ValueError, match='explicit first and last'):
        source.capture_sources(app, [dict(workspace=workspace, edition=first['edition'],
            kind='range', first=2)])
