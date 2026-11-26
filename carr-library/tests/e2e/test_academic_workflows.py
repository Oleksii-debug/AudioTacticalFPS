"""Academic exchange uses actual application snapshots and retained TXT evidence."""
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.reference_exchange import preview_references
from carr_library.library_backup import restore_backup


def test_reviewed_reference_update_preserves_historical_outline_and_backup(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Academic library')
    collection = app.create_collection(workspace, 'Course sources')
    topic = app.create_topic(workspace, 'Dissertation')
    child = app.create_topic(workspace, 'Railway evidence', parent=topic)
    imported = app.add_text(workspace, 'Original title', 'Exact historical phrase in the source.',
        authors=('Original author',), language='uk', edition='First', volume='II',
        collections=(collection,))
    hit, = app.search(workspace, 'historical phrase')
    saved_id = app.save_hit(workspace, child, hit.ref, hit.revision, 'My independent interpretation')
    app.set_evidence_tags(workspace, child, saved_id, ('seminar',))
    original_evidence = app.evidence(workspace, child)
    current = app.edition_metadata(workspace, imported['edition'])
    incoming = json.dumps([{
        'id': 'external-id-not-a-source', 'type': 'book', 'title': 'Нова назва',
        'author': [{'literal': 'Академія наук'}, {'family': 'Kováč', 'given': 'Ján'}],
        'language': 'uk', 'issued': {'date-parts': [[2024]]}, 'DOI': '10.0000/unused',
    }], ensure_ascii=False).encode('utf-8')
    hint, = preview_references(incoming, format='csl-json')
    assert {'DOI', 'issued'} <= set(hint.unsupported_fields)
    assert not hint.metadata_verified
    assert app.edition_metadata(workspace, imported['edition']) == current
    app.update_metadata(workspace, imported['edition'], title=hint.titles[0],
        authors=hint.authors, language=hint.languages[0], edition=current.edition,
        volume=current.volume, expected_metadata=current)

    items = json.loads(app.export_bibliography(workspace, collection=collection))
    assert len(items) == 1 and items[0]['title'] == 'Нова назва'
    assert items[0]['author'] == [{'literal': author} for author in hint.authors]
    assert items[0]['edition'] == 'First' and items[0]['volume'] == 'II'
    assert not {'DOI', 'issued', 'URL', 'note'} & set(items[0])
    assert items[0]['id'] != 'external-id-not-a-source'
    assert app.evidence(workspace, child) == original_evidence
    outline = app.export_outline(workspace, topic)
    for text in ('Dissertation', 'Railway evidence', 'historical phrase',
                 'Original title', 'Original author', 'My independent interpretation', 'seminar'):
        assert text in outline
    assert 'Нова назва' not in outline
    with pytest.raises(ValueError, match='metadata changed'):
        app.update_metadata(workspace, imported['edition'], title='Stale overwrite', expected_metadata=current)

    reopened = LibraryApp(app.root)
    assert reopened.export_outline(workspace, topic) == outline
    assert json.loads(reopened.export_bibliography(workspace, collection=collection)) == items
    backup = reopened.backup(tmp_path / 'backup')
    restore_backup(tmp_path / 'backup', tmp_path / 'restored', backup)
    restored = LibraryApp(tmp_path / 'restored')
    assert restored.export_outline(workspace, topic) == outline
    assert json.loads(restored.export_bibliography(workspace, collection=collection)) == items


def test_catalog_bibliography_is_available_without_research_evidence(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('One')
    other = app.create_workspace('Two')
    collection = app.create_collection(workspace, 'Selected')
    first = app.add_text(workspace, 'Repeated title', 'The first distinct edition.',
                        authors=('A and B Institute', 'Олена'), edition='First', collections=(collection,))
    app.add_text(workspace, 'Repeated title', 'The second distinct edition.', edition='Second')
    app.add_text(other, 'Private other library', 'Other scope.')
    records = json.loads(app.export_bibliography(workspace))
    assert len(records) == 2 and len({record['id'] for record in records}) == 2
    assert {record['edition'] for record in records} == {'First', 'Second'}
    selected, = json.loads(app.export_bibliography(workspace, collection=collection))
    assert selected['edition'] == 'First'
    for format in ('ris', 'csl-json'):
        hint, = preview_references(app.export_bibliography(workspace,
            collection=collection, format=format).encode('utf-8'), format=format)
        assert hint.titles == ('Repeated title',)
        assert hint.authors == ('A and B Institute', 'Олена')
    bibtex = app.export_bibliography(workspace, collection=collection, format='bibtex')
    assert '{A and B Institute} and {Олена}' in bibtex
    assert first['asset'] not in bibtex and str(app.root) not in bibtex
    with pytest.raises(InterruptedError):
        app.export_bibliography(workspace, cancelled=lambda: True)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        app.export_bibliography(workspace)


def test_outline_excludes_other_branches_and_fails_after_original_damage(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Research')
    root = app.create_topic(workspace, 'Selected branch')
    child = app.create_topic(workspace, 'Child', parent=root)
    hidden = app.create_topic(workspace, 'Archived branch', parent=root)
    hidden_child = app.create_topic(workspace, 'Hidden descendant', parent=hidden)
    unrelated = app.create_topic(workspace, 'Unrelated branch')
    imported = app.add_text(workspace, 'Verified original', 'Authentic source fragment.')
    hit, = app.search(workspace, 'source fragment')
    for topic, note in ((child, 'Included note'), (hidden_child, 'ARCHIVED NOTE'), (unrelated, 'UNRELATED NOTE')):
        app.save_hit(workspace, topic, hit.ref, hit.revision, note)
    with app.topic_transaction(workspace) as tree:
        tree.archive(hidden)
    output = app.export_outline(workspace, root)
    assert 'Included note' in output
    for excluded in ('ARCHIVED NOTE', 'UNRELATED NOTE', 'Archived branch', 'Hidden descendant', 'Unrelated branch'):
        assert excluded not in output
    with pytest.raises(InterruptedError):
        app.export_outline(workspace, root, cancelled=lambda: True)
    app.assets.resolve(imported['asset']).write_bytes(b'Changed retained original.')
    with pytest.raises(ValueError):
        app.export_outline(workspace, root)
