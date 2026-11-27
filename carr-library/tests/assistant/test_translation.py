"""Real files, locks, receipts and exports with synthetic provider responses only."""
from dataclasses import replace
from contextlib import closing
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket
import sqlite3
from threading import Event, Thread
import pytest

from carr_library.assistant.models import canonical
from carr_library.assistant.state import atomic_json
from carr_library.assistant.translation import (
    TranslationService, language_approval, translation_approval, split_text)
from carr_library.assistant.translation_state import (
    TranslationStore, allocations, is_state_path, snapshot_guard, state_limit,
    state_paths, validate_state_document, validate_state_tree, validate_translation_job)
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.providers import (
    BudgetLedger, ProfileStore, ProviderError, ProviderGateway, ProviderProfile,
    GOOGLE_TRANSLATION_ENDPOINT, translation_receipt_sha256)
from carr_library.reader.narration_text import NarrationDraft

SECRET = 'synthetic-account-key-do-not-send'


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
        self.calls, self.error = [], None

    def get(self, profile, route, headers, **kwargs):
        self.calls.append(('languages', profile.id, route, dict(headers)))
        if self.error:
            raise self.error
        return json.dumps({'data': {'languages': [
            {'language': 'en', 'name': 'English'}, {'language': 'uk', 'name': 'Ukrainian'},
            {'language': 'sl', 'name': 'Slovenian'}]}}).encode()

    def post(self, profile, route, payload, headers, **kwargs):
        self.calls.append(('text', profile.id, json.loads(payload), dict(headers)))
        if self.error:
            raise self.error
        return json.dumps({'data': {'translations': [{
            'translatedText': 'ПЕРЕКЛАД: ' + json.loads(payload)['q'],
            'detectedSourceLanguage': 'en', 'model': 'nmt'}]}}).encode()


@pytest.fixture
def ready(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('No real network in this suite'))
    root = tmp_path / 'library'
    root.mkdir()
    vault, transport = Vault(), Transport()
    profiles = ProfileStore(root / 'api-connections.json', vault)
    selected = profiles.save(ProviderProfile(
        'google-test', 'Synthetic NMT account', 'google-translation-v2',
        GOOGLE_TRANSLATION_ENDPOINT, 'nmt', ('translation',)), secret=SECRET)
    gateway = ProviderGateway(BudgetLedger(root / 'api-operations.sqlite'), vault=vault, transport=transport)
    service = TranslationService(root, gateway=gateway, profiles=profiles)
    service.refresh_languages(selected, approval=language_approval(selected))
    return service, selected, vault, transport


def submit(service, selected, value='Synthetic source', **changes):
    permit = translation_approval(selected, value, 'en', 'uk', 2_000_000)
    return service.submit(selected, value, 'en', 'uk', approval=permit, **changes)


def text_calls(transport):
    return [row for row in transport.calls if row[0] == 'text']


def test_opening_preview_and_submit_never_send_selected_text(ready):
    service, selected, _, transport = ready
    value = 'Український synthetic source 🙂\r\n' * 300
    result = service.preview(selected, value, 'en', 'uk', rate_per_million='20')
    assert result['source_sha256'] == sha256(value.encode()).hexdigest()
    assert Decimal(result['estimated_cost']) == Decimal(len(value)) * 20 / 1_000_000
    identity = submit(service, selected, value)
    assert service.jobs.load(identity)['schema'] == 'carr-translation-job/1'
    assert not text_calls(transport)
    assert len(transport.calls) == 1


def test_typed_translation_never_writes_generic_assistant_checkpoint(ready):
    service, selected, _, _ = ready
    identity = submit(service, selected)
    assert service.jobs.path(identity).parent == service.root / 'state' / 'translation'
    assert not (service.root / 'state' / 'assistant').exists()
    validate_translation_job(service.jobs.load(identity), identity=identity)


@pytest.mark.parametrize('source,target', [('xx', 'uk'), ('en', 'xx'), ('en', 'en')])
def test_supported_pair_is_required_before_job_creation(ready, source, target):
    service, selected, _, transport = ready
    with pytest.raises(ValueError):
        service.preview(selected, 'Synthetic source', source, target)
    assert not text_calls(transport)


def test_preview_requires_exact_profile_language_inventory(ready):
    service, selected, _, transport = ready
    service._languages_path(selected).unlink()
    with pytest.raises(ValueError, match='fetch the supported'):
        service.preview(selected, 'Synthetic source', 'en', 'uk')
    assert not text_calls(transport)


@pytest.mark.parametrize('change', ['text', 'target', 'profile', 'calls', 'mode', 'currency', 'loopback'])
def test_submit_cannot_expand_reviewed_authority(ready, change):
    service, selected, _, transport = ready
    value, target = 'Original exact text', 'uk'
    permit = translation_approval(selected, value, 'en', target, 100)
    if change == 'text':
        value = 'Different text'
    elif change == 'target':
        target = 'sl'
    elif change == 'profile':
        permit = replace(permit, profile_fingerprint='f' * 64)
    else:
        permit = replace(permit, **{
            'calls': {'max_calls': 2}, 'mode': {'mode': 'LOCAL_ONLY'},
            'currency': {'currency': 'EUR'}, 'loopback': {'allow_loopback': True},
        }[change])
    with pytest.raises(ValueError):
        service.submit(selected, value, 'en', target, approval=permit)
    assert not text_calls(transport) and not service.list()


def test_accepted_chunks_survive_restart_and_never_rebill(ready):
    service, selected, _, transport = ready
    source = 'Synthetic paragraph. ' * 1000
    identity = submit(service, selected, source)
    stop = Event()
    result = service.resume(identity, cancelled=stop.is_set, progress=lambda *args: stop.set())
    assert result['status'] == 'paused' and result['completed'] == 1
    before = len(text_calls(transport))
    reopened = TranslationService(service.root, gateway=service.gateway, profiles=service.profiles)
    assert reopened.resume(identity)['status'] == 'complete'
    expected = sum(bool(part.strip()) for part in split_text(source))
    assert len(text_calls(transport)) == expected and before == 1
    reopened.resume(identity)
    assert len(text_calls(transport)) == expected
    assert sum(row['estimate_micros'] for row in service.gateway.ledger.report(identity)) == 2_000_000


def test_whitespace_chunks_are_preserved_without_billable_requests(ready):
    service, selected, _, transport = ready
    source = ' ' * 4500 + 'Synthetic source\r\n'
    identity = submit(service, selected, source)
    result = service.resume(identity)
    job = service.jobs.load(identity)
    assert result['status'] == 'complete' and job['receipts'][0]['text'] == ' ' * 4500
    assert job['receipts'][0]['operation_id'] is None and job['allocations'][0] is None
    assert len(text_calls(transport)) == job['approval']['max_calls'] == 1


def test_known_unsent_pause_can_resume_exact_call(ready):
    service, selected, vault, transport = ready
    identity = submit(service, selected)
    stop = Event()
    vault.on_get = stop.set
    result = service.resume(identity, cancelled=stop.is_set)
    assert result['status'] == 'paused' and result['completed'] == 0
    assert service.gateway.ledger.report(identity)[0]['state'] == 'cancelled_before_send'
    assert not text_calls(transport)
    vault.on_get = lambda: None
    stop.clear()
    assert service.resume(identity, cancelled=stop.is_set)['status'] == 'complete'
    assert len(text_calls(transport)) == 1


def test_uncertain_charge_survives_restart_without_automatic_retry(ready):
    service, selected, _, transport = ready
    identity = submit(service, selected)
    transport.error = RuntimeError('Synthetic transport leak: ' + SECRET)
    with pytest.raises(ProviderError) as caught:
        service.resume(identity)
    assert SECRET not in str(caught.value)
    assert service.jobs.load(identity)['status'] == 'attention'
    assert service.gateway.ledger.report(identity)[0]['state'] == 'uncertain'
    transport.error = None
    reopened = TranslationService(service.root, gateway=service.gateway, profiles=service.profiles)
    with pytest.raises(ProviderError, match='not retried'):
        reopened.resume(identity)
    assert len(text_calls(transport)) == 1


def test_lost_paid_output_checkpoint_blocks_rebilling(ready, monkeypatch):
    service, selected, _, transport = ready
    identity = submit(service, selected)
    save = service.jobs.save
    def full_disk(value, **kwargs):
        if value['receipts']:
            raise OSError('synthetic full disk after paid response')
        return save(value, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(service.jobs, 'save', full_disk)
        with pytest.raises(OSError):
            service.resume(identity)
    assert service.jobs.load(identity)['pending'] is not None
    assert service.gateway.ledger.report(identity)[0]['state'] == 'complete'
    with pytest.raises(ProviderError, match='paid output is missing'):
        service.resume(identity)
    assert len(text_calls(transport)) == 1


def test_failed_intent_publication_sends_no_request(ready, monkeypatch):
    service, selected, _, transport = ready
    identity = submit(service, selected)
    save = service.jobs.save
    def stop_intent(value, **kwargs):
        if value['pending'] is not None:
            raise OSError('synthetic disk failure')
        return save(value, **kwargs)
    monkeypatch.setattr(service.jobs, 'save', stop_intent)
    with pytest.raises(OSError):
        service.resume(identity)
    assert not text_calls(transport) and not service.gateway.ledger.report(identity)


@pytest.mark.parametrize('change', ['profile-renamed', 'profile-removed', 'ledger-replaced', 'root-moved'])
def test_live_binding_changes_revoke_saved_remote_permission(ready, tmp_path, change):
    service, selected, _, transport = ready
    identity = submit(service, selected)
    if change == 'profile-renamed':
        service.profiles.save(replace(selected, name='Different reviewed account name'))
    elif change == 'profile-removed':
        service.profiles.remove(selected.id)
    elif change == 'ledger-replaced':
        ledger_path = service.gateway.ledger.path
        ledger_path.rename(ledger_path.with_suffix('.retained'))
        BudgetLedger(ledger_path)
    else:
        moved = tmp_path / 'restored-library'
        shutil.copytree(service.root, moved)
        service = TranslationService(moved, profiles=ProfileStore(moved / 'api-connections.json', service.profiles.vault),
            gateway=ProviderGateway(BudgetLedger(moved / 'api-operations.sqlite'),
                                    vault=service.profiles.vault, transport=transport))
    with pytest.raises((PermissionError, ValueError)):
        service.resume(identity)
    assert not text_calls(transport)


def test_unknown_pending_intent_without_ledger_never_restarts_silently(ready):
    service, selected, _, transport = ready
    identity = submit(service, selected)
    job = service.jobs.load(identity)
    job['status'] = 'running'
    job['pending'] = {'index': 0, 'operation_id': identity + '_0'}
    service.jobs.save(job)
    with pytest.raises(ProviderError, match='unresolved'):
        service.resume(identity)
    assert not text_calls(transport)


def test_export_is_new_source_pinned_narration_derivative(ready, tmp_path):
    service, selected, _, transport = ready
    source = 'Original synthetic source.\r\nУкраїнський текст.'
    original = tmp_path / 'source.txt'
    original.write_bytes(source.encode())
    identity = submit(service, selected, source)
    assert service.resume(identity)['status'] == 'complete'
    target = tmp_path / 'new-translation'
    service.export(identity, target)
    assert original.read_bytes() == (target / 'original.txt').read_bytes() == source.encode()
    draft = NarrationDraft.from_dict(json.loads((target / 'translation.narration.json').read_text(encoding='utf-8')),
                                    source_text=source, source_id='translation-source:' + sha256(source.encode()).hexdigest())
    assert draft.text != source and draft.source_range(0, len(draft.text))[2] is False
    assert draft.undo() and draft.text == source
    report = json.loads((target / 'translation-provenance.json').read_text())
    assert report['source_is_immutable'] is True and report['quotation_authority'] is False
    for name, digest in report['files'].items():
        assert sha256((target / name).read_bytes()).hexdigest() == digest
    for file in target.iterdir():
        assert SECRET not in file.read_text(encoding='utf-8')
        assert selected.credential_ref not in file.read_text(encoding='utf-8')
    with pytest.raises(FileExistsError):
        service.export(identity, target)
    with pytest.raises(ValueError, match='outside'):
        service.export(identity, service.root / 'forbidden-export')
    assert not (service.root / 'forbidden-export').exists()
    assert len(text_calls(transport)) == 1


def test_complete_restored_derivative_exports_offline_without_rebinding(ready, tmp_path):
    service, selected, _, transport = ready
    identity = submit(service, selected)
    service.resume(identity)
    moved = tmp_path / 'restored-library'
    shutil.copytree(service.root, moved)
    restored = TranslationService(moved)
    output = tmp_path / 'restored-export'
    restored.export(identity, output)
    report = json.loads((output / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert all(row['decoded_receipt_integrity'] == 'archived-unverified' for row in report['chunks'])
    assert len(text_calls(transport)) == 1


def test_pure_snapshot_validation_and_same_owner_lock_reuse(ready):
    service, selected, _, _ = ready
    identity = submit(service, selected)
    before = {path.name: path.read_bytes() for path in state_paths(service.root)}
    with service.jobs.lock(identity):
        with exclusive_writer(service.root), snapshot_guard(service.root):
            validate_state_tree(None, service.root)
            paths = state_paths(service.root)
            assert len(paths) == 2
            for path in paths:
                relative = path.relative_to(service.root).as_posix()
                assert is_state_path(relative)
                assert len(path.read_bytes()) <= state_limit(relative)
                validate_state_document(path.name, path.read_bytes())
    assert before == {path.name: path.read_bytes() for path in state_paths(service.root)}


def test_snapshot_cannot_borrow_another_threads_live_job_guard(ready):
    service, selected, _, _ = ready
    identity = submit(service, selected)
    entered, release = Event(), Event()
    errors = []
    def hold():
        try:
            with service.jobs.lock(identity):
                entered.set()
                assert release.wait(5)
        except BaseException as exc:
            errors.append(exc)
    worker = Thread(target=hold)
    worker.start()
    try:
        assert entered.wait(5)
        with exclusive_writer(service.root):
            with pytest.raises(RuntimeError):
                with snapshot_guard(service.root):
                    pytest.fail('Snapshot borrowed another owner lock')
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors


@pytest.mark.parametrize('mutation', ['extra-field', 'source', 'chunk-hash', 'budget-type', 'pending-index',
                                    'complete-early', 'profile-secret', 'receipt-order'])
def test_forged_state_is_rejected_before_resume_or_backup(ready, mutation):
    service, selected, _, transport = ready
    identity = submit(service, selected)
    if mutation in ('chunk-hash', 'receipt-order'):
        service.resume(identity)
    value = service.jobs.load(identity)
    if mutation == 'extra-field':
        value['unknown'] = True
    elif mutation == 'source':
        value['source_text'] += 'tampered'
    elif mutation == 'chunk-hash':
        value['receipts'][0]['translation_sha256'] = 'f' * 64
    elif mutation == 'budget-type':
        value['allocations'][0] = float(value['allocations'][0])
    elif mutation == 'pending-index':
        value['pending'] = {'index': 2, 'operation_id': identity + '_2'}
    elif mutation == 'complete-early':
        value['status'] = 'complete'
    elif mutation == 'profile-secret':
        value['profile']['api_key'] = SECRET
    else:
        value['receipts'][0]['index'] = True
    raw = canonical(value).encode()
    with pytest.raises(ValueError):
        validate_state_document(identity + '.json', raw)
    atomic_json(service.jobs.path(identity), value)  # Simulate untrusted/damaged storage, bypassing writer validation.
    before = len(transport.calls)
    with pytest.raises(ValueError):
        service.resume(identity)
    with pytest.raises(ValueError):
        validate_state_tree(None, service.root)
    assert len(transport.calls) == before


def test_snapshot_rejects_unknown_files_and_traversal(ready):
    service, _, _, _ = ready
    for relative in ('state/translation/../secret.json', 'state/translation/nested/' + 'a' * 32 + '.json',
                     'state/translation/.state.lock', 'state/assistant/' + 'a' * 32 + '.json'):
        assert not is_state_path(relative)
    rogue = service.directory / 'unknown.json'
    rogue.write_text('{}')
    with pytest.raises(ValueError, match='Unexpected'):
        state_paths(service.root)


def test_chunking_and_integer_allocations_preserve_all_unicode():
    source = '  абв🙂\r\n' * 3000
    parts = split_text(source)
    assert ''.join(parts) == source and max(map(len, parts)) <= 4500
    reserved = allocations(parts, 7)
    assert sum(value for value in reserved if value is not None) == 7
    assert all(value is None or type(value) is int for value in reserved)


@pytest.mark.parametrize('mutation', [
    'text-rehashed', 'detected-language-rehashed', 'receipt-digest-removed',
    'ledger-digest-removed', 'ledger-digest-changed',
])
def test_completed_translation_requires_independent_decoded_ledger_evidence(ready, tmp_path, mutation):
    service, selected, _, transport = ready
    identity = submit(service, selected)
    service.resume(identity)
    job = service.jobs.load(identity)
    original_source = job['source_text']
    row = job['receipts'][0]
    if mutation in ('text-rehashed', 'detected-language-rehashed'):
        if mutation == 'text-rehashed':
            row['text'] = 'Changed derivative with a recomputed local hash.'
            row['translation_sha256'] = sha256(row['text'].encode()).hexdigest()
        else:
            row['detected_source'] = 'uk'
        row['decoded_translation_sha256'] = translation_receipt_sha256(row['text'], row['detected_source'])
    elif mutation == 'receipt-digest-removed':
        row.pop('decoded_translation_sha256')
    else:
        digest = None if mutation == 'ledger-digest-removed' else 'f' * 64
        with closing(sqlite3.connect(service.gateway.ledger.path)) as database, database:
            database.execute('UPDATE operation SET decoded_translation_sha256=? WHERE id=?',
                             (digest, row['operation_id']))
    # The checkpoint's own hashes and typed schema remain valid. Only the
    # independent live evidence can detect these altered or downgraded claims.
    validate_translation_job(job)
    service.jobs.save(job)
    before = service.jobs.path(identity).read_bytes()
    calls = len(transport.calls)
    with pytest.raises(PermissionError, match='ledger'):
        service.resume(identity)
    target = tmp_path / 'refused-export'
    with pytest.raises(PermissionError, match='ledger'):
        service.export(identity, target)
    assert not target.exists() and len(transport.calls) == calls
    assert service.jobs.path(identity).read_bytes() == before
    assert service.jobs.load(identity)['source_text'] == original_source


def test_historical_wire_only_job_remains_exportable_without_invented_decoded_proof(ready, tmp_path):
    service, selected, _, transport = ready
    identity = submit(service, selected)
    service.resume(identity)
    job = service.jobs.load(identity)
    for row in job['receipts']:
        row.pop('decoded_translation_sha256', None)
    service.jobs.save(job)
    # Reconstruct the actual six-column historical ledger, then open it through
    # the current migration. No text was retained there to backfill a new hash.
    with closing(sqlite3.connect(service.gateway.ledger.path)) as database, database:
        database.execute('ALTER TABLE operation RENAME TO modern_operation')
        database.execute('CREATE TABLE operation (id TEXT PRIMARY KEY, job TEXT NOT NULL, request_hash TEXT NOT NULL,'
                         'estimate_micros INTEGER NOT NULL, state TEXT NOT NULL, output_sha256 TEXT)')
        database.execute('INSERT INTO operation SELECT id,job,request_hash,estimate_micros,state,output_sha256 FROM modern_operation')
        database.execute('DROP TABLE modern_operation')
    service.gateway.ledger = BudgetLedger(service.gateway.ledger.path)
    before = service.jobs.path(identity).read_bytes()
    calls = len(transport.calls)
    assert service.resume(identity)['status'] == 'complete'
    target = tmp_path / 'legacy-export'
    service.export(identity, target)
    report = json.loads((target / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert {row['decoded_receipt_integrity'] for row in report['chunks']} == {'legacy-wire-only'}
    assert all('decoded_translation_sha256' not in row for row in report['chunks'])
    assert service.jobs.path(identity).read_bytes() == before
    assert len(transport.calls) == calls
    assert all(row['decoded_translation_sha256'] is None for row in service.gateway.ledger.report(identity))
    restored_root = tmp_path / 'legacy-restored'
    shutil.copytree(service.root, restored_root)
    restored_target = tmp_path / 'legacy-restored-export'
    TranslationService(restored_root).export(identity, restored_target)
    restored_report = json.loads((restored_target / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert {row['decoded_receipt_integrity'] for row in restored_report['chunks']} == {'archived-unverified'}


def test_resumed_legacy_job_preserves_old_receipts_and_binds_only_new_calls(ready, tmp_path):
    service, selected, _, transport = ready
    source = 'First translated source. ' * 400
    identity = submit(service, selected, source)
    stop = Event()
    service.resume(identity, cancelled=stop.is_set, progress=lambda *args: stop.set())
    job = service.jobs.load(identity)
    assert len(job['receipts']) == 1
    historical = job['receipts'][0]
    historical.pop('decoded_translation_sha256')
    with closing(sqlite3.connect(service.gateway.ledger.path)) as database, database:
        database.execute('UPDATE operation SET decoded_translation_sha256=NULL WHERE id=?',
                         (historical['operation_id'],))
    service.jobs.save(job)
    old_receipt = canonical(historical)
    assert service.resume(identity)['status'] == 'complete'
    completed = service.jobs.load(identity)
    assert canonical(completed['receipts'][0]) == old_receipt
    assert all('decoded_translation_sha256' in row for row in completed['receipts'][1:])
    assert len(text_calls(transport)) == sum(bool(chunk.strip()) for chunk in split_text(source))
    target = tmp_path / 'mixed-export'
    service.export(identity, target)
    report = json.loads((target / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert report['chunks'][0]['decoded_receipt_integrity'] == 'legacy-wire-only'
    assert all(row['decoded_receipt_integrity'] == 'decoded-ledger-bound' for row in report['chunks'][1:])


@pytest.mark.parametrize('source', [
    'A' * 4498 + '\r\n' + 'B' * 100,
    'A' * 4499 + ' ' + 'B' * 100,
    'A' * 4500 + ' ' * 4500 + 'B',
    'A' * 4500 + 'B' * 100,
], ids=['crlf-boundary', 'space-boundary', 'whitespace-only-chunk', 'unbroken-word'])
def test_export_restores_source_chunk_separators_without_rewriting_receipts(ready, tmp_path, monkeypatch, source):
    service, selected, _, transport = ready
    post = transport.post

    def trim_outer_space(profile, route, payload, headers, **kwargs):
        raw = post(profile, route, payload, headers, **kwargs)
        result = json.loads(raw)
        result['data']['translations'][0]['translatedText'] = json.loads(payload)['q'].strip()
        return json.dumps(result).encode()

    monkeypatch.setattr(transport, 'post', trim_outer_space)
    identity = submit(service, selected, source)
    service.resume(identity)
    before = service.jobs.path(identity).read_bytes()
    target = tmp_path / 'joined-export'
    service.export(identity, target)
    assert (target / 'translated.txt').read_bytes() == source.encode()
    assert (target / 'original.txt').read_bytes() == source.encode()
    assert service.jobs.path(identity).read_bytes() == before
    job = service.jobs.load(identity)
    for chunk, row in zip(split_text(source), job['receipts']):
        assert row['text'] == (chunk.strip() if chunk.strip() else chunk)
    report = json.loads((target / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert all(row['decoded_receipt_integrity'] in ('decoded-ledger-bound', 'original-whitespace')
               for row in report['chunks'])


def test_single_chunk_preserves_provider_outer_whitespace_and_offline_profile_history(ready, tmp_path, monkeypatch):
    service, selected, vault, transport = ready
    post = transport.post
    accepted = '  Прийнятий переклад\r\n'

    def translated(profile, route, payload, headers, **kwargs):
        raw = post(profile, route, payload, headers, **kwargs)
        result = json.loads(raw)
        result['data']['translations'][0]['translatedText'] = accepted
        return json.dumps(result).encode()

    monkeypatch.setattr(transport, 'post', translated)
    identity = submit(service, selected, 'Original source')
    service.resume(identity)
    original_job = service.jobs.path(identity).read_bytes()
    service.profiles.remove(selected.id)
    vault.on_get = lambda: pytest.fail('Completed derivative attempted to reopen provider credentials.')
    target = tmp_path / 'single-export'
    service.export(identity, target)
    assert (target / 'translated.txt').read_bytes() == accepted.encode()
    assert service.jobs.path(identity).read_bytes() == original_job
    report = json.loads((target / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert report['chunks'][0]['decoded_receipt_integrity'] == 'decoded-ledger-bound'
    assert len(text_calls(transport)) == 1


@pytest.mark.parametrize('digest', [None, 'f' * 64], ids=['missing-value', 'wrong-content'])
def test_typed_translation_rejects_invalid_decoded_receipt_before_backup(ready, digest):
    service, selected, _, _ = ready
    identity = submit(service, selected)
    service.resume(identity)
    job = service.jobs.load(identity)
    job['receipts'][0]['decoded_translation_sha256'] = digest
    with pytest.raises(ValueError):
        validate_state_document(identity + '.json', canonical(job).encode())
