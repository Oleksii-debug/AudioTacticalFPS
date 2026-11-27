"""Actual loopback HTTP and CLI children; select only in the explicit loopback gate.

This suite cannot run in the all-socket-denied common runner. No external server,
model download, paid account or model inference is involved.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread, Event, get_ident
from time import time, monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

import carr_library
from carr_library.application import LibraryApp
from carr_library.assistant import providers
from carr_library.assistant.profiles import ProviderProfile, ProfileStore
from carr_library.assistant.providers import Gateway, ProviderError, translation_language_approval
from carr_library.assistant.translation import TranslationService, translation_approval


@pytest.fixture
def server():
    state = {'requests': [], 'status': 200, 'custom': None}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def handle_request(self, method):
            length = int(self.headers.get('Content-Length', '0'))
            assert 0 <= length <= 65536
            body = json.loads(self.rfile.read(length)) if length else None
            state['requests'].append((method, self.path, body, dict(self.headers)))
            if state['custom'] is not None:
                raw = state['custom']
            elif method == 'GET':
                raw = json.dumps([
                    {'code': 'en', 'name': 'English', 'targets': ['uk']},
                    {'code': 'uk', 'name': 'Ukrainian', 'targets': []},
                ]).encode()
            else:
                result = {'translatedText': 'Контроль: ' + body['q']}
                if body['source'] == 'auto':
                    result['detectedLanguage'] = {'language': 'en', 'confidence': 99}
                raw = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(state['status'])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            if state['status'] == 302:
                self.send_header('Location', 'https://example.invalid/forbidden')
            self.end_headers()
            stall = state.get('stall')
            if stall is not None:
                received, release = stall
                self.wfile.write(raw[:1])
                self.wfile.flush()
                received.set()
                release.wait(10)
                try:
                    self.wfile.write(raw[1:])
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.wfile.write(raw)
        def do_GET(self):
            self.handle_request('GET')
        def do_POST(self):
            self.handle_request('POST')
    host = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    host.daemon_threads = True
    thread = Thread(target=lambda: host.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    p = ProviderProfile('local_http', 'Explicit loopback fixture', 'libretranslate-local',
        'http://127.0.0.1:' + str(host.server_port), 'synthetic-protocol-only',
        ('translation.text', 'translation.languages'), local=True, enabled=True)
    try:
        yield SimpleNamespace(profile=p, state=state)
    finally:
        host.shutdown()
        host.server_close()
        thread.join(5)
        assert not thread.is_alive()


def initialized(tmp_path, server):
    service = TranslationService(tmp_path / 'translation')
    p = server.profile
    approval = translation_language_approval(p, expires_at=time() + 60)
    service.refresh_languages(p, authorization=approval)
    return service, p


def test_actual_loopback_preserves_exact_selected_request_and_receipt(tmp_path, server, monkeypatch):
    # The selected http.client transport ignores conventional proxy variables.
    monkeypatch.setenv('HTTP_PROXY', 'http://example.invalid:8080')
    monkeypatch.setenv('HTTPS_PROXY', 'http://example.invalid:8080')
    service, p = initialized(tmp_path, server)
    value = 'Selected Unicode 🙂 <literal> & text.'
    approval, cost = translation_approval(p, value, 'auto', 'uk', '0')
    identity = service.submit(p, value, 'auto', 'uk', authorization=approval, per_call_cost=cost)
    assert service.resume(identity)['status'] == 'complete'
    assert [(row[0], row[1]) for row in server.state['requests']] == [('GET', '/languages'), ('POST', '/translate')]
    sent = server.state['requests'][1]
    assert sent[2] == {'q': value, 'source': 'auto', 'target': 'uk', 'format': 'text'}
    assert not any(key.lower() in ('authorization', 'x-goog-api-key') for key in sent[3])
    assert service._load(identity)['receipts'][0]['text'] == 'Контроль: ' + value


@pytest.mark.parametrize('status', [302, 401, 503])
def test_actual_local_http_failure_never_redirects_or_retries(tmp_path, server, status):
    service, p = initialized(tmp_path, server)
    approval, cost = translation_approval(p, 'Approved text', 'en', 'uk', '0')
    identity = service.submit(p, 'Approved text', 'en', 'uk', authorization=approval, per_call_cost=cost)
    server.state['status'] = status
    with pytest.raises(ProviderError):
        service.resume(identity)
    before = len(server.state['requests'])
    with pytest.raises(ProviderError, match='already been attempted'):
        service.resume(identity)
    assert len(server.state['requests']) == before == 2


def test_actual_automatic_response_must_match_declared_pair(tmp_path, server):
    service, p = initialized(tmp_path, server)
    approval, cost = translation_approval(p, 'Approved text', 'auto', 'uk', '0')
    identity = service.submit(p, 'Approved text', 'auto', 'uk', authorization=approval, per_call_cost=cost)
    server.state['custom'] = b'{"translatedText":"invalid direction","detectedLanguage":{"language":"fr","confidence":99}}'
    with pytest.raises(ValueError, match='direction'):
        service.resume(identity)
    assert service.jobs.load(identity)['receipts'] == []
    assert len(server.state['requests']) == 2



def test_actual_loopback_has_no_discovery_or_text_transfer_without_own_approval(tmp_path, server):
    service = TranslationService(tmp_path / 'translation')
    p = server.profile
    for operation, payload, data_class in (
            ('translation.languages', {'target': 'en'}, 'user-instruction'),
            ('translation.text', {'q': 'Never approved', 'target': 'uk'}, 'selected-excerpt')):
        with pytest.raises(PermissionError, match='explicit local'):
            service.gateway.request(p, operation, payload, task_id='unapproved',
                                    data_class=data_class, max_cost='0')
    assert server.state['requests'] == []
    assert not tuple(service.jobs.identities())


def test_actual_local_pause_and_resume_keeps_completed_chunk_and_ledger(tmp_path, server):
    service, p = initialized(tmp_path, server)
    value = 'Chosen paragraph. ' * 350
    approval, cost = translation_approval(p, value, 'en', 'uk', '0')
    identity = service.submit(p, value, 'en', 'uk', authorization=approval, per_call_cost=cost)
    paused = []
    result = service.resume(identity, cancelled=lambda: bool(paused),
        progress=lambda *args: paused.append(True))
    assert result['status'] == 'paused' and result['completed'] == 1 and result['total'] == 2
    first = service._load(identity)['receipts'][0]
    assert len(server.state['requests']) == 2
    assert service.resume(identity)['status'] == 'complete'
    assert len(server.state['requests']) == 3
    assert service._load(identity)['receipts'][0] == first
    assert service.resume(identity)['status'] == 'complete'
    assert len(server.state['requests']) == 3
    ledger = service.gateway.ledger.get(approval.id)
    assert all(row['status'] == 'complete' and row['reserved_cost'] == cost
               for row in ledger['operations'].values())


@pytest.mark.parametrize('raw', [
    b'{"translatedText":"one","translatedText":"two"}',
    b'{"translatedText":"bad","detectedLanguage":{"language":"auto","confidence":90}}',
    ('{"translatedText":"bad","detectedLanguage":{"language":"en","confidence":'
       + '1' + '0' * 400 + '}}').encode(),
])
def test_actual_protocol_rejection_is_uncertain_and_never_retried(tmp_path, server, raw):
    service, p = initialized(tmp_path, server)
    approval, cost = translation_approval(p, 'Approved exact source', 'auto', 'uk', '0')
    identity = service.submit(p, 'Approved exact source', 'auto', 'uk',
                              authorization=approval, per_call_cost=cost)
    server.state['custom'] = raw
    with pytest.raises(ProviderError):
        service.resume(identity)
    assert service.jobs.load(identity)['receipts'] == []
    assert service.gateway.ledger.get(approval.id)['operations'][identity + '_0']['status'] == 'uncertain'
    with pytest.raises(ProviderError, match='already been attempted'):
        service.resume(identity)
    assert len(server.state['requests']) == 2


def cli(app, *arguments):
    # Same exact installed/source package as this parent, never another checkout.
    package_parent = str(Path(carr_library.__file__).resolve().parent.parent)
    batch = str(arguments[0]).startswith('batch-')
    module = 'translation_batch_cli' if batch else 'translation_cli'
    if batch:
        arguments = (str(arguments[0])[6:], *arguments[1:])
    code = ('import sys; sys.path.insert(0,sys.argv.pop(1)); '
            'from carr_library.assistant.' + module + ' import main; raise SystemExit(main())')
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, package_parent,
        '--data-dir', str(app.root), *map(str, arguments)], capture_output=True,
        text=True, encoding='utf-8', timeout=90)
    return result, json.loads(result.stdout) if result.stdout.strip() else None


@pytest.mark.parametrize('newline', ['\n', '\r\n'], ids=['lf', 'crlf'])
def test_actual_cli_requires_local_approval_and_keeps_completed_work_offline(tmp_path, server, newline):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('CLI local translator')
    ProfileStore(app.root / 'state/providers').save(server.profile)
    result, _ = cli(app, 'languages', '--profile', server.profile.id, '--approve-remote')
    assert result.returncode == 1 and not server.state['requests'], result.stderr
    result, rows = cli(app, 'languages', '--profile', server.profile.id, '--approve-local')
    assert result.returncode == 0 and rows[0]['targets'] == ['uk'], result.stderr
    selected = tmp_path / 'selected.txt'
    original = 'First paragraph. ' * 350 + newline + 'Second paragraph 🙂.'
    # Own the exact reviewed UTF-8 bytes on both platforms; text-mode output
    # would translate LF to CRLF before the application ever sees the input.
    selected.write_bytes(original.encode('utf-8'))
    options = ['--profile', server.profile.id, '--input', selected, '--source', 'en',
               '--target', 'uk', '--budget', '0']
    result, review = cli(app, 'preview', *options)
    assert result.returncode == 0 and review['calls'] == 2, result.stderr
    before = len(server.state['requests'])
    result, _ = cli(app, 'submit', *options, '--review-sha256', review['review_sha256'], '--approve-remote')
    assert result.returncode == 1 and len(server.state['requests']) == before, result.stderr
    result, saved = cli(app, 'submit', *options, '--review-sha256', review['review_sha256'], '--approve-local')
    assert result.returncode == 0 and len(server.state['requests']) == before, result.stderr
    result, completed = cli(app, 'resume', saved['id'])
    assert result.returncode == 0 and completed['status'] == 'complete', result.stderr
    ProfileStore(app.root / 'state/providers').remove(server.profile.id)
    result, exported = cli(app, 'export', saved['id'], '--output', tmp_path / 'offline')
    assert result.returncode == 0 and exported['status'] == 'complete', result.stderr
    assert (tmp_path / 'offline/original.txt').read_bytes() == original.encode()
    report = json.loads((tmp_path / 'offline/translation-provenance.json').read_bytes())
    assert report['local_runtime_identity'] == 'not attested by the local API'
    assert report['quotation_authority'] is False
    assert len(server.state['requests']) == before + 2
    assert selected.read_bytes() == original.encode()


def test_actual_cli_existing_library_batch_uses_saved_children_and_source_lineage(tmp_path, server):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit CLI local batch')
    entries = [app.add_text(workspace, 'Book ' + str(i), 'Original ' + str(i) + '\nRetained paragraph.')
               for i in range(2)]
    originals = [(app.assets.resolve(row['asset']), app.assets.resolve(row['asset']).read_bytes()) for row in entries]
    ProfileStore(app.root / 'state/providers').save(server.profile)
    result, _ = cli(app, 'languages', '--profile', server.profile.id, '--approve-local')
    assert result.returncode == 0, result.stderr
    batch_file = tmp_path / 'chosen.json'
    batch_file.write_text(json.dumps({'schema': 'carr-translation-batch-input/1',
        'title': 'Reviewed local source order', 'items': [
            {'profile': server.profile.id, 'workspace': workspace, 'edition': row['edition'],
             'source': 'en', 'target': 'uk', 'budget': '0'}
            for row in entries]}), encoding='utf-8')
    options = ['--batch-file', batch_file]
    result, review = cli(app, 'batch-preview', *options)
    assert result.returncode == 0 and review['calls'] == 2, result.stderr
    result, _ = cli(app, 'batch-submit', *options,
        '--approve-remote', '--review-sha256', review['review_sha256'])
    assert result.returncode == 1, result.stderr
    result, prepared = cli(app, 'batch-submit', *options,
        '--approve-local', '--review-sha256', review['review_sha256'])
    assert result.returncode == 0 and prepared['total'] == 2 and prepared['status'] == 'queued', result.stderr
    assert len(server.state['requests']) == 1
    result, finished = cli(app, 'batch-resume', prepared['id'])
    assert result.returncode == 0 and finished['status'] == 'complete', result.stderr
    result, exported = cli(app, 'batch-export', prepared['id'], '--output', tmp_path / 'batch-exports.zip')
    assert result.returncode == 0 and exported['status'] == 'complete', result.stderr
    from zipfile import ZipFile
    with ZipFile(tmp_path / 'batch-exports.zip') as archive:
        for index, row in enumerate(entries, 1):
            prefix = f'{index:04d}-translation/'
            report = json.loads(archive.read(prefix + 'translation-provenance.json'))
            assert report['source_anchor']['edition'] == row['edition']
            assert report['quotation_authority'] is False
            assert archive.read(prefix + 'original.txt') == originals[index - 1][1]
    assert all(path.read_bytes() == raw for path, raw in originals)
    before = len(server.state['requests'])
    result, reused = cli(app, 'batch-resume', prepared['id'])
    assert result.returncode == 0 and reused['status'] == 'complete', result.stderr
    assert len(server.state['requests']) == before == 3


@pytest.mark.parametrize('after_start', [False, True], ids=['before-thread-start', 'after-actual-thread-start'])
def test_actual_local_watcher_start_fault_releases_socket_and_started_thread(server, monkeypatch, after_start):
    from urllib.parse import urlsplit
    original_start = Thread.start
    original_connect = providers._Connection._connect
    owners, watchers = [], []
    def observed_connect(owner, address):
        owners.append(owner)
        return original_connect(owner, address)
    def fault(thread):
        if thread.name != 'carr-provider-watchdog':
            return original_start(thread)
        watchers.append(thread)
        if after_start:
            original_start(thread)
        raise RuntimeError('Injected watcher start failure')
    monkeypatch.setattr(providers._Connection, '_connect', observed_connect)
    monkeypatch.setattr(Thread, 'start', fault)
    with pytest.raises(RuntimeError, match='Injected watcher start failure'):
        providers._Connection(urlsplit(server.profile.endpoint), local=True,
                              deadline=monotonic() + 5, cancelled=lambda: False)
    assert len(owners) == len(watchers) == 1
    owner, watcher = owners[0], watchers[0]
    assert owner.watcher is watcher and owner.done.is_set()
    assert (watcher.ident is not None) is after_start
    assert not watcher.is_alive()
    assert owner.raw_socket.fileno() == owner.active_socket.fileno() == -1
    assert owner.connection.sock is None
    assert server.state['requests'] == []


def test_actual_stalled_local_body_is_cancelled_and_scope_guards_stay_on_request_thread(
        tmp_path, server):
    service, p = initialized(tmp_path, server)
    approval, cost = translation_approval(p, 'Explicit stalled text', 'en', 'uk', '0')
    received, release, stop = Event(), Event(), Event()
    server.state['stall'] = received, release
    owner_thread, guard_threads, cancel_threads = get_ident(), [], []
    def cancel_after_first_byte():
        if received.wait(5):
            stop.set()
    canceller = Thread(target=cancel_after_first_byte, name='local-test-canceller')
    canceller.start()
    def scope_guard():
        guard_threads.append(get_ident())
    def interrupted():
        cancel_threads.append(get_ident())
        return stop.is_set()
    before = monotonic()
    try:
        with pytest.raises(InterruptedError):
            service.gateway.request(p, 'translation.text',
                {'q': 'Explicit stalled text', 'source': 'en', 'target': 'uk'},
                task_id=approval.task_id, data_class='selected-excerpt',
                authorization=approval, max_cost=cost, operation_id='stalled_local',
                cancelled=stop.is_set, interrupt_cancelled=interrupted,
                before_request=scope_guard, before_publish=scope_guard, timeout=8)
        assert monotonic() - before < 5
        assert received.is_set()
        assert guard_threads and set(guard_threads) == {owner_thread}
        assert any(identity != owner_thread for identity in cancel_threads)
        assert service.gateway.ledger.get(approval.id)['operations']['stalled_local']['status'] == 'uncertain'
        assert len(server.state['requests']) == 2
        assert not any(t.name == 'carr-provider-watchdog' and t.is_alive()
                       for t in __import__('threading').enumerate())
    finally:
        release.set()
        canceller.join(5)
        assert not canceller.is_alive()


@pytest.mark.parametrize('cancel_body', [False, True], ids=['complete-tls', 'cancel-close-delimited-tls-body'])
def test_actual_tls_keeps_socket_owner_after_http_response_transfer(tmp_path, monkeypatch, cancel_body):
    from datetime import datetime, timedelta, timezone
    import ipaddress
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'CARR synthetic loopback TLS')])
    stamp = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(stamp - timedelta(minutes=5)).not_valid_after(stamp + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
            key_encipherment=True, data_encipherment=False, key_agreement=False,
            key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'fixture-cert.pem', tmp_path / 'fixture-key.pem'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    original_context = ssl.create_default_context
    def trusted_fixture_context(*args, **kwargs):
        context = original_context(*args, **kwargs)
        context.load_verify_locations(cafile=str(cert_path))
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        return context
    monkeypatch.setattr(ssl, 'create_default_context', trusted_fixture_context)
    received, response_owned, release, stop = Event(), Event(), Event(), Event()
    requests, owners = [], []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        def log_message(self, *args): pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            request = json.loads(raw)
            requests.append((self.path, request))
            result = json.dumps({'translatedText': 'TLS selected derivative &amp;'}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Connection', 'close')
            # Deliberately omit Content-Length: HTTPResponse now owns the socket.
            self.end_headers()
            if cancel_body:
                self.wfile.write(result[:1])
                self.wfile.flush()
                received.set()
                release.wait(10)
                try:
                    self.wfile.write(result[1:])
                except (OSError, ssl.SSLError):
                    pass
            else:
                self.wfile.write(result)
    host = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    host.daemon_threads = True
    host.socket = server_context.wrap_socket(host.socket, server_side=True)
    serving = Thread(target=lambda: host.serve_forever(poll_interval=0.02), daemon=True)
    serving.start()
    original_response = providers._Connection.getresponse
    def observed_response(owner):
        response = original_response(owner)
        owners.append(owner)
        assert owner.connection.sock is None
        assert isinstance(owner.active_socket, ssl.SSLSocket)
        response_owned.set()
        return response
    monkeypatch.setattr(providers._Connection, 'getresponse', observed_response)
    def cancel_after_byte():
        # This case targets body cancellation after HTTPResponse takes ownership,
        # so synchronize on both actual headers and the server's first body byte.
        if received.wait(5) and response_owned.wait(5):
            stop.set()
    canceller = Thread(target=cancel_after_byte, daemon=True) if cancel_body else None
    if canceller is not None:
        canceller.start()
    try:
        p = ProviderProfile('tls_local', 'Synthetic TLS protocol fixture', 'libretranslate-local',
            'https://127.0.0.1:' + str(host.server_port), 'synthetic-protocol-only',
            ('translation.text', 'translation.languages'), local=True, enabled=True)
        gateway = Gateway(tmp_path / 'tls-ledger')
        approval, cost = translation_approval(p, 'Selected TLS text', 'en', 'uk', '0')
        def invoke():
            return gateway.request(p, 'translation.text',
                {'q': 'Selected TLS text', 'source': 'en', 'target': 'uk'},
                task_id=approval.task_id, data_class='selected-excerpt',
                authorization=approval, max_cost=cost, operation_id='tls_request',
                cancelled=stop.is_set, interrupt_cancelled=stop.is_set, timeout=8)
        started = monotonic()
        if cancel_body:
            with pytest.raises(InterruptedError):
                invoke()
            assert received.is_set() and monotonic() - started < 5
        else:
            assert invoke()['text'] == 'TLS selected derivative &amp;'
        assert requests == [('/translate', {'q': 'Selected TLS text', 'source': 'en', 'target': 'uk', 'format': 'text'})]
        assert len(owners) == 1 and not owners[0].watcher.is_alive()
        assert owners[0].active_socket.fileno() == -1
        assert gateway.ledger.get(approval.id)['operations']['tls_request']['status'] == (
            'uncertain' if cancel_body else 'complete')
    finally:
        release.set()
        if canceller is not None:
            canceller.join(5)
            assert not canceller.is_alive()
        host.shutdown()
        host.server_close()
        serving.join(5)
        assert not serving.is_alive()
