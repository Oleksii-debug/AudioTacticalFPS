"""RIS review uses actual catalog transactions and historical source snapshots."""
from contextlib import contextmanager
from threading import Event
from dataclasses import replace

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.ris_apply import (apply_ris_metadata, prepare_ris_review,
                                           read_ris_file, selected_metadata)
from carr_library.catalog.ris_preview import preview_ris
from carr_library.storage.revisions import Storage


RIS = ('TY  - BOOK\nTI  - Нова назва — Étude\nTI  - Alternative title\n'
       'AU  - Олена Коваль\nAU  - Žan Novak\nAU  - Олена Коваль\nLA  - uk\n'
       'DO  - 10.1234/unverified\nSN  - 9780000000000\n'
       'L1  - ../../private.pdf\nN1  - Do not import this note\nER  -\n').encode('utf-8')


@pytest.fixture
def case(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('RIS review')
    for identity, language, edition in (('e1', 'en', 'First'), ('e2', 'sk', 'Second')):
        result = app.add_text(workspace, 'Original title', f'Historical railway passage {identity}.',
                             authors=('Original author',), language=language, edition=edition,
                             volume='II', work_id='shared-work', edition_id=identity)
        assert result['state'] == 'complete'
    return app, workspace


@pytest.fixture
def whitespace_case(tmp_path):
    """Genuine imported catalog data may exceed the interactive editor limits."""
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Partial metadata review')
    title = '  Original title — Історія  '
    authors = ('  Original Author  ', '  ' + 'Ж' * 600 + '  ', ' Another  Author ')
    for identity, language, edition in (('e1', 'en', ' First '), ('e2', 'sk', ' Second ')):
        source = tmp_path / f'{identity}.txt'
        source.write_text(f'Historical railway passage {identity}.', encoding='utf-8')
        # This executes the actual local TXT importer with explicitly authored
        # fixture bytes. Catalog registration preserves the original metadata.
        result = app._import_file(workspace, source, title=title, authors=authors,
            language=language, edition=edition, volume=' II ',
            work_id='shared-work', edition_id=identity)
        assert result['state'] == 'complete'
    assert app.edition_metadata(workspace, 'e1').title == title
    assert app.edition_metadata(workspace, 'e1').authors == authors
    return app, workspace


def state(app):
    with app.read() as db:
        return {name: tuple(tuple(row) for row in db.execute(f'SELECT * FROM {name} ORDER BY rowid'))
                for name in ('catalog_work', 'catalog_author', 'catalog_edition', 'revision',
                             'page', 'active', 'source_metadata', 'research_evidence')}


def review(case, data=RIS):
    app, workspace = case
    record, = preview_ris(data)
    return prepare_ris_review(app, workspace, 'e1', record)


def test_selected_partial_metadata_preserves_missing_fields_and_historical_identity(case, tmp_path):
    app, workspace = case
    path = tmp_path / 'references.ris'
    path.write_bytes(RIS)
    record, = read_ris_file(path)
    topic = app.create_topic(workspace, 'Historical quotations')
    for hit in app.search(workspace, 'railway passage'):
        app.save_hit(workspace, topic, hit.ref, hit.revision, 'Private research note')
    saved = app.evidence(workspace, topic)
    before_export = app.export_topic(workspace, topic, format='text')
    assets = {path.name: path.read_bytes() for path in app.assets_dir.iterdir()}
    prior = {row.edition_id: row for row in app.browse(workspace)}
    prepared = prepare_ris_review(app, workspace, 'e1', record)
    apply_ris_metadata(app, workspace, prepared, {'authors': record.authors, 'language': 'uk'})
    reopened = LibraryApp(app.root)
    current = {row.edition_id: row for row in reopened.browse(workspace)}
    assert current['e1'].title == current['e2'].title == 'Original title'
    assert current['e1'].authors == current['e2'].authors == ('Олена Коваль', 'Žan Novak', 'Олена Коваль')
    assert (current['e1'].language, current['e2'].language) == ('uk', 'sk')
    for identity in current:
        assert (current[identity].asset, current[identity].work_id, current[identity].edition,
                current[identity].volume) == (prior[identity].asset, prior[identity].work_id,
                                             prior[identity].edition, prior[identity].volume)
    assert reopened.evidence(workspace, topic) == saved
    assert reopened.export_topic(workspace, topic, format='text') == before_export
    assert {path.name: path.read_bytes() for path in app.assets_dir.iterdir()} == assets
    assert all(row.anchor.metadata.title == 'Original title' for row in saved)


def test_empty_absent_unsupported_and_unreviewed_choices_never_write(case):
    app, workspace = case
    prepared = review(case)
    before = state(app)
    for selected in ({}, {'title': ''}, {'title': 'Unreviewed title'}, {'edition': 'Third'},
                     {'identifiers': prepared.record.identifiers}, {'authors': ('Žan Novak',)}):
        with pytest.raises(ValueError):
            apply_ris_metadata(app, workspace, prepared, selected)
        assert state(app) == before
    values = selected_metadata(prepared, {'title': 'Alternative title'})
    assert values['title'] == 'Alternative title'
    assert values['authors'] == prepared.metadata.authors
    assert values['volume'] == prepared.metadata.volume


def test_stale_metadata_is_checked_atomically_at_the_actual_write_boundary(case, monkeypatch):
    app, workspace = case
    prepared = review(case)
    original = app.update_metadata
    calls = []
    def concurrent_edit(scope, edition_id, **values):
        assert values['selected_fields'] == ('language',)
        calls.append(values['expected_metadata'])
        original(scope, edition_id, title='Concurrent title', authors=('Concurrent author',),
                 language='fr', edition='Newer edition', volume='IX')
        return original(scope, edition_id, **values)
    monkeypatch.setattr(app, 'update_metadata', concurrent_edit)
    with pytest.raises(ValueError, match='metadata changed'):
        apply_ris_metadata(app, workspace, prepared, {'language': 'uk'})
    assert calls == [prepared.metadata]
    current = app.edition_metadata(workspace, 'e1')
    assert (current.title, current.authors, current.language, current.edition, current.volume) == (
        'Concurrent title', ('Concurrent author',), 'fr', 'Newer edition', 'IX')


def test_scope_and_work_identity_cannot_be_retargeted(case, tmp_path):
    app, workspace = case
    prepared = review(case)
    other = app.create_workspace('Different library')
    app.add_text(other, 'Other title', 'Other source.', edition_id='e1', work_id='shared-work')
    before = state(app)
    with pytest.raises(ValueError, match='library changed'):
        apply_ris_metadata(app, other, prepared, {'title': 'Alternative title'})
    forged_work = replace(prepared, metadata=replace(prepared.metadata, work_id='another-work'))
    with pytest.raises(ValueError, match='metadata changed'):
        apply_ris_metadata(app, workspace, forged_work, {'title': 'Alternative title'})
    other_app = LibraryApp(tmp_path / 'different-root')
    with pytest.raises(ValueError, match='library changed'):
        apply_ris_metadata(other_app, workspace, prepared, {'title': 'Alternative title'})
    assert state(app) == before
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        apply_ris_metadata(app, workspace, prepared, {'title': 'Alternative title'})


def test_cancellation_before_publication_and_malformed_batch_do_not_change_library(case, tmp_path):
    app, workspace = case
    prepared = review(case)
    before = state(app)
    for cancelled in (lambda: True, iter((False, True)).__next__):
        with pytest.raises(TimeoutError, match='cancelled'):
            apply_ris_metadata(app, workspace, prepared, {'title': 'Alternative title'}, cancelled=cancelled)
    path = tmp_path / 'malformed.ris'
    path.write_bytes(RIS + b'TY  - JOUR\nTI  - Unfinished')
    with pytest.raises(ValueError, match='incomplete'):
        read_ris_file(path)
    with pytest.raises(TimeoutError, match='cancelled'):
        read_ris_file(path, cancelled=lambda: True)
    assert state(app) == before


def test_failed_shared_work_publication_rolls_back_every_selected_field(case, monkeypatch):
    app, workspace = case
    prepared = review(case)
    before = state(app)
    original = Storage.stage_revision
    calls = []
    def fail_second(self, scope, edition_id, *args, **kwargs):
        calls.append(edition_id)
        if len(calls) == 2:
            raise RuntimeError('Synthetic second-edition storage failure')
        return original(self, scope, edition_id, *args, **kwargs)
    monkeypatch.setattr(Storage, 'stage_revision', fail_second)
    with pytest.raises(RuntimeError, match='second-edition'):
        apply_ris_metadata(app, workspace, prepared,
                           {'title': 'Alternative title', 'authors': prepared.record.authors, 'language': 'uk'})
    assert len(calls) == 2
    assert state(app) == before


def test_local_file_is_bounded_and_non_regular_paths_are_rejected(tmp_path, monkeypatch):
    from carr_library.catalog import ris_apply
    path = tmp_path / 'large.ris'
    path.write_bytes(RIS)
    monkeypatch.setattr(ris_apply, 'MAX_BYTES', 8)
    with pytest.raises(ValueError):
        read_ris_file(path)
    with pytest.raises(ValueError, match='regular local'):
        read_ris_file(tmp_path)


def test_language_only_import_keeps_exact_unchecked_metadata_and_sibling_revision(whitespace_case):
    app, workspace = whitespace_case
    hint, = preview_ris(b'TY  - BOOK\nLA  - uk\nER  -\n')
    assert hint.titles == hint.authors == ()
    before = {row.edition_id: row for row in app.browse(workspace)}
    with app.read(workspace) as db:
        revisions = dict(db.execute('SELECT edition,revision FROM active WHERE workspace=?', (workspace,)))
    topic = app.create_topic(workspace, 'Historical evidence')
    hit = next(row for row in app.search(workspace, 'railway passage') if row.edition_id == 'e1')
    app.save_hit(workspace, topic, hit.ref, hit.revision)
    saved = app.evidence(workspace, topic)
    prepared = prepare_ris_review(app, workspace, 'e1', hint)

    revision = apply_ris_metadata(app, workspace, prepared, {'language': 'uk'})

    reopened = LibraryApp(app.root)
    after = {row.edition_id: row for row in reopened.browse(workspace)}
    assert after['e1'] == replace(before['e1'], language='uk')
    assert after['e2'] == before['e2']
    with reopened.read(workspace) as db:
        current_revisions = dict(db.execute(
            'SELECT edition,revision FROM active WHERE workspace=?', (workspace,)))
    assert current_revisions['e1'] == revision != revisions['e1']
    assert current_revisions['e2'] == revisions['e2']
    assert reopened.evidence(workspace, topic) == saved


def test_explicit_author_selection_normalizes_only_authors_and_ignores_unchecked_input(whitespace_case):
    app, workspace = whitespace_case
    before = {row.edition_id: row for row in app.browse(workspace)}
    # The partial-update API must use the locked current values for unchecked
    # fields, even if a caller supplies different values alongside its mask.
    app.update_metadata(workspace, 'e1', title='Do not replace the current title',
        authors=('  New Author  ', '  Автор Два  '), language='Do not replace language',
        edition='Do not replace edition', volume='Do not replace volume',
        expected_metadata=before['e1'], selected_fields=('authors',))
    after = {row.edition_id: row for row in app.browse(workspace)}
    for identity in ('e1', 'e2'):
        assert after[identity] == replace(before[identity], authors=('New Author', 'Автор Два'))


def test_partial_field_mask_requires_expected_metadata_and_valid_explicit_fields(case):
    app, workspace = case
    current = app.edition_metadata(workspace, 'e1')
    before = state(app)
    invalid = (
        {'selected_fields': ('language',)},
        {'selected_fields': (), 'expected_metadata': current},
        {'selected_fields': ('identifier',), 'expected_metadata': current},
        {'selected_fields': ('language', 1), 'expected_metadata': current},
        {'selected_fields': 'language', 'expected_metadata': current},
    )
    for options in invalid:
        with pytest.raises(ValueError):
            app.update_metadata(workspace, 'e1', title='Must not be written', language='uk', **options)
        assert state(app) == before


def test_ordinary_full_metadata_edit_keeps_its_existing_normalization_and_defaults(case):
    app, workspace = case
    before = app.edition_metadata(workspace, 'e2')
    app.update_metadata(workspace, 'e1', title='  Updated title  ',
                        authors=('  New Author  ', '  Автор Два  '))
    selected = app.edition_metadata(workspace, 'e1')
    sibling = app.edition_metadata(workspace, 'e2')
    assert selected.title == 'Updated title'
    assert selected.authors == ('New Author', 'Автор Два')
    assert (selected.language, selected.edition, selected.volume) == (None, None, None)
    assert sibling == replace(before, title=selected.title, authors=selected.authors)


@pytest.mark.parametrize('cancel_after', (1, 2))
def test_partial_metadata_cancelled_after_staging_rolls_back_every_shared_edition(
        case, monkeypatch, cancel_after):
    app, workspace = case
    current = app.edition_metadata(workspace, 'e1')
    topic = app.create_topic(workspace, 'Historical evidence')
    for hit in app.search(workspace, 'railway passage'):
        app.save_hit(workspace, topic, hit.ref, hit.revision, 'Retained research note')
    before = state(app)
    assets = {row.asset: app.assets.resolve(row.asset).read_bytes()
              for row in app.browse(workspace)}
    original = Storage.stage_revision
    cancelled = Event()
    staged = []

    def cancel_after_staging(self, scope, edition_id, *args, **kwargs):
        result = original(self, scope, edition_id, *args, **kwargs)
        staged.append(edition_id)
        if len(staged) == cancel_after:
            cancelled.set()
        return result

    monkeypatch.setattr(Storage, 'stage_revision', cancel_after_staging)
    with pytest.raises(InterruptedError, match='Metadata update cancelled'):
        app.update_metadata(workspace, 'e1', title='Cancelled title',
            authors=('Cancelled author',), language='uk',
            edition='Unchecked edition', volume='Unchecked volume',
            expected_metadata=current, selected_fields=('title', 'authors', 'language'),
            cancelled=cancelled.is_set)

    assert cancelled.is_set()
    assert staged == ['e1', 'e2'][:cancel_after]
    reopened = LibraryApp(app.root)
    assert state(reopened) == before
    assert {asset: reopened.assets.resolve(asset).read_bytes() for asset in assets} == assets


def test_partial_metadata_with_cancellation_callback_preserves_unselected_values(whitespace_case):
    app, workspace = whitespace_case
    before = {row.edition_id: row for row in app.browse(workspace)}
    with app.read(workspace) as db:
        revisions = dict(db.execute('SELECT edition,revision FROM active WHERE workspace=?', (workspace,)))
    checked = []

    def not_cancelled():
        checked.append(True)
        return False

    revision = app.update_metadata(workspace, 'e1', title='Unchecked title',
        authors=('Unchecked author',), language='uk',
        edition='Unchecked edition', volume='Unchecked volume',
        expected_metadata=before['e1'], selected_fields=('language',),
        cancelled=not_cancelled)

    reopened = LibraryApp(app.root)
    after = {row.edition_id: row for row in reopened.browse(workspace)}
    assert checked
    assert after['e1'] == replace(before['e1'], language='uk')
    assert after['e2'] == before['e2']
    with reopened.read(workspace) as db:
        current_revisions = dict(db.execute(
            'SELECT edition,revision FROM active WHERE workspace=?', (workspace,)))
    assert current_revisions['e1'] == revision != revisions['e1']
    assert current_revisions['e2'] == revisions['e2']


def test_partial_metadata_cancelled_after_writer_acquisition_does_not_commit(case, monkeypatch):
    from carr_library import application

    app, workspace = case
    current = app.edition_metadata(workspace, 'e1')
    before = state(app)
    assets = {row.asset: app.assets.resolve(row.asset).read_bytes()
              for row in app.browse(workspace)}
    original_writer = application.exclusive_writer
    cancelled = Event()
    entered = []

    @contextmanager
    def cancel_when_locked(root, **kwargs):
        with original_writer(root, **kwargs) as locked_root:
            entered.append(locked_root)
            cancelled.set()
            yield locked_root

    monkeypatch.setattr(application, 'exclusive_writer', cancel_when_locked)
    with pytest.raises(InterruptedError, match='Metadata update cancelled'):
        app.update_metadata(workspace, 'e1', title='Must not be committed',
            authors=('Unselected author',), language='uk', expected_metadata=current,
            selected_fields=('title', 'language'), cancelled=cancelled.is_set)

    assert entered == [app.root.resolve()]
    reopened = LibraryApp(app.root)
    assert state(reopened) == before
    assert {asset: reopened.assets.resolve(asset).read_bytes() for asset in assets} == assets


def test_partial_metadata_rechecks_reviewed_snapshot_after_writer_acquisition(case, monkeypatch):
    from carr_library import application

    app, workspace = case
    reviewed = app.edition_metadata(workspace, 'e1')
    original_writer = application.exclusive_writer
    newer = LibraryApp(app.root)
    concurrent_state = []

    @contextmanager
    def edit_before_lock(root, **kwargs):
        # Restore the actual guard for the competing complete transaction. The
        # outer operation still has its old reviewed snapshot and must recheck
        # it only after entering its own real writer/SQLite transaction.
        monkeypatch.setattr(application, 'exclusive_writer', original_writer)
        newer.update_metadata(workspace, 'e1', title='Concurrent title',
            authors=('Concurrent author',), language='fr', edition='Later', volume='IX')
        concurrent_state.append(state(newer))
        with original_writer(root, **kwargs) as locked_root:
            yield locked_root

    monkeypatch.setattr(application, 'exclusive_writer', edit_before_lock)
    with pytest.raises(ValueError, match='metadata changed'):
        app.update_metadata(workspace, 'e1', title='Unselected title',
            language='uk', expected_metadata=reviewed, selected_fields=('language',))

    assert len(concurrent_state) == 1
    reopened = LibraryApp(app.root)
    assert state(reopened) == concurrent_state[0]
    assert reopened.edition_metadata(workspace, 'e1').language == 'fr'

