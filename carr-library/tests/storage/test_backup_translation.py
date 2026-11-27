"""Real translation producer checkpoints using a synthetic nonnetwork gateway."""
from decimal import Decimal
from hashlib import sha256
import json
import subprocess
from types import SimpleNamespace
import pytest
from carr_library.application import LibraryApp
from carr_library.assistant.models import canonical
from carr_library.assistant.profiles import ProviderProfile, ProfileStore, google_v3_parent
from carr_library.assistant.providers import (Authorization, Gateway, UsageLedger, _request,
    translation_receipt_sha256)
from carr_library.assistant.translation import TranslationService, ENDPOINT
from carr_library.library_backup import create_backup, restore_backup
from carr_library.assistant.translation import translation_approval
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.source import read_source_range


class SyntheticGateway:
    def __init__(self, directory): self.ledger = UsageLedger(directory / 'usage')
    def request(self, profile, operation, payload, *, authorization, operation_id, max_cost, **kwargs):
        _, body, _ = _request(profile, operation, payload)
        checksum = sha256(canonical(body).encode()).hexdigest()
        self.ledger.reserve(authorization, operation_id, checksum, Decimal(max_cost), profile, operation)
        advanced = profile.adapter == 'google-translation-v3'
        if operation == 'translation.languages':
            result = {'languages': [{'language': 'en', 'name': 'English'}, {'language': 'uk', 'name': 'Ukrainian'}]}
            if advanced:
                for row in result['languages']:
                    row.update(source=True, target=True)
            usage = None
        else:
            result = {'text': 'Synthetic translation: ' + payload['q'], 'detected_source': 'en'}
            if advanced:
                result.update(reported_model=google_v3_parent(profile.endpoint) + '/models/' + profile.model,
                    glossary_config={'glossary': payload['glossary'], 'ignoreCase': False} if payload.get('glossary') else None)
            usage = {'decoded_translation_sha256': translation_receipt_sha256(result['text'], result['detected_source'])}
        output = sha256(canonical(result).encode()).hexdigest()
        self.ledger.finish(authorization.id, operation_id, status='complete', output_hash=output, usage=usage)
        return {**result, 'request_sha256': checksum, 'output_sha256': output}


@pytest.fixture(params=['basic', 'advanced', 'advanced-glossary'])
def produced(tmp_path, request):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Translation backup')
    root = app.root / 'state/translation'
    gateway = SyntheticGateway(root / 'provider')
    service = TranslationService(root, gateway=gateway)
    advanced = request.param != 'basic'
    parent = 'projects/123456789/locations/us-central1'
    glossary = parent + '/glossaries/reviewed-terms' if request.param == 'advanced-glossary' else None
    profile = ProviderProfile('google_fixture', 'Synthetic account',
        'google-translation-v3' if advanced else 'google-translation-v2',
        'https://translation.googleapis.com/v3/' + parent if advanced else ENDPOINT,
        'general/nmt' if advanced else 'nmt', capabilities=('translation.text', 'translation.languages'),
        credential_ref='carr/provider/google_fixture/' + 'a' * 32, enabled=True)
    ProfileStore(app.root / 'state/providers').save(profile)
    approval = Authorization('languages', 'languages', ((profile.id, profile.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, 2000000000)
    service.refresh_languages(profile, authorization=approval)
    source = 'Synthetic source for retained translation. ' * 150
    authorization, cost = translation_approval(profile, source, 'en', 'uk', '1', glossary=glossary)
    identity = service.submit(profile, source, 'en', 'uk', authorization=authorization,
                              per_call_cost=cost, glossary=glossary)
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
    restored = TranslationService(target / 'state/translation', gateway=SimpleNamespace(
        ledger=UsageLedger(target / 'state/translation/provider/usage')))
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


def completed_case(tmp_path, *, advanced=False, bound=False):
    app = LibraryApp(tmp_path / 'source-library')
    workspace = app.create_workspace('Exact source and passive provider evidence')
    source = 'Original retained text. Україна e\u0301. '
    imported = app.add_text(workspace, 'Original', source)
    selection = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 1)
    parent = 'projects/123456789/locations/us-central1'
    endpoint = 'https://translation.googleapis.com/v3/' + parent if advanced else ENDPOINT
    profile = ProviderProfile('passive_fixture', 'Synthetic reviewed account',
        'google-translation-v3' if advanced else 'google-translation-v2', endpoint,
        'general/nmt' if advanced else 'nmt', capabilities=('translation.text', 'translation.languages'),
        credential_ref='carr/provider/passive_fixture/' + 'a' * 32, enabled=True)
    directory = app.root / 'state/translation'
    service = TranslationService(directory, gateway=SyntheticGateway(directory / 'provider'), library=app)
    languages = Authorization('languages', 'languages', ((profile.id, profile.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, 2000000000)
    service.refresh_languages(profile, authorization=languages)
    glossary = parent + '/glossaries/reviewed-terms' if advanced else None
    approval, cost = translation_approval(profile, selection.text, 'en', 'uk', '1',
        glossary=glossary, source_bound=bound)
    identity = service.submit(profile, selection.text, 'en', 'uk', title=selection.title,
        source_document=selection.reading_document() if bound else None,
        glossary=glossary, authorization=approval, per_call_cost=cost)
    assert service.resume(identity)['status'] == 'complete'
    return app, service, identity


@pytest.mark.parametrize('advanced,bound', [(False, False), (True, False), (False, True), (True, True)],
                         ids=['basic', 'advanced-glossary', 'basic-source', 'advanced-source-glossary'])
def test_current_completed_receipts_and_expired_approval_restore_without_activation(
        tmp_path, monkeypatch, advanced, bound):
    app, service, identity = completed_case(tmp_path, advanced=advanced, bound=bound)
    before = service.get(identity)
    def forbidden(*args, **kwargs):
        pytest.fail('Passive backup or receipt recovery activated a provider.')
    monkeypatch.setattr(Gateway, '__init__', forbidden)
    monkeypatch.setattr(Gateway, 'request', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr('carr_library.assistant.providers.time', lambda: 4000000000)
    assert before['authorization']['expires_at'] < 4000000000
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    original = {path.relative_to(backup): path.read_bytes() for path in backup.rglob('*') if path.is_file()}
    restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = TranslationService(restored_root / 'state/translation', library=LibraryApp(restored_root),
        gateway=SimpleNamespace(ledger=UsageLedger(restored_root / 'state/translation/provider/usage')))
    assert restored.get(identity) == before
    assert {path.relative_to(backup): path.read_bytes() for path in backup.rglob('*') if path.is_file()} == original
    if bound:
        with pytest.raises(PermissionError, match='moved or restored'):
            restored._source_context(restored.get(identity), remote=True)


def test_recomputed_translation_self_hash_cannot_replace_restored_usage_evidence(tmp_path, monkeypatch):
    app, service, identity = completed_case(tmp_path, advanced=True, bound=True)
    value = service.jobs.load(identity)
    receipt = value['receipts'][0]
    receipt['text'] += ' Forged replacement.'
    receipt['translation_sha256'] = sha256(receipt['text'].encode()).hexdigest()
    receipt['decoded_translation_sha256'] = translation_receipt_sha256(receipt['text'], receipt['detected_source'])
    service.jobs.save(value)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)  # A backup preserves bytes; it grants no output authority.
    restored_root = restore_backup(backup, tmp_path / 'restored', digest)
    restored = TranslationService(restored_root / 'state/translation', library=LibraryApp(restored_root),
        gateway=SimpleNamespace(ledger=UsageLedger(restored_root / 'state/translation/provider/usage')))
    def forbidden(*args, **kwargs):
        pytest.fail('Checking a damaged saved receipt made a provider call.')
    monkeypatch.setattr(Gateway, '__init__', forbidden)
    monkeypatch.setattr(Gateway, 'request', forbidden)
    with pytest.raises(ValueError, match='usage evidence'):
        restored.get(identity)
    destination = tmp_path / 'forged-export'
    with pytest.raises(ValueError, match='usage evidence'):
        restored.export(identity, destination)
    assert not destination.exists()


def test_legacy_basic_wire_receipt_remains_explicitly_legacy_after_backup(tmp_path):
    app, service, identity = completed_case(tmp_path)
    job = service.jobs.load(identity)
    job.pop('glossary')
    for row in job['receipts']:
        row.pop('decoded_translation_sha256')
    service.jobs.save(job)
    ledger_path = service.gateway.ledger.directory / (job['authorization']['id'] + '.usage.json')
    ledger = json.loads(ledger_path.read_bytes())
    for record in ledger['operations'].values():
        record['usage'].pop('decoded_translation_sha256')
    ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
    backup = tmp_path / 'legacy-basic'
    checksum = create_backup(app, backup)
    restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = TranslationService(restored_root / 'state/translation', gateway=SimpleNamespace(
        ledger=UsageLedger(restored_root / 'state/translation/provider/usage')))
    integrity = []
    assert restored._load(identity, receipt_integrity=integrity) == job
    assert integrity == ['legacy-wire-only']


@pytest.mark.parametrize('produced', ['basic'], indirect=True)
def test_legacy_basic_checkpoint_without_glossary_field_still_roundtrips(produced, tmp_path):
    app, service, identity = produced
    job = service.jobs.load(identity)
    assert job.pop('glossary') is None
    service.jobs.save(job)
    folder = tmp_path / 'legacy-backup'
    checksum = create_backup(app, folder)
    target = restore_backup(folder, tmp_path / 'legacy-restored', checksum)
    restored = TranslationService(target / 'state/translation', gateway=SimpleNamespace(
        ledger=UsageLedger(target / 'state/translation/provider/usage')))._load(identity)
    assert 'glossary' not in restored and len(restored['receipts']) == 1


@pytest.mark.parametrize('produced', ['advanced', 'advanced-glossary'], indirect=True)
@pytest.mark.parametrize('damage', ['glossary-resource', 'receipt-model', 'receipt-glossary',
                                   'language-direction', 'language-endpoint', 'language-model'])
def test_resigned_advanced_translation_authority_changes_are_rejected(produced, tmp_path, damage):
    app, service, identity = produced
    folder = tmp_path / 'advanced-backup'
    create_backup(app, folder)
    relative = ('state/translation/languages-google_fixture.json' if damage.startswith('language-')
                else 'state/translation/jobs/' + identity + '.json')
    path = folder / relative
    value = json.loads(path.read_bytes())
    if damage == 'glossary-resource':
        value['glossary'] = 'projects/999999999/locations/us-central1/glossaries/other-account'
    elif damage == 'receipt-model':
        value['receipts'][0]['reported_model'] = 'projects/999999999/locations/us-central1/models/general/nmt'
    elif damage == 'receipt-glossary':
        value['receipts'][0]['glossary_config'] = {
            'glossary': value.get('glossary') or 'projects/123456789/locations/us-central1/glossaries/unrequested',
            'ignoreCase': 0,  # Equality with False must not admit a mistyped provider declaration.
        }
    elif damage == 'language-direction':
        value['languages'][0]['source'] = 'true'
        value['languages_sha256'] = sha256(canonical(value['languages']).encode()).hexdigest()
    elif damage == 'language-endpoint':
        value['endpoint'] = 'https://example.invalid/v3/projects/123456789/locations/us-central1'
    else:
        value['model'] = 'general/translation-llm'
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    row = next(row for row in manifest['sidecars'] if row['filename'] == relative)
    row.update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    destination = tmp_path / 'rejected-advanced'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, resign(folder, manifest))
