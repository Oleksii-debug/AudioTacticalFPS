"""Synthetic regressions for exact TLS/cancellation and durable provider gates.

No provider, socket, real credential or paid service is used by these tests.
The TLS-context test uses the real verifying stdlib context without a handshake.
"""
from unittest.mock import Mock
import ssl

import pytest

from carr_library import providers as api


class MemoryVault:
    def __init__(self):
        self.values = {}
        self.reads = 0

    def put(self, reference, binding, secret):
        self.values[reference, binding] = secret

    def get(self, reference, binding):
        self.reads += 1
        return self.values[reference, binding]

    def delete(self, reference, binding):
        del self.values[reference, binding]


@pytest.fixture
def task(tmp_path):
    vault = MemoryVault()
    store = api.ProfileStore(tmp_path / 'profiles.json', vault)
    profile = store.save(api.ProviderProfile('synthetic', 'Synthetic account', 'openai-compatible',
        'https://example.com/v1', 'synthetic-model', ('llm',)), secret='synthetic-key')
    request = api.ProviderRequest('llm', 'Synthetic non-private request.')
    approval = api.JobApproval('job', profile.fingerprint, (request.fingerprint(profile),),
        budget_micros=100, max_calls=3, mode='APPROVED_REMOTE')
    ledger = api.BudgetLedger(tmp_path / 'budget.sqlite')
    transport = Mock()
    transport.post.return_value = b'{"choices":[{"message":{"content":"Synthetic response"}}]}'
    gateway = api.ProviderGateway(ledger, vault=vault, transport=transport)
    return store, profile, request, approval, ledger, vault, transport, gateway


def invoke(task, operation='first', estimate=10, *, cancelled=lambda: False):
    _, profile, request, approval, _, _, _, gateway = task
    return gateway.perform(profile, request, approval=approval, operation_id=operation,
                           estimated_micros=estimate, cancelled=cancelled)


def test_unknown_charge_blocks_different_operation_id_after_restart(task):
    store, profile, request, approval, ledger, vault, transport, gateway = task
    transport.post.side_effect = TimeoutError('synthetic-key must not reach status')
    with pytest.raises(api.ProviderError):
        invoke(task)
    restarted = api.ProviderGateway(api.BudgetLedger(ledger.path), vault=vault, transport=transport)
    with pytest.raises(api.ProviderError, match='Reconcile the previous'):
        restarted.perform(profile, request, approval=approval, operation_id='another', estimated_micros=10)
    assert transport.post.call_count == 1
    assert vault.reads == 1
    ledger.reconcile_no_charge('first', confirmed=True)
    transport.post.side_effect = None
    invoke(task, 'after-review')
    assert transport.post.call_count == 2


def test_budget_is_reserved_before_any_secret_is_opened(task):
    with pytest.raises(api.ProviderError, match='budget'):
        invoke(task, estimate=101)
    assert task[5].reads == 0
    task[6].post.assert_not_called()
    assert task[4].report('job') == []


def test_cancelled_during_vault_access_never_reaches_transport(task, monkeypatch):
    state = {'cancelled': False}
    original = task[5].get

    def read(reference, binding):
        state['cancelled'] = True
        return original(reference, binding)

    monkeypatch.setattr(task[5], 'get', read)
    with pytest.raises(api.ProviderError, match='No request was sent'):
        invoke(task, cancelled=lambda: state['cancelled'])
    task[6].post.assert_not_called()
    assert task[4].report('job')[0]['state'] == 'cancelled_before_send'


def test_failed_vault_access_releases_unsent_cost_but_retains_attempt(task, monkeypatch):
    original = task[5].get
    monkeypatch.setattr(task[5], 'get', Mock(side_effect=RuntimeError('synthetic-secret-value')))
    with pytest.raises(api.ProviderError) as caught:
        invoke(task, estimate=90)
    assert 'synthetic-secret-value' not in str(caught.value)
    assert caught.value.__cause__ is None
    assert task[4].report('job')[0]['state'] == 'cancelled_before_send'
    monkeypatch.setattr(task[5], 'get', original)
    invoke(task, 'corrected', estimate=90)
    assert task[6].post.call_count == 1


def test_failed_profile_deletion_does_not_destroy_saved_key(task, monkeypatch):
    store, profile, _, _, _, vault, _, _ = task
    monkeypatch.setattr(api, '_atomic_json', Mock(side_effect=OSError('synthetic disk failure')))
    with pytest.raises(OSError):
        store.remove(profile.id)
    assert store.profiles() == (profile,)
    assert vault.get(profile.credential_ref, profile.credential_binding) == 'synthetic-key'


def test_unused_key_cleanup_failure_preserves_committed_profile_state(task, monkeypatch):
    store, profile, _, _, _, vault, _, _ = task
    monkeypatch.setattr(vault, 'delete', Mock(side_effect=RuntimeError('synthetic-secret-value')))
    store.remove(profile.id)
    assert store.profiles() == ()
    assert 'could not be removed' in store.warning
    assert 'synthetic-secret-value' not in store.warning


def test_key_write_error_is_redacted_and_previous_profile_preserved(task, monkeypatch):
    store, profile, _, _, _, vault, _, _ = task
    monkeypatch.setattr(vault, 'put', Mock(side_effect=RuntimeError('synthetic-secret-value')))
    with pytest.raises(api.ProviderError) as caught:
        store.save(profile, secret='synthetic-secret-value')
    assert 'synthetic-secret-value' not in str(caught.value)
    assert caught.value.__cause__ is None
    assert store.profiles() == (profile,)
    assert vault.get(profile.credential_ref, profile.credential_binding) == 'synthetic-key'


def connection_double(monkeypatch, *, remote=False):
    profile = api.ProviderProfile('local', 'Synthetic service', 'openai-compatible',
        'https://example.com/v1' if remote else 'http://127.0.0.1:8080/v1',
        'synthetic', ('llm',), local=not remote)
    address = '93.184.216.34' if remote else '127.0.0.1'
    monkeypatch.setattr(api.socket, 'getaddrinfo', Mock(return_value=[(2, 1, 6, '', (address, 443))]))
    connection = Mock()
    response = connection.getresponse.return_value
    response.status = 200
    response.getheader.return_value = '9'
    response.read.side_effect = [b'synthetic', b'']
    monkeypatch.setattr(api.http.client, 'HTTPConnection', Mock(return_value=connection))
    return profile, connection


def test_cancellation_during_connect_prevents_post_and_closes_connection(monkeypatch):
    profile, connection = connection_double(monkeypatch)
    state = {'cancelled': False}
    connection.connect.side_effect = lambda: state.update(cancelled=True)
    with pytest.raises(api.ProviderError, match='cancelled'):
        api.DirectTransport().post(profile, '/chat/completions', b'{}', {},
                                   cancelled=lambda: state['cancelled'])
    connection.request.assert_not_called()
    connection.getresponse.assert_not_called()
    connection.close.assert_called_once()


def test_inherited_tls_key_logging_is_disabled_without_weakening_verification(tmp_path, monkeypatch):
    profile, connection = connection_double(monkeypatch, remote=True)
    keylog = tmp_path / 'must-not-contain-session-keys.log'
    monkeypatch.setenv('SSLKEYLOGFILE', str(keylog))
    context_factory = ssl.SSLContext
    contexts = []

    def context(protocol):
        actual = context_factory(protocol)
        actual.wrap_socket = Mock(return_value=connection.sock)
        contexts.append(actual)
        return actual

    monkeypatch.setattr(api.ssl, 'SSLContext', context)
    assert api.DirectTransport().post(profile, '/chat/completions', b'{}', {}) == b'synthetic'
    assert len(contexts) == 1
    actual = contexts[0]
    assert actual.check_hostname is True
    assert actual.verify_mode == ssl.CERT_REQUIRED
    assert actual.keylog_filename is None
    actual.wrap_socket.assert_called_once_with(connection.sock, server_hostname='example.com')
    assert not keylog.exists()


def test_streaming_response_cannot_extend_total_elapsed_time_forever(monkeypatch):
    profile, connection = connection_double(monkeypatch)
    current = [0.0]
    monkeypatch.setattr(api, 'monotonic', lambda: current[0])

    def slow_chunk(amount):
        current[0] = 121.0
        return b'a'

    connection.getresponse.return_value.read.side_effect = slow_chunk
    with pytest.raises(api.ProviderError, match='elapsed-time'):
        api.DirectTransport().post(profile, '/chat/completions', b'{}', {})
    assert connection.getresponse.return_value.read.call_count == 1
    connection.close.assert_called_once()


def test_connection_cleanup_exception_cannot_expose_headers(monkeypatch):
    profile, connection = connection_double(monkeypatch)
    connection.close.side_effect = RuntimeError('synthetic-secret-value')
    assert api.DirectTransport().post(profile, '/chat/completions', b'{}', {}) == b'synthetic'
