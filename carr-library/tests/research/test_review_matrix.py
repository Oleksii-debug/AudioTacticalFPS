"""Real storage/source checks for cross-topic literature-review exports."""
import csv
from hashlib import sha256
import io
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.anchors import Locator, make_anchor
from carr_library.research import review_matrix


def rows(text):
    return list(csv.DictReader(io.StringIO(text, newline='')))


def value(row, key):
    return json.loads(row[key + '_json'])


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Literature review')
    foreign = app.create_workspace('Unrelated private research')
    collection = app.create_collection(workspace, 'Included sources')
    parent = app.create_topic(workspace, 'Research / questions')
    child = app.create_topic(workspace, 'Україна — sources', parent=parent)
    outside = app.create_topic(workspace, 'Other source')
    raw = 'Exact literature evidence — Україна.\r\nFirst segment.\fSecond segment with evidence.'
    source = app.add_text(workspace, '=Original source title', raw,
                          authors=('Original; literal author', 'Другий автор'),
                          language='uk', edition='First', volume='II',
                          edition_id='source', collections=(collection,))
    assert source['state'] == 'complete'
    other = app.add_text(workspace, 'Outside selected collection',
                         'Outside collection evidence.', edition_id='other')
    assert other['state'] == 'complete'
    hit, = app.search(workspace, 'Exact literature evidence')
    evidence = app.save_hit(workspace, child, hit.ref, hit.revision,
                            '=SUM(1,2)\r\nIndependent interpretation, not a quotation.')
    app.set_evidence_tags(workspace, child, evidence, ('method', '+hypothesis'))
    other_hit, = app.search(workspace, 'Outside collection evidence')
    app.save_hit(workspace, outside, other_hit.ref, other_hit.revision, 'Outside note')
    # IDs and bytes may repeat in another authorized workspace without leaking.
    foreign_source = app.add_text(foreign, 'Foreign private title', raw, edition_id='source')
    assert foreign_source['state'] == 'complete'
    foreign_topic = app.create_topic(foreign, 'Secret topic')
    foreign_hit, = app.search(foreign, 'Exact literature evidence')
    app.save_hit(foreign, foreign_topic, foreign_hit.ref, foreign_hit.revision, 'Secret note')
    return app, workspace, collection, parent, child, outside, evidence, raw, foreign


def test_matrix_combines_visible_topic_paths_tags_and_exact_source_slices(library):
    app, workspace, collection, parent, child, outside, evidence, raw, foreign = library
    output = app.export_review_matrix(workspace)
    result = rows(output)
    assert len(result) == 2
    selected = next(row for row in result if value(row, 'evidence_id') == evidence)
    assert value(selected, 'topic_path') == ['Research / questions', 'Україна — sources']
    assert value(selected, 'tags') == ['+hypothesis', 'method']
    assert value(selected, 'quotation') == 'Exact literature evidence'
    assert value(selected, 'quotation') in raw
    assert value(selected, 'research_note').startswith('=SUM(1,2)\r\n')
    assert value(selected, 'author') == 'Original; literal author; Другий автор'
    assert value(selected, 'title') == '=Original source title'
    assert selected['locator_kind'] == 'logical_segment'
    assert selected['physical_page'] == '' and selected['logical_segment'] == '1'
    assert value(selected, 'printed_page') is None
    assert 'Segment 1 (unpaginated)' in value(selected, 'citation')
    assert 'Secret' not in output and foreign not in output
    for row in result:
        for key, cell in row.items():
            if key.endswith('_json'):
                json.loads(cell)
                assert not cell.startswith(('=', '+', '-', '@', '\t', '\r', '\n'))


def test_collection_uses_current_membership_without_rewriting_historical_attribution(library):
    app, workspace, collection, parent, child, outside, evidence, raw, foreign = library
    before = app.export_review_matrix(workspace, collection=collection)
    assert len(rows(before)) == 1 and 'Outside note' not in before
    app.update_metadata(workspace, 'source', title='Corrected current title',
                         authors=('New current author',), language='en',
                         edition='Changed', volume='III')
    assert app.edition_metadata(workspace, 'source').title == 'Corrected current title'
    assert app.export_review_matrix(workspace, collection=collection) == before
    with app.catalog_transaction(workspace) as catalog:
        catalog.remove(collection, 'source')
    assert rows(app.export_review_matrix(workspace, collection=collection)) == []
    assert len(rows(app.export_review_matrix(workspace))) == 2


def test_archived_topic_subtrees_are_excluded(library):
    app, workspace, collection, parent, child, outside, evidence, raw, foreign = library
    with app.topic_transaction(workspace) as tree:
        tree.archive(parent)
    output = app.export_review_matrix(workspace)
    result = rows(output)
    assert len(result) == 1 and value(result[0], 'topic_id') == outside
    assert 'Independent interpretation' not in output


def test_multiple_source_segments_keep_separate_quotes_and_shared_note(library):
    app, workspace, collection, parent, child, outside, evidence, raw, foreign = library
    saved, = app.evidence(workspace, child)
    anchors = tuple(make_anchor(saved.anchor.identity, Locator('logical_segment', n),
                               segment, 0, len(segment), saved.anchor.metadata,
                               text_verified=True)
                    for n, segment in enumerate(raw.split('\f'), 1))
    with app.evidence_transaction(workspace) as store:
        store.save(child, 'complete-span', anchors, 'Shared comparison note')
    result = [row for row in rows(app.export_review_matrix(workspace, collection=collection))
              if value(row, 'evidence_id') == 'complete-span']
    assert [value(row, 'quotation') for row in result] == raw.split('\f')
    assert [row['source_slice'] for row in result] == ['1', '2']
    assert [row['logical_segment'] for row in result] == ['1', '2']
    assert all(row['source_slices'] == '2' for row in result)
    assert all(value(row, 'research_note') == 'Shared comparison note' for row in result)
    assert all(row['physical_page'] == '' for row in result)


def test_missing_original_prevents_any_review_matrix_result(library):
    app, workspace, collection, parent, child, outside, evidence, raw, foreign = library
    saved, = app.evidence(workspace, child)
    source = app.assets.resolve(saved.anchor.identity.asset_sha256)
    source.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        app.export_review_matrix(workspace, collection=collection)


def test_damaged_saved_record_is_not_silently_skipped(library):
    app, workspace, collection, parent, child, outside, evidence, raw, foreign = library
    with app.write(workspace) as db:
        db.execute('INSERT INTO research_evidence VALUES(?,?,?,?,?,?)',
                   (workspace, child, 'damaged', '{}', '0' * 64, 'Damaged note'))
    with pytest.raises(ValueError):
        app.export_review_matrix(workspace, collection=collection)


@pytest.mark.parametrize('limit,value_', [('MAX_TOPICS', 1), ('MAX_EVIDENCE', 1),
                                         ('MAX_SOURCE_SLICES', 1), ('MAX_BYTES', 32),
                                         ('MAX_TOPIC_DEPTH', 1)])
def test_overflow_fails_instead_of_returning_a_partial_matrix(library, monkeypatch, limit, value_):
    app, workspace, *_ = library
    monkeypatch.setattr(review_matrix, limit, value_)
    with pytest.raises(ValueError, match='[Tt]oo|too large'):
        app.export_review_matrix(workspace)


def test_cancellation_before_and_during_verification(library):
    app, workspace, *_ = library
    with pytest.raises(TimeoutError):
        app.export_review_matrix(workspace, cancelled=lambda: True)
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        return calls >= 12

    with pytest.raises(TimeoutError):
        app.export_review_matrix(workspace, cancelled=cancel)
    assert calls >= 12


def test_scope_authorization_and_collection_visibility(library):
    app, workspace, collection, parent, child, outside, evidence, raw, foreign = library
    with pytest.raises(ValueError):
        app.export_review_matrix(foreign, collection=collection)
    with app.catalog_transaction(workspace) as catalog:
        catalog.archive(collection)
    with pytest.raises(ValueError):
        app.export_review_matrix(workspace, collection=collection)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        app.export_review_matrix(workspace)


def test_matrix_is_read_only_and_reopens_identically(library):
    app, workspace, *_ = library
    before = {path.name: sha256(path.read_bytes()).hexdigest()
              for path in app.assets_dir.iterdir()}
    with app.read(workspace) as db:
        load, metadata, _ = app._loaders(db, workspace)
        changes = db.total_changes
        result = review_matrix.export_review_matrix(db, workspace, load, metadata)
        assert db.total_changes == changes
    assert LibraryApp(app.root).export_review_matrix(workspace) == result
    assert {path.name: sha256(path.read_bytes()).hexdigest()
            for path in app.assets_dir.iterdir()} == before


def test_empty_library_has_an_explicit_matrix_header(tmp_path):
    app = LibraryApp(tmp_path / 'empty')
    workspace = app.create_workspace('No evidence yet')
    output = app.export_review_matrix(workspace)
    assert rows(output) == []
    assert next(csv.reader(io.StringIO(output))) == list(review_matrix.HEADERS)
