"""Real backup/restore of passive provider settings and budget audit history."""
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library import library_backup as backup
from carr_library import provider_recovery as recovery
from carr_library import providers as api
from carr_library.research.export_file import capture_export_destination
from carr_library.ui.api_connections import ConnectionsController


SECRET = 'synthetic-recovery-key-not-real'


class MemoryVault:
    def __init__(self):
        self.values = {}
    def put(self, reference, binding, secret):
        self.values[reference, binding] = secret
    def get(self, reference, binding):
        return self.values[reference, binding]
    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


def setup(root, *, profiles=True, ledger=True):
    app = LibraryApp(root)
    vault = MemoryVault()
    profile = api.ProviderProfile('account', 'Recovered synthetic account', 'openai-compatible',
        'https://example.com/v1', 'selected-model', ('llm',))
    if profiles:
        profile = api.ProfileStore(app.root / 'api-connections.json', vault).save(profile, secret=SECRET)
    if ledger:
        store = api.BudgetLedger(app.root / 'api-operations.sqlite')
        for index, state in enumerate(('complete', 'uncertain', 'reserved', 'cancelled_before_send', 'reconciled_no_charge')):
            request = api.ProviderRequest('llm', 'Synthetic history input ' + str(index))
            approval = api.JobApproval('job_' + str(index), profile.fingerprint, (request.fingerprint(profile),),
                budget_micros=100, mode='APPROVED_REMOTE')
            identity = 'operation_' + str(index)
            store.reserve(identity, approval, request.fingerprint(profile), 10)
            if state == 'complete':
                store.finish(identity, output_sha256='a' * 64, decoded_translation_sha256='b' * 64)
            elif state in ('uncertain', 'reconciled_no_charge'):
                store.finish(identity)
                if state == 'reconciled_no_charge':
                    store.reconcile_no_charge(identity, confirmed=True)
            elif state == 'cancelled_before_send':
                store.cancel_before_send(identity)
    return app, profile, vault


def deny_active_services(monkeypatch):
    monkeypatch.setattr(api, 'WindowsCredentialVault', lambda: pytest.fail('Passive recovery opened a vault.'))
    monkeypatch.setattr(api.ProviderGateway, 'perform', lambda *a, **k: pytest.fail('Passive recovery sent a request.'))


@pytest.mark.parametrize('contents', ['profiles', 'ledger', 'both', 'none'])
def test_complete_roundtrip_rebackup_keeps_history_without_restoring_active_authority(tmp_path, monkeypatch, contents):
    app, profile, vault = setup(tmp_path / 'library', profiles=contents in ('profiles', 'both'),
                               ledger=contents in ('ledger', 'both'))
    deny_active_services(monkeypatch)
    expected = recovery.snapshot_live(app.root)
    live = {name: (app.root / name).read_bytes() for name in ('api-connections.json', 'api-operations.sqlite')
            if (app.root / name).exists()}
    folder, restored = tmp_path / 'backup', tmp_path / 'restored'
    proof = backup.create_backup(app, folder)
    backup.verify_backup(folder, proof)
    backup.restore_backup(folder, restored, proof)
    assert not (restored / 'api-connections.json').exists()
    assert not (restored / 'api-operations.sqlite').exists()
    records = recovery.state_paths(restored)
    assert len(records) == (0 if contents == 'none' else 1)
    if records:
        raw, value = recovery.read_record(records[0])
        assert raw == expected and SECRET.encode() not in raw and b'url-api-' not in raw
        assert records[0].stem == sha256(raw).hexdigest()
        if contents in ('ledger', 'both'):
            assert {row['state'] for row in value['ledger']['operations']} == {
                'complete', 'uncertain', 'reserved', 'cancelled_before_send', 'reconciled_no_charge'}
        if contents in ('profiles', 'both'):
            assert value['profiles']['profiles'][0]['credential_ref'] is None
            assert value['profiles']['profiles'][0]['endpoint'] == profile.endpoint
    again = tmp_path / 'rebackup'
    second = backup.create_backup(LibraryApp(restored), again)
    backup.verify_backup(again, second)
    second_root = backup.restore_backup(again, tmp_path / 'twice-restored', second)
    assert [path.read_bytes() for path in recovery.state_paths(second_root)] == (
        [expected] if expected is not None else [])
    assert {name: (app.root / name).read_bytes() for name in live} == live
    if 'api-connections.json' in live:
        assert vault.get(profile.credential_ref, profile.credential_binding) == SECRET


@pytest.mark.parametrize('kind', ['profiles', 'ledger'])
def test_changed_live_source_after_capture_refuses_complete_manifest(tmp_path, monkeypatch, kind):
    app, _, _ = setup(tmp_path / 'library')
    original = backup._capture_provider_recovery
    changed = []
    def capture(root, stage, **kwargs):
        value = original(root, stage, **kwargs)
        if kind == 'profiles':
            path = root / 'api-connections.json'
            data = json.loads(path.read_bytes())
            data['profiles'][0]['name'] = 'Changed after captured snapshot'
            path.write_text(json.dumps(data), encoding='utf-8')
        else:
            api.BudgetLedger(root / 'api-operations.sqlite').reconcile_no_charge('operation_1', confirmed=True)
        changed.append(True)
        return value
    monkeypatch.setattr(backup, '_capture_provider_recovery', capture)
    with pytest.raises(ValueError, match='changed during backup'):
        backup.create_backup(app, tmp_path / 'changed')
    assert changed and not (tmp_path / 'changed' / backup.MANIFEST).exists()


@pytest.mark.parametrize('kind', ['profiles', 'ledger'])
def test_appearing_previously_absent_source_is_not_silently_omitted(tmp_path, monkeypatch, kind):
    app, _, _ = setup(tmp_path / 'library', profiles=False, ledger=False)
    original = backup._capture_provider_recovery
    def capture(root, stage, **kwargs):
        assert original(root, stage, **kwargs) is None
        if kind == 'profiles':
            profile = api.ProviderProfile('new', 'New account', 'openai-compatible',
                'https://example.com/v1', 'model', ('llm',))
            (root / 'api-connections.json').write_text(json.dumps({
                'version': 1, 'profiles': [asdict(profile)]}), encoding='utf-8')
        else:
            api.BudgetLedger(root / 'api-operations.sqlite')
        return None
    monkeypatch.setattr(backup, '_capture_provider_recovery', capture)
    with pytest.raises(ValueError, match='changed during backup'):
        backup.create_backup(app, tmp_path / 'changed')
    assert not (tmp_path / 'changed' / backup.MANIFEST).exists()


@pytest.mark.parametrize('damage', ['credential', 'extra_field', 'duplicate_approval', 'missing_approval',
    'complete_without_receipt', 'uncertain_with_receipt', 'negative_estimate', 'unknown_state',
    'source_missing', 'schema', 'nonfinite', 'duplicate_json'])
def test_rehashed_untrusted_recovery_data_never_becomes_valid_typed_state(tmp_path, damage):
    app, _, _ = setup(tmp_path / 'library')
    value = recovery.decode(recovery.snapshot_live(app.root))
    if damage == 'credential':
        value['profiles']['profiles'][0]['credential_ref'] = 'url-api-' + 'c' * 32
    elif damage == 'extra_field':
        value['ledger']['operations'][0]['permission'] = 'run-again'
    elif damage == 'duplicate_approval':
        value['ledger']['approvals'].append(dict(value['ledger']['approvals'][0]))
    elif damage == 'missing_approval':
        value['ledger']['approvals'].clear()
    elif damage == 'complete_without_receipt':
        value['ledger']['operations'][0]['output_sha256'] = None
    elif damage == 'uncertain_with_receipt':
        value['ledger']['operations'][1]['output_sha256'] = 'd' * 64
    elif damage == 'negative_estimate':
        value['ledger']['operations'][0]['estimate_micros'] = -1
    elif damage == 'unknown_state':
        value['ledger']['operations'][0]['state'] = 'approved-for-replay'
    elif damage == 'source_missing':
        value['source']['ledger_file_sha256'] = None
    elif damage == 'schema':
        value['schema'] = 'active-provider-settings/1'
    elif damage == 'nonfinite':
        value['ledger']['operations'][0]['estimate_micros'] = float('nan')
    raw = json.dumps(value, ensure_ascii=False).encode()
    if damage == 'duplicate_json':
        raw = b'{"schema":"' + recovery.SCHEMA.encode() + b'",' + raw[1:]
    base = app.root / recovery.DIRECTORY
    base.mkdir(parents=True)
    path = base / (sha256(raw).hexdigest() + '.json')
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        backup.create_backup(app, tmp_path / 'invalid')
    assert not (tmp_path / 'invalid' / backup.MANIFEST).exists()


def test_outer_manifest_rehash_cannot_hide_changed_snapshot_identity(tmp_path):
    app, _, _ = setup(tmp_path / 'library')
    folder = tmp_path / 'backup'
    backup.create_backup(app, folder)
    manifest = json.loads((folder / backup.MANIFEST).read_bytes())
    entry = next(row for row in manifest['sidecars'] if recovery.is_state_path(row['path']))
    target = folder / backup.SIDECARS / entry['path']
    value = recovery.decode(target.read_bytes())
    value['profiles']['profiles'][0]['name'] = 'A different valid account label'
    raw = json.dumps(value).encode()
    target.write_bytes(raw)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = json.dumps(manifest).encode()
    (folder / backup.MANIFEST).write_bytes(manifest_raw)
    with pytest.raises(ValueError, match='filename'):
        backup.restore_backup(folder, tmp_path / 'restored', sha256(manifest_raw).hexdigest())
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('target', ['profiles', 'ledger', 'history'])
def test_unreadable_existing_namespace_fails_instead_of_being_dropped(tmp_path, monkeypatch, target):
    app, _, _ = setup(tmp_path / 'library')
    selected = app.root / {'profiles': 'api-connections.json', 'ledger': 'api-operations.sqlite',
                            'history': recovery.DIRECTORY}[target]
    original = recovery.optional_present
    def denied(path):
        if Path(path) == selected:
            raise PermissionError('Synthetic read refusal.')
        return original(path)
    monkeypatch.setattr(recovery, 'optional_present', denied)
    with pytest.raises((PermissionError, ValueError)):
        backup.create_backup(app, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


@pytest.mark.parametrize('suffix', ['-journal', '-wal', '-shm'])
def test_active_or_orphan_journal_is_visible_refusal(tmp_path, suffix):
    app, _, _ = setup(tmp_path / 'library', ledger=False)
    (app.root / ('api-operations.sqlite' + suffix)).write_bytes(b'Uncommitted usage bytes')
    with pytest.raises(ValueError, match='orphan journal'):
        backup.create_backup(app, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


@pytest.mark.parametrize('damage', ['trigger', 'unknown_table', 'unknown_column', 'corrupt'])
def test_foreign_or_corrupt_live_ledger_never_runs_a_recovery_path(tmp_path, damage):
    app, _, _ = setup(tmp_path / 'library')
    path = app.root / 'api-operations.sqlite'
    if damage == 'corrupt':
        path.write_bytes(b'Not a SQLite database')
    else:
        with closing(sqlite3.connect(path)) as db:
            if damage == 'trigger':
                db.execute('CREATE TRIGGER extra AFTER INSERT ON approval BEGIN DELETE FROM operation; END')
            elif damage == 'unknown_table':
                db.execute('CREATE TABLE secrets(value TEXT)')
            else:
                db.execute('ALTER TABLE approval ADD COLUMN permission TEXT')
            db.commit()
    with pytest.raises(ValueError):
        backup.create_backup(app, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


def test_historical_ledger_without_decoded_receipt_column_is_captured_read_only(tmp_path):
    app, _, _ = setup(tmp_path / 'library', profiles=False, ledger=False)
    path = app.root / 'api-operations.sqlite'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE approval (id TEXT PRIMARY KEY, digest TEXT NOT NULL)')
        db.execute('CREATE TABLE operation (id TEXT PRIMARY KEY, job TEXT NOT NULL, request_hash TEXT NOT NULL,'
                   'estimate_micros INTEGER NOT NULL, state TEXT NOT NULL, output_sha256 TEXT)')
        db.execute('INSERT INTO approval VALUES(?,?)', ('job', 'a' * 64))
        db.execute('INSERT INTO operation VALUES(?,?,?,?,?,?)', ('op', 'job', 'b' * 64, 3, 'complete', 'c' * 64))
        db.commit()
    before = path.read_bytes()
    captured = recovery.decode(recovery.snapshot_live(app.root))
    assert captured['ledger']['operations'][0]['decoded_translation_sha256'] is None
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        assert len(db.execute('PRAGMA table_info(operation)').fetchall()) == 6


@pytest.mark.parametrize('limit', ['record_bytes', 'database_bytes', 'rows', 'files', 'collection_bytes'])
def test_recovery_resource_limits_refuse_complete_backup(tmp_path, monkeypatch, limit):
    app, _, _ = setup(tmp_path / 'library')
    if limit in ('files', 'collection_bytes'):
        stage = tmp_path / 'capture'
        recovery.capture_live(app.root, stage)
        source = next((stage / recovery.DIRECTORY).iterdir())
        target = app.root / recovery.DIRECTORY
        target.mkdir(parents=True)
        (target / source.name).write_bytes(source.read_bytes())
    key = {'record_bytes': 'MAX_BYTES', 'database_bytes': 'MAX_DATABASE_BYTES',
           'rows': 'MAX_ROWS', 'files': 'MAX_FILES', 'collection_bytes': 'MAX_COLLECTION_BYTES'}[limit]
    monkeypatch.setattr(recovery, key, 0 if limit == 'files' else 1)
    with pytest.raises(ValueError):
        backup.create_backup(app, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


def test_cancelled_capture_never_publishes_a_complete_backup(tmp_path):
    app, _, _ = setup(tmp_path / 'library')
    with pytest.raises(InterruptedError):
        recovery.capture_live(app.root, tmp_path / 'stage', cancelled=lambda: True)
    assert not (tmp_path / 'stage' / recovery.DIRECTORY).exists()


def test_explicit_recovery_import_and_approved_history_export_use_existing_services(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path / 'library')
    raw = recovery.snapshot_live(app.root)
    destination = tmp_path / 'restored'
    restored = LibraryApp(destination)
    deny_active_services(monkeypatch)
    controller = ConnectionsController(restored.root)
    with pytest.raises(api.ProviderError, match='Review'):
        controller.import_recovery(raw)
    assert not controller.store.path.exists()
    imported = controller.import_recovery(raw, approved=True)
    assert len(imported) == 1 and imported[0].credential_ref is None
    assert not (restored.root / 'api-operations.sqlite').exists()
    with pytest.raises(FileExistsError):
        controller.import_recovery(raw, approved=True)
    history = tmp_path / 'history.json'
    receipt = controller.export_recovery(raw, history)
    assert receipt.path == history and receipt.cleanup_warning is None and history.read_bytes() == raw
    history.write_bytes(b'Existing independent export')
    with pytest.raises(FileExistsError):
        controller.export_recovery(raw, history)
    approved = capture_export_destination(history, private_root=restored.root)
    receipt = controller.export_recovery(raw, history, approval=approved)
    assert receipt.path == history and receipt.cleanup_warning is None and history.read_bytes() == raw
    assert 'not bills' in recovery.describe(recovery.decode(raw))
