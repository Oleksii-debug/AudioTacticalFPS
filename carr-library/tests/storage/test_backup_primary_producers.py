"""Real producer checkpoints and SQLite backups; no engine or network use."""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.credentials import WindowsCredentialVault
from carr_library.assistant.profiles import ProfileStore, ProviderProfile
from carr_library.assistant.providers import Authorization, UsageLedger
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech import ReadingDocument, Voice
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.studio.render import ProductionService


class InventoryOnly:
    """Submitting a typed queued job needs inventory, not speech generation."""
    def voices(self):
        return (Voice('test:inventory', 'Inventory fixture', 'test', 'en'),)


@pytest.fixture
def produced(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Preserve producer data')
    app.add_text(workspace, 'Source', 'Original exact words.', edition_id='source')
    reader = ReaderDraftStore(app.root)
    document = ReadingDocument('Original exact words.')
    document.replace(0, 8, 'Reading')
    reader.save(ReaderResume(document, 'test:inventory', cursor=3))
    profiles = ProfileStore(app.root / 'state/providers')
    profile = ProviderProfile('fixture', 'Synthetic reference only', 'openai-compatible',
        'https://example.invalid/v1', 'model-name', credential_ref='carr/provider/fixture/' + 'b' * 32)
    profiles.save(profile)
    ledger = UsageLedger(app.root / 'state/providers/usage')
    authorization = Authorization('envelope', 'task', ((profile.id, profile.endpoint),),
        ('llm.chat',), ('synthetic-test',), '1.00', 3, 2000000000)
    ledger.reserve(authorization, 'operation', 'c' * 64, Decimal('0.20'), profile, 'llm.chat')
    ledger.finish('envelope', 'operation', status='uncertain')
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    studio = ProductionService(app.root / 'state/studio', voices=InventoryOnly())
    project = studio.create('Narrator: Preserve the queued script.', voice_ids=('test:inventory',))
    job = studio.submit(project, outputs)
    studio.request_pause(job)
    assistant = AssistantService(app)
    plan = Plan(Scope(workspace_id=workspace, output_root=outputs), (Step('library.list'),))
    assistant_job = assistant.submit(plan)
    assert assistant.run(assistant_job).status == 'complete'
    assistant.pause(assistant_job)
    return app, reader, profile, studio, job, assistant_job


def resign(folder, manifest):
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.mark.parametrize('version', [3, 4])
def test_all_primary_producers_roundtrip_without_activation(produced, tmp_path, monkeypatch, version):
    app, reader, profile, studio, job, assistant_job = produced
    def forbidden(*a, **k):
        pytest.fail('Backup must not open external assets, credentials or engines.')
    from carr_library.assistant import models
    from carr_library import runtime_settings
    monkeypatch.setattr(models, 'local_path', forbidden)
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)
    monkeypatch.setattr(WindowsCredentialVault, '_native', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    names = {entry['filename'] for entry in manifest['sidecars']}
    assert len(names) == 8
    assert 'state/providers/envelope.usage.json' not in names
    assert 'state/providers/usage/envelope.usage.json' in names
    assert not any(name.endswith('.lock') for name in names)
    if version == 3:
        manifest['version'] = 3
        manifest['state'] = [{**entry, 'filename': entry['filename'].removeprefix('state/')}
                             for entry in manifest.pop('sidecars')]
        checksum = resign(folder, manifest)
    before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    target = restore_backup(folder, tmp_path / 'restored', checksum)
    for name in names:
        assert (target / name).read_bytes() == (app.root / name).read_bytes()
    assert {p.relative_to(folder): p.read_bytes() for p in folder.rglob('*') if p.is_file()} == before
    restored = ReaderDraftStore(target).load()
    assert restored.document.text == 'Reading exact words.'
    assert restored.document.undo() and restored.document.text == 'Original exact words.'
    assert not (target / 'state/studio/cache').exists()
    # No retargeting of scopes or external outputs is authorized by restore.
    record = json.loads((target / f'state/assistant/{assistant_job}.json').read_bytes())
    assert record['data_dir'] == str(app.root)
    assert record['plan_sha256'] == sha256(models.canonical(record['plan']).encode()).hexdigest()


@pytest.mark.parametrize('kind', ['reader-source', 'profile-identity', 'profile-secret', 'studio-project',
    'studio-format', 'studio-node', 'studio-control', 'assistant-plan', 'assistant-scope',
    'assistant-control', 'usage-status', 'usage-cost'])
def test_resigned_tampering_is_rejected_before_destination(produced, tmp_path, kind):
    app, reader, profile, studio, job, assistant_job = produced
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    names = {
        'reader': reader.path.relative_to(app.root).as_posix(),
        'profile': 'state/providers/fixture.profile.json',
        'studio': f'state/studio/jobs/{job}/job.json',
        'assistant': f'state/assistant/{assistant_job}.json',
        'usage': 'state/providers/usage/envelope.usage.json',
    }
    name = names[kind.split('-')[0]]
    if kind == 'studio-control': name = f'state/studio/jobs/{job}/control.json'
    if kind == 'assistant-control': name = f'state/assistant/{assistant_job}.control.json'
    path = folder / name
    value = json.loads(path.read_bytes())
    if kind == 'reader-source': value['document']['source_text'] = 'Forged source'
    elif kind == 'profile-identity': value['id'] = 'other'
    elif kind == 'profile-secret': value['api_key'] = 'synthetic-marker'
    elif kind == 'studio-project': value['project']['title'] = 'Changed project'
    elif kind == 'studio-format': value['format'] = '../escape'
    elif kind == 'studio-node': value['nodes']['invalid'] = {}
    elif kind == 'studio-control': value['action'] = 'exec'
    elif kind == 'assistant-plan': value['plan']['steps'][0]['tool'] = 'shell'
    elif kind == 'assistant-scope': value['workspace'] = 'another-library'
    elif kind == 'assistant-control': value['command'] = 'exec'
    elif kind == 'usage-status': value['operations']['operation']['status'] = 'ignored'
    elif kind == 'usage-cost': value['operations']['operation']['reserved_cost'] = '-1'
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    entry = next(row for row in manifest['sidecars'] if row['filename'] == name)
    entry.update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    with pytest.raises((ValueError, PermissionError)):
        restore_backup(folder, tmp_path / 'rejected', resign(folder, manifest))
    assert not (tmp_path / 'rejected').exists()


def test_external_missing_windows_paths_are_passive_checkpoint_data(produced, tmp_path):
    app, _, _, studio, job, assistant_job = produced
    job_path = studio.jobs_root / job / 'job.json'
    value = json.loads(job_path.read_bytes())
    value['destination'] = r'C:\Missing installation\outputs'
    job_path.write_text(json.dumps(value))
    from carr_library.assistant.models import canonical
    path = app.root / f'state/assistant/{assistant_job}.json'
    value = json.loads(path.read_bytes())
    value['plan']['scope']['output_root'] = r'D:\Missing reviewed folder'
    value['plan']['scope']['input_roots'] = [r'C:\Missing reviewed input']
    value['plan_sha256'] = sha256(canonical(value['plan']).encode()).hexdigest()
    path.write_text(json.dumps(value))
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    target = restore_backup(backup, tmp_path / 'restored', checksum)
    assert (target / path.relative_to(app.root)).read_bytes() == path.read_bytes()


@pytest.mark.parametrize('name', ['state/providers/credentials.json', 'state/assistant/unsafe.json',
                                 'state/speech/unregistered.json'])
def test_unrecognized_private_state_remains_visible_failure(produced, tmp_path, name):
    app, *_ = produced
    (app.root / name).write_text('{"secret": "synthetic-marker"}')
    with pytest.raises(ValueError, match='validated backup adapter'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_actual_progress_preferences_roundtrip_without_notifications(tmp_path):
    from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Progress preferences')
    path = app.root / 'progress-preferences.json'
    store = ProgressPreferenceStore(path)
    preferences = ProgressPreferences(mode='beep', volume=15, muted=True)
    store.save(preferences)
    raw = path.read_bytes()
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    target = restore_backup(backup, tmp_path / 'restored', checksum)
    assert (target / path.name).read_bytes() == raw
    assert ProgressPreferenceStore(target / path.name).load() == preferences


@pytest.mark.parametrize('preferences', [{'mode': 'run-shell'}, {'volume': True}, {'interval_seconds': 0}])
def test_invalid_progress_settings_cannot_enter_backup(tmp_path, preferences):
    from dataclasses import asdict
    from carr_library.progress_preferences import ProgressPreferences
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Progress preferences')
    value = {'version': 1, 'preferences': {**asdict(ProgressPreferences()), **preferences}}
    (app.root / 'progress-preferences.json').write_text(json.dumps(value))
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
