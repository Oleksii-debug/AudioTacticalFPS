"""Real loopback HTTP/TLS regressions for a whole provider-call time budget.

Certificates, requests, bodies and credentials are synthetic. The test client
uses normal certificate/hostname verification against a temporary local CA.
No external provider, public DNS lookup or billable request is involved.
"""
import http.client
from hashlib import sha256
import errno
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
from threading import Event, Lock, Thread, Timer, enumerate as threads
from time import monotonic, time

import pytest

from carr_library.assistant import Authorization, Gateway, ProviderError, ProviderProfile
from carr_library.assistant import providers


PAYLOAD = {'messages': [{'role': 'user', 'content': 'Synthetic budget test'}]}
BODY = json.dumps({'choices': [{'message': {'content': 'Local TLS response'}}]}).encode()
BUDGET = 0.25
RETURN_LIMIT = 0.65


@pytest.fixture(scope='session')
def tls_material(tmp_path_factory):
    candidates = [shutil.which('openssl')]
    if os.name == 'nt':
        for variable in ('ProgramFiles', 'ProgramFiles(x86)'):
            root = os.environ.get(variable)
            if root:
                candidates.append(str(Path(root) / 'Git' / 'usr' / 'bin' / 'openssl.exe'))
    openssl = next((value for value in candidates if value and Path(value).is_file()), None)
    assert openssl, 'Real TLS tests require the existing OpenSSL executable (including Git for Windows).'
    folder = tmp_path_factory.mktemp('provider-test-tls')
    cert, key = folder / 'certificate.pem', folder / 'private-key.pem'
    command = [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256',
        '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=127.0.0.1',
        '-addext', 'subjectAltName=IP:127.0.0.1,DNS:api.example.test,'
                   'DNS:translation.googleapis.com,DNS:api.elevenlabs.io']
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, 'Synthetic TLS certificate generation failed: ' + process.stderr[-1000:]
        yield cert, key
    finally:
        cert.unlink(missing_ok=True)
        key.unlink(missing_ok=True)


class LoopbackProvider:
    """One actual connection, with explicit release and bounded thread cleanup."""
    def __init__(self, certificate, key, *, tls=True, framing='length', connection='close',
                 phase='fast', body=None, content_type='application/json', status=200,
                 cancel_event=None):
        self.tls, self.framing, self.connection, self.phase = tls, framing, connection, phase
        self.body = BODY if body is None else body
        self.content_type, self.status, self.cancel_event = content_type, status, cancel_event
        self.accepted, self.headers_received, self.body_started = Event(), Event(), Event()
        self.disconnected, self.release = Event(), Event()
        self.requests, self.errors, self.sni, self.timers = [], [], [], []
        self.socket_lock = Lock()
        self.sockets = []
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(0.05)
        self.port = self.listener.getsockname()[1]
        self.endpoint = ('https' if tls else 'http') + f'://127.0.0.1:{self.port}/v1'
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificate, key)
        self.context.set_servername_callback(lambda sock, name, context: self.sni.append(name))
        self.thread = Thread(target=self._serve, name='carr-test-provider-server', daemon=True)
        self.thread.start()

    def _track(self, sock):
        with self.socket_lock:
            self.sockets.append(sock)
        return sock

    def _cancel_later(self):
        if self.cancel_event is not None:
            timer = Timer(0.08, self.cancel_event.set)
            timer.daemon = True
            self.timers.append(timer)
            timer.start()

    def _wait_for_disconnect(self, sock):
        while not self.release.is_set():
            try:
                if not sock.recv(4096):
                    self.disconnected.set()
                    return
            except socket.timeout:
                continue

    def _serve(self):
        stream = None
        try:
            while not self.release.is_set():
                try:
                    raw, _ = self.listener.accept()
                    break
                except socket.timeout:
                    continue
            else:
                return
            self._track(raw)
            raw.settimeout(2)
            self.accepted.set()
            if self.phase == 'handshake_stall':
                self._cancel_later()
                self._wait_for_disconnect(raw)
                return
            sock = self._track(self.context.wrap_socket(raw, server_side=True)) if self.tls else raw
            stream = sock.makefile('rb')
            first = stream.readline(8193)
            assert first.endswith(b'\r\n') and len(first) <= 8192
            method, path, _ = first.decode('ascii').strip().split(' ', 2)
            headers, total = {}, 0
            while True:
                line = stream.readline(8193)
                total += len(line)
                assert total <= 65536 and line
                if line == b'\r\n':
                    break
                name, value = line.decode('latin-1').split(':', 1)
                headers[name.lower()] = value.strip()
            count = int(headers.get('content-length', '0'))
            assert 0 <= count <= 2 * 1024 * 1024
            request_body = stream.read(count)
            assert len(request_body) == count
            self.requests.append(dict(method=method, path=path, headers=headers, body=request_body))
            self.headers_received.set()
            self._cancel_later()
            if self.phase == 'headers_stall':
                self._wait_for_disconnect(sock)
                return
            lines = [f'HTTP/1.1 {self.status} Synthetic', f'Content-Type: {self.content_type}',
                     f'Connection: {self.connection}']
            if self.framing == 'length':
                lines.append(f'Content-Length: {len(self.body)}')
            elif self.framing == 'chunked':
                lines.append('Transfer-Encoding: chunked')
            elif self.framing != 'eof':
                raise AssertionError('Unknown test response framing')
            if 300 <= self.status < 400:
                lines.append('Location: http://127.0.0.1:9/must-not-follow')
            sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii'))
            if self.phase == 'eof_delay' and self.release.wait(0.08):
                return

            def send(chunk):
                self.body_started.set()
                if self.framing == 'chunked':
                    sock.sendall(f'{len(chunk):X}\r\n'.encode() + chunk + b'\r\n')
                else:
                    sock.sendall(chunk)

            if self.phase == 'drip':
                # Every idle gap is below the socket timeout. Only a whole-call
                # deadline/cancellation can stop read(65536) while it is active.
                for start in range(0, len(self.body), 3):
                    send(self.body[start:start + 3])
                    if self.release.wait(0.06):
                        return
            else:
                send(self.body)
            if self.phase == 'eof_delay' and self.release.wait(0.21):
                return
            if self.framing == 'chunked':
                sock.sendall(b'0\r\n\r\n')
            if self.connection == 'keep-alive' and self.framing != 'eof':
                self._wait_for_disconnect(sock)
        except (ConnectionError, ssl.SSLError, OSError):
            # A cancelled/timed-out client is expected to abort its socket.
            self.disconnected.set()
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            if stream is not None:
                stream.close()
            with self.socket_lock:
                for sock in reversed(self.sockets):
                    sock.close()

    def close(self):
        self.release.set()
        self.listener.close()
        with self.socket_lock:
            for sock in self.sockets:
                try:
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
        for timer in self.timers:
            timer.cancel()
            timer.join(0.5)
        self.thread.join(2.5)
        assert not self.thread.is_alive(), 'Synthetic provider server did not terminate.'
        assert not self.errors, self.errors


@pytest.fixture
def provider_server(tls_material, monkeypatch):
    cert, key = tls_material
    servers = []

    def client_context(*args, **kwargs):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=cert)
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        return context

    monkeypatch.setattr(providers.ssl, 'create_default_context', client_context)

    def create(**kwargs):
        server = LoopbackProvider(cert, key, **kwargs)
        servers.append(server)
        return server

    yield create
    for server in reversed(servers):
        server.close()


def _profile(server):
    return ProviderProfile('loopback', 'Synthetic local provider', 'openai-compatible',
                           server.endpoint, 'local-test-model', local=True, enabled=True)


def _request(tmp_path, server, *, cancelled=lambda: False, timeout=BUDGET):
    gateway = Gateway(tmp_path / 'gateway')
    return gateway.request(_profile(server), 'llm.chat', PAYLOAD, task_id='synthetic-task',
        data_class='synthetic-test', cancelled=cancelled, timeout=timeout)


def _response_spy(monkeypatch):
    observed = []
    original = http.client.HTTPConnection.getresponse

    def inspect(connection):
        active = connection.sock
        response = original(connection)
        observed.append((active, connection.sock, response))
        return response

    monkeypatch.setattr(http.client.HTTPConnection, 'getresponse', inspect)
    return observed


def _closed_response(observed, *, detached):
    assert len(observed) == 1
    active, retained, response = observed[0]
    assert response.will_close is detached
    assert (retained is None) is detached
    assert response.fp is None, 'HTTPResponse retained the socket-backed file after request exit.'
    assert active.fileno() == -1, 'The response-owned native socket is still open.'


@pytest.mark.parametrize('framing,connection', [
    ('length', 'close'), ('length', 'keep-alive'), ('chunked', 'close'),
    ('chunked', 'keep-alive'), ('eof', 'close'),
])
def test_real_tls_drip_obeys_whole_call_deadline(tmp_path, provider_server, monkeypatch, framing, connection):
    server = provider_server(phase='drip', framing=framing, connection=connection)
    observed = _response_spy(monkeypatch)
    before = {thread.ident for thread in threads() if thread.name == 'carr-provider-watchdog'}
    started = monotonic()
    with pytest.raises((InterruptedError, TimeoutError, ProviderError)):
        _request(tmp_path, server)
    assert monotonic() - started < RETURN_LIMIT
    assert server.headers_received.is_set() and server.body_started.is_set()
    _closed_response(observed, detached=connection == 'close' or framing == 'eof')
    assert {thread.ident for thread in threads() if thread.name == 'carr-provider-watchdog'} == before


@pytest.mark.parametrize('tls', [False, True])
def test_active_body_read_is_cancellable(tmp_path, provider_server, monkeypatch, tls):
    cancel = Event()
    server = provider_server(tls=tls, phase='drip', cancel_event=cancel)
    observed = _response_spy(monkeypatch)
    started = monotonic()
    with pytest.raises(InterruptedError):
        _request(tmp_path, server, cancelled=cancel.is_set, timeout=5)
    assert monotonic() - started < RETURN_LIMIT and cancel.is_set()
    assert server.headers_received.is_set()
    _closed_response(observed, detached=True)


@pytest.mark.parametrize('framing', ['chunked', 'eof'])
def test_late_end_of_body_never_returns_success(tmp_path, provider_server, framing):
    # The first read returns exactly 65536 bytes before the deadline. The
    # terminator/EOF arrives later, but within the per-socket idle timeout.
    # Accepting an empty read before checking the global deadline is incorrect.
    body = BODY + b' ' * (65536 - len(BODY))
    server = provider_server(phase='eof_delay', framing=framing, body=body)
    started = monotonic()
    with pytest.raises((InterruptedError, TimeoutError, ProviderError)):
        _request(tmp_path, server)
    assert monotonic() - started < RETURN_LIMIT
    assert server.headers_received.is_set()


@pytest.mark.parametrize('phase', ['handshake_stall', 'headers_stall'])
def test_tls_setup_and_headers_cancellation_is_observed(tmp_path, provider_server, phase):
    cancel = Event()
    server = provider_server(phase=phase, cancel_event=cancel)
    started = monotonic()
    with pytest.raises(InterruptedError):
        _request(tmp_path, server, cancelled=cancel.is_set, timeout=1)
    assert monotonic() - started < RETURN_LIMIT and cancel.is_set()
    assert server.accepted.is_set()
    if phase == 'handshake_stall':
        assert not server.requests
    else:
        assert len(server.requests) == 1


@pytest.mark.parametrize('connection', ['close', 'keep-alive'])
def test_real_tls_fast_success_preserves_protocol_and_closes_response(tmp_path, provider_server, monkeypatch, connection):
    server = provider_server(connection=connection)
    observed = _response_spy(monkeypatch)
    result = _request(tmp_path, server, timeout=3)
    assert result['text'] == 'Local TLS response'
    assert len(server.requests) == 1
    sent = server.requests[0]
    assert sent['method'] == 'POST' and sent['path'] == '/v1/chat/completions'
    assert sent['headers']['host'] == f'127.0.0.1:{server.port}'
    assert json.loads(sent['body'])['messages'] == PAYLOAD['messages']
    _closed_response(observed, detached=connection == 'close')


@pytest.mark.parametrize('status', [302, 503])
def test_real_tls_error_never_follows_redirect_and_closes_response(tmp_path, provider_server, monkeypatch, status):
    server = provider_server(status=status)
    observed = _response_spy(monkeypatch)
    with pytest.raises(ProviderError):
        _request(tmp_path, server)
    assert len(server.requests) == 1
    _closed_response(observed, detached=True)


def _approved_gateway(tmp_path, server, monkeypatch, adapter='openai-compatible'):
    """Route a synthetic approved authority locally, retaining real TLS.

    DNS policy has its own no-socket regressions. Both the canonical transport
    and the pinned transport are routed locally here, so baseline runs cannot
    send even synthetic credentials to an external service.
    """
    host, path, model, capabilities, voice = {
        'openai-compatible': ('api.example.test', '/v1', 'test-model', ('llm.chat',), None),
        'google-translation-v2': ('translation.googleapis.com', '/language/translate/v2',
                                 'nmt', ('translation.text', 'translation.languages'), None),
        'elevenlabs': ('api.elevenlabs.io', '/v1', 'test-model', ('speech.synthesize',), 'test_voice'),
    }[adapter]
    profile = ProviderProfile('remote_test', 'Synthetic TLS account', adapter,
        'https://' + host + path, model, capabilities=capabilities, voice=voice, enabled=True,
        credential_ref='carr/provider/remote_test/' + 'a' * 32)
    approval = Authorization('tls_approval', 'tls_task', ((profile.id, profile.endpoint),),
        capabilities, ('synthetic-test',), '1', 2, time() + 60)
    destinations = []

    def resolve(name, port, *args, **kwargs):
        assert name == host and port == 443
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
                 ('127.0.0.1', server.port))]

    def approved_route(name, port, *, local, deadline, cancelled):
        assert not local
        destinations.append((name, port))
        return [(family, kind, protocol, address)
                for family, kind, protocol, _, address in resolve(name, port)]

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(providers, '_addresses', approved_route, raising=False)

    class Vault:
        def get(self, *args):
            return 'SYNTHETIC_HTTP_TLS_TOKEN'

    gateway = Gateway(tmp_path / 'remote-gateway', vault=Vault(), mode='APPROVED_REMOTE')
    options = dict(task_id='tls_task', data_class='synthetic-test', authorization=approval,
                   operation_id='only_call', max_cost='0.20')
    return gateway, profile, approval, options, destinations


def test_google_discovery_remains_get_with_verified_original_tls_authority(
        tmp_path, provider_server, monkeypatch):
    body = json.dumps({'data': {'languages': [{'language': 'uk', 'name': 'Ukrainian'}]}}).encode()
    server = provider_server(body=body)
    gateway, profile, approval, options, destinations = _approved_gateway(
        tmp_path, server, monkeypatch, 'google-translation-v2')
    result = gateway.request(profile, 'translation.languages', {'target': 'en'}, timeout=3, **options)
    assert result['languages'] == [{'language': 'uk', 'name': 'Ukrainian'}]
    sent, = server.requests
    assert sent['method'] == 'GET' and sent['body'] == b''
    assert sent['path'] == '/language/translate/v2/languages?target=en&model=nmt'
    assert sent['headers']['host'] == 'translation.googleapis.com'
    assert sent['headers']['x-goog-api-key'] == 'SYNTHETIC_HTTP_TLS_TOKEN'
    assert server.sni == ['translation.googleapis.com']
    assert destinations == [('translation.googleapis.com', 443)]
    assert gateway.ledger.get(approval.id)['operations']['only_call']['status'] == 'complete'


@pytest.mark.parametrize('first_failure', [None, 'refused', 'socket_unavailable', 'cancel', 'deadline'])
def test_public_dns_answers_stay_pinned_across_bounded_tcp_fallback(
        tmp_path, provider_server, monkeypatch, first_failure):
    server = provider_server()
    addresses = providers._addresses
    gateway, profile, approval, options, _ = _approved_gateway(tmp_path, server, monkeypatch)
    monkeypatch.setattr(providers, '_addresses', addresses)
    lookups, dialled, sockets, created, cancel, offset = [], [], [], [], Event(), [0]
    native_socket = socket.socket
    first, last = ('93.184.216.35', 443), ('93.184.216.34', 443)
    if first_failure == 'socket_unavailable':
        first = ('2606:4700:4700::1111', 443, 0, 0)
    approved = [last] if first_failure is None else [first, last]
    monkeypatch.setattr(providers, 'monotonic', lambda: monotonic() + offset[0])

    def resolve(host, port, *args, **kwargs):
        lookups.append((host, port))
        assert lookups == [('api.example.test', 443)], 'A second DNS answer could change the destination.'
        return [(socket.AF_INET6 if len(address) == 4 else socket.AF_INET,
                 socket.SOCK_STREAM, socket.IPPROTO_TCP, '', address)
                for address in approved]

    class RoutedSocket(native_socket):
        def __init__(self, family=socket.AF_INET, kind=socket.SOCK_STREAM, protocol=0, fileno=None):
            if fileno is None:
                created.append(family)
            if family == socket.AF_INET6 and first_failure == 'socket_unavailable':
                raise OSError(errno.EAFNOSUPPORT, 'Synthetic disabled IPv6 sockets')
            super().__init__(family, kind, protocol, fileno=fileno)

        def connect_ex(self, address):
            dialled.append(address)
            sockets.append(self)
            if address == first:
                if first_failure == 'cancel':
                    cancel.set()
                elif first_failure == 'deadline':
                    offset[0] = 10
                return errno.ECONNREFUSED
            assert address == last
            return super().connect_ex(('127.0.0.1', server.port))

        def connect(self, address):
            # Keep a baseline HTTPConnection run local too; no test variant can
            # contact the synthetic public address supplied by the DNS double.
            dialled.append(address)
            sockets.append(self)
            if address == first:
                raise OSError(errno.ECONNREFUSED, 'Synthetic first address refusal')
            assert address == last
            return super().connect(('127.0.0.1', server.port))

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(socket, 'socket', RoutedSocket)
    if first_failure in ('cancel', 'deadline'):
        with pytest.raises(InterruptedError if first_failure == 'cancel' else TimeoutError):
            gateway.request(profile, 'llm.chat', PAYLOAD, timeout=3, cancelled=cancel.is_set, **options)
        assert dialled == [first] and not server.requests
        assert gateway.ledger.get(approval.id)['operations']['only_call']['status'] == 'not_sent'
    else:
        result = gateway.request(profile, 'llm.chat', PAYLOAD, timeout=3, **options)
        assert result['text'] == 'Local TLS response'
        assert dialled == ([last] if first_failure == 'socket_unavailable' else approved)
        if first_failure == 'socket_unavailable':
            assert created == [socket.AF_INET6, socket.AF_INET]
        assert len(server.requests) == 1
        assert server.requests[0]['headers']['host'] == 'api.example.test'
        assert server.sni == ['api.example.test']
        assert gateway.ledger.get(approval.id)['operations']['only_call']['status'] == 'complete'
    assert lookups == [('api.example.test', 443)]
    assert sockets and all(sock.fileno() == -1 for sock in sockets)


@pytest.mark.parametrize('phase,reason', [
    ('handshake_stall', 'deadline'), ('handshake_stall', 'cancel'),
    ('headers_stall', 'deadline'), ('drip', 'cancel'),
])
def test_remote_charge_state_tracks_whether_http_transmission_began(
        tmp_path, provider_server, monkeypatch, phase, reason):
    cancel = Event()
    server = provider_server(phase=phase, cancel_event=cancel if reason == 'cancel' else None)
    gateway, profile, approval, options, _ = _approved_gateway(tmp_path, server, monkeypatch)
    started = monotonic()
    error = TimeoutError if reason == 'deadline' else InterruptedError
    with pytest.raises(error):
        gateway.request(profile, 'llm.chat', PAYLOAD, cancelled=cancel.is_set,
                        timeout=BUDGET if reason == 'deadline' else 3, **options)
    assert monotonic() - started < RETURN_LIMIT
    row = gateway.ledger.get(approval.id)['operations']['only_call']
    sent = phase != 'handshake_stall'
    assert row['status'] == ('uncertain' if sent else 'not_sent')
    assert row['output_sha256'] is None and row['actual_cost'] is None
    assert len(server.requests) == int(sent)
    assert 'SYNTHETIC_HTTP_TLS_TOKEN' not in json.dumps(gateway.ledger.get(approval.id))
    if sent:
        with pytest.raises(ProviderError, match='already been attempted'):
            gateway.request(profile, 'llm.chat', PAYLOAD, **options)
        assert len(server.requests) == 1


@pytest.mark.parametrize('framing', ['length', 'chunked', 'eof'])
def test_authorized_audio_stays_streamed_and_verified_with_all_body_framings(
        tmp_path, provider_server, monkeypatch, framing):
    body = b'ID3' + b'synthetic-audio-bytes' * 4000
    server = provider_server(body=body, framing=framing, content_type='audio/mpeg')
    gateway, profile, approval, options, _ = _approved_gateway(tmp_path, server, monkeypatch, 'elevenlabs')
    output = tmp_path / 'generated.mp3'
    result = gateway.request(profile, 'speech.synthesize', {'text': 'Synthetic speech'},
                             output=output, timeout=3, **options)
    assert output.read_bytes() == body and result['output_sha256'] == sha256(body).hexdigest()
    assert result['artifacts'] == [{'path': str(output), 'sha256': sha256(body).hexdigest(), 'size': len(body)}]
    assert not list(tmp_path.glob('.carr-provider-*.tmp'))
    sent, = server.requests
    assert sent['path'] == '/v1/text-to-speech/test_voice?output_format=mp3_44100_128'
    assert sent['headers']['host'] == 'api.elevenlabs.io'
    assert sent['headers']['xi-api-key'] == 'SYNTHETIC_HTTP_TLS_TOKEN'
    assert server.sni == ['api.elevenlabs.io']
    assert gateway.ledger.get(approval.id)['operations']['only_call']['status'] == 'complete'


@pytest.mark.parametrize('reason', ['deadline', 'cancel'])
def test_interrupted_audio_discards_already_streamed_bytes_and_never_retries(
        tmp_path, provider_server, monkeypatch, reason):
    body = b'ID3' + b'a' * (65536 - 3)
    server = provider_server(body=body, phase='eof_delay', framing='eof', content_type='audio/mpeg')
    gateway, profile, approval, options, _ = _approved_gateway(tmp_path, server, monkeypatch, 'elevenlabs')
    original_temporary = providers.NamedTemporaryFile
    writes, paths, cancel = [], [], Event()

    class ObservedStream:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, block):
            written = self.stream.write(block)
            writes.append(written)
            if reason == 'cancel':
                cancel.set()
            return written

    def temporary(*args, **kwargs):
        stream = original_temporary(*args, **kwargs)
        paths.append(Path(stream.name))
        return ObservedStream(stream)

    monkeypatch.setattr(providers, 'NamedTemporaryFile', temporary)
    output = tmp_path / 'must-not-publish.mp3'
    with pytest.raises(TimeoutError if reason == 'deadline' else InterruptedError):
        gateway.request(profile, 'speech.synthesize', {'text': 'Synthetic speech'}, output=output,
            timeout=BUDGET if reason == 'deadline' else 3, cancelled=cancel.is_set, **options)
    assert writes == [65536]  # Real temporary-file writes preceded the interruption.
    assert not output.exists() and paths and all(not path.exists() for path in paths)
    assert not list(tmp_path.glob('.carr-provider-*.tmp'))
    row = gateway.ledger.get(approval.id)['operations']['only_call']
    assert row['status'] == 'uncertain' and row['output_sha256'] is None
    with pytest.raises(ProviderError, match='already been attempted'):
        gateway.request(profile, 'speech.synthesize', {'text': 'Synthetic speech'}, output=output, **options)
    assert len(server.requests) == 1


@pytest.mark.parametrize('reason', ['deadline', 'cancel'])
def test_valid_decoded_response_cannot_complete_after_its_budget_or_cancellation(
        tmp_path, provider_server, monkeypatch, reason):
    server = provider_server()
    gateway, profile, approval, options, _ = _approved_gateway(tmp_path, server, monkeypatch)
    cancel, offset = Event(), [0]
    original_check = providers._reject_decoded_credential
    monkeypatch.setattr(providers, 'monotonic', lambda: monotonic() + offset[0])

    def decoded(*args):
        original_check(*args)
        if reason == 'deadline':
            offset[0] = 10
        else:
            cancel.set()

    monkeypatch.setattr(providers, '_reject_decoded_credential', decoded)
    with pytest.raises(TimeoutError if reason == 'deadline' else InterruptedError):
        gateway.request(profile, 'llm.chat', PAYLOAD, timeout=3, cancelled=cancel.is_set, **options)
    row = gateway.ledger.get(approval.id)['operations']['only_call']
    assert row['status'] == 'uncertain' and row['output_sha256'] is None
    assert len(server.requests) == 1


def test_observed_cancellation_stays_latched_when_callback_later_clears(
        tmp_path, provider_server, monkeypatch):
    server = provider_server()
    gateway, profile, approval, options, _ = _approved_gateway(tmp_path, server, monkeypatch)
    cancel, stopped = Event(), Event()
    original_check = providers._reject_decoded_credential
    original_interrupt = providers._Connection._interrupt

    def interrupt(connection):
        original_interrupt(connection)
        stopped.set()

    def decoded(*args):
        original_check(*args)
        cancel.set()
        assert stopped.wait(0.5), 'Watchdog did not observe the synthetic cancellation.'
        cancel.clear()

    monkeypatch.setattr(providers._Connection, '_interrupt', interrupt)
    monkeypatch.setattr(providers, '_reject_decoded_credential', decoded)
    with pytest.raises(InterruptedError):
        gateway.request(profile, 'llm.chat', PAYLOAD, timeout=3, cancelled=cancel.is_set, **options)
    assert not cancel.is_set() and stopped.is_set()
    row = gateway.ledger.get(approval.id)['operations']['only_call']
    assert row['status'] == 'uncertain' and row['output_sha256'] is None


def _advanced_gateway(tmp_path, server, monkeypatch, operation, payload):
    """Use the real pinned transport; every synthetic public address stays local."""
    from carr_library.assistant.profiles import google_oauth_credential
    parent = 'projects/123456789/locations/us-central1'
    profile = ProviderProfile('advanced_tls', 'Synthetic Advanced TLS account', 'google-translation-v3',
        'https://translation.googleapis.com/v3/' + parent, 'general/nmt',
        capabilities=('translation.text', 'translation.languages'), enabled=True,
        credential_ref='carr/provider/advanced_tls/' + 'a' * 32)
    route, body, _ = providers._request(profile, operation, payload)
    request_hash = sha256(providers.canonical(body).encode('utf-8')).hexdigest()
    approval = Authorization('advanced_tls_approval', 'advanced_tls_task', ((profile.id, profile.endpoint),),
        (operation,), ('selected-excerpt',), '1', 1, time() + 60, (request_hash,))
    token = 'SYNTHETIC_ADVANCED_TLS_TOKEN'
    class Vault:
        def get(self, identity, reference):
            assert (identity, reference) == (profile.id, profile.credential_ref)
            return google_oauth_credential(token, time() + 1800, profile.endpoint)
    gateway = Gateway(tmp_path / 'advanced-gateway', vault=Vault(), mode='APPROVED_REMOTE')
    options = dict(task_id=approval.task_id, data_class='selected-excerpt', authorization=approval,
                   operation_id='advanced_only_call', max_cost='0.20')
    native_socket = socket.socket
    lookups, dialled = [], []
    destination = ('93.184.216.34', 443)

    def resolve(host, port, *args, **kwargs):
        lookups.append((host, port))
        assert lookups == [('translation.googleapis.com', 443)]
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', destination)]

    class RoutedSocket(native_socket):
        def connect_ex(self, address):
            assert address == destination
            dialled.append(address)
            return super().connect_ex(('127.0.0.1', server.port))

        def connect(self, address):
            assert address == destination
            dialled.append(address)
            return super().connect(('127.0.0.1', server.port))

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(socket, 'socket', RoutedSocket)
    return gateway, profile, approval, options, lookups, dialled, body, route


@pytest.mark.parametrize('operation,glossary', [
    ('translation.languages', False), ('translation.text', False), ('translation.text', True),
])
def test_advanced_v3_keeps_verified_tls_authority_and_exact_plaintext(
        tmp_path, provider_server, monkeypatch, operation, glossary):
    parent = 'projects/123456789/locations/us-central1'
    model = parent + '/models/general/nmt'
    value = '  Literal &amp; Україна.\n'
    if operation == 'translation.languages':
        payload = {'target': 'en'}
        response = {'languages': [{'languageCode': 'uk', 'displayName': 'Ukrainian', 'supportTarget': True}]}
    else:
        payload = {'q': 'Selected source &amp; text.', 'source': 'en', 'target': 'uk'}
        result = {'translatedText': value, 'model': model}
        if glossary:
            payload['glossary'] = parent + '/glossaries/test-terms'
            result['glossaryConfig'] = {'glossary': payload['glossary'], 'ignoreCase': False}
        response = {'glossaryTranslations' if glossary else 'translations': [result]}
    server = provider_server(body=json.dumps(response, ensure_ascii=False).encode('utf-8'))
    gateway, profile, approval, options, lookups, dialled, body, route = _advanced_gateway(
        tmp_path, server, monkeypatch, operation, payload)
    observed = _response_spy(monkeypatch)
    result = gateway.request(profile, operation, payload, timeout=3, **options)
    sent, = server.requests
    assert sent['path'] == '/v3/' + parent + route
    assert sent['headers']['host'] == 'translation.googleapis.com'
    assert sent['headers']['authorization'] == 'Bearer SYNTHETIC_ADVANCED_TLS_TOKEN'
    assert sent['headers']['x-goog-user-project'] == '123456789'
    assert 'x-goog-api-key' not in sent['headers']
    assert server.sni == ['translation.googleapis.com']
    assert lookups == [('translation.googleapis.com', 443)]
    assert dialled == [('93.184.216.34', 443)]
    if operation == 'translation.languages':
        assert sent['method'] == 'GET' and sent['body'] == b''
        assert result['languages'] == [{'language': 'uk', 'name': 'Ukrainian', 'source': False, 'target': True}]
    else:
        assert sent['method'] == 'POST' and json.loads(sent['body']) == body
        assert body['contents'] == [payload['q']] and body['mimeType'] == 'text/plain'
        assert result['text'] == value and result['reported_model'] == model
        assert result['glossary_config'] == body.get('glossaryConfig')
    assert gateway.ledger.get(approval.id)['operations']['advanced_only_call']['status'] == 'complete'
    _closed_response(observed, detached=True)


@pytest.mark.parametrize('phase,reason', [('drip', 'deadline'), ('drip', 'cancel'), ('eof_delay', 'deadline')])
def test_advanced_v3_late_body_cannot_publish_or_retry(
        tmp_path, provider_server, monkeypatch, phase, reason):
    parent = 'projects/123456789/locations/us-central1'
    raw = json.dumps({'translations': [{'translatedText': 'Late result',
        'model': parent + '/models/general/nmt'}]}).encode()
    if phase == 'eof_delay':
        raw += b' ' * (65536 - len(raw))
    cancel = Event()
    server = provider_server(phase=phase, framing='eof' if phase == 'eof_delay' else 'length',
                             body=raw, cancel_event=cancel if reason == 'cancel' else None)
    payload = {'q': 'Selected immutable text.', 'source': 'en', 'target': 'uk'}
    gateway, profile, approval, options, lookups, dialled, _, _ = _advanced_gateway(
        tmp_path, server, monkeypatch, 'translation.text', payload)
    observed = _response_spy(monkeypatch)
    before = {thread.ident for thread in threads() if thread.name == 'carr-provider-watchdog'}
    started = monotonic()
    with pytest.raises(InterruptedError if reason == 'cancel' else TimeoutError):
        gateway.request(profile, 'translation.text', payload, cancelled=cancel.is_set,
                        timeout=3 if reason == 'cancel' else BUDGET, **options)
    assert monotonic() - started < RETURN_LIMIT
    assert len(server.requests) == 1 and server.body_started.is_set()
    recorded = gateway.ledger.get(approval.id)['operations']['advanced_only_call']
    assert recorded['status'] == 'uncertain' and recorded['output_sha256'] is None
    with pytest.raises(ProviderError, match='already been attempted'):
        gateway.request(profile, 'translation.text', payload, timeout=3, **options)
    assert len(server.requests) == 1 and lookups == [('translation.googleapis.com', 443)]
    assert dialled == [('93.184.216.34', 443)]
    _closed_response(observed, detached=True)
    assert {thread.ident for thread in threads() if thread.name == 'carr-provider-watchdog'} == before
