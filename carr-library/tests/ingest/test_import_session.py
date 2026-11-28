"""Actual cooperative storage accounting and precommit rollback boundaries.

Disk exhaustion is simulated only through the free-space observation; SQLite,
retained originals, publication receipts and rollback all execute normally.
"""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.catalog.intake import source_snapshot
from carr_library.ingest.retained_assets import SUFFIXES
from carr_library.catalog.registration import Registration
from carr_library.extract.page_ocr import Config
from carr_library.ingest import session as module
from carr_library.ingest import worker
from carr_library.ingest.publication_identity import PublicationAttempt, request_identity
from carr_library.ingest.reconciliation import reconciliation
from carr_library.ingest.resource_budget import ImportBudget, MIB
from carr_library.storage.revisions import CURRENT_SCHEMA, Page, Storage


def _budget(**changes):
    return replace(ImportBudget(source_bytes=2*MIB, segment_bytes=2*MIB,
        segments=1000, steps=1001, memory_bytes=128*MIB,
        library_bytes=64*MIB, temporary_bytes=16*MIB), **changes)


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Cooperative storage accounting')
    return app, workspace


def _retain(app, owner, directory, index, raw=None):
    raw = raw if raw is not None else ('Original %d — точні bytes.\r\n' % index).encode()
    path = directory / ('source-%d.txt' % index)
    path.write_bytes(raw)
    asset = source_snapshot(directory, path.name,
        max_bytes=owner.policy.source_bytes, file_backed=True)
    target = app.assets_dir / (asset.digest + SUFFIXES[asset.mime])
    owner.begin_original(asset, target)
    actual = app.assets.retain_snapshot(asset, max_bytes=owner.policy.source_bytes)
    owner.finish_original(asset, actual)
    assert actual == target and target.read_bytes() == raw
    return path, asset, target


def _registration(asset, metadata):
    return Registration(metadata['work_id'], metadata['edition_id'], asset.manifest(),
        metadata['title'], tuple(metadata['authors']), metadata['language'],
        metadata['edition'], metadata['volume'], tuple(metadata['collections']))


def _metadata():
    return dict(title='Measured source', authors=['Author'], language=None,
                edition=None, volume=None, collections=[],
                work_id=uuid4().hex, edition_id=uuid4().hex)


def test_checkpoint_original_work_is_constant_and_slice_exit_rechecks_all(library, tmp_path, monkeypatch):
    app, workspace = library
    observed = []
    original_regular = module.regular

    def observe(path, **options):
        result = original_regular(path, **options)
        path = Path(path)
        if path.parent == app.assets_dir and not options.get('directory', False):
            observed.append(path)
        return result

    with module.import_session(app, workspace, _budget()) as owner:
        paths = [_retain(app, owner, tmp_path, i)[2] for i in range(48)]
        actual_total = sum(path.stat().st_size for path in paths)
        monkeypatch.setattr(module, 'regular', observe)
        observed.clear()
        for _ in range(32):
            assert owner.check() >= actual_total
        # Real path checks still occur at each checkpoint, but only for its
        # current original. The preceding 47 originals are not walked again.
        assert observed == [paths[-1]] * 32
        observed.clear()
    # The normal context exit performs one finite whole-slice check, including
    # the current original even when it was already present before this slice.
    assert set(observed) == set(paths)
    assert len(observed) <= len(paths) + 1
    assert all(path.exists() for path in paths)


@pytest.mark.parametrize('which', ['current', 'prior'])
def test_actual_original_size_change_is_caught_at_its_accounting_boundary(
        library, tmp_path, which):
    app, workspace = library
    with pytest.raises(ValueError, match='retained original changed'):
        with module.import_session(app, workspace, _budget()) as owner:
            first = _retain(app, owner, tmp_path, 1)[2]
            current = _retain(app, owner, tmp_path, 2)[2]
            chosen = current if which == 'current' else first
            with chosen.open('ab') as stream:
                stream.write(b'new allocation')
            if which == 'current':
                owner.check()
            else:
                # Earlier raw bytes grant no reading authority here. Their
                # cumulative allocation is revalidated at actual slice exit.
                owner.check()
    assert chosen.read_bytes().endswith(b'new allocation')


def test_existing_original_is_not_recharged_but_remains_current_checked(library, tmp_path):
    app, workspace = library
    with module.import_session(app, workspace, _budget()) as first_owner:
        source, asset, target = _retain(app, first_owner, tmp_path, 1)
        assert first_owner.check() == asset.size
    before = target.read_bytes()
    with pytest.raises(ValueError, match='retained original changed'):
        with module.import_session(app, workspace, _budget()) as second_owner:
            second_owner.begin_original(asset, target)
            second_owner.finish_original(asset, target)
            assert second_owner.check() == 0
            with target.open('ab') as stream:
                stream.write(b'extra')
    assert target.read_bytes() == before + b'extra'
    assert source.read_bytes() == before


def test_actual_sqlite_pages_and_retained_source_exceeding_budget_roll_back(library, tmp_path):
    app, workspace = library
    metadata = _metadata()
    revision = sha256(b'actual measured transaction').hexdigest()
    # One original fits the reviewed 2 MiB allowance; retained + raw page +
    # active indexed text must exceed it. No counter or SQLite call is mocked.
    raw = ('authored corpus token and exact source words\n' * 26000).encode()
    assert MIB < len(raw) < 2*MIB
    with pytest.raises(module.ImportBudgetExceeded, match='library-growth'):
        with module.import_session(app, workspace, _budget(library_bytes=2*MIB)) as owner:
            _, asset, target = _retain(app, owner, tmp_path, 1, raw)
            publisher = _CatalogPublisher(owner.storage, _registration(asset, metadata))
            publisher.bind_resources(owner)
            publisher.publish_revision(workspace, metadata['edition_id'], revision,
                asset.digest, 'actual-budget-fixture-v1', [Page(raw.decode())])
    assert target.read_bytes() == raw
    with closing(Storage(app.database, schema_version=CURRENT_SCHEMA)) as storage:
        for table in ('revision', 'page', 'sealed', 'import_publication'):
            column = 'id' if table == 'revision' else 'revision'
            assert storage.db.execute(
                'SELECT 1 FROM ' + table + ' WHERE ' + column + '=?', (revision,)).fetchone() is None
        assert storage.db.execute('SELECT 1 FROM catalog_edition WHERE id=?',
                                  (metadata['edition_id'],)).fetchone() is None
        storage.check()


def test_final_disk_reserve_failure_keeps_hint_but_no_committed_receipt(
        library, tmp_path, monkeypatch):
    app, workspace = library
    metadata = _metadata()
    revision = sha256(b'precommit resource boundary').hexdigest()
    config = Config()
    source = tmp_path / 'receipt-source.txt'
    raw = b'Exact original source.\r\n'
    source.write_bytes(raw)
    asset = source_snapshot(tmp_path, source.name, file_backed=True)
    budget = _budget()
    request = worker.prepare_request(app.root, workspace, source, metadata, config,
                                    expected_manifest=asset.manifest(), import_budget=budget)
    attempt_id = uuid4().hex
    worker.record_progress(app.root, request['request_id'], attempt=attempt_id,
        asset_sha256=asset.digest, publication_candidate=None)
    captured = worker.load_request(app.root, request['request_id'])
    attempt = PublicationAttempt(attempt_id, request_identity(captured))
    actual_disk_usage = module.shutil.disk_usage
    observations = []
    armed = False

    def disk_usage(path):
        actual = actual_disk_usage(path)
        observations.append((Path(path), armed, actual.free))
        if armed:
            return SimpleNamespace(total=actual.total, used=actual.used,
                                   free=module.WRITE_HEADROOM - 1)
        return actual

    monkeypatch.setattr(module.shutil, 'disk_usage', disk_usage)
    with pytest.raises(module.ImportBudgetExceeded, match='headroom'):
        with module.import_session(app, workspace, budget) as owner:
            target = app.assets_dir / (asset.digest + '.txt')
            owner.begin_original(asset, target)
            app.assets.retain_snapshot(asset, max_bytes=budget.source_bytes)
            owner.finish_original(asset, target)
            publisher = _CatalogPublisher(owner.storage, _registration(asset, metadata))
            publisher.bind_receipt(app.root, request['request_id'],
                                   publication_attempt=attempt, config=config)
            publisher.bind_resources(owner)

            def after_actual_receipt(_workspace, _revision):
                nonlocal armed
                row = owner.storage.db.execute(
                    'SELECT request_id,attempt FROM import_publication WHERE revision=?',
                    (revision,)).fetchone()
                assert tuple(row) == (request['request_id'], attempt_id)
                assert worker.load_request(app.root, request['request_id'])['publication_candidate']
                armed = True

            publisher.before_commit = after_actual_receipt
            publisher.publish_revision(workspace, metadata['edition_id'], revision,
                asset.digest, 'actual-budget-fixture-v1', [Page(raw.decode())])
    assert any(observed for _, observed, _ in observations)
    assert target.read_bytes() == raw == source.read_bytes()
    saved = worker.load_request(app.root, request['request_id'])
    assert saved['publication_candidate']['revision'] == revision
    assert worker._reconcile_publication(app.root, request['request_id'], attempt_id,
                                         initial=captured) is None
    with closing(Storage(app.database, schema_version=CURRENT_SCHEMA)) as storage:
        assert storage.db.execute('SELECT 1 FROM revision WHERE id=?', (revision,)).fetchone() is None
        assert storage.db.execute('SELECT 1 FROM import_publication WHERE revision=?',
                                  (revision,)).fetchone() is None
        storage.check()

    # A later real writer can publish the equal deterministic revision with its
    # own receipt. It cannot turn the rolled-back owner's hint into completion.
    armed = False
    foreign = worker.prepare_request(app.root, workspace, source, metadata, config,
                                    expected_manifest=asset.manifest(), import_budget=budget)
    foreign_attempt = uuid4().hex
    worker.record_progress(app.root, foreign['request_id'], attempt=foreign_attempt,
                           asset_sha256=asset.digest, publication_candidate=None)
    foreign_saved = worker.load_request(app.root, foreign['request_id'])
    with closing(Storage(app.database, schema_version=CURRENT_SCHEMA)) as storage:
        other = _CatalogPublisher(storage, _registration(asset, metadata))
        other.bind_receipt(app.root, foreign['request_id'],
            publication_attempt=PublicationAttempt(foreign_attempt, request_identity(foreign_saved)),
            config=config)
        other.publish_revision(workspace, metadata['edition_id'], revision,
            asset.digest, 'actual-budget-fixture-v1', [Page(raw.decode())])
        storage.check()
    assert worker._reconcile_publication(app.root, request['request_id'], attempt_id,
                                         initial=captured) is None
    recovered = worker._reconcile_publication(app.root, foreign['request_id'], foreign_attempt,
                                              initial=foreign_saved)
    assert recovered['state'] == 'complete' and recovered['publication_recovered'] is True
    with reconciliation(app, workspace) as owner:
        assert worker._reconcile_publication(app.root, request['request_id'], attempt_id,
                                             initial=captured, _context=owner) is None
        exact = worker._reconcile_publication(app.root, foreign['request_id'], foreign_attempt,
                                             initial=foreign_saved, _context=owner)
        assert exact['state'] == 'complete' and exact['publication_recovered'] is True


def test_cancelled_current_session_has_no_publication_side_effect(library, tmp_path):
    app, workspace = library
    stopped = False
    with pytest.raises(InterruptedError, match='accounting'):
        with module.import_session(app, workspace, _budget(), cancelled=lambda: stopped) as owner:
            _, _, target = _retain(app, owner, tmp_path, 1)
            original = target.read_bytes()
            stopped = True
            owner.check()
    assert target.read_bytes() == original
    with app.read(workspace) as db:
        assert not db.execute('SELECT 1 FROM catalog_edition WHERE workspace=?', (workspace,)).fetchall()


def test_resource_capability_cannot_cross_workspace_or_outlive_its_owner(library, tmp_path):
    app, workspace = library
    other_workspace = app.create_workspace('Separate scope')
    metadata = _metadata()
    revision = sha256(b'wrong scoped resource owner').hexdigest()
    with module.import_session(app, workspace, _budget()) as owner:
        _, asset, _ = _retain(app, owner, tmp_path, 1)
        publisher = _CatalogPublisher(owner.storage, _registration(asset, metadata))
        publisher.bind_resources(owner)
        with pytest.raises(ValueError, match='owner or reviewed budget'):
            publisher.publish_revision(other_workspace, metadata['edition_id'], revision,
                asset.digest, 'scoped-fixture-v1', [Page('Scope must remain exact.')])
        assert owner.storage.db.execute('SELECT 1 FROM revision WHERE id=?', (revision,)).fetchone() is None
    with pytest.raises(ValueError, match='owner or reviewed budget'):
        owner.validate(app, workspace, _budget())


def test_accounting_reuses_only_shared_path_work_and_observes_every_real_leaf(
        library, tmp_path, monkeypatch):
    from collections import Counter
    from carr_library import durable_state
    app, workspace = library
    actual_regular = module.regular
    actual_space = module.shutil.disk_usage
    observed, volumes = Counter(), []

    def regular(path, **options):
        observed[Path(path)] += 1
        return actual_regular(path, **options)

    def space(path):
        result = actual_space(path)
        volumes.append((Path(path), result.free))
        return result

    with module.import_session(app, workspace, _budget()) as owner:
        source, asset, target = _retain(app, owner, tmp_path, 1)
        expected = source.read_bytes()
        chain = {*owner.root.parents, owner.root}
        leaves = tuple(owner._paths)
        assert len(leaves) == 8 and any(not path.exists() for path in leaves)
        # Observe the genuine shared guard too, so the former per-sidecar
        # local_path calls cannot hide their repeated ancestor work.
        monkeypatch.setattr(module, 'regular', regular)
        monkeypatch.setattr(durable_state, 'regular', regular)
        monkeypatch.setattr(module.shutil, 'disk_usage', space)
        for _ in range(4):
            observed.clear()
            volumes.clear()
            assert owner.check() == asset.size
            assert all(observed[path] >= 1 for path in leaves)
            assert observed[target] >= 1
            assert observed[app.assets_dir] >= 1
            assert all(observed[parent] >= 2 for parent in chain)
            assert sum(observed[parent] for parent in chain) <= 2 * len(chain)
            assert len(volumes) == 1 and volumes[0][0] == app.root
            assert target.read_bytes() == source.read_bytes() == expected


def test_new_actual_sidecar_growth_is_recounted_without_prior_poll_credit(library, tmp_path):
    app, workspace = library
    with module.import_session(app, workspace, _budget(library_bytes=2*MIB)) as owner:
        source, asset, target = _retain(app, owner, tmp_path, 1)
        assert owner.check() == asset.size
        sidecar = app.root / 'imports.sqlite-journal'
        assert not sidecar.exists()
        try:
            sidecar.write_bytes(b'\0' * (2*MIB + 1))
            with pytest.raises(module.ImportBudgetExceeded, match='library-growth'):
                owner.check()
        finally:
            sidecar.unlink(missing_ok=True)
        assert owner.check() == asset.size
        assert target.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('kind', ['hardlink', 'directory'])
def test_actual_optional_sqlite_leaf_never_admits_an_alias_or_directory(
        library, tmp_path, kind):
    import os
    app, workspace = library
    with module.import_session(app, workspace, _budget()) as owner:
        sidecar = app.root / 'imports.sqlite-journal'
        assert not sidecar.exists()
        outside = tmp_path / 'unowned-original'
        outside.write_bytes(b'Unowned allocation is not a valid SQLite sidecar.')
        if kind == 'hardlink':
            os.link(outside, sidecar)
            assert outside.stat().st_nlink == sidecar.stat().st_nlink == 2
        else:
            sidecar.mkdir()
        try:
            with pytest.raises(ValueError, match='regular local files'):
                owner.check()
        finally:
            sidecar.rmdir() if kind == 'directory' else sidecar.unlink()
        assert outside.read_bytes() == b'Unowned allocation is not a valid SQLite sidecar.'
        assert owner.check() == 0


def test_same_byte_original_replacement_still_fails_the_actual_leaf_identity(
        library, tmp_path):
    import os
    app, workspace = library
    with pytest.raises(ValueError, match='retained original changed'):
        with module.import_session(app, workspace, _budget()) as owner:
            source, asset, target = _retain(app, owner, tmp_path, 1)
            before = target.stat()
            replacement = app.assets_dir / 'ordinary-replacement'
            replacement.write_bytes(source.read_bytes())
            os.chmod(replacement, before.st_mode & 0o777)
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(replacement, target)
            after = target.stat()
            assert (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            assert after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns
            assert target.read_bytes() == source.read_bytes()
            owner.check()


def test_original_directory_replacement_after_real_leaf_stat_is_not_accepted(
        library, tmp_path, monkeypatch):
    app, workspace = library
    actual = module.regular
    replaced = False
    with pytest.raises(ValueError, match='original directory changed'):
        with module.import_session(app, workspace, _budget()) as owner:
            source, asset, target = _retain(app, owner, tmp_path, 1)
            before = target.stat()
            prior = app.root / 'prior-original-directory'

            def observed(path, **options):
                nonlocal replaced
                value = actual(path, **options)
                if Path(path) == target and not options.get('directory') and not replaced:
                    replaced = True
                    app.assets_dir.rename(prior)
                    app.assets_dir.mkdir()
                    (prior / target.name).rename(target)
                    # The actual original inode and bytes are unchanged; it is
                    # the directory observed around that leaf which changed.
                    assert target.stat().st_ino == before.st_ino
                return value

            monkeypatch.setattr(module, 'regular', observed)
            owner.check()
    assert replaced and target.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('boundary', ['root-ancestor', 'optional-leaf', 'original-directory'])
def test_permission_failure_is_never_treated_as_absent_accounting(
        library, tmp_path, monkeypatch, boundary):
    app, workspace = library
    actual = module.regular
    denial = PermissionError('Authored accounting access denial.')
    with module.import_session(app, workspace, _budget()) as owner:
        source, asset, target = _retain(app, owner, tmp_path, 1)
        selected = {'root-ancestor': app.root.parent,
                    'optional-leaf': app.root / 'imports.sqlite-journal',
                    'original-directory': app.assets_dir}[boundary]
        called = []

        def guarded(path, **options):
            if Path(path) == selected:
                called.append(Path(path))
                raise denial
            return actual(path, **options)

        with monkeypatch.context() as faults:
            faults.setattr(module, 'regular', guarded)
            with pytest.raises(PermissionError) as caught:
                owner.check()
        assert caught.value is denial and called
        assert owner.check() == asset.size
        assert target.read_bytes() == source.read_bytes()


def test_cancel_during_the_actual_last_sidecar_probe_is_seen_before_return(
        library, tmp_path, monkeypatch):
    app, workspace = library
    actual = module.regular
    stopped = False
    observed = []
    with module.import_session(app, workspace, _budget(), cancelled=lambda: stopped) as owner:
        source, asset, target = _retain(app, owner, tmp_path, 1)
        selected = app.root / 'imports.sqlite-journal'
        assert not selected.exists()

        def probing(path, **options):
            nonlocal stopped
            try:
                return actual(path, **options)
            finally:
                if Path(path) == selected:
                    observed.append(Path(path))
                    stopped = True

        with monkeypatch.context() as observe:
            observe.setattr(module, 'regular', probing)
            with pytest.raises(InterruptedError, match='accounting'):
                owner.check()
        assert observed and stopped
        stopped = False
        assert owner.check() == asset.size
        assert target.read_bytes() == source.read_bytes()


def test_real_child_directories_do_not_replace_the_current_root(library, tmp_path):
    app, workspace = library
    with module.import_session(app, workspace, _budget()) as owner:
        before = app.root.lstat()
        selected = [app.root / 'import-requests', app.root / 'import-progress']
        assert all(not path.exists() for path in selected)
        for path in selected:
            path.mkdir()
        after = app.root.lstat()
        assert (after.st_dev, after.st_ino, after.st_mode) == (
            before.st_dev, before.st_ino, before.st_mode)
        # POSIX increases the root directory link count for genuine child
        # directories; Windows may report the same count. Neither is replacement.
        if module.os.name == 'posix':
            assert after.st_nlink == before.st_nlink + len(selected)
        assert owner.validate(app, workspace, owner.budget) is owner.storage
        source, asset, target = _retain(app, owner, tmp_path, 17)
        assert owner.check() == asset.size
        for path in selected:
            path.rmdir()
        assert owner.check() == asset.size
        assert target.read_bytes() == source.read_bytes()
    assert app.root.lstat().st_ino == before.st_ino
