"""Real journal, original bytes and publication: bounded streaming validation."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import sqlite3

import pytest

from carr_library.ingest.pipeline import Ingest, IngestError
from carr_library.ingest.resource_budget import ImportBudget
from carr_library.storage.revisions import Storage


@pytest.fixture
def services(tmp_path):
    storage = Storage(tmp_path / 'library.sqlite')
    ingest = Ingest(tmp_path / 'imports.sqlite', storage)
    try:
        yield tmp_path, storage, ingest
    finally:
        ingest.close()
        storage.close()


def source(root, count=40):
    rows = [f'Exact source {i:05d}. Київ.\r\nSecond original line.\n' for i in range(count)]
    raw = '\f'.join(rows).encode('utf-8')
    path = root / 'original.txt'
    path.write_bytes(raw)
    return path, rows, raw


def budget(count):
    return ImportBudget(source_bytes=8 * 1024**2, segment_bytes=1024**2,
                        segments=count, steps=count + 1, seconds=120,
                        memory_bytes=256 * 1024**2, library_bytes=256 * 1024**2,
                        temporary_bytes=256 * 1024**2)


def test_actual_2001_segments_resume_and_publish_without_collecting_checkpoint_text(services, monkeypatch):
    root, storage, ingest = services
    path, rows, raw = source(root, 2001)
    selected = budget(len(rows))
    job = ingest.start('A', 'E', root, path.name, import_budget=selected)
    real_record = ingest._record
    checks = []
    def checked(row, ordinal, value):
        checks.append(ordinal)
        return real_record(row, ordinal, value)
    monkeypatch.setattr(ingest, '_record', checked)
    first = ingest.run('A', job, root, path.name, max_steps=1000, import_budget=selected)
    assert first['state'] == 'pending' and first['completed'] == 1000
    assert storage.db.execute('SELECT count(*) FROM active_page').fetchone()[0] == 0
    # A fresh actual connection has no in-memory validation generation to inherit.
    ingest.close()
    ingest.db = sqlite3.connect(root / 'imports.sqlite', isolation_level=None)
    ingest.db.row_factory = sqlite3.Row
    ingest.db.execute('PRAGMA foreign_keys=ON')
    result = ingest.run('A', job, root, path.name, import_budget=selected)
    assert result['state'] == 'complete' and result['completed'] == len(rows)
    actual = storage.db.execute('SELECT raw,physical FROM active_page ORDER BY ordinal')
    for expected, retained in zip(rows, actual, strict=True):
        assert tuple(retained) == (expected, None)
    assert path.read_bytes() == raw and sha256(raw).hexdigest() == result['asset']
    assert len(checks) < 6 * len(rows) + 20, len(checks)
    saved = ingest.db.execute('SELECT config,count FROM job WHERE id=?', (job,)).fetchone()
    assert tuple(saved) == ('{"adapter":"strict-utf8-v1","kind":"logical_segment"}', len(rows))


@pytest.mark.parametrize('foreign_connection', [False, True])
def test_unknown_journal_write_cannot_reuse_a_previous_checked_generation(services, monkeypatch, foreign_connection):
    root, storage, ingest = services
    path, rows, raw = source(root, 4)
    job = ingest.start('A', 'E', root, path.name)
    original_transaction = ingest.transaction
    changed = []

    @contextmanager
    def transaction():
        with original_transaction():
            yield
        if not changed and ingest.db.execute('SELECT count(*) FROM checkpoint WHERE job=?', (job,)).fetchone()[0]:
            changed.append(True)
            if foreign_connection:
                with sqlite3.connect(root / 'imports.sqlite') as other:
                    other.execute('UPDATE checkpoint SET payload=? WHERE job=? AND ordinal=1', ('{}', job))
            else:
                ingest.db.execute('UPDATE checkpoint SET payload=? WHERE job=? AND ordinal=1', ('{}', job))

    monkeypatch.setattr(ingest, 'transaction', transaction)
    with pytest.raises(IngestError, match='integrity'):
        ingest.run('A', job, root, path.name)
    assert changed == [True]
    assert storage.db.execute('SELECT count(*) FROM active_page').fetchone()[0] == 0
    assert path.read_bytes() == raw


def test_changed_journal_during_streamed_publication_rolls_back_actual_storage(services, monkeypatch):
    root, storage, ingest = services
    path, _, raw = source(root, 4)
    job = ingest.start('A', 'E', root, path.name)
    publish = storage.publish_revision
    changed = []

    def actual_publish(workspace, edition, revision, asset, config, pages):
        def interrupted_pages():
            for index, page in enumerate(pages):
                yield page
                if not index:
                    changed.append(True)
                    ingest.db.execute('UPDATE checkpoint SET payload=? WHERE job=? AND ordinal=1', ('{}', job))
        return publish(workspace, edition, revision, asset, config, interrupted_pages())

    monkeypatch.setattr(storage, 'publish_revision', actual_publish)
    with pytest.raises(IngestError, match='changed|integrity'):
        ingest.run('A', job, root, path.name)
    assert changed == [True]
    assert storage.db.execute('SELECT count(*) FROM active_page').fetchone()[0] == 0
    assert storage.db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
    assert path.read_bytes() == raw


def test_selected_step_budget_leaves_exact_checkpoints_for_explicit_resume(services):
    root, storage, ingest = services
    path, rows, raw = source(root, 7)
    selected = replace(budget(7), steps=3)
    job = ingest.start('A', 'E', root, path.name, import_budget=selected)
    first = ingest.run('A', job, root, path.name, import_budget=selected)
    assert (first['state'], first['completed']) == ('pending', 3)
    saved = tuple(ingest.db.execute('SELECT ordinal,payload,digest FROM checkpoint WHERE job=? ORDER BY ordinal', (job,)))
    second = ingest.run('A', job, root, path.name, import_budget=selected)
    assert (second['state'], second['completed']) == ('pending', 6)
    assert tuple(ingest.db.execute('SELECT ordinal,payload,digest FROM checkpoint WHERE job=? AND ordinal<=3 ORDER BY ordinal', (job,))) == saved
    third = ingest.run('A', job, root, path.name, import_budget=selected)
    assert third['state'] == 'complete'
    assert [row[0] for row in storage.db.execute('SELECT raw FROM active_page ORDER BY ordinal')] == rows
    assert path.read_bytes() == raw
