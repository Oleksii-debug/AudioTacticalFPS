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
    assert service.get(identity)['receipts'][0]['text'] == 'Контроль: ' + value


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


def cli(app, *arguments):
    # Pin the same actual source/installed package used by this test, and keep
    # -I/-B. The gate supplies the process-level loopback-only network boundary.
    package_parent = str(Path(carr_library.__file__).resolve().parent.parent)
    code = ('import sys; sys.path.insert(0,sys.argv.pop(1)); '
            'from carr_library.assistant.translation_cli import main; raise SystemExit(main())')
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, package_parent,
        '--data-dir', str(app.root), *map(str, arguments)], capture_output=True,
        text=True, encoding='utf-8', timeout=90)
    return result, json.loads(result.stdout) if result.stdout.strip() else None


def test_actual_cli_requires_local_approval_and_keeps_complete_work_offline(tmp_path, server):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('CLI local translator')
    ProfileStore(app.root / 'state/providers').save(server.profile)
    result, _ = cli(app, 'languages', '--profile', server.profile.id, '--approve-remote')
    assert result.returncode == 1 and not server.state['requests']
    result, rows = cli(app, 'languages', '--profile', server.profile.id, '--approve-local')
    assert result.returncode == 0 and rows[0]['targets'] == ['uk']
    selected = tmp_path / 'selected.txt'
    selected.write_text('First paragraph.\n\nSecond paragraph 🙂.', encoding='utf-8')
    options = ['--profile', server.profile.id, '--input', selected, '--source', 'en',
               '--target', 'uk', '--budget', '0', '--preserve-paragraphs']
    result, review = cli(app, 'preview', *options)
    assert result.returncode == 0 and review['calls'] == 2
    before = len(server.state['requests'])
    result, saved = cli(app, 'submit', *options, '--review-sha256', review['review_sha256'], '--approve-local')
    assert result.returncode == 0 and len(server.state['requests']) == before
    result, completed = cli(app, 'resume', saved['id'])
    assert result.returncode == 0 and completed['status'] == 'complete'
    ProfileStore(app.root / 'state/providers').remove(server.profile.id)
    result, exported = cli(app, 'export', saved['id'], '--output', tmp_path / 'offline')
    assert result.returncode == 0 and exported['status'] == 'complete'
    assert (tmp_path / 'offline/translation-structure.json').is_file()
    assert len(server.state['requests']) == before + 2


def test_actual_cli_retained_batch_reuses_only_existing_jobs_and_originals(tmp_path, server):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit CLI batch')
    entries = [app.add_text(workspace, 'Book ' + str(i), 'Original ' + str(i) + '\n\nRetained paragraph.')
               for i in range(2)]
    originals = [(app.assets.resolve(row['asset']), app.assets.resolve(row['asset']).read_bytes()) for row in entries]
    ProfileStore(app.root / 'state/providers').save(server.profile)
    result, _ = cli(app, 'languages', '--profile', server.profile.id, '--approve-local')
    assert result.returncode == 0
    selections = tmp_path / 'chosen.json'
    selections.write_text(json.dumps([{'workspace': workspace, 'edition': row['edition']} for row in entries]))
    options = ['--profile', server.profile.id, '--selections', selections,
               '--source', 'en', '--target', 'uk', '--budget', '0']
    result, review = cli(app, 'documents-preview', *options)
    assert result.returncode == 0 and review['calls'] == 4
    result, prepared = cli(app, 'documents-submit', *options,
        '--approve-local', '--review-sha256', review['review_sha256'])
    assert result.returncode == 0 and len(prepared['jobs']) == 2
    assert len(server.state['requests']) == 1
    result, finished = cli(app, 'documents-run', *prepared['jobs'])
    assert result.returncode == 0 and finished['status'] == 'complete'
    parent = tmp_path / 'batch-exports'
    parent.mkdir()
    result, exported = cli(app, 'documents-export', *prepared['jobs'], '--parent', parent)
    assert result.returncode == 0 and len(exported['exports']) == 2
    assert all(path.read_bytes() == raw for path, raw in originals)
    assert len(server.state['requests']) == 5
