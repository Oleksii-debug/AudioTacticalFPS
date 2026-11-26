"""Saved-search lifecycle through the real application and existing backup."""
from dataclasses import replace

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.research.query_manager import QueryManager


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research')
    topic = app.create_topic(workspace, 'Transport')
    imported = app.add_text(workspace, 'Synthetic source', 'Railway history. Café and Straße.',
                            authors=('Synthetic author',))
    assert imported['state'] == 'complete'
    hit, = app.search(workspace, 'Railway history')
    evidence = app.save_hit(workspace, topic, hit.ref, hit.revision, 'Моя власна гіпотеза про транспорт.')
    return app, workspace, topic, evidence


def test_saved_criteria_rerun_current_notes_without_changing_original_evidence(library):
    app, workspace, topic, evidence = library
    manager = QueryManager(app, workspace, topic)
    query = manager.create('Notes', 'гіпотеза', field='note', limit=8)
    hit, = manager.run(query)
    assert hit.matched_field == 'note' and hit.evidence.evidence_id == evidence
    original_quote = hit.evidence.anchor.quote
    with app.evidence_transaction(workspace) as store:
        store.edit_note(topic, evidence, 'Оновлена примітка без початкового слова.')
    assert manager.run(query) == ()
    changed = manager.update(query, 'Exact quotations', 'Railway', field='quote', limit=2)
    assert changed.id == query.id and changed.field == 'quote'
    assert manager.run(changed)[0].evidence.anchor.quote == original_quote


def test_archive_restore_duplicate_and_reopen_preserve_definition(library):
    app, workspace, topic, _ = library
    manager = QueryManager(app, workspace, topic)
    query = manager.create('Original', 'history', limit=7)
    archived = manager.archive(query)
    assert archived == replace(query, archived=True)
    assert manager.list().rows == ()
    assert manager.list(include_archived=True).rows == (archived,)
    with pytest.raises(ValueError, match='Restore'):
        manager.run(archived)
    with pytest.raises(ValueError):
        manager.update(archived, 'Hidden edit', 'changed')
    duplicate = manager.duplicate(archived, 'Separate copy')
    assert duplicate.id != query.id and duplicate.query == query.query and duplicate.limit == 7
    restored = manager.restore(archived)
    assert restored == query
    reopened = QueryManager(LibraryApp(app.root), workspace, topic)
    assert reopened.get(query.id) == query
    assert {q.id for q in reopened.list().rows} == {query.id, duplicate.id}


@pytest.mark.parametrize('action', ['update', 'archive', 'duplicate', 'run'])
def test_stale_definition_cannot_overwrite_or_run_another_editor_change(library, action):
    app, workspace, topic, _ = library
    first = QueryManager(app, workspace, topic)
    second = QueryManager(LibraryApp(app.root), workspace, topic)
    old = first.create('Shared query', 'Railway')
    latest = second.update(old, 'Revised query', 'гіпотеза', field='note', limit=4)
    with pytest.raises(ValueError, match='changed'):
        if action == 'update':
            first.update(old, 'Overwritten', 'history')
        elif action == 'duplicate':
            first.duplicate(old, 'Stale copy')
        else:
            getattr(first, action)(old)
    assert first.get(old.id) == latest


def test_restore_requires_fresh_archived_definition(library):
    app, workspace, topic, _ = library
    manager = QueryManager(app, workspace, topic)
    active = manager.create('Query', 'history')
    with pytest.raises(ValueError, match='already active'):
        manager.restore(active)
    archived = manager.archive(active)
    manager.restore(archived)
    with pytest.raises(ValueError, match='changed'):
        manager.restore(archived)


def test_query_pages_preserve_duplicate_names_and_explicit_archive_filter(library):
    app, workspace, topic, _ = library
    manager = QueryManager(app, workspace, topic)
    rows = [manager.create('Same name', 'Railway') for _ in range(5)]
    manager.archive(rows[1])
    page = manager.list(limit=2)
    assert page.total == 4 and len(page.rows) == 2 and page.next_cursor
    following = manager.list(limit=2, after=page.next_cursor)
    assert following.next_cursor is None
    assert len({q.id for q in page.rows + following.rows}) == 4
    assert manager.list(include_archived=True).total == 5
    for options in ({'limit': True}, {'include_archived': 1}, {'after': ['name', 'id']}):
        with pytest.raises(ValueError):
            manager.list(**options)


@pytest.mark.parametrize('scope', ['other_workspace', 'other_topic', 'archived_topic', 'archived_workspace'])
def test_saved_queries_cannot_cross_or_escape_archived_scopes(library, scope):
    app, workspace, topic, _ = library
    manager = QueryManager(app, workspace, topic)
    query = manager.create('Scoped', 'history')
    if scope == 'other_workspace':
        target = app.create_workspace('Other')
        manager = QueryManager(app, target, app.create_topic(target, 'Transport'))
    elif scope == 'other_topic':
        manager = QueryManager(app, workspace, app.create_topic(workspace, 'Other topic'))
    elif scope == 'archived_topic':
        with app.topic_transaction(workspace) as tree:
            tree.archive(topic)
    else:
        app.archive_workspace(workspace)
    with pytest.raises(ValueError):
        manager.run(query)
    with pytest.raises(ValueError):
        manager.archive(query)


def test_backup_restores_active_and_archived_query_definitions(library, tmp_path):
    app, workspace, topic, _ = library
    manager = QueryManager(app, workspace, topic)
    active = manager.create('Quotations', 'history')
    archived = manager.archive(manager.create('Research notes', 'гіпотеза', field='note'))
    bundle = tmp_path / 'backup'
    checksum = app.backup(bundle)
    destination = tmp_path / 'restored'
    restore_backup(bundle, destination, expected_manifest_sha256=checksum)
    restored = QueryManager(LibraryApp(destination), workspace, topic)
    assert set(restored.list(include_archived=True).rows) == {active, archived}
    assert restored.run(active)[0].evidence.anchor.quote == 'Railway history'


def test_cancelled_search_and_changed_original_never_produce_saved_results(library):
    app, workspace, topic, _ = library
    manager = QueryManager(app, workspace, topic)
    query = manager.create('Query', 'history')
    with pytest.raises(TimeoutError):
        manager.run(query, cancelled=lambda: True)
    original, = app.assets_dir.glob('*.txt')
    original.write_bytes(b'Different original')
    with pytest.raises(ValueError):
        manager.run(query)
