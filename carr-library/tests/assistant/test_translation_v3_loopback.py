"""Real owned HTTP sockets for the Advanced protocol, never a Google request.

The transport seam maps the already-validated official HTTPS authority to this
test's 127.0.0.1 listener. It does not qualify TLS, account access or billing.
Run in the explicit loopback gate, outside the all-sockets-disabled suites.
"""
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import ssl
from threading import Thread
from time import time

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.profiles import ProviderProfile, google_oauth_credential
from carr_library.assistant.providers import Authorization, Gateway, ProviderError
from carr_library.assistant.translation import TranslationService, translation_approval
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.source import read_source_range

pytestmark = pytest.mark.requires_loopback

PARENT = 'projects/123456789/locations/us-central1'
ADDRESS = 'https://translation.googleapis.com/v3/' + PARENT
GLOSSARY = PARENT + '/glossaries/selected-terms'
TOKEN = 'SYNTHETIC_OWNED_LOOPBACK_ONLY'


@pytest.fixture
def protocol(monkeypatch):
    state = {'requests': [], 'connections': [], 'mode': 'valid'}

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            state['requests'].append((self.command, self.path, dict(self.headers), raw))
            if state['mode'] == 'redirect':
                self.send_response(302)
                self.send_header('Location', 'https://forbidden.invalid/must-not-connect')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.command == 'GET':
                result = {'languages': [{'languageCode': code, 'displayName': code,
                    'supportSource': True, 'supportTarget': True} for code in ('en', 'uk')]}
            else:
                body = json.loads(raw)
                row = {'translatedText': 'Похідний &amp; ' + body['contents'][0],
                    'model': body['model'], 'glossaryConfig': dict(body['glossaryConfig'])}
                if state['mode'] == 'wrong-glossary':
                    row['glossaryConfig']['glossary'] += '-unapproved'
                result = {'glossaryTranslations': [row]}
            encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = do_POST = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class OwnedTransport(http.client.HTTPConnection):
        def __init__(self, host, port, *, context, timeout):
            assert host == 'translation.googleapis.com' and port in (None, 443)
            assert isinstance(context, ssl.SSLContext)
            state['connections'].append((host, port))
            super().__init__('127.0.0.1', server.server_port, timeout=timeout)

    monkeypatch.setattr(http.client, 'HTTPSConnection', OwnedTransport)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


def configured(tmp_path, *, large=False):
    profile = ProviderProfile('loopback_advanced', 'Synthetic protocol only', 'google-translation-v3',
        ADDRESS, 'general/nmt', capabilities=('translation.text', 'translation.languages'),
        credential_ref='carr/provider/loopback_advanced/' + 'a' * 32, enabled=True)

    class Vault:
        calls = 0

        def get(self, identity, reference):
            assert identity == profile.id and reference == profile.credential_ref
            self.calls += 1
            return google_oauth_credential(TOKEN, time() + 600, ADDRESS)

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Owned protocol source')
    raw = 'Unchanged source Україна &amp; text. ' * (200 if large else 1)
    imported = app.add_text(workspace, 'Source', raw)
    source = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 1)
    directory = app.root / 'state' / 'translation'
    vault = Vault()
    service = TranslationService(directory, library=app,
        gateway=Gateway(directory / 'provider', vault=vault, mode='APPROVED_REMOTE'))
    approval = Authorization('owned_languages', 'owned_languages', ((profile.id, profile.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
    service.refresh_languages(profile, authorization=approval)
    authorization, cost = translation_approval(profile, source.text, 'en', 'uk', '1',
        glossary=GLOSSARY, source_bound=True)
    identity = service.submit(profile, source.text, 'en', 'uk', glossary=GLOSSARY,
        source_document=source.reading_document(), authorization=authorization, per_call_cost=cost)
    return service, profile, source, identity, authorization, vault, app.assets.resolve(imported['asset'])


def test_real_advanced_protocol_resume_and_retained_source_export(tmp_path, protocol):
    service, profile, source, identity, approval, vault, original = configured(tmp_path, large=True)
    original_bytes = original.read_bytes()
    stopped = []
    result = service.resume(identity, cancelled=lambda: bool(stopped), progress=lambda *args: stopped.append(True))
    assert result['status'] == 'paused' and result['completed'] == 1
    accepted = service.get(identity)['receipts'][0]
    before = len(protocol['requests'])
    result = TranslationService(service.directory, gateway=service.gateway, library=service.library).resume(identity)
    assert result['status'] == 'complete' and len(protocol['requests']) - before == result['total'] - 1
    assert service.get(identity)['receipts'][0] == accepted
    requests = protocol['requests']
    assert requests[0][0] == 'GET' and requests[0][1].startswith('/v3/' + PARENT + '/supportedLanguages?')
    assert [sha256(row[3]).hexdigest() for row in requests[1:]] == list(approval.request_hashes)
    for method, route, headers, raw in requests:
        assert headers['Authorization'] == 'Bearer ' + TOKEN
        assert headers['X-goog-user-project'] == '123456789' and 'X-goog-api-key' not in headers
        assert TOKEN not in route
        if method == 'POST':
            body = json.loads(raw)
            assert route == '/v3/' + PARENT + ':translateText'
            assert body['mimeType'] == 'text/plain' and body['model'] == PARENT + '/models/general/nmt'
            assert body['sourceLanguageCode'] == 'en' and body['targetLanguageCode'] == 'uk'
            assert body['glossaryConfig'] == {'glossary': GLOSSARY, 'ignoreCase': False}
    destination = tmp_path / 'derivative'
    service.export(identity, destination)
    report = json.loads((destination / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert report['decoded_text_binding'] == 'usage-ledger-sha256'
    assert report['source_anchor'] == source.reading_document().source_anchor
    assert (destination / 'original.txt').read_bytes() == source.text.encode('utf-8')
    assert original.read_bytes() == original_bytes
    assert vault.calls == len(requests) == len(protocol['connections'])
    assert all(TOKEN not in path.read_text(encoding='utf-8') for path in service.directory.rglob('*.json'))


@pytest.mark.parametrize('mode', ['wrong-glossary', 'redirect'])
def test_real_protocol_refuses_unapproved_result_or_redirect_without_retry(tmp_path, protocol, mode):
    service, profile, source, identity, approval, vault, original = configured(tmp_path)
    protocol['mode'] = mode
    with pytest.raises(ProviderError):
        service.resume(identity)
    assert service.get(identity)['receipts'] == [] and len(protocol['requests']) == 2
    usage = service.gateway.ledger.get(approval.id)
    assert next(iter(usage['operations'].values()))['status'] == 'uncertain'
    with pytest.raises(ProviderError, match='already been attempted'):
        service.resume(identity)
    assert len(protocol['requests']) == len(protocol['connections']) == vault.calls == 2


def test_real_loopback_listener_sees_nothing_when_default_mode_blocks(tmp_path, protocol):
    profile = ProviderProfile('disabled_remote', 'Default local mode', 'google-translation-v3',
        ADDRESS, 'general/nmt', capabilities=('translation.text',),
        credential_ref='carr/provider/disabled_remote/' + 'b' * 32, enabled=True)
    reads = []
    class Vault:
        def get(self, *args):
            reads.append(True)
            raise AssertionError('Local-only mode must stop before reading a secret')
    with pytest.raises(PermissionError, match='LOCAL ONLY'):
        Gateway(tmp_path / 'gateway', vault=Vault()).request(profile, 'translation.text',
            {'q': 'Never transmitted', 'target': 'uk'}, task_id='unapproved', data_class='selected-excerpt')
    assert not reads and not protocol['requests'] and not protocol['connections']
