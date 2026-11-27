"""Compatibility of additive contents with real canonical EPUB revisions."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import runpy
import sqlite3
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.catalog.intake import snapshot
from carr_library.catalog.registration import Registration
from carr_library.extract.epub_ordered import extract_epub
from carr_library.ingest.pipeline import canonical
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.reader import ebook_contents
from carr_library.reader.ebook import list_ebooks, read_ebook_section
from carr_library.reader.ebook_contents import extraction_settings, open_ebook, read_chapter
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.storage.revisions import Page, Storage, CURRENT_SCHEMA_VERSION


write_book = runpy.run_path(str(Path(__file__).with_name('test_ebook_reader.py')))['book']


def _legacy_settings(extracted):
    """The exact settings layout used before contents metadata was introduced."""
    return dict(adapter='epub-spine-v1', kind='logical_segment',
        provenance=extracted['provenance'],
        chapters=[record['logical_locator'] for record in extracted['records']],
        alternatives=[record.get('alternatives', []) for record in extracted['records']],
        warnings=extracted['warnings'])


def _published(tmp_path, *, enhanced, mutate=None, revision_override=None, source_bytes=None):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Contents compatibility')
    source = tmp_path / 'source.epub'
    if source_bytes is None:
        write_book(source)
    else:
        source.write_bytes(source_bytes)
    asset = snapshot(source.parent, source.name)
    extracted = extract_epub(asset.data)
    settings = json.loads(extraction_settings(extracted)) if enhanced else _legacy_settings(extracted)
    if mutate is not None:
        mutate(settings)
    config = canonical(settings)
    revision = revision_override or sha256(canonical(
        [workspace, 'edition', asset.digest, config]).encode('utf-8')).hexdigest()
    record = Registration('work', 'edition', asset.manifest(), 'Catalog title',
                          ('Catalog author',), 'uk', None, None, ())
    with exclusive_writer(app.root):
        app.assets.retain(asset.data, asset.mime, asset.digest)
        with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
            _CatalogPublisher(storage, record).publish_revision(workspace, 'edition', revision,
                asset.digest, config, tuple(Page(item['raw_text']) for item in extracted['records']))
    return app, workspace, revision, source, extracted, config


def test_new_settings_preserve_canonical_contract_and_add_navigation(tmp_path):
    source = tmp_path / 'source.epub'
    write_book(source)
    extracted = extract_epub(source.read_bytes())
    before = deepcopy(extracted)
    settings = json.loads(extraction_settings(extracted))
    for key, value in _legacy_settings(extracted).items():
        assert settings[key] == value
    assert extracted == before
    assert settings['ebook']['toc'] == extracted['toc']
    assert settings['ebook']['page_hints'] == extracted['page_hints']
    for record, descriptor in zip(extracted['records'], settings['ebook']['chapters']):
        assert descriptor['text_sha256'] == record['text_sha256']
        assert descriptor['characters'] == len(record['raw_text'])
        assert descriptor['headings'] == record['headings']
    assert settings['ebook']['chapters'][0]['image_descriptions'] == [
        'Авторський опис діаграми джерел.']
    assert 'Авторський опис' not in extracted['records'][0]['raw_text']


def test_old_import_has_chapter_fallback_without_reparse_or_settings_rewrite(tmp_path, monkeypatch):
    app, workspace, revision, _, extracted, config = _published(tmp_path, enhanced=False)

    def forbidden(*args, **kwargs):
        pytest.fail('Opening existing contents must not parse the original EPUB again.')

    monkeypatch.setattr('carr_library.extract.epub_ordered.extract_epub', forbidden)
    contents = open_ebook(app, workspace, 'edition')
    assert 'embedded_contents_unavailable' in contents['warnings']
    assert contents['page_hints'] == [] and contents['metadata_hints']['titles'] == []
    assert contents['toc'] == [dict(title=record['logical_locator']['title'],
        ordinal=number, offset=0, depth=0) for number, record in enumerate(extracted['records'], 1)]
    assert all(not chapter['headings'] for chapter in contents['chapters'])
    assert read_chapter(app, workspace, 'edition', revision, 1)['raw_text'] == extracted['records'][0]['raw_text']
    with app.read(workspace) as db:
        assert db.execute('SELECT config FROM revision WHERE id=?', (revision,)).fetchone()[0] == config
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


@pytest.mark.parametrize('enhanced', [False, True])
def test_contents_canonical_section_api_and_generic_reader_agree(tmp_path, enhanced):
    app, workspace, revision, _, extracted, _ = _published(tmp_path, enhanced=enhanced)
    contents = open_ebook(app, workspace, 'edition')
    summary, = list_ebooks(app, workspace)
    assert summary.revision == revision and summary.title == contents['title'] == 'Catalog title'
    desk = ReadingDesk(app, workspace, 'edition')
    for ordinal, record in enumerate(extracted['records'], 1):
        new = read_chapter(app, workspace, 'edition', revision, ordinal)
        legacy = read_ebook_section(app, workspace, 'edition', revision, ordinal)
        ordinary = desk.page(ordinal)
        assert new['raw_text'] == legacy.raw_text == ordinary.raw_text == record['raw_text']
        assert new['verified'] is legacy.verified is ordinary.verified is False
        assert new['member'] == legacy.resource == record['logical_locator']['href']
        assert new['image_descriptions'] == [text for _, text in legacy.alternatives]
    assert app._selections == {}


@pytest.mark.parametrize('enhanced', [False, True])
def test_metadata_uuid_revisions_keep_both_reader_apis_compatible(tmp_path, enhanced):
    app, workspace, revision, _, extracted, config = _published(tmp_path, enhanced=enhanced)
    historical = read_ebook_section(app, workspace, 'edition', revision, 1)
    previous = revision
    for title in ('Corrected catalog title', 'Second correction'):
        current = app.update_metadata(workspace, 'edition', title=title, authors=('New author',))
        assert current != previous
        with pytest.raises(ValueError, match='revision changed'):
            read_chapter(app, workspace, 'edition', previous, 1)
        assert read_ebook_section(app, workspace, 'edition', revision, 1) == historical
        contents = open_ebook(app, workspace, 'edition')
        assert contents['revision'] == current and contents['title'] == title
        assert contents['authors'] == ['New author']
        assert read_chapter(app, workspace, 'edition', current, 1)['raw_text'] == extracted['records'][0]['raw_text']
        assert read_ebook_section(app, workspace, 'edition', current, 1).raw_text == historical.raw_text
        with app.read(workspace) as db:
            assert db.execute('SELECT config FROM revision WHERE id=?', (current,)).fetchone()[0] == config
        previous = current


@pytest.mark.parametrize('enhanced', [False, True])
def test_scope_and_cancellation_are_checked_before_source_access(tmp_path, monkeypatch, enhanced):
    app, workspace, revision, _, _, _ = _published(tmp_path, enhanced=enhanced)
    other = app.create_workspace('Other library')
    foreign_topic = app.create_topic(other, 'Other topic')
    topic = app.create_topic(workspace, 'Archived topic')
    with app.topic_transaction(workspace) as tree:
        tree.archive(topic)

    def forbidden(*args, **kwargs):
        pytest.fail('Unauthorized or cancelled contents request reached original bytes.')

    monkeypatch.setattr(app.assets, 'resolve', forbidden)
    for operation in (
        lambda: open_ebook(app, other, 'edition'),
        lambda: open_ebook(app, workspace, 'edition', authorized_topic=foreign_topic),
        lambda: read_chapter(app, workspace, 'edition', revision, 1, authorized_topic=topic),
    ):
        with pytest.raises(ValueError):
            operation()
    with pytest.raises(InterruptedError):
        open_ebook(app, workspace, 'edition', cancelled=lambda: True)
    with pytest.raises(InterruptedError):
        read_chapter(app, workspace, 'edition', revision, 1, cancelled=lambda: True)


@pytest.mark.parametrize('enhanced', [False, True])
def test_retained_original_drift_blocks_contents_and_cached_chapter(tmp_path, enhanced):
    app, workspace, revision, _, _, _ = _published(tmp_path, enhanced=enhanced)
    contents = open_ebook(app, workspace, 'edition')
    app.assets.resolve(contents['asset_sha256']).write_bytes(b'Changed original EPUB')
    with pytest.raises(ValueError):
        open_ebook(app, workspace, 'edition')
    with pytest.raises(ValueError):
        read_chapter(app, workspace, 'edition', revision, 1)


@pytest.mark.parametrize('enhanced', [False, True])
def test_uuid_revision_without_deterministic_baseline_is_not_trusted(tmp_path, enhanced):
    app, workspace, _, _, _, _ = _published(tmp_path, enhanced=enhanced, revision_override=uuid4().hex)
    with pytest.raises(ValueError, match='original EPUB extraction revision'):
        open_ebook(app, workspace, 'edition')


@pytest.mark.parametrize('mutation', [
    lambda settings: settings['ebook']['chapters'][0].update(member='wrong.xhtml'),
    lambda settings: settings['ebook']['chapters'][0].update(text_sha256='0' * 64),
    lambda settings: settings['ebook']['toc'][0].update(offset=1_000_000),
    lambda settings: settings.update(ebook=None),
])
def test_new_contents_must_match_canonical_sections_and_page_digests(tmp_path, mutation):
    app, workspace, _, _, _, _ = _published(tmp_path, enhanced=True, mutate=mutation)
    with pytest.raises(ValueError):
        open_ebook(app, workspace, 'edition')


def _damage_page(app, revision, raw, digest):
    with sqlite3.connect(app.database) as db:
        trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='page_bu'").fetchone()[0]
        db.execute('DROP TRIGGER page_bu')
        db.execute('UPDATE page SET raw=?,digest=? WHERE revision=? AND ordinal=1', (raw, digest, revision))
        db.execute(trigger)


@pytest.mark.parametrize('enhanced', [False, True])
def test_selected_raw_text_is_rehashed_even_when_length_and_stored_digest_match(tmp_path, enhanced):
    app, workspace, revision, _, extracted, _ = _published(tmp_path, enhanced=enhanced)
    raw = extracted['records'][0]['raw_text']
    _damage_page(app, revision, 'X' + raw[1:], sha256(raw.encode('utf-8')).hexdigest())
    with pytest.raises(ValueError, match='text has changed'):
        read_chapter(app, workspace, 'edition', revision, 1)


def test_old_metadata_revision_cannot_retarget_rehashed_text(tmp_path):
    app, workspace, _, _, _, _ = _published(tmp_path, enhanced=False)
    current = app.update_metadata(workspace, 'edition', title='Metadata correction')
    raw = 'Different rehashed chapter'
    _damage_page(app, current, raw, sha256(raw.encode('utf-8')).hexdigest())
    with pytest.raises(ValueError, match='original extraction'):
        open_ebook(app, workspace, 'edition')


def test_valid_old_settings_over_two_mebibytes_keep_legacy_four_mebibyte_limit(tmp_path):
    def enlarge(settings):
        settings['provenance']['legacy_extension'] = 'x' * (2 * 1024 * 1024)
    app, workspace, revision, _, _, config = _published(tmp_path, enhanced=False, mutate=enlarge)
    assert ebook_contents.MAX_NAVIGATION < len(config.encode('utf-8')) < ebook_contents.MAX_SETTINGS
    assert read_ebook_section(app, workspace, 'edition', revision, 1).raw_text
    assert open_ebook(app, workspace, 'edition')['toc'][0]['offset'] == 0


def test_fallback_notice_does_not_reject_legacy_warning_count_boundary(tmp_path):
    def full_warnings(settings):
        settings['warnings'] = ['layout_not_verified'] * 1000
    app, workspace, revision, _, _, _ = _published(tmp_path, enhanced=False, mutate=full_warnings)
    assert len(read_ebook_section(app, workspace, 'edition', revision, 1).warnings) == 1000
    contents = open_ebook(app, workspace, 'edition')
    assert len(contents['warnings']) == 1001
    assert contents['warnings'][-1] == 'embedded_contents_unavailable'


def test_new_navigation_descriptor_remains_bounded_independently_of_legacy_settings(tmp_path):
    def enlarge(settings):
        settings['ebook']['unexpected_padding'] = 'x' * ebook_contents.MAX_NAVIGATION
    app, workspace, _, _, _, config = _published(tmp_path, enhanced=True, mutate=enlarge)
    assert len(config.encode('utf-8')) < ebook_contents.MAX_SETTINGS
    with pytest.raises(ValueError, match='navigation budget'):
        open_ebook(app, workspace, 'edition')


def test_repeated_spine_navigation_budget_keeps_both_reader_apis_available(tmp_path):
    fixtures = runpy.run_path(str(Path(__file__).parents[1] / 'extract' / 'test_epub_navigation.py'))
    chapter = fixtures['chapter']('<h1 id="a">A</h1><h2 id="b">B</h2><h3 id="c">C</h3>')
    source = fixtures['build_epub']([('one', 'EPUB/one.xhtml', chapter)], spine=['one'] * 1000)
    app, workspace, revision, _, extracted, config = _published(tmp_path, enhanced=True, source_bytes=source)
    assert len(extracted['records']) == 1000
    assert all(record['raw_text'] == 'A\nB\nC' and record['headings'] == []
               for record in extracted['records'])
    assert extracted['toc'] == extracted['page_hints'] == []
    assert 'embedded_navigation_budget_exceeded' in extracted['warnings']
    assert len(canonical(json.loads(config)['ebook']).encode('utf-8')) <= ebook_contents.MAX_NAVIGATION
    contents = open_ebook(app, workspace, 'edition')
    assert len(contents['chapters']) == 1000
    assert contents['toc'] == contents['page_hints'] == []
    assert 'embedded_navigation_budget_exceeded' in contents['warnings']
    legacy = read_ebook_section(app, workspace, 'edition', revision, 1000)
    enhanced = read_chapter(app, workspace, 'edition', revision, 1000)
    assert legacy.position == legacy.count == enhanced['ordinal'] == 1000
    assert legacy.raw_text == enhanced['raw_text'] == 'A\nB\nC'
    assert legacy.verified is enhanced['verified'] is False
