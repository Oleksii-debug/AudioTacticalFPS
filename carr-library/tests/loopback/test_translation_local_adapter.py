"""Current local adapter through real loopback HTTP; responses are synthetic fixtures."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.models import canonical
from carr_library.assistant.translation import TranslationService, language_approval, translation_approval
from carr_library.assistant.translation_local import request, response, require_pair, validate_languages
from carr_library.assistant.translation_state import validate_translation_job
from carr_library.assistant.translation_structure import capture_text, capture_library, source_text
from carr_library.providers import (
    BudgetLedger, DirectTransport, GoogleTranslationRequest, JobApproval,
    LocalTranslationRequest, ProfileStore, ProviderGateway, ProviderProfile, ProviderError)


def profile(**changes):
    values = dict(id='local-translator', name='Synthetic local translator',
        provider='libretranslate-local', endpoint='http://127.0.0.1:55123',
        model='declared-synthetic-models', capabilities=('translation',), local=True)
    values.update(changes)
    return ProviderProfile(**values)


@pytest.fixture
def local(tmp_path, monkeypatch):
    state = SimpleNamespace(calls=[], status=200, raw=None, after=None)
    languages = [
        {'code': 'en', 'name': 'English', 'targets': ['uk']},
        {'code': 'uk', 'name': 'Ukrainian', 'targets': []}]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def send(self, value):
            raw = state.raw if state.raw is not None else json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(state.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            state.calls.append(('GET', self.path, None, dict(self.headers)))
            assert self.path == '/languages'
            self.send(languages)
        def do_POST(self):
            count = int(self.headers['Content-Length'])
            assert 0 < count <= 512 * 1024 and self.path == '/translate'
            body = json.loads(self.rfile.read(count))
            state.calls.append(('POST', self.path, body, dict(self.headers)))
            result = {'translatedText': 'DERIVED: ' + body['q'] + ' &amp;'}
            if body['source'] == 'auto':
                result['detectedLanguage'] = {'language': 'en', 'confidence': 91.5}
            self.send(result)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    original = socket.getaddrinfo
    def explicit_loopback(host, port, *args, **kwargs):
        if host != '127.0.0.1' or port != server.server_port:
            raise AssertionError('Only the selected synthetic loopback server may be contacted.')
        return original(host, port, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', explicit_loopback)
    class NoVault:
        def get(self, *args): raise AssertionError('Local translation cannot retrieve credentials.')
        def put(self, *args): raise AssertionError('Local translation cannot save credentials.')
        def delete(self, *args): raise AssertionError('Local translation cannot delete credentials.')
    app = LibraryApp(tmp_path / 'library')
    profiles = ProfileStore(app.root / 'api-connections.json', NoVault())
    selected = profiles.save(profile(endpoint=f'http://127.0.0.1:{server.server_port}'))
    service = TranslationService(app.root, profiles=profiles,
        gateway=ProviderGateway(BudgetLedger(app.root / 'api-operations.sqlite'), vault=NoVault()))
    assert not state.calls
    service.refresh_languages(selected, approval=language_approval(selected))
    yield SimpleNamespace(app=app, service=service, profile=selected, state=state)
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    assert not thread.is_alive()


def submit(local, text='First exact paragraph.\n\nSecond exact paragraph.', *, source='en', document=None):
    service, selected = local.service, local.profile
    permit = translation_approval(selected, text, source, 'uk', 0, structure=document)
    return service.submit(selected, text, source, 'uk', approval=permit, structure=document)


@pytest.mark.parametrize('changes', [
    {'endpoint': 'http://localhost:5000'}, {'endpoint': 'https://example.org'},
    {'endpoint': 'http://10.0.0.1:5000'}, {'endpoint': 'http://127.0.0.1:5000/api'},
    {'endpoint': 'http://user:secret@127.0.0.1:5000'}, {'endpoint': 'http://127.0.0.1:5000?key=x'},
    {'local': False}, {'credential_ref': 'url-api-' + 'a' * 32},
    {'capabilities': ('llm',)},
])
def test_local_profile_refuses_remote_destinations_and_credentials(changes):
    with pytest.raises(ValueError):
        profile(**changes)


def test_explicit_loopback_discovery_is_separate_and_local_jobs_keep_zero_remote_reservation(local, tmp_path):
    service, selected, state = local.service, local.profile, local.state
    assert len(state.calls) == 1 and state.calls[0][:3] == ('GET', '/languages', None)
    identity = submit(local, 'Exact original <literal> &amp; 🙂', source='auto')
    assert len(state.calls) == 1
    before = service.jobs.load(identity)
    assert before['schema'] == 'carr-translation-job/4'
    assert before['approval']['mode'] == 'LOCAL_ONLY'
    assert before['approval']['allow_loopback'] is True
    assert before['approval']['budget_micros'] == 0
    assert service.resume(identity)['status'] == 'complete'
    saved = service.jobs.load(identity)
    assert saved['receipts'][0]['text'].endswith('&amp;')
    assert saved['receipts'][0]['detected_source'] == 'en'
    _, route, body, headers = state.calls[-1]
    assert route == '/translate' and set(body) == {'q', 'source', 'target', 'format'}
    assert body['format'] == 'text'
    assert not {'authorization', 'x-goog-api-key', 'xi-api-key'}.intersection(k.lower() for k in headers)
    assert all(row['estimate_micros'] == 0 for row in service.gateway.ledger.report(identity))
    output = tmp_path / 'export'
    assert service.export(identity, output)['status'] == 'complete'
    provenance = json.loads((output / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert provenance['schema'] == 'carr-translation-export/4'
    assert provenance['local_runtime_identity'] == 'not attested by the local API'
    assert not provenance['quotation_authority']
    assert service.resume(identity)['status'] == 'complete' and len(state.calls) == 2


def test_pair_direction_glossary_price_and_credentials_are_refused_before_local_execution(local):
    service, selected, state = local.service, local.profile, local.state
    with pytest.raises(ValueError, match='direction'):
        service.preview(selected, 'Text', 'uk', 'en')
    with pytest.raises(ValueError, match='price'):
        service.preview(selected, 'Text', 'en', 'uk', rate_per_million='2')
    with pytest.raises(ValueError):
        service.preview(selected, 'Text', 'en', 'uk', glossary='unsupported')
    with pytest.raises(ValueError, match='zero'):
        translation_approval(selected, 'Text', 'en', 'uk', 1)
    with pytest.raises(ValueError, match='credentials'):
        service.profiles.save(selected, secret='never-stored')
    assert len(state.calls) == 1 and service.list() == ()


def test_local_typed_request_cannot_be_sent_with_a_google_request_or_remote_permission(local):
    service, selected = local.service, local.profile
    request = LocalTranslationRequest('Text', 'en', 'uk')
    permit = translation_approval(selected, 'Text', 'en', 'uk', 0)
    with pytest.raises(ProviderError):
        service.gateway.perform(selected, GoogleTranslationRequest('Text', 'en', 'uk'),
            approval=permit, operation_id='wrong-type', estimated_micros=0)
    for changed in (replace(permit, allow_loopback=False), replace(permit, mode='APPROVED_REMOTE'),
                    replace(permit, budget_micros=1)):
        with pytest.raises(ProviderError):
            service.gateway.perform(selected, request, approval=changed,
                                    operation_id='unapproved', estimated_micros=0)
    assert len(local.state.calls) == 1


@pytest.mark.parametrize('source', ['en', 'auto'])
@pytest.mark.parametrize('damage', ['missing', 'hash', 'direction'])
def test_resumed_local_job_checks_exact_direction_inventory_before_another_request(local, source, damage):
    service = local.service
    identity = submit(local, source=source)
    stop = Event()
    first = service.resume(identity, cancelled=stop.is_set, progress=lambda *args: stop.set())
    assert first['status'] == 'paused'
    accepted = deepcopy(service.jobs.load(identity)['receipts'])
    calls = len(local.state.calls)
    cache = service._languages_path(local.profile)
    raw = cache.read_bytes()
    if damage == 'missing':
        cache.unlink()
    else:
        changed = json.loads(raw)
        if damage == 'hash':
            changed['languages_sha256'] = '0' * 64
        else:
            changed['languages'][0]['targets'] = []
            changed['languages_sha256'] = sha256(canonical(changed['languages']).encode('utf-8')).hexdigest()
        cache.write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ValueError):
        service.resume(identity)
    assert len(local.state.calls) == calls
    assert service.jobs.load(identity)['receipts'][:len(accepted)] == accepted
    cache.write_bytes(raw)
    assert service.resume(identity)['status'] == 'complete'
    assert len(local.state.calls) == calls + 1


def test_accepted_local_response_keeps_pre_request_inventory_even_if_cache_disappears(local, monkeypatch):
    service, selected = local.service, local.profile
    identity = submit(local, 'Exact source.', source='auto')
    perform = service.gateway.perform
    def accepted(*args, **kwargs):
        raw = perform(*args, **kwargs)
        service._languages_path(selected).unlink()
        return raw
    monkeypatch.setattr(service.gateway, 'perform', accepted)
    assert service.resume(identity)['status'] == 'complete'
    assert service.jobs.load(identity)['receipts'][0]['detected_source'] == 'en'
    assert len(local.state.calls) == 2


@pytest.mark.parametrize('status', [302, 401, 500])
def test_actual_loopback_errors_never_redirect_fall_back_or_repeat_an_uncertain_attempt(local, status):
    identity = submit(local, 'Exact source.')
    local.state.status = status
    with pytest.raises(ProviderError):
        local.service.resume(identity)
    count = len(local.state.calls)
    with pytest.raises(ProviderError):
        local.service.resume(identity)
    assert len(local.state.calls) == count == 2


@pytest.mark.parametrize('raw', [
    b'{"translatedText":"one","translatedText":"two"}',
    b'{"translatedText":"ok","detectedLanguage":{"language":"en","confidence":NaN}}',
    b'{"translatedText":"ok","detectedLanguage":{"language":"en","confidence":true}}',
    b'{"translatedText":"ok"}',
    b'{"translatedText":"ok","detectedLanguage":{"language":"en","confidence":101}}',
    b'{"translatedText":"ok","detectedLanguage":{"language":"../en","confidence":90}}',
])
def test_local_detection_requires_one_finite_bounded_receipt(raw):
    with pytest.raises(ValueError):
        response(raw, 'translation.text', {'source': 'auto'}, maximum=1000)


def test_local_structured_job_cannot_drop_its_immutable_source_context(local):
    workspace = local.app.create_workspace('Current source')
    book = local.app.add_text(workspace, 'Original title', 'Exact source.')
    document = capture_library(local.app, [{'workspace': workspace, 'edition': book['edition'],
        'kind': 'document', 'first': 1, 'last': None}])
    identity = submit(local, source_text(document), document=document)
    changed = deepcopy(local.service.jobs.load(identity))
    changed['structure'] = None
    with pytest.raises(ValueError):
        validate_translation_job(changed)
    local.app.archive_workspace(workspace)
    with pytest.raises(ValueError):
        local.service.resume(identity)
    assert len(local.state.calls) == 1


def test_complete_local_translation_backup_restores_derivatives_without_endpoint_authority(local, tmp_path):
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    workspace = local.app.create_workspace('Local translation source')
    book = local.app.add_text(workspace, 'Original', '# Exact heading\n\nSource text.')
    document = capture_library(local.app, [{'workspace': workspace, 'edition': book['edition'],
        'kind': 'document', 'first': 1, 'last': None}], markdown=True)
    value = source_text(document)
    completed = submit(local, value, document=document)
    assert local.service.resume(completed)['status'] == 'complete'
    queued = submit(local, value, document=document)
    before = {identity: local.service.jobs.path(identity).read_bytes() for identity in (completed, queued)}
    backup = tmp_path / 'snapshot'
    pin = create_backup(local.app, backup)
    verify_backup(backup, pin)
    restored_root = tmp_path / 'restored'
    restore_backup(backup, restored_root, pin)
    for identity, raw in before.items():
        assert (restored_root / 'state/translation' / (identity + '.json')).read_bytes() == raw
    assert not (restored_root / 'api-connections.json').exists()
    assert not (restored_root / 'api-operations.sqlite').exists()
    restored = TranslationService(restored_root)
    calls = len(local.state.calls)
    output = tmp_path / 'restored-export'
    assert restored.export(completed, output)['status'] == 'complete'
    with pytest.raises(PermissionError, match='moved/restored'):
        restored.resume(queued)
    assert len(local.state.calls) == calls
    repacked = tmp_path / 'rebackup'
    verify_backup(repacked, create_backup(LibraryApp(restored_root), repacked))


def test_actual_cli_local_review_queue_run_and_offline_export(local, tmp_path, capsys):
    from carr_library import translation_cli
    source = tmp_path / 'source.md'
    source.write_text('# Title\n\nSource paragraph.', encoding='utf-8')
    common = ['--profile', local.profile.id, '--input', str(source), '--markdown',
              '--source', 'en', '--target', 'uk', '--budget-micros', '0']
    def invoke(*args):
        code = translation_cli.main(['--data-dir', str(local.app.root), *args],
                                    service_factory=lambda root: local.service)
        return code, json.loads(capsys.readouterr().out)
    code, preview = invoke('preview', *common)
    assert code == 0 and preview['estimated_cost'] == '0' and not preview['network_performed']
    code, queued = invoke('queue', *common, '--approve-profile', local.profile.fingerprint,
                         '--approve-preview', preview['review_sha256'], '--approve-transfer')
    assert code == 0 and len(local.state.calls) == 1
    code, completed = invoke('run', '--job', queued['id'], '--allow-network',
                             '--approve-job-sha256', queued['job_sha256'])
    assert code == 0 and completed['status'] == 'complete'
    code, exported = invoke('export', '--job', queued['id'], '--destination', str(tmp_path / 'cli-export'))
    assert code == 0 and exported['status'] == 'complete'
