"""Actual SQLite/original accounting under the canonical current writer.

Only free-space observations and explicit metadata-race facets are controlled.
The library, journal pages, retained source, receipt and rollback are real.
"""
from contextlib import closing, contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import os
import sqlite3
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest import session as module
from carr_library.ingest import worker
from carr_library.ingest.resource_budget import ImportBudget, MIB
from carr_library.ingest.streamed_text import TextProfile, inspect_text
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION, Storage


def budget(**changes):
    return replace(ImportBudget(source_bytes=2*MIB, segment_bytes=MIB,
        segments=1000, steps=1001, memory_bytes=128*MIB,
        library_bytes=64*MIB, temporary_bytes=16*MIB), **changes)


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    return app, app.create_workspace('Current cooperative accounting')


@contextmanager
def accounting(app, workspace, policy, **options):
    with exclusive_writer(app.root):
        with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
            with module.import_accounting(app, workspace, storage, policy, **options) as owner:
                yield owner


def retain(app, owner, directory, index, raw=None):
    raw = raw if raw is not None else ('Original %d — точні bytes.\r\n' % index).encode('utf-8')
    path = directory / ('source-%d.txt' % index)
    path.write_bytes(raw)
    profile = TextProfile(owner.policy.source_bytes, owner.policy)
    asset = inspect_text(path, profile)
    target = app.assets_dir / (asset.digest + '.txt')
    owner.begin_original(asset, target)
    actual = app.assets.retain_file(path, asset.mime, asset.digest, asset.size,
        profile=profile, expected_identity=asset.identity)
    owner.finish_original(asset, actual)
    assert actual == target and target.read_bytes() == raw
    return path, asset, target


def test_accounting_uses_current_storage_and_rechecks_whole_finite_original_set_on_exit(
        library, tmp_path, monkeypatch):
    app, workspace = library
    observed, original = [], module.regular
    def regular(path, **options):
        result = original(path, **options)
        if Path(path).parent == app.assets_dir and not options.get('directory', False):
            observed.append(Path(path))
        return result
    with accounting(app, workspace, budget()) as owner:
        paths = [retain(app, owner, tmp_path, index)[2] for index in range(8)]
        total = sum(path.stat().st_size for path in paths)
        monkeypatch.setattr(module, 'regular', regular)
        observed.clear()
        for _ in range(5):
            assert owner.check() >= total
        assert observed == [paths[-1]] * 5
        assert owner.validate(app, workspace, owner.policy) is owner.storage
        observed.clear()
    assert set(observed) == set(paths) and len(observed) <= len(paths) + 1
    assert all(path.exists() for path in paths)
    with pytest.raises(ValueError, match='closed'):
        owner.check()


@pytest.mark.parametrize('which', ['current', 'prior'])
def test_changed_real_retained_allocation_is_refused_at_current_or_final_boundary(library, tmp_path, which):
    app, workspace = library
    with pytest.raises(ValueError, match='retained original changed'):
        with accounting(app, workspace, budget()) as owner:
            first = retain(app, owner, tmp_path, 1)[2]
            current = retain(app, owner, tmp_path, 2)[2]
            chosen = current if which == 'current' else first
            with chosen.open('ab') as stream:
                stream.write(b'new allocation')
            owner.check()
    assert chosen.read_bytes().endswith(b'new allocation')


def test_existing_retained_source_is_conservatively_charged_and_still_checked(library, tmp_path):
    app, workspace = library
    with accounting(app, workspace, budget()) as owner:
        source, asset, target = retain(app, owner, tmp_path, 1)
        assert owner.check() >= asset.size
    with accounting(app, workspace, budget()) as owner:
        owner.begin_original(asset, target)
        owner.finish_original(asset, target)
        assert owner.check() >= asset.size
    assert source.read_bytes() == target.read_bytes()


def test_actual_unflushed_sqlite_allocation_is_charged_before_transaction_commit(library):
    app, workspace = library
    policy = budget(source_bytes=4096, library_bytes=128*1024)
    with accounting(app, workspace, policy) as owner:
        with pytest.raises(module.ImportBudgetExceeded, match='library-growth'):
            with owner.storage.transaction():
                owner.storage.db.execute('CREATE TABLE measured_temporary_test(payload BLOB)')
                owner.storage.db.execute('INSERT INTO measured_temporary_test VALUES(zeroblob(?))', (2*MIB,))
                owner.check()
        assert owner.storage.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='measured_temporary_test'").fetchone() is None
        owner.storage.check()
    assert app.browse(workspace) == ()


def test_current_import_journal_allocation_joins_same_bounded_owner(library):
    app, workspace = library
    with accounting(app, workspace, budget(source_bytes=4096, library_bytes=128*1024)) as owner:
        path = app.root / 'imports.sqlite'
        with closing(sqlite3.connect(path, isolation_level=None)) as journal:
            with owner.journal(journal):
                with pytest.raises(module.ImportBudgetExceeded, match='library-growth'):
                    journal.execute('BEGIN')
                    try:
                        journal.execute('CREATE TABLE accounting_fixture(payload BLOB)')
                        journal.execute('INSERT INTO accounting_fixture VALUES(zeroblob(?))', (2*MIB,))
                        owner.check()
                    finally:
                        journal.rollback()
            assert owner._journal is None
            assert journal.execute("SELECT 1 FROM sqlite_master WHERE name='accounting_fixture'").fetchone() is None


@pytest.mark.parametrize('change', ['budget', 'app', 'workspace', 'thread'])
def test_accounting_capability_cannot_move_to_other_scope_or_thread(library, change):
    app, workspace = library
    policy = budget()
    other_app = LibraryApp(app.root) if change == 'app' else app
    with accounting(app, workspace, policy) as owner:
        if change == 'thread':
            errors = []
            def other_thread():
                try:
                    owner.check()
                except BaseException as error:
                    errors.append(error)
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join(5)
            assert not thread.is_alive() and len(errors) == 1
            assert isinstance(errors[0], ValueError) and 'thread' in str(errors[0])
        else:
            with pytest.raises(ValueError, match='owner or reviewed budget'):
                owner.validate(other_app,
                    'other-workspace' if change == 'workspace' else workspace,
                    replace(policy, seconds=121) if change == 'budget' else policy)
        assert owner.validate(app, workspace, policy) is owner.storage


@pytest.mark.parametrize('kind', ['permission', 'directory', 'hardlink'])
def test_optional_sqlite_sidecar_cannot_hide_invalid_current_object(library, tmp_path, monkeypatch, kind):
    app, workspace = library
    selected = app.root / 'library.sqlite-shm'
    with accounting(app, workspace, budget()) as owner:
        if kind == 'permission':
            actual = module.regular
            error = PermissionError('Actual metadata observation denied')
            def denied(path, **options):
                if Path(path) == selected:
                    raise error
                return actual(path, **options)
            monkeypatch.setattr(module, 'regular', denied)
            with pytest.raises(PermissionError) as failure:
                owner.check()
            assert failure.value is error
            monkeypatch.setattr(module, 'regular', actual)
        else:
            assert not selected.exists()
            if kind == 'directory':
                selected.mkdir()
            else:
                original = tmp_path / 'foreign'
                original.write_bytes(b'Foreign sidecar bytes')
                os.link(original, selected)
            try:
                with pytest.raises(ValueError):
                    owner.check()
            finally:
                if kind == 'directory':
                    selected.rmdir()
                else:
                    selected.unlink()
                    assert original.read_bytes() == b'Foreign sidecar bytes'


def test_cancellation_arriving_during_actual_disk_observation_is_not_missed(library, monkeypatch):
    app, workspace = library
    stopped = threading.Event()
    real = module.shutil.disk_usage
    with pytest.raises(InterruptedError, match='accounting'):
        with accounting(app, workspace, budget(), cancelled=stopped.is_set) as owner:
            def observed(path):
                result = real(path)
                stopped.set()
                return result
            monkeypatch.setattr(module.shutil, 'disk_usage', observed)
            owner.check()


def test_actual_receipt_is_still_rollbackable_when_final_resource_guard_refuses(
        library, tmp_path, monkeypatch):
    app, workspace = library
    source = tmp_path / 'receipt.txt'
    raw = b'Original receipt authority.\r\n'
    source.write_bytes(raw)
    policy = budget()
    profile = TextProfile(policy.source_bytes, policy)
    metadata = dict(title='Final resource guard', authors=[], language=None, edition=None,
        volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    request = worker.prepare_request(app.root, workspace, source, metadata, Config(), source_profile=profile)
    identity, attempt = worker.request_identity(request), uuid4().hex
    worker.record_progress(app.root, request['request_id'], attempt_id=attempt, attempt=attempt)
    armed, observed = [], []
    prepare, disk = worker.prepare_publication, module.shutil.disk_usage
    def after_receipt(root, request_id, db, revision, before, result):
        returned = prepare(root, request_id, db, revision, before, result)
        assert db.in_transaction
        row = db.execute('SELECT request_id,attempt FROM import_publication WHERE revision=?', (revision,)).fetchone()
        assert tuple(row) == (request_id, attempt)
        assert worker.load_request(root, request_id)['publication_candidate']
        observed.append(revision)
        armed.append(True)
        return returned
    def low_disk(path):
        actual = disk(path)
        return (SimpleNamespace(total=actual.total, used=actual.used, free=module.WRITE_HEADROOM-1)
                if armed else actual)
    monkeypatch.setattr(worker, 'prepare_publication', after_receipt)
    monkeypatch.setattr(module.shutil, 'disk_usage', low_disk)
    with pytest.raises(module.ImportBudgetExceeded, match='headroom'):
        app._import_file(workspace, source, **metadata, config=Config(),
            _request_id=request['request_id'], _source_profile=profile,
            _request_sha256=identity, _request_attempt=attempt)
    assert len(observed) == 1
    current = worker.load_request(app.root, request['request_id'])
    assert current['publication']['committed'] is False and current['publication_candidate']
    retained = app.assets.resolve(sha256(raw).hexdigest(), profile=profile)
    assert retained.read_bytes() == raw
    with app.read(workspace) as db:
        for table in ('revision', 'page', 'sealed', 'import_publication'):
            column = 'id' if table == 'revision' else 'revision'
            assert db.execute('SELECT 1 FROM ' + table + ' WHERE ' + column + '=?', (observed[0],)).fetchone() is None
        assert db.execute('SELECT 1 FROM catalog_edition WHERE id=?', (metadata['edition_id'],)).fetchone() is None
    assert app.browse(workspace) == ()
