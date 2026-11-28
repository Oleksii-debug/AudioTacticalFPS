"""Advanced gateway composition with real retained sources and durable receipts.

Only HTTPS and the credential vault are synthetic. Library reads, approval,
chunking, usage records, checkpoints, cancellation and exports are real.
"""
from hashlib import sha256
import http.client
import json
from time import time
from types import SimpleNamespace
from uuid import UUID

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.profiles import ProviderProfile, google_oauth_credential
from carr_library.assistant.providers import Authorization, Gateway
from carr_library.assistant.state import read_json
from carr_library.assistant.translation import TranslationService, translation_approval
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.source import read_source_range


PARENT = 'projects/123456789/locations/us-central1'
GLOSSARY = PARENT + '/glossaries/selected-terms'


@pytest.fixture
def advanced_source(tmp_path, monkeypatch):
    calls = []

    class Connection:
        def __init__(self, host, port, **kwargs):
            assert host == 'translation.googleapis.com' and port in (None, 443)
            self.position = 0

        def request(self, method, route, body, headers):
            calls.append((method, route, body))
            assert headers['Authorization'] == 'Bearer SYNTHETIC_SOURCE_TOKEN'
            if method == 'GET':
                data = {'languages': [
                    {'languageCode': code, 'displayName': code,
                     'supportSource': True, 'supportTarget': True} for code in ('en', 'uk')]}
            else:
                payload = json.loads(body)
                row = {'translatedText': 'Переклад &amp; ' + payload['contents'][0],
                       'model': payload['model']}
                if 'glossaryConfig' in payload:
                    row['glossaryConfig'] = payload['glossaryConfig']
                data = {('glossaryTranslations' if 'glossaryConfig' in payload else 'translations'): [row]}
            self.raw = json.dumps(data, ensure_ascii=False).encode('utf-8')

        def getresponse(self):
            self.status = 200
            return self

        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)

        def read(self, size):
            data = self.raw[self.position:self.position + size]
            self.position += len(data)
            return data

        def close(self):
            pass

    monkeypatch.setattr(http.client, 'HTTPSConnection', Connection)
    profile = ProviderProfile(id='advanced_source', name='Synthetic source account',
        adapter='google-translation-v3', endpoint='https://translation.googleapis.com/v3/' + PARENT,
        model='general/nmt', capabilities=('translation.text', 'translation.languages'),
        credential_ref='carr/provider/advanced_source/' + 'a' * 32, enabled=True)

    class Vault:
        revoked = False

        def get(self, identity, reference):
            assert not self.revoked, 'Completed export must not read credentials.'
            assert (identity, reference) == (profile.id, profile.credential_ref)
            return google_oauth_credential('SYNTHETIC_SOURCE_TOKEN', time() + 3500, profile.endpoint)

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Advanced source')
    topic = app.create_topic(workspace, 'Reviewed selection')
    raw = 'Retained first section.\f' + 'Selected український e\u0301 текст. ' * 220 + '\fFinal retained section.'
    imported = app.add_text(workspace, 'Immutable source book', raw)
    source = read_source_range(ReadingDesk(app, workspace, imported['edition'], topic=topic), 2, 3)
    vault = Vault()
    directory = app.root / 'state' / 'translation'
    service = TranslationService(directory, library=app,
        gateway=Gateway(directory / 'provider', vault=vault, mode='APPROVED_REMOTE'))
    authorization = Authorization('advanced_languages', 'advanced_languages',
        ((profile.id, profile.endpoint),), ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
    service.refresh_languages(profile, authorization=authorization)
    return SimpleNamespace(app=app, workspace=workspace, topic=topic, imported=imported, raw=raw,
        source=source, document=source.reading_document(), service=service,
        profile=profile, vault=vault, calls=calls)


def submit(case, glossary):
    approval, cost = translation_approval(case.profile, case.source.text, 'en', 'uk', '2',
                                         source_bound=True, glossary=glossary)
    identity = case.service.submit(case.profile, case.source.text, 'en', 'uk', title=case.source.title,
        source_document=case.document, topic=case.topic, glossary=glossary,
        authorization=approval, per_call_cost=cost)
    return identity, approval


@pytest.mark.parametrize('glossary', [None, GLOSSARY], ids=['nmt', 'glossary'])
def test_advanced_retained_range_resumes_and_exports_offline_without_losing_source(advanced_source, tmp_path, glossary):
    case = advanced_source
    original = case.app.assets.resolve(case.imported['asset'])
    before = original.read_bytes()
    identity, approval = submit(case, glossary)
    assert UUID(identity).version == 5
    paused = []
    result = case.service.resume(identity, cancelled=lambda: bool(paused), progress=lambda *args: paused.append(True))
    assert result['status'] == 'paused' and result['completed'] == 1 and result['total'] > 1
    first = case.service.jobs.load(identity)['receipts'][0]
    assert len(case.calls) == 2
    assert case.service.resume(identity)['status'] == 'complete'
    job = case.service.get(identity)
    assert job['receipts'][0] == first and len(case.calls) == 1 + len(job['chunks'])
    usage = case.service.gateway.ledger.get(approval.id)['operations']
    for index, receipt in enumerate(job['receipts']):
        assert receipt['reported_model'] == PARENT + '/models/general/nmt'
        assert receipt['glossary_config'] == ({'glossary': glossary, 'ignoreCase': False} if glossary else None)
        assert usage[identity + '_' + str(index)]['usage']['translation_sha256'] == sha256(receipt['text'].encode('utf-8')).hexdigest()
    case.vault.revoked = True
    calls = len(case.calls)
    # A reopened local-only gateway can read the durable evidence and export.
    reopened = TranslationService(case.service.directory, library=case.app)
    output = tmp_path / 'advanced-export'
    assert reopened.export(identity, output)['status'] == 'complete'
    assert len(case.calls) == calls and original.read_bytes() == before == case.raw.encode('utf-8')
    assert (output / 'original.txt').read_bytes() == case.source.text.encode('utf-8')
    reading = ReadingDocument.load(output / 'translation.reading.json')
    assert reading.source_anchor == case.document.source_anchor and reading.source_text == case.source.text
    assert '&amp;' in reading.text and not reading.source_span(0, len(reading.text)).exact
    assert reading.undo() and reading.text == case.source.text
    report = read_json(output / 'translation-provenance.json')
    assert report['glossary'] == glossary and report['provider_endpoint'] == case.profile.endpoint
    assert report['decoded_text_binding'] == 'usage-ledger-sha256' and report['quotation_authority'] is False
    assert [row['ordinal'] for row in report['source_anchor']['positions']] == [2, 3]


@pytest.mark.parametrize('glossary', [None, GLOSSARY], ids=['nmt', 'glossary'])
def test_advanced_source_rejects_rewritten_text_even_with_a_matching_self_hash(advanced_source, tmp_path, glossary):
    case = advanced_source
    identity, _ = submit(case, glossary)
    assert case.service.resume(identity)['status'] == 'complete'
    job = case.service.jobs.load(identity)
    job['receipts'][0]['text'] = 'An unaccepted replacement.'
    job['receipts'][0]['translation_sha256'] = sha256(job['receipts'][0]['text'].encode('utf-8')).hexdigest()
    case.service.jobs.save(job)
    calls = len(case.calls)
    with pytest.raises(ValueError, match='decoded-output'):
        case.service.export(identity, tmp_path / 'must-not-exist')
    assert not (tmp_path / 'must-not-exist').exists() and len(case.calls) == calls


def test_source_glossary_change_requires_new_approval_before_job_publication(advanced_source):
    case = advanced_source
    approval, cost = translation_approval(case.profile, case.source.text, 'en', 'uk', '2',
                                         source_bound=True, glossary=GLOSSARY)
    with pytest.raises(PermissionError, match='Approval does not match'):
        case.service.submit(case.profile, case.source.text, 'en', 'uk', source_document=case.document,
            topic=case.topic, glossary=GLOSSARY + '-other', authorization=approval, per_call_cost=cost)
    assert case.service.jobs.identities() == () and len(case.calls) == 1


@pytest.mark.parametrize('change', ['workspace', 'topic', 'revision', 'original'])
def test_advanced_source_revocation_retains_paid_prefix_and_prevents_next_call(advanced_source, change):
    case = advanced_source
    identity, _ = submit(case, GLOSSARY)

    def revoke(done, total, phase):
        assert done == 1 and total > 1
        if change == 'workspace':
            case.app.archive_workspace(case.workspace)
        elif change == 'topic':
            with case.app.topic_transaction(case.workspace) as tree:
                tree.archive(case.topic)
        elif change == 'revision':
            case.app.update_metadata(case.workspace, case.imported['edition'], title='Changed revision')
        else:
            path = case.app.assets.resolve(case.imported['asset'])
            path.write_bytes(b'X' + path.read_bytes()[1:])

    with pytest.raises((ValueError, PermissionError)):
        case.service.resume(identity, progress=revoke)
    before = case.service.jobs.load(identity)['receipts']
    assert len(before) == 1 and len(case.calls) == 2
    with pytest.raises((ValueError, PermissionError)):
        case.service.resume(identity)
    assert case.service.jobs.load(identity)['receipts'] == before and len(case.calls) == 2
