"""Real translation/ledger snapshots with synthetic public text and no network."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import socket
from threading import Event

import pytest

from carr_library import library_backup, providers
from carr_library.application import LibraryApp
from carr_library.assistant import translation_state
from carr_library.assistant.translation import TranslationService, language_approval, translation_approval
from carr_library.assistant.translation_state import validate_state_document
from carr_library.library_backup import create_backup, restore_backup
from carr_library.providers import BudgetLedger, ProfileStore, ProviderGateway, ProviderProfile, GOOGLE_TRANSLATION_ENDPOINT


SECRET = 'synthetic-backup-translation-key-never-real'


def forbidden(*args, **kwargs):
    raise AssertionError('Unexpected network or credential access during backup/restore')


class Vault:
    def __init__(self):
        self.values = {}
        self.on_get = lambda: None

    def put(self, reference, binding, secret):
        self.values[reference, binding] = secret

    def get(self, reference, binding):
        self.on_get()
        return self.values[reference, binding]

    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


class Transport:
    def __init__(self):
        self.calls = []

    def get(self, profile, route, headers, **kwargs):
        self.calls.append('languages')
        return json.dumps({'data': {'languages': [
            {'language': 'en', 'name': 'English'}, {'language': 'uk', 'name': 'Ukrainian'}]}}).encode()

    def post(self, profile, route, body, headers, **kwargs):
        self.calls.append('text')
        return json.dumps({'data': {'translations': [{
            'translatedText': 'Синтетичний переклад: ' + json.loads(body)['q'], 'model': 'nmt'}]}}).encode()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic translation backup')
    selected_text = 'Public synthetic original. Український текст.\r\n' * 220
    original = tmp_path / 'source.txt'
    original.write_bytes(selected_text.encode('utf-8'))
    result = app.import_files(workspace, [original])
    assert len(result) == 1 and result[0]['state'] == 'complete'
    vault, transport = Vault(), Transport()
    profiles = ProfileStore(app.root / 'api-connections.json', vault)
    profile = profiles.save(ProviderProfile(
        'translation-backup', 'Synthetic translation account', 'google-translation-v2',
        GOOGLE_TRANSLATION_ENDPOINT, 'nmt', ('translation',)), secret=SECRET)
    ledger = BudgetLedger(app.root / 'api-operations.sqlite')
    service = TranslationService(app.root, profiles=profiles,
        gateway=ProviderGateway(ledger, vault=vault, transport=transport))
    service.refresh_languages(profile, approval=language_approval(profile))
    permit = translation_approval(profile, selected_text, 'en', 'uk', 100)
    identity = service.submit(profile, selected_text, 'en', 'uk', approval=permit)
    return app, service, profile, identity, original, vault, transport


def tree_bytes(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def tree_directories(root):
    return tuple(sorted(path.relative_to(root).as_posix()
                        for path in root.rglob('*') if path.is_dir()))


@pytest.mark.parametrize('state', ['queued', 'paused', 'complete'])
def test_real_backup_restores_translation_history_but_never_rebinds_permission(
        prepared, tmp_path, monkeypatch, state):
    app, service, profile, identity, original, vault, transport = prepared
    if state == 'paused':
        stopped = Event()
        result = service.resume(identity, cancelled=stopped.is_set, progress=lambda *args: stopped.set())
        assert result['status'] == 'paused' and result['completed'] == 1
    elif state == 'complete':
        assert service.resume(identity)['status'] == 'complete'
    checkpoint = service.jobs.path(identity).read_bytes()
    cache = service.jobs.cache_path(profile).read_bytes()
    charge_history = service.gateway.ledger.report(identity)
    original_bytes = original.read_bytes()
    retained = tree_bytes(app.root / 'originals')
    requests = list(transport.calls)
    monkeypatch.setattr(vault, 'get', forbidden)
    monkeypatch.setattr(providers.DirectTransport, 'post', forbidden)
    monkeypatch.setattr(providers.DirectTransport, 'get', forbidden)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == 5 and manifest['provider_restore_mode'] == 'LOCAL_ONLY'
    names = {entry['filename'] for entry in manifest['sidecars']}
    assert 'state/translation/' + identity + '.json' in names
    assert 'state/translation/languages-' + profile.id + '.json' in names
    assert (folder / 'state/translation' / (identity + '.json')).read_bytes() == checkpoint
    assert (folder / 'state/translation' / ('languages-' + profile.id + '.json')).read_bytes() == cache
    assert SECRET.encode() not in b''.join(tree_bytes(folder).values())
    assert b'url-api-' not in (folder / 'api-connections.json').read_bytes()
    before, directories = tree_bytes(folder), tree_directories(folder)

    # Restore must validate a published archive, never open its live locks.
    with monkeypatch.context() as patch:
        patch.setattr(library_backup, 'translation_snapshot_guard', forbidden)
        patch.setattr(translation_state, 'state_write_guard', forbidden)
        patch.setattr(translation_state, 'snapshot_job_guard', forbidden)
        restored_root = restore_backup(folder, tmp_path / 'restored', digest)

    assert tree_bytes(folder) == before and tree_directories(folder) == directories
    assert not tuple(folder.rglob('*.lock'))
    restored = TranslationService(restored_root)
    assert restored.jobs.path(identity).read_bytes() == checkpoint
    assert all(item.credential_ref is None for item in restored.available_profiles())
    assert restored.gateway.ledger.report(identity) == charge_history
    assert validate_state_document(identity + '.json', checkpoint)['status'] == state
    if state == 'complete':
        output = tmp_path / 'restored-derivative'
        restored.export(identity, output)
        assert (output / 'original.txt').read_bytes() == original_bytes
        report = json.loads((output / 'translation-provenance.json').read_bytes())
        assert report['quotation_authority'] is False
    else:
        with pytest.raises(PermissionError, match='moved/restored'):
            restored.resume(identity)
    assert transport.calls == requests
    assert original.read_bytes() == original_bytes
    assert tree_bytes(app.root / 'originals') == retained


def test_known_unsent_charge_state_passes_the_existing_strict_ledger_backup(
        prepared, tmp_path):
    app, service, _, identity, _, vault, transport = prepared
    cancelled = Event()
    vault.on_get = cancelled.set
    result = service.resume(identity, cancelled=cancelled.is_set)
    assert result['status'] == 'paused'
    before = service.gateway.ledger.report(identity)
    assert len(before) == 1 and before[0]['state'] == 'cancelled_before_send'
    assert transport.calls == ['languages']
    folder = tmp_path / 'unsent-backup'
    digest = create_backup(app, folder)
    providers.validate_ledger_snapshot((folder / 'api-operations.sqlite').read_bytes())
    restored = restore_backup(folder, tmp_path / 'unsent-restored', digest)
    assert BudgetLedger(restored / 'api-operations.sqlite').report(identity) == before
    with pytest.raises(PermissionError, match='moved/restored'):
        TranslationService(restored).resume(identity)
    assert transport.calls == ['languages']


def test_translation_snapshot_guard_is_held_until_provider_ledger_capture_finishes(
        prepared, tmp_path, monkeypatch):
    app, service, _, identity, *_ = prepared
    original_snapshot = library_backup.snapshot_provider_files
    observations = []

    def attempt():
        with service.jobs.lock(identity):
            return 'unexpected owner'

    with ThreadPoolExecutor(max_workers=1) as executor:
        def capture(root):
            for before in (True, False):
                if not before:
                    result = original_snapshot(root)
                with pytest.raises(RuntimeError, match='already running'):
                    executor.submit(attempt).result(timeout=5)
                observations.append(before)
            return result
        monkeypatch.setattr(library_backup, 'snapshot_provider_files', capture)
        create_backup(app, tmp_path / 'guarded-backup')
    assert observations == [True, False]
    with service.jobs.lock(identity):
        pass  # Native job ownership is released when the combined snapshot ends.


def test_active_translation_cannot_be_snapshotted_as_idle_history(
        prepared, tmp_path):
    app, service, _, identity, *_ = prepared
    held, release = Event(), Event()
    def own_job():
        with service.jobs.lock(identity):
            held.set()
            assert release.wait(10)
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(own_job)
        try:
            assert held.wait(5)
            with pytest.raises(RuntimeError, match='already running'):
                create_backup(app, tmp_path / 'rejected')
        finally:
            release.set()
            task.result(timeout=5)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('ceiling', ['files', 'bytes'])
def test_translation_shares_existing_aggregate_backup_limits(
        prepared, tmp_path, monkeypatch, ceiling):
    app, service, *_ = prepared
    snapshot = library_backup._snapshot_sidecars
    captured = {}
    def observe(root, *, live=False):
        result = snapshot(root, live=live)
        captured.update(result)
        return result
    monkeypatch.setattr(library_backup, '_snapshot_sidecars', observe)
    # Existing global ceilings are lowered, not enlarged for the new component.
    if ceiling == 'files':
        monkeypatch.setattr(library_backup, 'MAX_SIDECARS', 3)
    else:
        monkeypatch.setattr(library_backup, 'MAX_SIDECAR_BYTES', 1)
    with pytest.raises(ValueError, match='sidecar inventory limit'):
        create_backup(app, tmp_path / 'limited')
    assert any(name.startswith('state/translation/') for name in captured)
    assert not (tmp_path / 'limited').exists()


@pytest.mark.parametrize('damage', ['source-hash', 'duplicate-json', 'lock-file'])
def test_resigned_invalid_translation_archive_is_rejected_without_writes(
        prepared, tmp_path, damage):
    app, service, _, identity, *_ = prepared
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    path = folder / 'state/translation' / (identity + '.json')
    if damage == 'source-hash':
        value = json.loads(path.read_bytes())
        value['source_sha256'] = 'f' * 64
        path.write_text(json.dumps(value), encoding='utf-8')
    elif damage == 'duplicate-json':
        raw = path.read_bytes()
        path.write_bytes(b'{"schema":"carr-translation-job/1",' + raw[1:])
    else:
        (folder / 'state/translation' / (identity + '.lock')).write_bytes(b'')
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    for entry in manifest['sidecars']:
        raw = (folder / entry['filename']).read_bytes()
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    raw_manifest = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    (folder / 'manifest.json').write_bytes(raw_manifest)
    digest = sha256(raw_manifest).hexdigest()
    before, directories = tree_bytes(folder), tree_directories(folder)
    with pytest.raises(ValueError):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert tree_bytes(folder) == before and tree_directories(folder) == directories
    assert not (tmp_path / 'rejected').exists()
