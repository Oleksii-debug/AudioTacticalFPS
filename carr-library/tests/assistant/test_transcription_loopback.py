"""Actual bounded loopback HTTP; explicit STT protocol, no paid service or inference.

Select only with the existing loopback firewall runner, never the common
all-socket-denied source runner. The production endpoint stays the documented
HTTPS host; only the test HTTPSConnection constructor selects this local peer.
"""
from dataclasses import replace
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import importlib.util
import json
from pathlib import Path
from threading import Thread
from time import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from carr_library.assistant.providers import Authorization, Gateway, ProviderError
from carr_library.assistant.transcription_provider import AudioUpload, OPERATION, request
from carr_library.assistant.models import canonical
from carr_library.durable_state import read_json

_spec = importlib.util.spec_from_file_location('remote_stt_loopback_fixture',
    Path(__file__).parents[1] / 'transcription' / 'remote_fixture.py')
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


@pytest.fixture
def server(monkeypatch):
    state = {'requests': [], 'status': 200, 'response': fixture.RESPONSE, 'errors': []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', '-1'))
                if not 0 < size <= 65536:
                    raise ValueError('Synthetic loopback request exceeds its closed allowance.')
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError('Synthetic loopback request ended early.')
                state['requests'].append((self.path, dict(self.headers), raw))
                body = json.dumps(state['response'], ensure_ascii=False).encode('utf-8')
                self.send_response(state['status'])
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                if state['status'] == 302:
                    self.send_header('Location', 'http://127.0.0.1:9/never-follow')
                self.end_headers()
                self.wfile.write(body)
            except BaseException as error:
                state['errors'].append(type(error).__name__)
                raise
    host = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    host.daemon_threads = True
    thread = Thread(target=lambda: host.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    original_http = http.client.HTTPConnection
    def selected_tls(hostname, port=None, **options):
        assert hostname == 'api.elevenlabs.io' and port in (None, 443)
        assert 0 < options['timeout'] <= 30
        return original_http('127.0.0.1', host.server_port, timeout=options['timeout'])
    monkeypatch.setattr(http.client, 'HTTPSConnection', selected_tls)
    monkeypatch.setenv('HTTP_PROXY', 'http://example.invalid:8080')
    monkeypatch.setenv('HTTPS_PROXY', 'http://example.invalid:8080')
    try:
        yield SimpleNamespace(state=state)
    finally:
        host.shutdown()
        host.server_close()
        thread.join(5)
        assert not thread.is_alive()
        assert not state['errors']


def prepared(tmp_path):
    selected = tmp_path / 'selected original.wav'
    selected.write_bytes(fixture.RAW)
    profile = fixture.profile()
    payload = {'audio': {'sha256': sha256(fixture.RAW).hexdigest(),
        'size_bytes': len(fixture.RAW), 'suffix': '.wav'}, 'language': 'uk'}
    route, body, _ = request(profile, payload)
    approval = Authorization(uuid4().hex, 'loopback-task', ((profile.id, profile.endpoint),),
        (OPERATION,), ('selected-audio',), '0.50', 1, time() + 60,
        (sha256(canonical(body).encode('utf-8')).hexdigest(),))
    vault = fixture.Vault()
    gateway = Gateway(tmp_path / 'gateway', vault=vault, mode='APPROVED_REMOTE')
    options = dict(task_id='loopback-task', data_class='selected-audio',
        authorization=approval, max_cost='0.50', operation_id=uuid4().hex,
        upload=AudioUpload(selected), timeout=10)
    return selected, profile, payload, approval, vault, gateway, options


def test_actual_stt_loopback_uploads_only_reviewed_bytes_and_returns_bound_usage(tmp_path, server):
    selected, profile, payload, approval, vault, gateway, options = prepared(tmp_path)
    result = gateway.request(profile, OPERATION, payload, **options)
    assert len(server.state['requests']) == 1 and len(vault.calls) == 1
    route, headers, body = server.state['requests'][0]
    assert route == '/v1/speech-to-text?enable_logging=true'
    assert headers['xi-api-key'] == 'synthetic-stt-secret'
    assert body.count(fixture.RAW) == 1
    assert b'filename="selected-audio.wav"' in body
    assert selected.name.encode('utf-8') not in body
    assert b'name="model_id"\r\n\r\nscribe_v2\r\n' in body
    assert b'name="language_code"\r\n\r\nuk\r\n' in body
    assert b'name="webhook"\r\n\r\nfalse\r\n' in body
    assert b'name="diarize"\r\n\r\nfalse\r\n' in body
    assert result['transcript']['text'] == fixture.TEXT
    assert result['local'] is False and result['actual_cost'] is None
    assert result['request_sha256'] == approval.request_hashes[0]
    ledger = read_json(tmp_path / 'gateway' / 'usage' / (approval.id + '.usage.json'))
    assert ledger['operations'][options['operation_id']]['status'] == 'complete'
    assert ledger['operations'][options['operation_id']]['usage']['submitted_audio_bytes'] == len(fixture.RAW)
    assert selected.read_bytes() == fixture.RAW


def test_actual_stt_local_only_never_reaches_loopback_or_credentials(tmp_path, server):
    selected, profile, payload, approval, vault, gateway, options = prepared(tmp_path)
    gateway.mode = 'LOCAL_ONLY'
    selected.unlink()
    with pytest.raises(PermissionError):
        gateway.request(profile, OPERATION, payload, **options)
    assert not vault.calls and not server.state['requests']
    assert not list((tmp_path / 'gateway').rglob('*.usage.json'))


@pytest.mark.parametrize('status', [302, 401, 503])
def test_actual_stt_http_refusal_never_redirects_or_retries(tmp_path, server, status):
    selected, profile, payload, approval, vault, gateway, options = prepared(tmp_path)
    server.state['status'] = status
    server.state['response'] = {'error': 'synthetic-stt-secret'}
    with pytest.raises(ProviderError) as failure:
        gateway.request(profile, OPERATION, payload, **options)
    assert 'synthetic-stt-secret' not in str(failure.value)
    with pytest.raises((ProviderError, PermissionError, ValueError)):
        gateway.request(profile, OPERATION, payload, **options)
    assert len(server.state['requests']) == 1
    ledger = read_json(tmp_path / 'gateway' / 'usage' / (approval.id + '.usage.json'))
    assert ledger['operations'][options['operation_id']]['status'] == 'uncertain'
    assert selected.read_bytes() == fixture.RAW


def test_actual_stt_loopback_rejects_unrequested_response_shape_without_resending(tmp_path, server):
    selected, profile, payload, approval, vault, gateway, options = prepared(tmp_path)
    server.state['response'] = {**fixture.RESPONSE, 'transcripts': [{'text': 'unrequested synthetic transcript'}]}
    with pytest.raises((ValueError, ProviderError)):
        gateway.request(profile, OPERATION, payload, **options)
    with pytest.raises((ProviderError, PermissionError, ValueError)):
        gateway.request(profile, OPERATION, payload, **options)
    assert len(server.state['requests']) == 1 and selected.read_bytes() == fixture.RAW
    ledger = read_json(tmp_path / 'gateway' / 'usage' / (approval.id + '.usage.json'))
    assert ledger['operations'][options['operation_id']]['status'] == 'uncertain'
