"""Real translation producer checkpoints using a synthetic nonnetwork gateway."""
from decimal import Decimal
from copy import deepcopy
from types import SimpleNamespace
from hashlib import sha256
import json
import subprocess
import pytest
from carr_library.application import LibraryApp
from carr_library.assistant.models import canonical
from carr_library.assistant.profiles import ProviderProfile, ProfileStore, google_v3_parent
from carr_library.assistant.providers import (Authorization, Gateway, UsageLedger, _request,
    translation_receipt_sha256)
from carr_library.assistant.translation import TranslationService, ENDPOINT
from carr_library.library_backup import create_backup, restore_backup
from carr_library.ui.translation_dialog import translation_approval


class SyntheticGateway:
    def __init__(self, directory): self.ledger = UsageLedger(directory / 'usage')
    def request(self, profile, operation, payload, *, authorization, operation_id, max_cost, **kwargs):
        _, body, _ = _request(profile, operation, payload)
        checksum = sha256(canonical(body).encode()).hexdigest()
        self.ledger.reserve(authorization, operation_id, checksum, Decimal(max_cost), profile, operation)
        usage = None
        if operation == 'translation.languages':
            rows = [{'language': 'en', 'name': 'English'}, {'language': 'uk', 'name': 'Ukrainian'}]
            if profile.adapter == 'google-translation-v3':
                rows = [{**row, 'source': True, 'target': True} for row in rows]
            result = {'languages': rows}
        else:
            result = {'text': 'Synthetic translation: ' + payload['q'], 'detected_source': 'en'}
            if profile.adapter == 'google-translation-v3':
                result.update(reported_model=google_v3_parent(profile.endpoint) + '/models/' + profile.model,
                              glossary_config=body.get('glossaryConfig'))
            usage = {'submitted_characters': len(payload['q']),
                     'decoded_translation_sha256': translation_receipt_sha256(
                         result['text'], result['detected_source'])}
        output = sha256(canonical(result).encode()).hexdigest()
        self.ledger.finish(authorization.id, operation_id, status='complete', output_hash=output, usage=usage)
        return {**result, 'request_sha256': checksum, 'output_sha256': output}


def _produced(tmp_path, *, advanced=False, bound=False):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Translation backup')
    root = app.root / 'state/translation'
    gateway = SyntheticGateway(root / 'provider')
    service = TranslationService(root, gateway=gateway, library=app if bound else None)
    endpoint = ('https://translation.googleapis.com/v3/projects/123456789/locations/us-central1'
                if advanced else ENDPOINT)
    profile = ProviderProfile('google_fixture', 'Synthetic account',
        'google-translation-v3' if advanced else 'google-translation-v2', endpoint,
        'general/nmt' if advanced else 'nmt', capabilities=('translation.text', 'translation.languages'),
        credential_ref='carr/provider/google_fixture/' + 'a' * 32, enabled=True)
    ProfileStore(app.root / 'state/providers').save(profile)
    approval = Authorization('languages', 'languages', ((profile.id, profile.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, 2000000000)
    service.refresh_languages(profile, authorization=approval)
    source = 'Synthetic source for retained translation. ' * 150
    selected = None
    if bound:
        from carr_library.reader.reading_desk import ReadingDesk
        from carr_library.speech.source import read_source_range
        retained = app.add_text(workspace, 'Retained translation source', source)
        selected = read_source_range(ReadingDesk(app, workspace, retained['edition']), 1, 1).reading_document()
        source = selected.source_text
    glossary = google_v3_parent(endpoint) + '/glossaries/reviewed-terms' if advanced else None
    authorization, cost = translation_approval(profile, source, 'en', 'uk', '1',
        glossary=glossary, source_bound=bound)
    identity = service.submit(profile, source, 'en', 'uk', authorization=authorization,
        per_call_cost=cost, glossary=glossary, source_document=selected)
    stop = []
    result = service.resume(identity, cancelled=lambda: bool(stop), progress=lambda *args: stop.append(True))
    assert result['status'] == 'paused' and result['completed'] == 1
    service.jobs.control(identity, 'pause')
    return app, service, identity


@pytest.fixture
def produced(tmp_path):
    return _produced(tmp_path)


def manifest_rows(manifest):
    # Preserve the real row objects: tampering below must resign the actual
    # inventory, whether reading the canonical envelope or typed V5.
    if 'sidecars' in manifest:
        return {row['filename']: row for row in manifest['sidecars']}
    return {'state/' + row['filename']: row for row in manifest.get('state', [])}


def offline_gateway(root):
    # Loading receipts requires the retained ledger, never a credential client.
    return SimpleNamespace(ledger=UsageLedger(root / 'provider' / 'usage'))


def resign(folder, manifest):
    raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def test_translation_state_roundtrips_without_credentials_or_gateway(produced, tmp_path, monkeypatch):
    app, service, identity = produced
    def forbidden(*a, **k): pytest.fail('Passive backup activated a provider.')
    monkeypatch.setattr(Gateway, '__init__', forbidden)
    monkeypatch.setattr(Gateway, 'request', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    target = restore_backup(folder, tmp_path / 'restored', digest)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    rows = manifest_rows(manifest)
    assert len(rows) == 6
    for name in rows:
        assert (target / name).read_bytes() == (app.root / name).read_bytes()
    restored = TranslationService(target / 'state/translation',
                                  gateway=offline_gateway(target / 'state/translation'))
    job = restored._load(identity)
    assert job['status'] == 'paused' and len(job['receipts']) == 1
    assert restored.jobs.control(identity) == 'pause'


@pytest.mark.parametrize('damage', ['source', 'chunks', 'receipt-text', 'request', 'authorization',
                                   'complete', 'secret-field', 'language-digest', 'profile-digest'])
def test_resigned_translation_tampering_is_rejected(produced, tmp_path, damage):
    app, service, identity = produced
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    relative = 'state/translation/jobs/' + identity + '.json'
    if damage == 'language-digest': relative = 'state/translation/languages-google_fixture.json'
    if damage == 'profile-digest':
        relative = next(p.relative_to(folder).as_posix() for p in
            (folder / 'state/translation/provider/usage').glob('*.usage.json'))
    path = folder / relative
    value = json.loads(path.read_bytes())
    if damage == 'source': value['source_text'] += 'forged'
    elif damage == 'chunks': value['chunks'][0] += 'forged'
    elif damage == 'receipt-text': value['receipts'][0]['text'] += 'forged'
    elif damage == 'request': value['receipts'][0]['request_sha256'] = '0' * 64
    elif damage == 'authorization': value['authorization']['profiles'][0][1] = 'https://example.invalid/'
    elif damage == 'complete': value['status'] = 'complete'
    elif damage == 'secret-field': value['profile']['api_key'] = 'synthetic-secret-marker'
    elif damage == 'language-digest': value['languages'][0]['name'] += 'forged'
    else: next(iter(value['operations'].values()))['profile_sha256'] = 'not-a-digest'
    raw = json.dumps(value).encode(); path.write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    row = manifest_rows(manifest)[relative]
    row.update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError): restore_backup(folder, destination, resign(folder, manifest))
    assert not destination.exists()


@pytest.mark.parametrize('advanced,bound', [(True, False), (False, True), (True, True)],
                         ids=['advanced-pasted', 'basic-source', 'advanced-source'])
def test_current_translation_receipts_roundtrip_passively(tmp_path, monkeypatch, advanced, bound):
    app, service, identity = _produced(tmp_path, advanced=advanced, bound=bound)
    original = deepcopy(service._load(identity))
    def forbidden(*args, **kwargs):
        pytest.fail('Passive backup activated a provider, credential store or source path resolver.')
    from carr_library.assistant import models
    from carr_library.assistant.credentials import WindowsCredentialVault
    monkeypatch.setattr(Gateway, '__init__', forbidden)
    monkeypatch.setattr(Gateway, 'request', forbidden)
    monkeypatch.setattr(WindowsCredentialVault, '_native', forbidden)
    monkeypatch.setattr(models, 'local_path', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    target = restore_backup(folder, tmp_path / 'restored', checksum)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    rows = manifest_rows(manifest)
    assert f'state/translation/jobs/{identity}.json' in rows
    for name in rows:
        assert (target / name).read_bytes() == (app.root / name).read_bytes()
    restored = TranslationService(target / 'state/translation',
                                  gateway=offline_gateway(target / 'state/translation'))
    actual = restored._load(identity)
    assert actual == original
    assert actual['receipts'][0]['decoded_translation_sha256']
    assert restored.jobs.control(identity) == 'pause'
    if advanced:
        assert actual['receipts'][0]['reported_model'].endswith('/models/general/nmt')
        assert actual['receipts'][0]['glossary_config'] == {
            'glossary': actual['glossary'], 'ignoreCase': False}
    if bound:
        assert actual['kind'] == 'translation/2'
        assert actual['source_binding']['library_root'] == str(app.root)
        assert actual['source_binding']['anchor'] == original['source_binding']['anchor']


@pytest.mark.parametrize('damage', [
    'decoded', 'model', 'glossary', 'source-downgrade', 'source-root-relative',
    'language-direction', 'usage-decoded',
])
def test_resigned_current_translation_tampering_is_rejected(tmp_path, damage):
    app, service, identity = _produced(tmp_path, advanced=True, bound=True)
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    relative = f'state/translation/jobs/{identity}.json'
    if damage == 'language-direction':
        relative = 'state/translation/languages-google_fixture.json'
    elif damage == 'usage-decoded':
        job = service._load(identity)
        relative = f"state/translation/provider/usage/{job['authorization']['id']}.usage.json"
    selected = folder / relative
    value = json.loads(selected.read_bytes())
    if damage == 'decoded':
        value['receipts'][0]['decoded_translation_sha256'] = '0' * 64
    elif damage == 'model':
        value['receipts'][0]['reported_model'] = 'projects/987/locations/us-central1/models/general/nmt'
    elif damage == 'glossary':
        value['receipts'][0]['glossary_config']['ignoreCase'] = True
    elif damage == 'source-downgrade':
        value['kind'] = 'translation/1'
        value.pop('source_binding')
    elif damage == 'source-root-relative':
        value['source_binding']['library_root'] = 'relative/not-a-library'
    elif damage == 'language-direction':
        value['languages'][0]['source'] = 'yes'
        value['languages_sha256'] = sha256(canonical(value['languages']).encode()).hexdigest()
    else:
        next(iter(value['operations'].values()))['usage']['decoded_translation_sha256'] = 'not-a-digest'
    raw = json.dumps(value, ensure_ascii=False).encode()
    selected.write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest_rows(manifest)[relative].update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, resign(folder, manifest))
    assert not destination.exists()
