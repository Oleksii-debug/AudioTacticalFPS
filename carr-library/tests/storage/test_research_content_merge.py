"""Real common4 research graphs, explicit conflicts and fresh-root publication."""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.backup_budget import BackupBudget
from carr_library.catalog.registration import Registration
from carr_library.ingest.resource_budget import ImportBudget
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.operations.broker import ResourceBroker
from carr_library.research.academic_library import AcademicLibrary
from carr_library.storage.revisions import Storage, Page
from carr_library import research_merge as merging


def inventory(root):
    return {str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def logical(app):
    # Independent complete canonical research snapshot. Only the internal page
    # rowid and the derived FTS/index tables are deliberately excluded.
    tables = ('workspace', 'workspace_meta', 'catalog_asset', 'catalog_work',
              'catalog_author', 'catalog_edition', 'catalog_collection', 'catalog_membership',
              'revision', 'page', 'sealed', 'active', 'source_metadata', 'source_page_review',
              'research_topic', 'research_evidence', 'research_saved_query', 'research_tag',
              'research_evidence_tag', 'academic_reference', 'import_publication')
    with app.read() as db:
        result = {}
        for table in tables:
            columns = ('revision,ordinal,physical,printed,verified,raw,digest'
                       if table == 'page' else '*')
            rows = [tuple(row) for row in db.execute('SELECT ' + columns + ' FROM ' + table)]
            result[table] = sorted(rows, key=repr)
        return result


@pytest.fixture
def factory(tmp_path):
    opened = []
    def create(label, *, history=True):
        app = LibraryApp(tmp_path / (label + '-library'))
        opened.append(app)
        workspace = app.create_workspace(label, 'Exact workspace description')
        collection = app.create_collection(workspace, 'Selected sources')
        parent = app.create_topic(workspace, 'Parent')
        child = app.create_topic(workspace, 'Child', parent=parent)
        text = 'Needle exact Україна e\u0301 — Žilina.\r\nSecond line.\fTail without inferred page labels.'
        result = app.add_text(workspace, 'Original title', text, authors=('Автор',),
                              work_id='same-scoped-work', edition_id='same-scoped-edition',
                              collections=(collection,))
        assert result['state'] == 'complete'
        hit, = app.search(workspace, 'Needle')
        evidence = app.save_hit(workspace, child, hit.ref, hit.revision, label + ' exact note')
        app.set_evidence_tags(workspace, child, evidence, ('tag one', 'tag two'))
        query = app.save_query(workspace, child, 'Saved literal', 'Needle')
        with app.write(workspace) as db:
            reference = AcademicLibrary(db, workspace).create(
                title='Exact linked reference', authors=('Jane Author',), notes=label,
                edition_id=hit.edition_id, year='2026', tags=('saved',))
        if history:
            app.update_metadata(workspace, hit.edition_id, title='Current title',
                                authors=('Current Author',), language='uk',
                                expected_metadata=app.edition_metadata(workspace, hit.edition_id))
        ResourceBroker.for_app(app.root)
        return SimpleNamespace(app=app, workspace=workspace, collection=collection,
            parent=parent, topic=child, evidence=evidence, query=query, reference=reference,
            original=text, snapshot=logical(app), saved=app.evidence(workspace, child))
    yield create
    for app in opened:
        assert app.close_search()


def backup(item, path):
    checksum = create_backup(item.app, path)
    return path, checksum


def merge_pair(tmp_path, left, right):
    a, ah = backup(left, tmp_path / 'left-backup')
    b, bh = backup(right, tmp_path / 'right-backup')
    action = merging.ResearchContentMerge(a, ah, b, bh, tmp_path / 'merged')
    return action, a, b


def assert_content(item, app):
    assert app.evidence(item.workspace, item.topic) == item.saved
    assert app.run_query(item.workspace, item.topic, item.query)
    assert app.evidence_tags(item.workspace, item.topic, item.evidence) == ('tag one', 'tag two')
    hit, = app.search(item.workspace, item.original.split('\f')[0], collection=item.collection)
    assert app.copy(item.workspace, hit.ref, hit.revision, 'quote') == hit.quote
    assert hit.quote == item.original.split('\f')[0]
    with app.read(item.workspace) as db:
        assert AcademicLibrary(db, item.workspace).get(item.reference.id) == item.reference
    return hit


def test_disjoint_full_graphs_keep_history_evidence_exact_ids_and_sources(factory, tmp_path):
    left, right = factory('Left'), factory('Right')
    action, a, b = merge_pair(tmp_path, left, right)
    before = inventory(a), inventory(b)
    preview = action.prepare()
    description = json.loads(preview.payload)
    assert description['profile'] == merging.PROFILE
    assert {row['state'] for row in description['workspaces']} == {'left_only', 'right_only'}
    assert not description['incompatible_stable_ids']
    assert all(description['omitted'][side] for side in ('left', 'right'))
    assert 'operational' in description['notice'] and 'audio' in description['notice']
    result = action.merge(preview, {left.workspace: 'left', right.workspace: 'right'},
                          acknowledged_omissions=True)
    restored = LibraryApp(result.path)
    try:
        expected = {table: sorted(set(left.snapshot[table] + right.snapshot[table]), key=repr)
                    for table in left.snapshot}
        assert logical(restored) == expected
        assert_content(left, restored)
        assert_content(right, restored)
        with restored.read() as db:
            assert db.execute('SELECT count(*) FROM sealed').fetchone()[0] == 4
            assert db.execute('SELECT count(*) FROM indexed').fetchone()[0] == 4
            assert db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'Needle'").fetchone()[0] == 2
            ids = [row[0] for row in db.execute('SELECT id FROM page')]
            assert len(ids) == len(set(ids)) == 8
        assert len(tuple(restored.assets_dir.iterdir())) == 1
        assert next(restored.assets_dir.iterdir()).read_bytes() == left.original.encode('utf-8')
        assert not (result.path / 'state' / 'operations' / 'resources.json').exists()
        assert not (result.path / 'imports.sqlite').exists()
        assert not (result.path / INCOMPLETE).exists()
        followup = tmp_path / 'merged-full-backup'
        checksum = create_backup(restored, followup)
        final_root = restore_backup(followup, tmp_path / 'merged-roundtrip', checksum)
        final = LibraryApp(final_root)
        try:
            assert logical(final) == expected
            assert_content(left, final)
            assert_content(right, final)
        finally:
            assert final.close_search()
    finally:
        assert restored.close_search()
    assert (inventory(a), inventory(b)) == before
    assert not list(tmp_path.glob('.carr-research-merge-*'))


def test_identical_workspace_needs_one_whole_side_and_deduplicates(factory, tmp_path):
    left = factory('One')
    a, ah = backup(left, tmp_path / 'left-backup')
    b, bh = backup(left, tmp_path / 'right-backup')
    action = merging.ResearchContentMerge(a, ah, b, bh, tmp_path / 'merged')
    preview = action.prepare()
    row, = json.loads(preview.payload)['workspaces']
    assert row['state'] == 'identical'
    result = action.merge(preview, {left.workspace: 'right'}, acknowledged_omissions=True)
    app = LibraryApp(result.path)
    try:
        assert logical(app) == left.snapshot
        assert_content(left, app)
    finally:
        assert app.close_search()


@pytest.mark.parametrize('side', ['left', 'right'])
def test_conflicting_workspace_chooses_complete_snapshot_without_retargeting(factory, tmp_path, side):
    left = factory('Conflict')
    a, ah = backup(left, tmp_path / 'left-backup')
    left.app.rename_workspace(left.workspace, 'Changed on right')
    with left.app.write(left.workspace) as db:
        db.execute('UPDATE research_evidence SET note=? WHERE workspace=? AND topic=? AND id=?',
                   ('Right note, same immutable anchor', left.workspace, left.topic, left.evidence))
    right_snapshot = logical(left.app)
    b, bh = backup(left, tmp_path / 'right-backup')
    action = merging.ResearchContentMerge(a, ah, b, bh, tmp_path / 'merged')
    preview = action.prepare()
    row, = json.loads(preview.payload)['workspaces']
    assert row['state'] == 'conflict' and row['left']['sha256'] != row['right']['sha256']
    result = action.merge(preview, {left.workspace: side}, acknowledged_omissions=True)
    app = LibraryApp(result.path)
    try:
        assert logical(app) == (left.snapshot if side == 'left' else right_snapshot)
        evidence, = app.evidence(left.workspace, left.topic)
        assert evidence.anchor == left.saved[0].anchor
        assert evidence.note == (left.saved[0].note if side == 'left' else 'Right note, same immutable anchor')
    finally:
        assert app.close_search()


def test_cross_workspace_global_revision_collision_is_previewed_and_refused(tmp_path):
    inputs, originals = [], []
    for label in ('left', 'right'):
        app = LibraryApp(tmp_path / (label + '-library'))
        workspace = app.create_workspace(label)
        raw = ('Needle ' + label).encode()
        digest = sha256(raw).hexdigest()
        app.assets.retain(raw, 'text/plain', digest)
        registration = Registration('work', 'edition',
            dict(sha256=digest, mime='text/plain', size=len(raw)), title=label)
        with closing(Storage(app.database, schema_version=5)) as storage:
            _CatalogPublisher(storage, registration).publish_revision(
                workspace, 'edition', 'fixed-shared-revision', digest, '{}', (Page(raw.decode()),))
        folder = tmp_path / (label + '-backup')
        checksum = create_backup(app, folder)
        inputs.append((folder, checksum, workspace))
        originals.append(inventory(folder))
        assert app.close_search()
    left, right = inputs
    action = merging.ResearchContentMerge(*left[:2], *right[:2], tmp_path / 'merged')
    preview = action.prepare()
    conflict, = json.loads(preview.payload)['incompatible_stable_ids']
    assert conflict == dict(kind='revision', identity='fixed-shared-revision',
                            left_workspaces=[left[2]], right_workspaces=[right[2]])
    with pytest.raises(ValueError, match='incompatible stable identities'):
        action.merge(preview, {left[2]: 'left', right[2]: 'right'}, acknowledged_omissions=True)
    assert not action.destination.exists()
    result = action.merge(preview, {left[2]: 'left', right[2]: 'omit'}, acknowledged_omissions=True)
    app = LibraryApp(result.path)
    try:
        assert [w.id for w in app.workspaces()] == [left[2]]
        assert app.search(left[2], 'Needle')
    finally:
        assert app.close_search()
    assert [inventory(row[0]) for row in inputs] == originals


@pytest.mark.parametrize('invalid', ['missing-choice', 'unavailable-side', 'all-omitted', 'no-ack', 'forged-preview'])
def test_explicit_exact_preview_and_choices_are_required(factory, tmp_path, invalid):
    left, right = factory('Left', history=False), factory('Right', history=False)
    action, a, b = merge_pair(tmp_path, left, right)
    preview = action.prepare()
    choices, ack = {left.workspace: 'left', right.workspace: 'right'}, True
    if invalid == 'missing-choice':
        choices.pop(right.workspace)
    elif invalid == 'unavailable-side':
        choices[left.workspace] = 'right'
    elif invalid == 'all-omitted':
        choices = dict.fromkeys(choices, 'omit')
    elif invalid == 'no-ack':
        ack = False
    else:
        preview = merging.MergePreview(preview.payload, action)
    before = inventory(a), inventory(b)
    with pytest.raises(ValueError):
        action.merge(preview, choices, acknowledged_omissions=ack)
    assert not action.destination.exists() and (inventory(a), inventory(b)) == before


@pytest.mark.parametrize('damage', ['original', 'database', 'manifest'])
def test_invalid_input_never_reaches_copy_or_destination(factory, tmp_path, monkeypatch, damage):
    left, right = factory('Left', history=False), factory('Right', history=False)
    action, a, b = merge_pair(tmp_path, left, right)
    if damage == 'original':
        next((a / 'originals').iterdir()).unlink()
    elif damage == 'database':
        with (a / 'library.sqlite').open('ab') as stream:
            stream.write(b'damaged')
    else:
        (a / 'manifest.json').write_text('{}')
    def forbidden(*args, **kwargs):
        raise AssertionError('No research copy before full input verification')
    monkeypatch.setattr(action, '_copy_workspace', forbidden)
    with pytest.raises(ValueError):
        action.prepare()
    assert not action.destination.exists()


@pytest.mark.parametrize('error_type', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_fault_after_real_graph_copy_rolls_back_and_closes_private_databases(factory, tmp_path, monkeypatch, error_type):
    left, right = factory('Left'), factory('Right')
    action, a, b = merge_pair(tmp_path, left, right)
    preview, before = action.prepare(), (inventory(a), inventory(b))
    original = action._copy_workspace
    reached = []
    def fault(db, incoming, identity, check):
        original(db, incoming, identity, check)
        assert db.execute('SELECT count(*) FROM research_evidence').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM page').fetchone()[0] == 4
        path = Path(db.execute('PRAGMA database_list').fetchone()[2])
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader:
            assert reader.execute('SELECT count(*) FROM workspace').fetchone()[0] == 0
        reached.append(path)
        raise error_type('Actual rows copied, before commit')
    monkeypatch.setattr(action, '_copy_workspace', fault)
    with pytest.raises(error_type, match='Actual rows'):
        action.merge(preview, {left.workspace: 'left', right.workspace: 'right'},
                     acknowledged_omissions=True)
    assert len(reached) == 1 and not reached[0].exists()
    assert not action.destination.exists()
    assert (inventory(a), inventory(b)) == before
    assert not list(tmp_path.glob('.carr-research-merge-*'))


def test_real_sqlite_validation_progress_remains_cancellable(factory, tmp_path, monkeypatch):
    left, right = factory('Left'), factory('Right')
    action, _, _ = merge_pair(tmp_path, left, right)
    preview = action.prepare()
    original, validating, observed = Storage.rebuild, threading.Event(), []
    def rebuild(storage):
        validating.set()
        try:
            return original(storage)
        finally:
            validating.clear()
    def cancelled():
        if validating.is_set():
            observed.append(True)
            return True
        return False
    monkeypatch.setattr(Storage, 'rebuild', rebuild)
    with pytest.raises(InterruptedError):
        action.merge(preview, {left.workspace: 'left', right.workspace: 'right'},
                     acknowledged_omissions=True, cancelled=cancelled)
    assert observed and not action.destination.exists()


def test_source_directory_replacement_after_preview_is_refused(factory, tmp_path):
    left, right = factory('Left', history=False), factory('Right', history=False)
    action, a, _ = merge_pair(tmp_path, left, right)
    preview = action.prepare()
    old = a.with_name('old-left')
    a.rename(old)
    a.mkdir()
    try:
        with pytest.raises(ValueError, match='changed'):
            action.merge(preview, {left.workspace: 'left', right.workspace: 'right'},
                         acknowledged_omissions=True)
        assert not action.destination.exists()
    finally:
        a.rmdir()
        old.rename(a)


def test_existing_output_and_undersized_staging_are_refused_before_restore(factory, tmp_path, monkeypatch):
    left, right = factory('Left', history=False), factory('Right', history=False)
    a, ah = backup(left, tmp_path / 'left-backup')
    b, bh = backup(right, tmp_path / 'right-backup')
    occupied = tmp_path / 'occupied'
    occupied.mkdir()
    (occupied / 'keep').write_bytes(b'unchanged')
    with pytest.raises(FileExistsError):
        merging.ResearchContentMerge(a, ah, b, bh, occupied)
    action = merging.ResearchContentMerge(a, ah, b, bh, tmp_path / 'merged',
                                          backup_budget=replace(BackupBudget(), staging_bytes=1))
    def forbidden(*args, **kwargs):
        raise AssertionError('No restore after failed measured staging admission')
    monkeypatch.setattr(merging.library_backup, 'restore_backup', forbidden)
    with pytest.raises(ValueError, match='staging-byte'):
        action.prepare()
    assert (occupied / 'keep').read_bytes() == b'unchanged' and not action.destination.exists()


def test_completed_real_publication_wins_over_late_cancellation(factory, tmp_path, monkeypatch):
    left, right = factory('Left', history=False), factory('Right', history=False)
    action, _, _ = merge_pair(tmp_path, left, right)
    preview, stopped = action.prepare(), threading.Event()
    original = merging.library_backup.restore_backup
    def restore(source, destination, checksum, **options):
        result = original(source, destination, checksum, **options)
        if Path(destination) == action.destination:
            assert not (result / INCOMPLETE).exists()
            stopped.set()
        return result
    monkeypatch.setattr(merging.library_backup, 'restore_backup', restore)
    result = action.merge(preview, {left.workspace: 'left', right.workspace: 'right'},
                          acknowledged_omissions=True, cancelled=stopped.is_set)
    assert stopped.is_set() and result.path == action.destination
    app = LibraryApp(result.path)
    try:
        assert len(app.workspaces()) == 2
        assert_content(left, app)
        assert_content(right, app)
    finally:
        assert app.close_search()


def test_actual_owned_import_receipt_is_preserved_without_request_replay(factory, tmp_path):
    left, right = factory('Left', history=False), factory('Right', history=False)
    source = tmp_path / 'owned-source.txt'
    source.write_text('Genuine child receipt source.', encoding='utf-8')
    imported = left.app.import_file(left.workspace, source, title='Owned import',
                                    import_budget=ImportBudget())
    assert imported['state'] == 'complete'
    with left.app.read() as db:
        receipt, = [tuple(row) for row in db.execute('SELECT * FROM import_publication')]
    action, _, _ = merge_pair(tmp_path, left, right)
    preview = action.prepare()
    assert any('import' in value for value in json.loads(preview.payload)['omitted']['left'])
    result = action.merge(preview, {left.workspace: 'left', right.workspace: 'right'},
                          acknowledged_omissions=True)
    app = LibraryApp(result.path)
    try:
        with app.read() as db:
            assert [tuple(row) for row in db.execute('SELECT * FROM import_publication')] == [receipt]
        assert app.import_requests(left.workspace) == ()
        hit, = app.search(left.workspace, 'Genuine child receipt source.')
        assert app.copy(left.workspace, hit.ref, hit.revision, 'quote') == 'Genuine child receipt source.'
    finally:
        assert app.close_search()


def test_genuine_cancellation_callback_cannot_reenter_or_replace_active_preview(factory, tmp_path):
    left, right = factory('Left', history=False), factory('Right', history=False)
    action, _, _ = merge_pair(tmp_path, left, right)
    attempts = []
    def cancelled():
        if not attempts:
            attempts.append(True)
            with pytest.raises(ValueError, match='already active'):
                action.prepare()
        return False
    preview = action.prepare(cancelled=cancelled)
    assert attempts and action._preview is preview
    with pytest.raises(InterruptedError):
        action.merge(preview, {left.workspace: 'left', right.workspace: 'right'},
                     acknowledged_omissions=True, cancelled=lambda: True)
    # The same owner is released after cancellation and can be explicitly retried.
    current = action.prepare()
    assert current is not preview and not action.destination.exists()
