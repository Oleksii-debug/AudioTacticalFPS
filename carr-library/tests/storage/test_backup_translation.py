"""Real translation producer checkpoints using a synthetic nonnetwork gateway."""
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import subprocess
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


@pytest.fixture
def advanced_source(tmp_path, monkeypatch):
    # Reuse the real Advanced service/approval fixture. Only HTTPS is synthetic;
    # there is no live provider, socket, vault or alternate receipt implementation.
    helpers = runpy.run_path(str(Path(__file__).parents[1] / 'assistant' / 'test_translation_v3.py'))
    transport = helpers['transport'].__wrapped__(monkeypatch)
    case = helpers['source_case'](tmp_path, transport)
    ProfileStore(case.app.root / 'state/providers').save(case.p)
    identity, approval = helpers['submit_source'](case)
    return case, identity, approval, transport


@pytest.mark.parametrize('state', ['queued', 'accepted-prefix', 'complete'])
@pytest.mark.parametrize('same_path', [False, True], ids=['relocated', 'same-path-new-root'])
def test_source_bound_advanced_restore_keeps_receipts_without_remote_approval(
        advanced_source, tmp_path, monkeypatch, state, same_path):
    from carr_library.speech.reading import ReadingDocument
    case, identity, approval, transport = advanced_source
    if state == 'accepted-prefix':
        stop = []
        result = case.service.resume(identity, cancelled=lambda: bool(stop),
                                     progress=lambda *args: stop.append(True))
        assert result['status'] == 'paused' and result['completed'] == 1 < result['total']
    elif state == 'complete':
        assert case.service.resume(identity)['status'] == 'complete'
    saved = case.service.get(identity)
    original = case.app.assets.resolve(case.imported['asset']).read_bytes()
    calls = len(transport['calls'])

    def forbidden(*args, **kwargs):
        pytest.fail('Backup or restored evidence activated a provider or process.')

    monkeypatch.setattr(Gateway, '__init__', forbidden)
    monkeypatch.setattr(Gateway, 'request', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    folder = tmp_path / 'backup'
    digest = create_backup(case.app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == 4 and 'state' not in manifest
    if same_path:
        destination = case.app.root
        case.app.root.rename(tmp_path / 'retired-original-root')
    else:
        destination = tmp_path / 'relocated'
    restored = LibraryApp(restore_backup(folder, destination, digest))
    gateway = SimpleNamespace(ledger=UsageLedger(restored.root / 'state/translation/provider/usage'),
                              request=forbidden)
    service = TranslationService(restored.root / 'state/translation', library=restored, gateway=gateway)
    recovered = service.get(identity)
    assert recovered == saved
    assert recovered['kind'] == 'translation/2'
    assert recovered['source_binding'] == saved['source_binding']
    assert restored.assets.resolve(case.imported['asset']).read_bytes() == original
    usage_before = {path.name: path.read_bytes() for path in
                    (restored.root / 'state/translation/provider/usage').glob('*.usage.json')}
    if state == 'complete':
        output = tmp_path / 'offline-restored-export'
        assert service.export(identity, output)['status'] == 'complete'
        reading = ReadingDocument.load(output / 'translation.reading.json')
        assert reading.source_text == case.source.text
        assert reading.source_anchor == case.document.source_anchor
        assert not reading.source_span(0, len(reading.text)).exact
        assert reading.undo() and reading.text == case.source.text
        report = json.loads((output / 'translation-provenance.json').read_bytes())
        assert report['decoded_text_binding'] == 'usage-ledger-sha256'
        assert report['quotation_authority'] is False
    else:
        with pytest.raises(PermissionError, match='moved or restored'):
            service.resume(identity)
        assert service.get(identity)['receipts'] == saved['receipts']
        with pytest.raises(ValueError, match='Finish and verify'):
            service.export(identity, tmp_path / 'unfinished-export')
        assert not (tmp_path / 'unfinished-export').exists()
    assert {path.name: path.read_bytes() for path in
            (restored.root / 'state/translation/provider/usage').glob('*.usage.json')} == usage_before
    assert len(transport['calls']) == calls


def test_advanced_backup_rejects_text_and_self_hash_tamper_against_unchanged_usage(
        advanced_source, tmp_path):
    case, identity, approval, transport = advanced_source
    assert case.service.resume(identity)['status'] == 'complete'
    folder = tmp_path / 'backup'
    create_backup(case.app, folder)
    relative = 'state/translation/jobs/' + identity + '.json'
    path = folder / relative
    job = json.loads(path.read_bytes())
    job['receipts'][0]['text'] = 'Forged decoded text with the original raw response receipt.'
    job['receipts'][0]['translation_sha256'] = sha256(job['receipts'][0]['text'].encode()).hexdigest()
    raw = json.dumps(job, ensure_ascii=False).encode('utf-8')
    path.write_bytes(raw)
    ledger = folder / 'state/translation/provider/usage' / (approval.id + '.usage.json')
    original_usage = ledger.read_bytes()
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    row = next(row for row in manifest['sidecars'] if row['filename'] == relative)
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    destination = tmp_path / 'rejected-advanced'
    with pytest.raises(ValueError, match='decoded-output'):
        restore_backup(folder, destination, resign(folder, manifest))
    assert not destination.exists() and ledger.read_bytes() == original_usage
