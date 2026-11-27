"""Synthetic DNS boundaries; these cases cannot open a network socket."""
from dataclasses import replace
import socket
import threading
from time import monotonic, time

import pytest

from carr_library.assistant import providers
from carr_library.assistant.providers import Authorization, Gateway, ProviderError
from carr_library.assistant.profiles import ProviderProfile


PAYLOAD = {'messages': [{'role': 'user', 'content': 'Synthetic deadline test'}]}


def approved_gateway(tmp_path):
    profile = ProviderProfile('timing_test', 'Synthetic provider', 'openai-compatible',
        'https://api.example.test/v1', 'test-model', enabled=True,
        credential_ref='carr/provider/timing_test/' + 'a' * 32)
    approval = Authorization('timing_approval', 'timing_task', ((profile.id, profile.endpoint),),
        ('llm.chat',), ('synthetic-test',), '1', 2, time() + 60)

    class Vault:
        def get(self, *args):
            return 'SYNTHETIC_NOT_A_LIVE_KEY'

    return Gateway(tmp_path, vault=Vault(), mode='APPROVED_REMOTE'), profile, approval


def request(gateway, profile, approval, **kwargs):
    return gateway.request(profile, 'llm.chat', PAYLOAD, task_id='timing_task',
        data_class='synthetic-test', authorization=approval, operation_id='timing_op',
        max_cost='0.25', **kwargs)


@pytest.fixture
def no_socket(monkeypatch):
    opened = []

    def refuse(*args, **kwargs):
        opened.append(args)
        raise AssertionError('This DNS fixture may not open a network socket.')

    monkeypatch.setattr(socket, 'socket', refuse)
    return opened


@pytest.mark.parametrize('addresses', [
    ['127.0.0.1'], ['::1'], ['10.1.2.3'], ['169.254.169.254'], ['224.0.0.1'],
    ['0.0.0.0'], ['::ffff:127.0.0.1'], ['93.184.216.34', '10.1.2.3'],
])
def test_remote_dns_rejects_every_disallowed_answer_before_connect(
        tmp_path, monkeypatch, no_socket, addresses):
    records = [(socket.AF_INET6 if ':' in address else socket.AF_INET,
        socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, 443)) for address in addresses]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: records)
    gateway, profile, approval = approved_gateway(tmp_path)
    with pytest.raises(ProviderError, match='disallowed network destination'):
        request(gateway, profile, approval)
    assert no_socket == []
    assert gateway.ledger.get(approval.id)['operations']['timing_op']['status'] == 'not_sent'


@pytest.mark.parametrize('reason', ['deadline', 'cancel'])
def test_late_dns_answer_cannot_connect_or_hold_the_caller(
        tmp_path, monkeypatch, no_socket, reason):
    entered, release, finished, cancelled = (threading.Event() for _ in range(4))
    resolvers = []

    def blocked_dns(*args, **kwargs):
        resolvers.append(threading.current_thread())
        entered.set()
        try:
            release.wait(0.9)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
                     ('93.184.216.34', 443))]
        finally:
            finished.set()

    monkeypatch.setattr(socket, 'getaddrinfo', blocked_dns)
    gateway, profile, approval = approved_gateway(tmp_path)
    timer = threading.Timer(0.1, cancelled.set)
    if reason == 'cancel':
        timer.start()
    started = monotonic()
    try:
        expected = TimeoutError if reason == 'deadline' else InterruptedError
        with pytest.raises((TimeoutError, InterruptedError, ProviderError)) as failure:
            request(gateway, profile, approval, timeout=0.25 if reason == 'deadline' else 2,
                    cancelled=cancelled.is_set)
        assert monotonic() - started < 0.65
        assert isinstance(failure.value, expected)
        assert entered.is_set() and not finished.is_set()
        assert not no_socket
        assert gateway.ledger.get(approval.id)['operations']['timing_op']['status'] == 'not_sent'
    finally:
        release.set()
        timer.cancel()
        if timer.ident is not None:
            timer.join(2)
        assert finished.wait(2)
        for resolver in resolvers:
            if resolver is not threading.current_thread():
                resolver.join(2)
                assert not resolver.is_alive()
    assert not no_socket  # A late answer has no authority to connect.


def test_dns_exhausting_exact_budget_never_gets_an_extra_connect_allowance(
        tmp_path, monkeypatch, no_socket):
    clock = [10.0]

    def resolve(*args, **kwargs):
        clock[0] = 11.0
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
                 ('93.184.216.34', 443))]

    monkeypatch.setattr(providers, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    gateway, profile, approval = approved_gateway(tmp_path)
    with pytest.raises(TimeoutError, match='time budget'):
        request(gateway, profile, approval, timeout=1)
    assert not no_socket
    assert gateway.ledger.get(approval.id)['operations']['timing_op']['status'] == 'not_sent'


def test_cancelled_call_does_not_construct_a_connection(tmp_path, no_socket):
    gateway, profile, approval = approved_gateway(tmp_path)
    with pytest.raises(InterruptedError):
        request(gateway, profile, approval, cancelled=lambda: True)
    assert not no_socket
    row = gateway.ledger.get(approval.id)['operations']['timing_op']
    assert row['status'] == 'not_sent' and row['output_sha256'] is None


def test_explicit_zero_port_is_not_rewritten_to_the_tls_default(tmp_path, monkeypatch, no_socket):
    gateway, profile, approval = approved_gateway(tmp_path)
    profile = replace(profile, endpoint='https://api.example.test:0/v1')
    approval = replace(approval, profiles=((profile.id, profile.endpoint),))
    ports = []

    def resolve(host, port, *args, **kwargs):
        ports.append(port)
        return []

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    with pytest.raises(ProviderError):
        request(gateway, profile, approval)
    assert ports == [0] and not no_socket


def test_invalid_dns_encoding_fails_promptly_without_an_orphan_lookup(tmp_path, monkeypatch, no_socket):
    gateway, profile, approval = approved_gateway(tmp_path)
    def resolve(*args, **kwargs):
        raise UnicodeError('Synthetic invalid IDNA label')
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    started = monotonic()
    with pytest.raises(ProviderError, match='could not be resolved'):
        request(gateway, profile, approval, timeout=3)
    assert monotonic() - started < 0.65 and not no_socket
    assert gateway.ledger.get(approval.id)['operations']['timing_op']['status'] == 'not_sent'
