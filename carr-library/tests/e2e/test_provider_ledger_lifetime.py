"""Real SQLite handles must close on every provider-ledger outcome.

Keeping captured handles alive makes leaks reproducible on Linux as well as
Windows, where an unclosed handle can also prevent backup or cleanup.
"""
import sqlite3

import pytest

from carr_library import providers


@pytest.fixture
def ledger_connections(monkeypatch):
    original_connect = sqlite3.connect
    opened = []

    def capture_connection(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(providers.sqlite3, 'connect', capture_connection)
    try:
        yield opened
    finally:
        for connection in opened:
            connection.close()


def assert_closed(connections):
    assert connections, 'The test must exercise a real SQLite connection.'
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')


def test_corrupt_budget_database_closes_connection_before_propagating_error(
        tmp_path, ledger_connections):
    path = tmp_path / 'budget.sqlite'
    original = b'Synthetic damaged provider-budget database.\n' * 256
    path.write_bytes(original)

    with pytest.raises(sqlite3.DatabaseError):
        providers.BudgetLedger(path)

    assert len(ledger_connections) == 1
    assert_closed(ledger_connections)
    assert path.read_bytes() == original


def test_normal_report_and_rejected_reconciliation_close_connections(
        tmp_path, ledger_connections):
    ledger = providers.BudgetLedger(tmp_path / 'budget.sqlite')
    assert ledger.report('missing-job') == []

    with pytest.raises(providers.ProviderError, match='uncertain'):
        ledger.reconcile_no_charge('missing-operation', confirmed=True)

    assert len(ledger_connections) == 3
    assert_closed(ledger_connections)
