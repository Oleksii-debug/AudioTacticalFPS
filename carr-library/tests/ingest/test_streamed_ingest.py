import json
import sqlite3

import pytest

from carr_library.ingest.pipeline import canonical, digest, IngestError
from carr_library.ingest.streamed_ingest import StreamedTextIngest
from carr_library.ingest.streamed_text import TextProfile, FRAGMENT_CHARS
from carr_library.storage.revisions import Storage


@pytest.fixture
def local(tmp_path):
    storage = Storage(tmp_path / 'library.sqlite')
    ingest = StreamedTextIngest(tmp_path / 'imports.sqlite', storage, profile=TextProfile())
    path = tmp_path / 'book.txt'
    path.write_text('First\r\n' + 'x' * FRAGMENT_CHARS + '\fДруге джерело', encoding='utf-8', newline='')
    try:
        yield path, storage, ingest
    finally:
        ingest.close()
        storage.close()


def test_streamed_resume_uses_existing_journal_and_atomic_logical_fragments(local):
    path, storage, ingest = local
    job = ingest.start('A', 'edition', path.parent, path.name)
    assert ingest.start('A', 'edition', path.parent, path.name) == job
    result = ingest.run('A', job, path.parent, path.name, max_steps=1)
    assert result['state'] == 'pending' and result['completed'] == 1
    assert storage.db.execute('SELECT 1 FROM active_page').fetchone() is None
    with pytest.raises(ValueError):
        ingest.inspect('B', job)
    ingest.close()
    ingest.db = sqlite3.connect(path.parent / 'imports.sqlite', isolation_level=None)
    ingest.db.row_factory = sqlite3.Row
    ingest.db.execute('PRAGMA foreign_keys=ON')
    result = ingest.run('A', job, path.parent, path.name)
    assert result['state'] == 'complete' and result['completed'] == result['total'] == 3
    pages = tuple(storage.db.execute('SELECT physical,raw FROM active_page ORDER BY ordinal'))
    assert all(item[0] is None for item in pages)
    assert pages[0][1] + pages[1][1] + '\f' + pages[2][1] == path.read_bytes().decode('utf-8')
    assert ingest.run('A', job, path.parent, path.name)['state'] == 'complete'
    assert storage.db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


def test_cancel_preserves_checkpoints_and_retry_revalidates_everything(local):
    path, storage, ingest = local
    job = ingest.start('A', 'edition', path.parent, path.name)
    ingest.run('A', job, path.parent, path.name, max_steps=1)
    assert ingest.run('A', job, path.parent, path.name, cancelled=lambda: True)['state'] == 'cancelled'
    assert ingest.inspect('A', job)['completed'] == 1
    assert storage.db.execute('SELECT 1 FROM revision').fetchone() is None
    ingest.retry('A', job)
    assert ingest.run('A', job, path.parent, path.name)['state'] == 'complete'


@pytest.mark.parametrize('change', ['text', 'offset', 'extra', 'sequence'])
def test_self_consistent_checkpoint_tampering_cannot_become_source_evidence(local, change):
    path, storage, ingest = local
    job = ingest.start('A', 'edition', path.parent, path.name)
    ingest.run('A', job, path.parent, path.name, max_steps=1)
    row = ingest.db.execute('SELECT payload FROM checkpoint WHERE job=?', (job,)).fetchone()
    value = json.loads(row[0])
    if change == 'text':
        value['raw_text'] = 'invented text'
    elif change == 'offset':
        value['provenance']['offset'] = 1
    elif change == 'extra':
        value['provenance']['approved'] = True
    else:
        ingest.db.execute('UPDATE checkpoint SET ordinal=2 WHERE job=?', (job,))
    raw = canonical(value)
    ingest.db.execute('UPDATE checkpoint SET payload=?,digest=? WHERE job=?', (raw, digest(raw), job))
    with pytest.raises(IngestError):
        ingest.run('A', job, path.parent, path.name)
    assert storage.db.execute('SELECT 1 FROM revision').fetchone() is None


def test_changed_source_and_configuration_do_not_retarget_a_partial_job(local):
    path, storage, ingest = local
    job = ingest.start('A', 'edition', path.parent, path.name)
    ingest.run('A', job, path.parent, path.name, max_steps=1)
    original = path.read_bytes()
    path.write_bytes(original[:-1] + b'X')
    with pytest.raises(ValueError):
        ingest.run('A', job, path.parent, path.name)
    path.write_bytes(original)
    ingest.profile = TextProfile(70 * 1024**2)
    with pytest.raises(ValueError):
        ingest.retry('A', job)
    assert storage.db.execute('SELECT 1 FROM revision').fetchone() is None


def test_real_publication_failure_preserves_resumable_checkpoints(local, monkeypatch):
    path, storage, ingest = local
    job = ingest.start('A', 'edition', path.parent, path.name)
    publish = storage.publish_revision
    def failed(*args):
        raise OSError('injected disk full')
    monkeypatch.setattr(storage, 'publish_revision', failed)
    with pytest.raises(OSError):
        ingest.run('A', job, path.parent, path.name)
    assert ingest.inspect('A', job)['completed'] == 3
    assert storage.db.execute('SELECT 1 FROM revision').fetchone() is None
    monkeypatch.setattr(storage, 'publish_revision', publish)
    ingest.retry('A', job)
    assert ingest.run('A', job, path.parent, path.name)['state'] == 'complete'


def test_cross_database_crash_after_commit_recovers_without_republishing(local, monkeypatch):
    path, storage, ingest = local
    job = ingest.start('A', 'edition', path.parent, path.name)
    publish = storage.publish_revision
    def crashed(*args):
        publish(*args)
        raise KeyboardInterrupt('after durable publication')
    monkeypatch.setattr(storage, 'publish_revision', crashed)
    with pytest.raises(KeyboardInterrupt):
        ingest.run('A', job, path.parent, path.name)
    assert ingest.inspect('A', job)['state'] == 'pending'
    monkeypatch.setattr(storage, 'publish_revision', lambda *_: pytest.fail('must recover exact existing publication'))
    assert ingest.run('A', job, path.parent, path.name)['state'] == 'complete'
    assert storage.db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


def test_new_active_revision_is_never_overwritten_by_stale_streamed_work(local):
    path, storage, ingest = local
    original = path.read_bytes()
    first = ingest.start('A', 'edition', path.parent, path.name)
    path.write_bytes(b'New independent revision')
    second = ingest.start('A', 'edition', path.parent, path.name)
    assert ingest.run('A', second, path.parent, path.name)['state'] == 'complete'
    path.write_bytes(original)
    assert ingest.run('A', first, path.parent, path.name)['state'] == 'conflict'
    assert storage.db.execute('SELECT revision FROM active').fetchone()[0] == second


def test_start_cancellation_keeps_the_existing_journal_empty(local):
    path, storage, ingest = local
    with pytest.raises(InterruptedError, match='cancelled'):
        ingest.start('A', 'edition', path.parent, path.name, cancelled=lambda: True)
    assert ingest.db.execute('SELECT count(*) FROM job').fetchone()[0] == 0
    assert storage.db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
