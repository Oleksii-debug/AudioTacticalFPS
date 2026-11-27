"""Version-four complete recovery uses real SQLite state and retained bytes."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.agent import LocalAgent, Plan, Step
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import prepare_request, record_progress
from carr_library.library_backup import create_backup, restore_backup
from carr_library.providers import ProviderManager, ProviderProfile
from carr_library.studio import ProjectStore, narration_project


class NoVault:
    def __getattr__(self, name):
        raise AssertionError('Backups must never access the operating-system credential vault.')


@pytest.fixture
def full_state(tmp_path):
    app = LibraryApp(tmp_path / 'Дані úlohy')
    workspace = app.create_workspace('Existing library')
    source = app.add_text(workspace, 'Unchanged source', 'Exact retained original words.')
    identity = uuid4().hex
    project = ProjectStore(app.root / 'audio-projects' / identity)
    authored = 'Exact authored text.\r\nУкраїнська e\u0301.\r\n'
    plan = narration_project(authored, 'fixture:voice')
    revision = project.save(plan)
    providers = ProviderManager(app.root / 'provider-settings', vault=NoVault())
    profile = ProviderProfile('example', 'Explicit example', 'llm', 'https://example.com/v1',
        'selected-model', enabled=True, requires_key=False, request_budget_cents=3, total_budget_cents=20)
    providers.save(profile)
    providers.set_mode('HYBRID')
    with sqlite3.connect(providers.database) as db:
        db.execute('UPDATE profiles SET spent_cents=3,credential_ref=?', ('a' * 64 + '/example/' + 'b' * 32,))
        db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)', ('c' * 64, profile.id, 'reserved', 3, None, 1.0))
    agent = LocalAgent(app, app.root / 'agent-jobs', workspaces=(workspace,))
    write = Plan('Create a topic explicitly', (Step('topic', 'research.create_topic', {'workspace': workspace, 'name': 'Saved topic'}),))
    agent.run(write, approval_sha256=write.digest)
    return app, workspace, source, identity, project, plan, revision, providers, agent, write


def read_manifest(folder):
    return json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))


def reseal(folder, *, repair_records=False):
    manifest = read_manifest(folder)
    if repair_records:
        for row in manifest['recovery']['files']:
            data = (folder / 'recovery' / row['path']).read_bytes()
            row.update(size_bytes=len(data), sha256=sha256(data).hexdigest())
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def test_full_library_backup_restores_real_studio_profiles_and_agent_without_automatic_approval(full_state, tmp_path):
    app, workspace, source, identity, project, plan, revision, providers, agent, write = full_state
    database_before = project.database.read_bytes()
    folder = tmp_path / 'full backup'
    digest = create_backup(app, folder)
    manifest = read_manifest(folder)
    assert manifest['version'] == 4
    assert manifest['recovery']['projects'] == [dict(id=identity, manifest_sha256=
        sha256((folder / 'recovery/projects' / identity / 'manifest.json').read_bytes()).hexdigest())]
    assert project.database.read_bytes() == database_before
    destination = restore_backup(folder, tmp_path / 'Нове місце čítanie', digest)
    restored = LibraryApp(destination)
    assert restored.search(workspace, 'retained')[0].quote == app.search(workspace, 'retained')[0].quote
    recovered = ProjectStore(destination / 'audio-projects' / identity)
    assert recovered.load() == plan and recovered.load().authored_script == plan.authored_script
    assert (recovered.revisions / (revision + '.json')).read_bytes() == (project.revisions / (revision + '.json')).read_bytes()
    connected = ProviderManager(destination / 'provider-settings', vault=NoVault())
    assert connected.mode == 'LOCAL_ONLY' and not connected.profiles()[0].enabled
    with sqlite3.connect(connected.database) as db:
        assert db.execute('SELECT spent_cents,credential_ref FROM profiles').fetchone() == (3, None)
        assert db.execute('SELECT state,reserved_cents FROM requests').fetchone() == ('uncertain', 3)
    recovered_agent = LocalAgent(restored, destination / 'agent-jobs', workspaces=(workspace,))
    with pytest.raises(ValueError, match='interrupted or failed receipt'):
        recovered_agent.run(write, approval_sha256=write.digest)
    with sqlite3.connect(recovered_agent.database) as db:
        state, result = db.execute('SELECT state,result FROM receipts').fetchone()
        assert state == 'needs_review' and result
    assert not (destination / '.restore-incomplete').exists()


def test_scoped_agent_backup_excludes_independent_studio_provider_and_agent_state(full_state, tmp_path):
    app, workspace, *_ = full_state
    folder = tmp_path / 'scoped'
    digest = create_backup(app, folder, authorized_workspaces={workspace})
    manifest = read_manifest(folder)
    assert manifest['version'] in (2, 3) and 'recovery' not in manifest
    target = restore_backup(folder, tmp_path / 'scoped-restored', digest)
    assert not any((target / path).exists() for path in ('audio-projects', 'provider-settings', 'agent-jobs'))


@pytest.mark.parametrize('windows_binding', [False, True])
def test_pending_import_retains_original_and_resumes_after_external_file_disappears(tmp_path, windows_binding):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pending work')
    external = tmp_path / 'input.txt'
    external.write_text('The pending source remains exact.\fAnother segment.', encoding='utf-8')
    request = prepare_request(app.root, workspace, external,
        dict(title='Pending text', authors=[], language=None, edition=None,
             volume=None, collections=[], work_id=None, edition_id=None), Config())
    result = app._import_file(workspace, external, title='Pending text',
        work_id=request['metadata']['work_id'], edition_id=request['metadata']['edition_id'],
        _request_id=request['request_id'], cancelled=lambda: True)
    record_progress(app.root, request['request_id'], state=result['state'], result=result)
    assert result['state'] == 'cancelled' and not app.search(workspace, 'pending')
    folder = tmp_path / 'pending backup'
    digest = create_backup(app, folder)
    manifest = read_manifest(folder)
    pending = [entry for entry in manifest['recovery']['files'] if entry['path'].startswith('pending-originals/')]
    assert len(pending) == 1 and 'imports.sqlite' in {row['path'] for row in manifest['recovery']['files']}
    if windows_binding:
        source_root = r'C:\Users\Example\Research Library'
        manifest['recovery']['source_root'] = source_root
        (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        path = folder / 'recovery/import-requests' / (request['request_id'] + '.json')
        value = json.loads(path.read_text(encoding='utf-8'))
        value.update(data_dir=source_root, source=r'C:\Original books\input.txt')
        path.write_text(json.dumps(value), encoding='utf-8')
        digest = reseal(folder, repair_records=True)
    external.unlink()
    restored = LibraryApp(restore_backup(folder, tmp_path / 'resumed', digest))
    result = restored.resume_import(workspace, request['request_id'], timeout=30)
    assert result['state'] == 'complete', result
    assert restored.search(workspace, 'The pending source remains exact.')[0].quote == 'The pending source remains exact.'


@pytest.mark.parametrize('damage', ['bytes', 'missing', 'extra', 'symlink', 'hardlink', 'traversal', 'bool-size', 'schema', 'profile-secret'])
def test_corrupt_recovery_never_publishes_a_destination(full_state, tmp_path, damage):
    app, *_ = full_state
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    target = folder / 'recovery/provider-settings/providers.sqlite'
    if damage == 'bytes':
        target.write_bytes(target.read_bytes() + b'bad')
    elif damage == 'missing':
        target.unlink()
    elif damage == 'extra':
        (folder / 'recovery/extra.txt').write_text('not admitted')
    elif damage in ('symlink', 'hardlink'):
        moved = tmp_path / 'outside.sqlite'
        target.rename(moved)
        target.symlink_to(moved) if damage == 'symlink' else os.link(moved, target)
    elif damage in ('traversal', 'bool-size'):
        manifest = read_manifest(folder)
        manifest['recovery']['files'][0]['path' if damage == 'traversal' else 'size_bytes'] = '../escape' if damage == 'traversal' else True
        (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        digest = reseal(folder)
    else:
        with sqlite3.connect(target) as db:
            if damage == 'schema':
                db.execute('CREATE TABLE unexpected(secret TEXT)')
            else:
                raw = json.loads(db.execute('SELECT document FROM profiles').fetchone()[0])
                raw['api_key'] = 'must-not-be-admitted'
                db.execute('UPDATE profiles SET document=?', (json.dumps(raw),))
        digest = reseal(folder, repair_records=True)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises((ValueError, TypeError, OSError, sqlite3.Error)):
        restore_backup(folder, destination, digest)
    assert not destination.exists()


def test_uncommitted_provider_change_is_not_copied(full_state, tmp_path):
    app, *_, providers, agent, write = full_state
    with sqlite3.connect(providers.database) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('UPDATE profiles SET spent_cents=19')
        folder = tmp_path / 'snapshot'
        digest = create_backup(app, folder)
        writer.rollback()
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    with sqlite3.connect(restored / 'provider-settings/providers.sqlite') as db:
        assert db.execute('SELECT spent_cents FROM profiles').fetchone()[0] == 3


def test_active_studio_job_is_restored_interrupted_and_authored_revision_is_exact(full_state, tmp_path):
    app, _, _, identity, project, plan, revision, *_ = full_state
    with project.connect() as db:
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', ('d' * 32, revision, 'running', '{}', None, None, 1.0))
    folder = tmp_path / 'active'
    digest = create_backup(app, folder)
    restored = restore_backup(folder, tmp_path / 'recovery', digest)
    with sqlite3.connect(restored / 'audio-projects' / identity / 'studio.sqlite') as db:
        assert db.execute('SELECT state FROM jobs').fetchone() == ('interrupted',)
    assert ProjectStore(restored / 'audio-projects' / identity).load() == plan


def test_full_backup_relocates_real_render_results_and_reuses_accepted_pcm(full_state, tmp_path):
    from carr_library.studio.director import Director
    app, _, _, identity, project, plan, *_ = full_state
    calls = []
    def synthetic_speech(request, target, cancelled=None):
        calls.append(request.text)
        with wave.open(str(target), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'synthetic fixture'))
            output.writeframes(b'\x00\x10\x00\xf0' * 1200)
        return dict(path=str(target), sha256=sha256(target.read_bytes()).hexdigest(),
                    frames=2400, sample_rate=24000, channels=1, duration_seconds=.1,
                    engine_fingerprint='recovery-synthetic-v1')
    original = Director(project, speech_renderer=synthetic_speech,
        engine_identity=lambda voice: 'recovery-synthetic-v1').render(plan)
    folder = tmp_path / 'rendered-backup'
    digest = create_backup(app, folder)
    restored = restore_backup(folder, tmp_path / 'different-root', digest)
    store = ProjectStore(restored / 'audio-projects' / identity)
    with sqlite3.connect(store.database) as db:
        result = json.loads(db.execute("SELECT result FROM jobs WHERE state='complete'").fetchone()[0])
        assert Path(result['master']['path']).is_relative_to(store.root)
        assert Path(result['master']['path']).is_file()
    count = len(calls)
    resumed = Director(store, speech_renderer=synthetic_speech,
        engine_identity=lambda voice: 'recovery-synthetic-v1').render(store.load())
    assert len(calls) == count
    assert resumed['master']['sha256'] == original['master']['sha256']


def test_component_budget_fails_before_final_destination(full_state, tmp_path, monkeypatch):
    from carr_library import recovery_backup
    app, *_ = full_state
    monkeypatch.setattr(recovery_backup, 'MAX_BYTES', 1)
    folder = tmp_path / 'too-small'
    with pytest.raises(ValueError, match='budget'):
        create_backup(app, folder)
    assert not folder.exists()


def test_library_scope_does_not_authorize_an_unbound_private_narration(full_state, tmp_path):
    from carr_library.reader.narration import NarrationStore, new_project
    app, workspace, *_ = full_state
    independent = new_project('Unrelated private authored script.', title='Outside the selected library')
    NarrationStore(app.root).save(independent)
    full = tmp_path / 'all-authorized'
    create_backup(app, full)
    assert any(row['filename'] == f'narration-projects/{independent.id}.json'
               for row in read_manifest(full)['sidecars'])
    scoped = tmp_path / 'library-only'
    create_backup(app, scoped, authorized_workspaces={workspace})
    assert all(not row['filename'].startswith('narration-projects/')
               for row in read_manifest(scoped).get('sidecars', []))


def test_busy_import_receipt_is_not_copied_as_a_consistent_snapshot(tmp_path):
    from filelock import FileLock
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Queued work')
    source = tmp_path / 'source.txt'
    source.write_text('Original queue input.', encoding='utf-8')
    request = prepare_request(app.root, workspace, source,
        dict(title='Pending text', authors=[], language=None, edition=None,
             volume=None, collections=[], work_id=None, edition_id=None), Config())
    with FileLock(app.root / 'import-requests' / (request['request_id'] + '.lock')):
        target = tmp_path / 'busy-backup'
        with pytest.raises(ValueError, match='retry the backup'):
            create_backup(app, target)
        assert not target.exists()


def test_legitimate_provider_credential_column_migration_remains_recoverable(full_state, tmp_path):
    app, *_, providers, agent, write = full_state
    with sqlite3.connect(providers.database) as db:
        row = db.execute('SELECT id,document,spent_cents FROM profiles').fetchone()
        db.execute('DROP TABLE profiles')
        db.execute('CREATE TABLE profiles(id TEXT PRIMARY KEY,document TEXT NOT NULL,spent_cents INTEGER NOT NULL DEFAULT 0)')
        db.execute('INSERT INTO profiles VALUES(?,?,?)', row)
    ProviderManager(providers.root, vault=NoVault())  # Actual supported ALTER migration.
    folder = tmp_path / 'legacy-providers'
    digest = create_backup(app, folder)
    restored = restore_backup(folder, tmp_path / 'migrated-restore', digest)
    manager = ProviderManager(restored / 'provider-settings', vault=NoVault())
    manager.save(replace(manager.profiles()[0], name='Reviewed after restoring'))
    assert manager.profiles()[0].name == 'Reviewed after restoring'
