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
from threading import Thread
from time import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

import carr_library
from carr_library.application import LibraryApp
from carr_library.assistant.profiles import ProviderProfile, ProfileStore
from carr_library.assistant.providers import Gateway, LocalTranslationAuthorization, ProviderError
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
    identity = uuid4().hex
    approval = LocalTranslationAuthorization(identity, identity, ((p.id, p.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
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
