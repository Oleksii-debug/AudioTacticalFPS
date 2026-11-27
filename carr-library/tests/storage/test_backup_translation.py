"""Real translation producer checkpoints using a synthetic nonnetwork gateway."""
from decimal import Decimal
from hashlib import sha256
import json
import subprocess
from types import SimpleNamespace
import pytest
from carr_library.application import LibraryApp
from carr_library.assistant.models import canonical
from carr_library.assistant.profiles import ProviderProfile, ProfileStore
from carr_library.assistant.providers import Authorization, Gateway, UsageLedger, _request
from carr_library.assistant.translation import TranslationService, ENDPOINT
from carr_library.library_backup import create_backup, restore_backup
from carr_library.ui.translation_dialog import translation_approval


class SyntheticGateway:
    def __init__(self, directory): self.ledger = UsageLedger(directory / 'usage')
    def request(self, profile, operation, payload, *, authorization, operation_id, max_cost, **kwargs):
        _, body, _ = _request(profile, operation, payload)
        checksum = sha256(canonical(body).encode()).hexdigest()
        self.ledger.reserve(authorization, operation_id, checksum, Decimal(max_cost), profile, operation)
        if operation == 'translation.languages':
            result = {'languages': [{'language': 'en', 'name': 'English'}, {'language': 'uk', 'name': 'Ukrainian'}]}
        else:
            result = {'text': 'Synthetic translation: ' + payload['q'], 'detected_source': 'en'}
        output = sha256(canonical(result).encode()).hexdigest()
        self.ledger.finish(authorization.id, operation_id, status='complete', output_hash=output)
        return {**result, 'request_sha256': checksum, 'output_sha256': output}


@pytest.fixture
def produced(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Translation backup')
    root = app.root / 'state/translation'
    gateway = SyntheticGateway(root / 'provider')
    service = TranslationService(root, gateway=gateway)
    profile = ProviderProfile('google_fixture', 'Synthetic account', 'google-translation-v2', ENDPOINT,
        'nmt', capabilities=('translation.text', 'translation.languages'),
        credential_ref='carr/provider/google_fixture/' + 'a' * 32, enabled=True)
    ProfileStore(app.root / 'state/providers').save(profile)
    approval = Authorization('languages', 'languages', ((profile.id, profile.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, 2000000000)
    service.refresh_languages(profile, authorization=approval)
    source = 'Synthetic source for retained translation. ' * 150
    authorization, cost = translation_approval(profile, source, 'en', 'uk', '1')
    identity = service.submit(profile, source, 'en', 'uk', authorization=authorization, per_call_cost=cost)
    stop = []
    result = service.resume(identity, cancelled=lambda: bool(stop), progress=lambda *args: stop.append(True))
    assert result['status'] == 'paused' and result['completed'] == 1
    service.jobs.control(identity, 'pause')
    return app, service, identity


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
    assert len(manifest['sidecars']) == 6
    for row in manifest['sidecars']:
        assert (target / row['filename']).read_bytes() == (app.root / row['filename']).read_bytes()
    # Accepted chunks still require their actual restored local usage evidence;
    # reading that ledger grants no provider connection or fresh transfer consent.
    passive = SimpleNamespace(ledger=UsageLedger(target / 'state/translation/provider/usage'),
                              request=forbidden)
    restored = TranslationService(target / 'state/translation', gateway=passive)
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
    row = next(row for row in manifest['sidecars'] if row['filename'] == relative)
    row.update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError): restore_backup(folder, destination, resign(folder, manifest))
    assert not destination.exists()
