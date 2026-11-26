"""Real SQLite/provider backup boundaries; no credentials, requests or paid calls."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library import library_backup, providers
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.library_backup import create_backup, restore_backup
from carr_library.providers import (
    BudgetLedger, JobApproval, ProfileStore, ProviderError, ProviderGateway,
    ProviderProfile, ProviderRequest, LEDGER_FILENAME, PROFILES_FILENAME,
    snapshot_provider_files, validate_ledger_snapshot,
)
from carr_library.progress import (
    FILENAME as PROGRESS_SETTINGS, ProgressSettings, ProgressSettingsStore,
)


class Vault:
    def __init__(self):
        self.values = {}

    def put(self, reference, binding, secret):
        self.values[reference, binding] = secret

    def get(self, reference, binding):
        return self.values[reference, binding]

    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


def forbidden(*args, **kwargs):
    raise AssertionError('Backup or restore attempted credential/network access')


@pytest.fixture
def provider_library(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека čítanie')
    workspace = app.create_workspace('Research')
    app.add_text(workspace, 'Source', 'Незмінний оригінал. Unchanged source text.')
    vault = Vault()
    store = ProfileStore(app.root / PROFILES_FILENAME, vault)
    profile = store.save(ProviderProfile('account', 'Робочий профіль',
        'openai-compatible', 'https://example.test/v1', 'selected-model', ('llm',)),
        secret='synthetic-secret-only-for-backup-test')
    store.save(ProviderProfile('local', 'Local', 'openai-compatible',
        'http://127.0.0.1:8080/v1', 'local-model', ('llm',), local=True))
    request = ProviderRequest('llm', 'Synthetic task; never transmitted.')
    approval = JobApproval('job', profile.fingerprint, (request.fingerprint(profile),),
        budget_micros=100, max_calls=10, mode='APPROVED_REMOTE')
    ledger = BudgetLedger(app.root / LEDGER_FILENAME)
    for identity, estimate in [('reserved', 30), ('uncertain', 20), ('complete', 10), ('released', 40)]:
        ledger.reserve(identity, approval, request.fingerprint(profile), estimate)
    ledger.finish('uncertain')
    ledger.finish('complete', output_sha256='2' * 64)
    ledger.reconcile_no_charge('released', confirmed=True)
    return app, store, profile, request, approval, ledger, vault


def read_manifest(folder):
    return json.loads((folder / 'manifest.json').read_bytes())


def resign(folder, *, manifest=None):
    manifest = read_manifest(folder) if manifest is None else manifest
    for item in manifest.get('sidecars', []):
        path = folder / item['filename']
        if path.is_file():
            raw = path.read_bytes()
            item.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def test_provider_backup_restores_disconnected_profiles_and_every_budget_state(
        provider_library, tmp_path, monkeypatch):
    app, store, profile, request, approval, ledger, vault = provider_library
    profile_before = store.path.read_bytes()
    report_before = ledger.report('job')
    ledger_before = ledger.path.read_bytes()
    monkeypatch.setattr(providers, 'WindowsCredentialVault', forbidden)
    monkeypatch.setattr(providers.DirectTransport, 'post', forbidden)
    monkeypatch.setattr(vault, 'get', forbidden)
    folder = tmp_path / 'Резервна копія'
    digest = create_backup(app, folder)
    manifest = read_manifest(folder)
    assert manifest['version'] == 4
    assert manifest['provider_restore_mode'] == 'LOCAL_ONLY'
    assert {item['filename'] for item in manifest['sidecars']} == {PROFILES_FILENAME, LEDGER_FILENAME}
    raw = (folder / PROFILES_FILENAME).read_bytes()
    assert b'url-api-' not in raw and b'synthetic-secret' not in raw
    assert all(p['credential_ref'] is None for p in json.loads(raw)['profiles'])
    assert store.path.read_bytes() == profile_before
    assert ledger.path.read_bytes() == ledger_before
    backup_before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    restored = restore_backup(folder, tmp_path / 'Відновлена бібліотека', digest)
    profiles = ProfileStore(restored / PROFILES_FILENAME).profiles()
    assert profiles == (replace(profile, credential_ref=None), store.profiles()[1])
    restored_ledger = BudgetLedger(restored / LEDGER_FILENAME)
    assert restored_ledger.report('job') == report_before
    for identity in ('reserved', 'uncertain', 'complete', 'released'):
        with pytest.raises(ProviderError, match='already recorded'):
            restored_ledger.reserve(identity, approval, request.fingerprint(profile), 0)
    # Preserved reserved/uncertain/complete estimates total 60. Released rows
    # stay in history and their operation IDs cannot be recycled.
    restored_ledger.reserve('new-explicit-operation', approval, request.fingerprint(profile), 40)
    with pytest.raises(ProviderError, match='budget'):
        restored_ledger.reserve('over-budget', approval, request.fingerprint(profile), 1)
    restored_profile = profiles[0]
    fresh_default = JobApproval('new-job', restored_profile.fingerprint,
                               (request.fingerprint(restored_profile),), budget_micros=100)
    with pytest.raises(ProviderError, match='LOCAL ONLY'):
        ProviderGateway(restored_ledger).perform(restored_profile, request,
            approval=fresh_default, operation_id='no-restored-egress', estimated_micros=1)
    assert restored_ledger.report('new-job') == []
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == backup_before
    assert (restored / PROFILES_FILENAME).stat().st_nlink == 1
    assert (restored / LEDGER_FILENAME).stat().st_nlink == 1


def test_live_wal_commits_are_included_instead_of_copying_stale_main_file(provider_library, tmp_path):
    app, _, profile, request, approval, ledger, _ = provider_library
    with closing(sqlite3.connect(ledger.path)) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('BEGIN')
        writer.execute('SELECT count(*) FROM operation').fetchone()
        ledger.reserve('wal-only', approval, request.fingerprint(profile), 1)
        ledger.finish('wal-only')
        # An open connection keeps the committed WAL alive. The stale main file
        # alone cannot be treated as a complete operations ledger.
        assert Path(str(ledger.path) + '-wal').stat().st_size > 0
        assert writer.execute("SELECT 1 FROM operation WHERE id='wal-only'").fetchone() is None
        folder = tmp_path / 'wal-backup'
        digest = create_backup(app, folder)
        target = restore_backup(folder, tmp_path / 'wal-restored', digest)
        actual = BudgetLedger(target / LEDGER_FILENAME).report('job')
        assert next(row for row in actual if row['id'] == 'wal-only')['state'] == 'uncertain'
        assert (folder / LEDGER_FILENAME).read_bytes()[18:20] == b'\x01\x01'
        assert not list(folder.glob('api-operations.sqlite-*'))


def test_snapshot_during_paid_job_retains_reservation_while_completion_can_commit(
        provider_library, tmp_path, monkeypatch):
    app, _, profile, request, approval, ledger, vault = provider_library
    sent, release = Event(), Event()

    class Transport:
        def post(self, *args, **kwargs):
            sent.set()
            if not release.wait(10):
                raise AssertionError('Test did not release synthetic response')
            return b'{"choices":[{"message":{"content":"Synthetic complete result"}}]}'

    live_approval = replace(approval, job_id='in-flight')
    gateway = ProviderGateway(ledger, vault=vault, transport=Transport())
    original_snapshot = library_backup.snapshot_provider_files
    with ThreadPoolExecutor(max_workers=1) as executor:
        operation = executor.submit(gateway.perform, profile, request, approval=live_approval,
                                    operation_id='in-flight-operation', estimated_micros=75)
        try:
            assert sent.wait(5)

            def snapshot_then_complete(root):
                files = original_snapshot(root)
                release.set()
                # create_backup still owns the root writer lock here. Completion
                # must not wait for the large originals copy or lose its result.
                assert b'Synthetic complete result' in operation.result(timeout=5)
                return files

            monkeypatch.setattr(library_backup, 'snapshot_provider_files', snapshot_then_complete)
            folder = tmp_path / 'active-backup'
            digest = create_backup(app, folder)
        finally:
            release.set()
    assert ledger.report('in-flight')[0]['state'] == 'complete'
    target = restore_backup(folder, tmp_path / 'active-restored', digest)
    restored = BudgetLedger(target / LEDGER_FILENAME)
    assert restored.report('in-flight')[0]['state'] == 'reserved'
    with pytest.raises(ProviderError, match='already recorded'):
        restored.reserve('in-flight-operation', live_approval, request.fingerprint(profile), 75)
    with pytest.raises(ProviderError, match='budget'):
        restored.reserve('must-not-retry', live_approval, request.fingerprint(profile), 75)


def test_uncommitted_ledger_transaction_is_not_partially_exported(provider_library, tmp_path):
    app, _, _, _, _, ledger, _ = provider_library
    folder = tmp_path / 'transaction-backup'
    with closing(sqlite3.connect(ledger.path)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('INSERT INTO approval VALUES(?,?)', ('pending-job', 'a' * 64))
        writer.execute('INSERT INTO operation VALUES(?,?,?,?,?,NULL)',
                       ('pending-operation', 'pending-job', 'b' * 64, 5, 'reserved'))
        digest = create_backup(app, folder)
        writer.commit()
    target = restore_backup(folder, tmp_path / 'transaction-restored', digest)
    assert BudgetLedger(target / LEDGER_FILENAME).report('pending-job') == []
    assert ledger.report('pending-job')[0]['state'] == 'reserved'
    with closing(sqlite3.connect(target / LEDGER_FILENAME)) as db:
        assert db.execute("SELECT 1 FROM approval WHERE id='pending-job'").fetchone() is None


def test_profile_writer_cannot_change_configuration_halfway_through_snapshot(
        provider_library, tmp_path, monkeypatch):
    app, store, profile, *_ = provider_library
    original_snapshot = library_backup.snapshot_provider_files
    started = Event()

    def update_profile():
        started.set()
        return store.save(replace(profile, name='Changed after the snapshot'))

    with ThreadPoolExecutor(max_workers=1) as executor:
        tasks = []

        def snapshot_while_writer_waits(root):
            pending = executor.submit(update_profile)
            tasks.append(pending)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                pending.result(timeout=0.05)
            return original_snapshot(root)

        monkeypatch.setattr(library_backup, 'snapshot_provider_files', snapshot_while_writer_waits)
        folder = tmp_path / 'coherent-backup'
        create_backup(app, folder)
        assert tasks[0].result(timeout=5).name == 'Changed after the snapshot'
    exported = providers.validate_backup_profiles((folder / PROFILES_FILENAME).read_bytes())
    assert exported[0].name == profile.name
    assert store.profiles()[0].name == 'Changed after the snapshot'


@pytest.mark.parametrize('sql', [
    "UPDATE approval SET digest='not-a-digest'",
    'UPDATE approval SET id=NULL',
    "UPDATE operation SET id=NULL WHERE id='reserved'",
    "UPDATE operation SET job='missing' WHERE id='reserved'",
    "UPDATE operation SET request_hash='private-path' WHERE id='reserved'",
    "UPDATE operation SET estimate_micros=-1 WHERE id='reserved'",
    "UPDATE operation SET estimate_micros=0.5 WHERE id='reserved'",
    "UPDATE operation SET estimate_micros=1000000000001 WHERE id='reserved'",
    "UPDATE operation SET estimate_micros=1000000000000 WHERE id='reserved'",
    "UPDATE operation SET state='retryable' WHERE id='reserved'",
    "UPDATE operation SET state='complete' WHERE id='reserved'",
    "UPDATE operation SET output_sha256='bad' WHERE id='complete'",
    "UPDATE operation SET state='uncertain' WHERE id='complete'",
    "CREATE TABLE secret (value TEXT)",
    "CREATE VIEW secret AS SELECT * FROM operation",
    "CREATE TRIGGER hidden AFTER UPDATE ON operation BEGIN SELECT 1; END",
    "CREATE INDEX unannounced ON operation(job)",
    'PRAGMA user_version=1',
])
def test_resigned_ledger_with_invalid_schema_or_state_never_publishes_restore(
        provider_library, tmp_path, sql):
    app, *_ = provider_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    with closing(sqlite3.connect(folder / LEDGER_FILENAME)) as db:
        db.execute(sql)
        db.commit()
    digest = resign(folder)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='provider'):
        restore_backup(folder, target, digest)
    assert not target.exists()


@pytest.mark.parametrize('damage', ['credential', 'extra-secret', 'path', 'duplicate-profile',
                                    'duplicate-key', 'boolean-version', 'endpoint-key'])
def test_resigned_profiles_cannot_restore_credentials_unknown_fields_or_ambiguous_json(
        provider_library, tmp_path, damage):
    app, *_ = provider_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    path = folder / PROFILES_FILENAME
    data = json.loads(path.read_bytes())
    if damage == 'credential':
        data['profiles'][0]['credential_ref'] = 'url-api-' + '1' * 32
    elif damage == 'extra-secret':
        data['profiles'][0]['api_key'] = 'must-never-be-in-settings'
    elif damage == 'path':
        data['profiles'][0]['output_dir'] = str(tmp_path / 'outside')
    elif damage == 'duplicate-profile':
        data['profiles'].append(data['profiles'][0])
    elif damage == 'boolean-version':
        data['version'] = True
    elif damage == 'endpoint-key':
        data['profiles'][0]['endpoint'] = 'https://example.test/v1?api_key=private'
    raw = json.dumps(data).encode()
    if damage == 'duplicate-key':
        raw = b'{"version":1,' + raw[1:]
    path.write_bytes(raw)
    digest = resign(folder)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='provider'):
        restore_backup(folder, target, digest)
    assert not target.exists()


@pytest.mark.parametrize('name', [PROFILES_FILENAME, LEDGER_FILENAME, LEDGER_FILENAME + '-wal'])
@pytest.mark.parametrize('damage', ['symlink', 'hardlink', 'directory'])
def test_live_provider_files_and_sqlite_journals_cannot_follow_other_files(
        provider_library, tmp_path, name, damage):
    app, *_ = provider_library
    path = app.root / name
    raw = path.read_bytes() if path.exists() else b'Untrusted journal'
    path.unlink(missing_ok=True)
    if damage == 'directory':
        path.mkdir()
    else:
        external = tmp_path / 'external-file'
        external.write_bytes(raw)
        if damage == 'symlink':
            # Like the existing reader-backup gate, require an actual link.
            # A runner without link capability must fail this acceptance check,
            # rather than turn a required filesystem boundary into a skip.
            path.symlink_to(external)
        else:
            os.link(external, path)
    folder = tmp_path / 'rejected'
    with pytest.raises((OSError, ValueError)):
        create_backup(app, folder)
    assert not folder.exists()


def test_orphan_ledger_journal_is_not_silently_excluded(provider_library, tmp_path):
    app, _, _, _, _, ledger, _ = provider_library
    ledger.path.unlink()
    Path(str(ledger.path) + '-journal').write_bytes(b'Unrecovered ledger state')
    with pytest.raises(ProviderError, match='orphaned'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('damage', ['version3', 'remote-mode', 'missing-mode', 'unannounced'])
def test_provider_manifest_version_and_local_only_policy_are_strict(provider_library, tmp_path, damage):
    app, *_ = provider_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = read_manifest(folder)
    if damage == 'version3':
        manifest['version'] = 3
        del manifest['provider_restore_mode']
    elif damage == 'remote-mode':
        manifest['provider_restore_mode'] = 'APPROVED_REMOTE'
    elif damage == 'missing-mode':
        del manifest['provider_restore_mode']
    else:
        manifest['sidecars'] = [row for row in manifest['sidecars'] if row['filename'] != LEDGER_FILENAME]
    digest = resign(folder, manifest=manifest)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(folder, target, digest)
    assert not target.exists()


@pytest.mark.parametrize('name', [PROFILES_FILENAME, LEDGER_FILENAME])
def test_changed_provider_bytes_do_not_restore_under_an_unchanged_manifest(
        provider_library, tmp_path, name):
    app, *_ = provider_library
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    path = folder / name
    path.write_bytes(path.read_bytes() + b' ')
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='sidecar'):
        restore_backup(folder, target, digest)
    assert not target.exists()


def test_external_audio_projects_and_credentials_are_not_followed(provider_library, tmp_path):
    app, *_ = provider_library
    external = tmp_path / 'External audio'
    external.mkdir()
    (external / 'credential.txt').write_text('Private unrelated value')
    (app.root / 'unregistered-audio-project.json').write_text(
        json.dumps({'output_dir': str(external)}), encoding='utf-8')
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    assert set(p.name for p in folder.iterdir()) == {
        'library.sqlite', 'originals', 'manifest.json', PROFILES_FILENAME, LEDGER_FILENAME}
    assert b'Private unrelated value' not in (folder / LEDGER_FILENAME).read_bytes()


def test_deleted_ledger_page_content_does_not_enter_backup(provider_library, tmp_path):
    app, _, _, _, _, ledger, _ = provider_library
    marker = 'SYNTHETIC_DELETED_PRIVATE_PAYLOAD_' * 1000
    with closing(sqlite3.connect(ledger.path)) as db:
        db.execute('PRAGMA secure_delete=OFF')
        db.execute('INSERT INTO approval VALUES(?,?)', ('deleted', marker))
        db.commit()
        db.execute("DELETE FROM approval WHERE id='deleted'")
        db.commit()
    assert marker[:80].encode() in ledger.path.read_bytes()
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    raw = (folder / LEDGER_FILENAME).read_bytes()
    assert marker[:80].encode() not in raw
    validate_ledger_snapshot(raw)


def test_invalid_live_ledger_and_resource_limits_abort_without_publishing(
        provider_library, tmp_path, monkeypatch):
    app, _, _, _, _, ledger, _ = provider_library
    monkeypatch.setattr(providers, 'MAX_LEDGER_ROWS', 3)
    with pytest.raises(ProviderError, match='record budget'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    monkeypatch.setattr(providers, 'MAX_LEDGER_ROWS', 100000)
    monkeypatch.setattr(providers, 'MAX_LEDGER_BYTES', ledger.path.stat().st_size - 1)
    with exclusive_writer(app.root), pytest.raises(ProviderError, match='resource budget'):
        snapshot_provider_files(app.root)


def test_ledger_without_profiles_retains_history_and_no_overwrite(provider_library, tmp_path):
    app, store, _, _, _, ledger, _ = provider_library
    store.path.unlink()
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    assert [item['filename'] for item in read_manifest(folder)['sidecars']] == [LEDGER_FILENAME]
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert not (target / PROFILES_FILENAME).exists()
    assert BudgetLedger(target / LEDGER_FILENAME).report('job') == ledger.report('job')
    before = (target / LEDGER_FILENAME).read_bytes()
    with pytest.raises(FileExistsError):
        restore_backup(folder, target, digest)
    with pytest.raises(FileExistsError):
        create_backup(app, folder)
    assert (target / LEDGER_FILENAME).read_bytes() == before


def test_profiles_without_prior_calls_still_use_disconnected_v4_backup(provider_library, tmp_path):
    app, store, _, _, _, ledger, _ = provider_library
    ledger.path.unlink()
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = read_manifest(folder)
    assert manifest['version'] == 4
    assert [item['filename'] for item in manifest['sidecars']] == [PROFILES_FILENAME]
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert not (target / LEDGER_FILENAME).exists()
    assert ProfileStore(target / PROFILES_FILENAME).profiles() == tuple(
        replace(profile, credential_ref=None) for profile in store.profiles())


def test_provider_stage_failure_preserves_live_state_and_never_claims_backup_complete(
        provider_library, tmp_path, monkeypatch):
    app, store, _, _, _, ledger, _ = provider_library
    before = store.path.read_bytes(), ledger.path.read_bytes()
    sync_file = library_backup._sync_file

    def disk_full(path, raw):
        if path.name == LEDGER_FILENAME:
            raise OSError('Synthetic disk full')
        return sync_file(path, raw)

    monkeypatch.setattr(library_backup, '_sync_file', disk_full)
    target = tmp_path / 'rejected'
    with pytest.raises(OSError, match='disk full'):
        create_backup(app, target)
    assert not target.exists()
    assert (store.path.read_bytes(), ledger.path.read_bytes()) == before


@pytest.mark.parametrize('with_provider_state', [False, True])
def test_progress_preferences_roundtrip_as_v4_with_or_without_provider_files(
        provider_library, tmp_path, with_provider_state):
    app, store, _, _, _, ledger, _ = provider_library
    if not with_provider_state:
        store.path.unlink()
        ledger.path.unlink()
    settings = ProgressSettings(mode='beep', volume=35, interval_seconds=9,
                                muted=True, background=False)
    ProgressSettingsStore(app.root).save(settings)
    before = (app.root / PROGRESS_SETTINGS).read_bytes()
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = read_manifest(folder)
    assert manifest['version'] == 4
    assert manifest['provider_restore_mode'] == 'LOCAL_ONLY'
    assert any(row['filename'] == PROGRESS_SETTINGS for row in manifest['sidecars'])
    assert (folder / PROGRESS_SETTINGS).read_bytes() == before
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert ProgressSettingsStore(target).load() == settings
    assert (target / PROGRESS_SETTINGS).stat().st_nlink == 1
    if not with_provider_state:
        assert not (target / PROFILES_FILENAME).exists()
        assert not (target / LEDGER_FILENAME).exists()


@pytest.mark.parametrize('damage', ['unknown-field', 'boolean-volume', 'duplicate-key', 'version3'])
def test_progress_preferences_require_valid_schema_and_v4_manifest(provider_library, tmp_path, damage):
    app, *_ = provider_library
    if damage == 'version3':
        (app.root / PROFILES_FILENAME).unlink()
        (app.root / LEDGER_FILENAME).unlink()
    ProgressSettingsStore(app.root).save(ProgressSettings())
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    path = folder / PROGRESS_SETTINGS
    payload = json.loads(path.read_bytes())
    if damage == 'unknown-field':
        payload['audio_file'] = str(tmp_path / 'external.wav')
    elif damage == 'boolean-volume':
        payload['volume'] = True
    raw = json.dumps(payload).encode()
    if damage == 'duplicate-key':
        raw = b'{"mode":"off",' + raw[1:]
    path.write_bytes(raw)
    manifest = read_manifest(folder)
    if damage == 'version3':
        manifest['version'] = 3
        del manifest['provider_restore_mode']
    digest = resign(folder, manifest=manifest)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(folder, target, digest)
    assert not target.exists()


def test_linked_progress_preferences_cannot_enter_backup(provider_library, tmp_path):
    app, *_ = provider_library
    ProgressSettingsStore(app.root).save(ProgressSettings())
    os.link(app.root / PROGRESS_SETTINGS, tmp_path / 'external-preferences.json')
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
