"""Actual retained sources and bounded explicit file captures for translation batches."""
from dataclasses import asdict
from pathlib import Path
import runpy

import pytest

from carr_library.assistant.translation_source import capture_source, source_options, validate_source, read_text_file
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.source import read_source_range

_fixture = runpy.run_path(str(Path(__file__).with_name('translation_batch_fixtures.py')))
transport, source_library = (_fixture[name] for name in ('transport', 'source_library'))


def test_source_capture_reuses_exact_reading_positions_and_original_bytes(source_library):
    e = source_library
    selected = capture_source(e.app, e.workspace, e.imported['edition'], topic=e.topic)
    desk = ReadingDesk(e.app, e.workspace, e.imported['edition'], topic=e.topic)
    original = read_source_range(desk, 1, len(desk.pages)).reading_document()
    assert selected['text'] == original.source_text
    assert selected['source_anchor'] == original.source_anchor
    assert selected['source_metadata'] == asdict(desk.metadata)
    assert selected['source_anchor']['quotation_authority'] is False
    validate_source(e.app, selected['text'], selected['source_anchor'], selected['source_metadata'], e.topic)
    assert e.app.assets.resolve(e.imported['asset']).read_bytes() == e.original.encode('utf-8')
    second = capture_source(e.app, e.workspace, e.imported['edition'], topic=e.topic, first=2, last=2)
    assert second['text'] == e.original.split('\f')[1]


def test_actual_epub_chapter_uses_retained_spine_order(source_library, tmp_path):
    e = source_library
    factory = runpy.run_path(str(Path(__file__).parents[1] / 'reader' / 'test_ebook_reader.py'))['book']
    source = tmp_path / 'chapters.epub'
    factory(source)
    imported = e.app.import_file(e.workspace, source, title='Reviewed EPUB', authors=('Source author',))
    assert imported['state'] == 'complete'
    selected = capture_source(e.app, e.workspace, imported['edition'], topic=e.topic, chapter=2)
    assert 'Archive context.' in selected['text'] and 'Railway research.' not in selected['text']
    assert selected['source_anchor']['positions'][0]['ordinal'] == 2
    labels = source_options(e.app, e.workspace, imported['edition'], topic=e.topic)
    assert labels['positions'][1].startswith('EPUB chapter 2:')
    assert e.app.assets.resolve(selected['source_anchor']['asset_sha256']).read_bytes() == source.read_bytes()


@pytest.mark.parametrize('change', ['revision', 'workspace', 'topic', 'original'])
def test_source_capture_and_validation_preserve_revocation(source_library, change):
    e = source_library
    selected = capture_source(e.app, e.workspace, e.imported['edition'], topic=e.topic)
    if change == 'revision':
        e.app.update_metadata(e.workspace, e.imported['edition'], title='Changed retained title')
    elif change == 'workspace':
        e.app.archive_workspace(e.workspace)
    elif change == 'topic':
        with e.app.topic_transaction(e.workspace) as tree:
            tree.archive(e.topic)
    else:
        e.app.assets.resolve(e.imported['asset']).write_bytes(b'Changed original bytes')
    with pytest.raises((ValueError, PermissionError)):
        validate_source(e.app, selected['text'], selected['source_anchor'], selected['source_metadata'], e.topic)


def test_explicit_file_capture_preserves_crlf_unicode_and_does_not_reopen_later(tmp_path):
    source = tmp_path / 'exact.txt'
    raw = 'Україна\r\nSecond line. 🙂\r\n'.encode('utf-8')
    source.write_bytes(raw)
    captured = read_text_file(source)
    source.write_bytes(b'Replaced after capture')
    assert captured.encode('utf-8') == raw


@pytest.mark.parametrize('kind', ['oversized', 'hardlink', 'bad-utf8', 'cancelled'])
def test_explicit_file_capture_fails_closed(tmp_path, kind):
    import os
    source = tmp_path / 'input.txt'
    source.write_bytes(b'Original text')
    if kind == 'oversized':
        with source.open('r+b') as stream:
            stream.truncate(8_000_001)
    elif kind == 'hardlink':
        os.link(source, tmp_path / 'alias.txt')
    elif kind == 'bad-utf8':
        source.write_bytes(b'\xff\xfe')
    with pytest.raises((ValueError, InterruptedError)):
        read_text_file(source, cancelled=lambda: kind == 'cancelled')
