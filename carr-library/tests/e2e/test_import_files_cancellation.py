"""Cancellation stops the selected-file loop before opening further originals."""
from pathlib import Path

from carr_library.application import LibraryApp


def test_cancel_after_committed_text_leaves_next_original_unopened(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected files')
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    first.write_text('Published first railway source.', encoding='utf-8')
    second.write_text('Unattempted second railway source.', encoding='utf-8')
    results = app.import_files(workspace, [first, second],
                               cancelled=lambda: bool(app.browse(workspace)))
    assert [row['state'] for row in results] == ['complete', 'cancelled']
    assert results[1]['file'] == 'second.txt'
    assert len(list(app.assets_dir.iterdir())) == 1
    reopened = LibraryApp(app.root)
    assert len(reopened.browse(workspace)) == 1
    assert len(reopened.search(workspace, 'Published')) == 1


def test_precancelled_selection_opens_no_source(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Before selected files')
    def forbidden(*args, **kwargs):
        raise AssertionError('A cancelled selection must not open another source.')
    monkeypatch.setattr(app, 'import_file', forbidden)
    result, = app.import_files(workspace, [tmp_path / 'one.txt'], cancelled=lambda: True)
    assert result['file'] == 'one.txt' and result['state'] == 'cancelled'
    assert list(app.assets_dir.iterdir()) == []


def test_interrupted_error_is_acknowledged_cancellation_and_stops_selection(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Interrupted selection')
    attempted = []
    def interrupted(scope, path, **options):
        attempted.append(Path(path).name)
        raise InterruptedError('The import acknowledged cancellation.')
    monkeypatch.setattr(app, 'import_file', interrupted)
    results = app.import_files(workspace, [tmp_path / 'one.txt', tmp_path / 'two.txt'])
    assert attempted == ['one.txt']
    assert len(results) == 1 and results[0]['state'] == 'cancelled'


def test_cancelled_receipt_stops_even_if_later_callback_is_clear(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Acknowledged worker stop')
    attempted = []
    def stopped(scope, path, **options):
        attempted.append(Path(path).name)
        return {'state': 'cancelled', 'message': 'Worker stopped.'}
    monkeypatch.setattr(app, 'import_file', stopped)
    results = app.import_files(workspace, [tmp_path / 'one.txt', tmp_path / 'two.txt'],
                               cancelled=lambda: False)
    assert attempted == ['one.txt']
    assert len(results) == 1 and results[0]['state'] == 'cancelled'


def test_ordinary_file_error_does_not_cancel_later_valid_source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Recoverable file error')
    valid = tmp_path / 'valid.txt'
    valid.write_text('A valid railway source after a missing file.', encoding='utf-8')
    results = app.import_files(workspace, [tmp_path / 'missing.txt', valid])
    assert [row['state'] for row in results] == ['failed', 'complete']
    assert len(app.browse(workspace)) == 1


def _observe_real_parsing_descriptors(monkeypatch):
    """Forward the actual held-descriptor context; do not replace its checks."""
    from contextlib import contextmanager
    from carr_library.catalog import intake

    original = intake._opened
    active, completed = [], []

    @contextmanager
    def observed(path, identity, *, cancelled=lambda: False):
        row = None
        try:
            with original(path, identity, cancelled=cancelled) as stream:
                if (Path(path).name == 'source.txt'
                        and Path(path).parent.name.startswith('carr-ingest-')):
                    row = dict(path=Path(path), stream=stream, validated=False)
                    active.append(row)
                yield stream
            if row is not None:
                row['validated'] = True
        finally:
            if row is not None:
                row['closed'] = row['stream'].closed
                active.remove(row)
                completed.append(row)

    monkeypatch.setattr(intake, '_opened', observed)
    return active, completed


def test_late_cancel_keeps_real_parsing_descriptor_through_commit_and_stops_next_input(
        tmp_path, monkeypatch):
    from hashlib import sha256
    from carr_library import application

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Real commit precedes late cancellation')
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    raw = b'Committed railway text.\r\n'
    first.write_bytes(raw)
    second.write_bytes(b'Unopened next railway original.')
    before_second = second.read_bytes()
    active, completed = _observe_real_parsing_descriptors(monkeypatch)
    publish = application._CatalogPublisher.publish_revision
    committed_reader, attempts = [], []
    original_import = app.import_file

    def imported(scope, path, **options):
        attempts.append(Path(path))
        return original_import(scope, path, **options)

    def observed_publish(owner, *args, **kwargs):
        assert len(active) == 1 and not active[0]['stream'].closed
        held = active[0]
        value = publish(owner, *args, **kwargs)
        # This independent connection sees the real canonical COMMIT while
        # the exact parsing descriptor is still held by its original owner.
        assert len(app.browse(workspace)) == 1
        with app.read(workspace) as db:
            assert tuple(db.execute('SELECT raw,digest FROM page').fetchone()) == (
                raw.decode('utf-8'), sha256(raw).hexdigest())
        assert active == [held] and not held['stream'].closed
        committed_reader.append(held)
        return value

    monkeypatch.setattr(app, 'import_file', imported)
    monkeypatch.setattr(application._CatalogPublisher, 'publish_revision', observed_publish)
    try:
        results = app.import_files(
            workspace, [first, second], cancelled=lambda: bool(app.browse(workspace)))
        assert [result['state'] for result in results] == ['complete', 'cancelled']
        assert attempts == [first] and results[1]['file'] == second.name
        assert len(committed_reader) == 1
        assert committed_reader[0] in completed
        assert committed_reader[0]['validated'] and committed_reader[0]['closed']
        assert active == [] and all(row['closed'] for row in completed)
        assert first.read_bytes() == raw and second.read_bytes() == before_second
        originals = tuple(app.assets_dir.iterdir())
        assert len(originals) == 1 and originals[0].read_bytes() == raw
    finally:
        assert app.close_search()


def test_cancellation_before_real_text_publication_still_rolls_back(tmp_path, monkeypatch):
    import threading
    from carr_library import application

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Cancellation before canonical publication')
    source = tmp_path / 'source.txt'
    raw = b'Cancellation must not publish this original.'
    source.write_bytes(raw)
    stop = threading.Event()
    active, completed = _observe_real_parsing_descriptors(monkeypatch)
    publish = application._CatalogPublisher.publish_revision
    reached = []

    def cancelled_publish(owner, *args, **kwargs):
        assert len(active) == 1 and not active[0]['stream'].closed
        reached.append(True)
        stop.set()
        return publish(owner, *args, **kwargs)

    monkeypatch.setattr(application._CatalogPublisher, 'publish_revision', cancelled_publish)
    try:
        result = app.import_file(workspace, source, cancelled=stop.is_set)
        assert result['state'] == 'cancelled' and reached == [True]
        assert app.browse(workspace) == ()
        with app.read(workspace) as db:
            for table in ('revision', 'sealed', 'page', 'indexed', 'catalog_edition'):
                assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
        assert active == [] and completed and all(row['closed'] for row in completed)
        assert source.read_bytes() == raw
    finally:
        assert app.close_search()


def test_postcommit_staged_mutation_is_not_hidden_by_late_cancellation(tmp_path, monkeypatch):
    import os
    import threading
    import pytest
    from hashlib import sha256
    from carr_library import application
    from carr_library.catalog.intake import IntakeError

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Postcommit source identity still checked')
    source = tmp_path / 'source.txt'
    raw = b'Original canonical railway text.'
    source.write_bytes(raw)
    stop = threading.Event()
    active, completed = _observe_real_parsing_descriptors(monkeypatch)
    publish = application._CatalogPublisher.publish_revision
    changed = []

    def changed_after_commit(owner, *args, **kwargs):
        assert len(active) == 1
        held = active[0]
        value = publish(owner, *args, **kwargs)
        assert len(app.browse(workspace)) == 1 and not held['stream'].closed
        path = held['path']
        before = path.stat()
        assert path.read_bytes() == raw
        path.write_bytes(b'X' + raw[1:])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        assert path.stat().st_size == before.st_size
        stop.set()
        changed.append(held)
        return value

    monkeypatch.setattr(application._CatalogPublisher, 'publish_revision', changed_after_commit)
    try:
        with pytest.raises(IntakeError, match='Source changed while reading'):
            app.import_file(workspace, source, cancelled=stop.is_set)
        assert len(changed) == 1
        assert changed[0] in completed and changed[0]['closed']
        assert changed[0]['validated'] is False and active == []
        assert source.read_bytes() == raw
        assert tuple(app.assets_dir.iterdir())[0].read_bytes() == raw
        with app.read(workspace) as db:
            assert tuple(db.execute('SELECT raw,digest FROM page').fetchone()) == (
                raw.decode('utf-8'), sha256(raw).hexdigest())
    finally:
        assert app.close_search()


def test_recovered_revision_never_acquires_this_runs_publication_latch(tmp_path, monkeypatch):
    import threading
    import pytest
    from carr_library import application
    from carr_library.ingest.pipeline import Ingest

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Existing publication is a different boundary')
    source = tmp_path / 'source.txt'
    raw = b'Already published railway source.'
    source.write_bytes(raw)
    metadata = dict(work_id='a' * 32, edition_id='b' * 32)
    first = app.import_file(workspace, source, **metadata)
    assert first['state'] == 'complete'
    stop = threading.Event()
    active, completed = _observe_real_parsing_descriptors(monkeypatch)
    recover = Ingest._recover
    recovered = []

    def observed_recover(owner, row, records):
        result = recover(owner, row, records)
        if result:
            assert len(active) == 1 and not active[0]['stream'].closed
            recovered.append(active[0])
            stop.set()
        return result

    def forbidden_publish(*args, **kwargs):
        raise AssertionError('A genuine recovered revision must not be republished.')

    monkeypatch.setattr(Ingest, '_recover', observed_recover)
    monkeypatch.setattr(application._CatalogPublisher, 'publish_revision', forbidden_publish)
    try:
        with pytest.raises(InterruptedError, match='Source verification cancelled'):
            app.import_file(workspace, source, cancelled=stop.is_set, **metadata)
        assert len(recovered) == 1 and recovered[0] in completed
        assert recovered[0]['closed'] and not recovered[0]['validated']
        assert active == [] and all(row['closed'] for row in completed)
        assert len(app.browse(workspace)) == 1 and source.read_bytes() == raw
        with app.read(workspace) as db:
            assert db.execute('SELECT id FROM revision').fetchone()[0] == first['id']
            assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
    finally:
        assert app.close_search()
