"""Actual SQLite handles must close before Windows recovery removes a folder."""
from contextlib import closing
import sqlite3
from types import SimpleNamespace

import pytest

from carr_library.agent import LocalAgent, Plan, Step
from carr_library.providers import ProviderManager, ProviderProfile
from carr_library import recovery_backup


@pytest.fixture
def retained_connections(monkeypatch):
    connect = sqlite3.connect
    handles = []

    def retain(*args, **kwargs):
        db = connect(*args, **kwargs)
        # Retain actual connection objects so garbage collection cannot hide
        # a leaked Windows file handle behind CPython's reference counting.
        handles.append(db)
        return db

    monkeypatch.setattr(sqlite3, 'connect', retain)
    try:
        yield handles, connect
    finally:
        for db in handles:
            db.close()


def assert_closed(handles):
    assert handles
    for db in handles:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            db.execute('SELECT 1')


def test_canonical_recovery_schema_closes_handles_before_temporary_cleanup(retained_connections):
    handles, _ = retained_connections
    recovery_backup._canonical_schemas.cache_clear()
    try:
        shapes = recovery_backup._canonical_schemas()
        assert set(shapes) == {'providers', 'agent', 'imports'}
        assert all(shape[1] for shape in shapes.values())
        assert_closed(handles)
    finally:
        recovery_backup._canonical_schemas.cache_clear()


def test_provider_settings_commit_and_close_actual_connections(tmp_path, retained_connections):
    handles, connect = retained_connections
    manager = ProviderManager(tmp_path / 'providers', vault=object())
    profile = ProviderProfile('local', 'Local fixture', 'llm',
                              'https://fixture.invalid/v1', 'fixture', requires_key=False)
    assert manager.mode == 'LOCAL_ONLY'
    manager.save(profile)
    manager.set_mode('HYBRID')
    assert manager.profiles() == (profile,)
    manager.disable(profile.id)
    assert manager.profiles()[0].enabled is False
    assert_closed(handles)
    # Read through an independent connection to prove the explicit closing
    # did not lose the former transaction context manager's commit behavior.
    with closing(connect(manager.database)) as db:
        assert db.execute('SELECT mode FROM policy').fetchone() == ('HYBRID',)
        assert db.execute('SELECT id FROM profiles').fetchone() == (profile.id,)


@pytest.mark.parametrize('fail', [False, True], ids=['completed-and-reused', 'needs-review'])
def test_agent_receipts_commit_and_close_on_every_outcome(tmp_path, retained_connections, fail):
    handles, connect = retained_connections
    agent = LocalAgent(SimpleNamespace(workspaces=lambda: ()), tmp_path / 'agent')
    plan = Plan('List selected libraries', (Step('list', 'library.list', {}),))
    if fail:
        def denied(*args):
            raise RuntimeError('Synthetic tool failure')
        agent._call = denied
        with pytest.raises(RuntimeError, match='Synthetic tool failure'):
            agent.run(plan, approval_sha256=plan.digest)
    else:
        result = agent.run(plan, approval_sha256=plan.digest)
        assert result['state'] == 'complete'
        assert agent.run(plan, approval_sha256=plan.digest) == result
    assert_closed(handles)
    with closing(connect(agent.database)) as db:
        state, = db.execute('SELECT state FROM receipts').fetchone()
        assert state == ('needs_review' if fail else 'complete')
