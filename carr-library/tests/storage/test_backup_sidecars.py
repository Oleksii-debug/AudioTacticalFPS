"""Real full-library recovery of reading, audio and durable import sidecars."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_sidecars as sidecars
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.audio.projects import ProjectStore, parse_script, read_json, atomic_json
from carr_library.audio.director import ProductionDirector
from carr_library.reader.reading_state import ReadingLocation, ReadingState
from carr_library.assistant.providers import ConnectionStore, ProviderProfile
from carr_library.assistant.workflows import LocalAssistant, Scope, Step, WorkflowPlan


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('Research')
    app.add_text(workspace, 'Source', 'Original railway quotation.')
    return app, workspace


def reading(app, workspace):
    hit, = app.search(workspace, 'railway')
    # The reading state is a derivative, never citation approval.
    with app.read(workspace) as db:
        anchor, load, _ = app._anchor(db, workspace, hit.ref, hit.revision)
        _, _, raw, _ = load(anchor.identity, anchor.locator)
    location = ReadingLocation(workspace, hit.edition_id, hit.revision,
        anchor.locator.kind, anchor.locator.index,
        sha256(raw.encode('utf-8')).hexdigest(), anchor.identity.asset_sha256)
    state = ReadingState(app.root)
    state.remember(location)
    bookmark = state.add_bookmark(location, 'Current chapter')
    return location, bookmark


def project(app):
    store = ProjectStore(app.root)
    first = store.create(parse_script('Narrator: Original script.', ['installed-local-voice'],
                                       project_id='work-authored-project'))
    second = parse_script(first.script + '\nNarrator: New line.', ['installed-local-voice'], previous=first)
    store.save(second)
    store.save_draft(first.id, 'Unfinished draft @never-execute')
    director = ProductionDirector(app.root, registry=SimpleNamespace())
    return store, second, director


def test_legacy_shaped_library_keeps_v2_despite_manual_ingest_journal(library, tmp_path):
    app, _ = library
    assert (app.root / 'imports.sqlite').is_file()
    ProjectStore(app.root)  # Empty directories and locks are not personal state.
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert manifest['version'] == 2 and 'sidecars' not in manifest
    assert not (backup / 'sidecars').exists()


def test_full_v3_roundtrip_preserves_reading_revisions_drafts_profiles_and_plan(library, tmp_path):
    app, workspace = library
    location, bookmark = reading(app, workspace)
    store, saved, director = project(app)
    external = tmp_path / 'external-audio'
    job_id = director.create_job(saved.id, external)
    profile = ProviderProfile('account', 'Remote account', 'openai-compatible',
        'https://api.example.com/v1', 'chosen-model', 'chat', credential_ref='wincred:' + 'a' * 32)
    ConnectionStore(app.root).save_profile(profile)
    scope = Scope(workspaces=(workspace,))
    assistant = LocalAssistant(app, scope)
    plan = WorkflowPlan('review-plan', 'Find source', (Step('find', 'library.search',
        {'workspace': workspace, 'query': 'railway'}),))
    assistant.prepare(plan)
    # Temporary work and invalidated cache objects are excluded deliberately.
    work = store.path(saved.id) / 'work-incomplete'
    work.mkdir()
    (work / 'partial.wav').write_bytes(b'not an accepted render')
    (store.path(saved.id) / 'cache' / ('a' * 64 + '.wav.invalid-old')).write_bytes(b'diagnostic')
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert manifest['version'] == 3
    paths = {row['path'] for row in manifest['sidecars']['files']}
    assert 'reading-state.json' in paths and 'connections.json' in paths
    assert 'imports.sqlite' not in paths
    assert all('incomplete' not in path and '.invalid-' not in path for path in paths)
    restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = LibraryApp(restored_root)
    state = ReadingState(restored.root)
    assert state.resume(workspace, location.edition) == location
    assert state.bookmarks(workspace, location.edition) == (bookmark,)
    copies = ProjectStore(restored.root)
    assert copies.load(saved.id) == saved
    assert read_json(copies.path(saved.id) / 'revisions' / '00000001.json')['script'] == 'Narrator: Original script.'
    assert read_json(copies.path(saved.id) / 'draft.json')['script'] == 'Unfinished draft @never-execute'
    assert all((copies.path(saved.id) / name).is_dir() for name in ('jobs','assets','cache','revisions'))
    restored_job = read_json(copies.path(saved.id) / 'jobs' / (job_id + '.json'))
    assert restored_job['restore_requires_new_output'] is True
    assert restored_job['output_dir'] == '' and restored_job['outputs'] == [] and restored_job['result'] is None
    assert restored_job['restored_from_output_dir'] == str(external)
    with pytest.raises(ValueError, match='restored|Restored|new output'):
        ProductionDirector(restored.root, registry=SimpleNamespace()).render(saved.id, job_id=job_id)
    copied_profile, = ConnectionStore(restored.root).list_profiles()
    assert copied_profile.endpoint == profile.endpoint and copied_profile.credential_ref is None
    restored_plan = read_json(restored.root / 'assistant' / 'plans' / (plan.id + '.json'))
    assert restored_plan['plan'] == asdict(plan) or restored_plan['plan'] == json.loads(json.dumps(asdict(plan)))
    assert restored_plan['restore_requires_review'] is True
    with pytest.raises(ValueError, match='restored'):
        LocalAssistant(restored, scope).inspect(plan.id)
    assert len(restored.search(workspace, 'railway')) == 1
    assert not external.exists() and not (restored.root / INCOMPLETE).exists()
    assert ConnectionStore(app.root).get(profile.id).credential_ref == profile.credential_ref
    assert read_json(store.path(saved.id) / 'jobs' / (job_id + '.json'))['output_dir'] == str(external)


def test_pending_import_retained_original_and_root_binding_survive_restore(library, tmp_path):
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import prepare_request, load_request

    app, workspace = library
    original = tmp_path / 'queued.txt'
    payload = b'Previously unregistered retained manuscript.'
    original.write_bytes(payload)
    batch = app.plan_import_batch(workspace, [original])
    row, = batch['rows']
    metadata = dict(title=row['title'], authors=[], language=None, edition=None,
        volume=None, collections=[], work_id=row['work_id'], edition_id=row['edition_id'])
    request = prepare_request(app.root, workspace, original, metadata, Config(),
        request_id=row['request_id'], expected_manifest=row['manifest'])
    result = app._import_file(workspace, original, **metadata, _request_id=request['request_id'],
        _expected_manifest=row['manifest'], cancelled=lambda: True)
    assert result['state'] == 'cancelled'
    asset = sha256(payload).hexdigest()
    assert app.assets.resolve(asset).read_bytes() == payload
    assert not app.search(workspace, 'manuscript')
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    files = {item['path'] for item in manifest['sidecars']['files']}
    assert 'imports.sqlite' in files and f'pending-originals/{asset}.txt' in files
    original.unlink()  # Resume must use the retained original, not this path.
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = LibraryApp(root)
    copied_request = load_request(root, request['request_id'])
    assert copied_request['data_dir'] == str(root)
    assert copied_request['source'] == str(original)
    assert copied_request['workspace'] == workspace
    assert copied_request['metadata']['edition_id'] == row['edition_id']
    copied_batch = restored.import_batch(workspace, batch['batch_id'])
    assert copied_batch['data_dir'] == str(root) and copied_batch['rows'][0]['source'] == str(original)
    assert restored.assets.resolve(asset).read_bytes() == payload
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete'
    hit, = restored.search(workspace, 'manuscript')
    assert hit.edition_id == row['edition_id']


@pytest.mark.parametrize('damage', ('bytes', 'extra', 'missing', 'symlink', 'traversal', 'secret'))
def test_invalid_sidecar_bundle_never_publishes_a_restore(library, tmp_path, damage):
    app, workspace = library
    reading(app, workspace)
    profile = ProviderProfile('local', 'Local service', 'openai-compatible',
        'http://127.0.0.1:8080/v1', 'local-model', 'chat', local=True)
    ConnectionStore(app.root).save_profile(profile)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    path = backup / 'sidecars' / 'reading-state.json'
    if damage == 'bytes':
        path.write_bytes(b'x' * path.stat().st_size)
    elif damage == 'extra':
        (path.parent / 'unexpected.json').write_text('{}')
    elif damage == 'missing':
        path.unlink()
    elif damage == 'symlink':
        outside = tmp_path / 'outside.json'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            # Windows without symlink rights can still exercise a forbidden
            # multi-link file, with the same outside-data protection.
            os.link(outside, path)
    elif damage == 'traversal':
        manifest['sidecars']['files'][0]['path'] = '../outside.json'
    else:
        path = backup / 'sidecars' / 'connections.json'
        value = json.loads(path.read_text())
        value['profiles'][0]['api_key'] = 'must-not-be-in-a-backup'
        path.write_text(json.dumps(value))
        row = next(row for row in manifest['sidecars']['files'] if row['path'] == 'connections.json')
        row.update(size_bytes=path.stat().st_size, sha256=sha256(path.read_bytes()).hexdigest())
    if damage in ('traversal', 'secret'):
        manifest_path.write_text(json.dumps(manifest))
        checksum = sha256(manifest_path.read_bytes()).hexdigest()
    target = tmp_path / 'restored'
    with pytest.raises((ValueError, OSError)):
        restore_backup(backup, target, checksum)
    assert not target.exists()
    assert ReadingState(app.root).bookmarks(workspace, app.search(workspace, 'railway')[0].edition_id)


def test_snapshot_resource_budget_and_live_state_lock_fail_without_partial_backup(library, tmp_path, monkeypatch):
    app, _ = library
    store, saved, _ = project(app)
    target = tmp_path / 'backup'
    with store.transaction():
        with pytest.raises(ValueError, match='busy'):
            create_backup(app, target)
    assert not target.exists()
    monkeypatch.setattr(sidecars, 'MAX_BYTES', 32)
    with pytest.raises(ValueError, match='resource limit|byte limit'):
        create_backup(app, target)
    assert not target.exists()


def test_accepted_asset_and_cache_bytes_are_checked_before_backup_publication(library, tmp_path):
    app, _ = library
    store, saved, _ = project(app)
    folder = store.path(saved.id)
    payload = b'opaque accepted waveform bytes for checksum test'
    checksum = sha256(payload).hexdigest()
    (folder / 'assets' / (checksum + '.wav')).write_bytes(payload)
    key = 'b' * 64
    (folder / 'cache' / (key + '.wav')).write_bytes(payload)
    atomic_json(folder / 'cache' / (key + '.json'), dict(key=key, sha256=checksum))
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    root = restore_backup(backup, tmp_path / 'restored', digest)
    restored = ProjectStore(root).path(saved.id)
    assert (restored / 'assets' / (checksum + '.wav')).read_bytes() == payload
    assert (restored / 'cache' / (key + '.wav')).read_bytes() == payload
    (folder / 'cache' / (key + '.wav')).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='chunk digest'):
        create_backup(app, tmp_path / 'rejected-backup')
    assert not (tmp_path / 'rejected-backup').exists()


def test_provider_usage_history_is_preserved_but_cannot_reauthorize_paid_calls(library, tmp_path):
    from carr_library.assistant.providers import (ProviderGateway, PlanApproval,
        ProviderError, validate_ledger_document)

    app, _ = library
    profile = ProviderProfile('account', 'Reviewed account', 'openai-compatible',
        'https://api.example.com/v1', 'chosen-model', 'chat')
    store = ConnectionStore(app.root)
    store.save_profile(profile)
    approval = PlanApproval.for_profiles((profile,), plan_id='paid-plan',
        mode='APPROVED_REMOTE', max_budget_microunits=100)
    transport = Mock()
    gateway = ProviderGateway(store, approval=approval, _transport=transport)
    ledger = gateway._ledger()
    ledger['operations']['prior-call'] = dict(profile_id=profile.id, request_sha256='f' * 64,
        state='uncertain', reserved_microunits=75, billed_microunits=None, data_class='user_text')
    validate_ledger_document(ledger, plan_id=approval.plan_id)
    atomic_json(gateway.ledger_path, ledger)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    copied = read_json(root / 'provider-jobs' / 'paid-plan.json')
    assert copied['operations'] == ledger['operations']
    assert copied['approval_sha256'] == ledger['approval_sha256']
    assert copied['restored'] is True
    restored = ProviderGateway(ConnectionStore(root), approval=approval, _transport=transport)
    with pytest.raises(ProviderError, match='Restored provider history'):
        restored.chat(profile.id, [{'role': 'user', 'content': 'Selected request text'}],
                      operation_id='new-call', estimated_cost_microunits=1)
    transport.request.assert_not_called()
    assert read_json(gateway.ledger_path)['restored'] is False


def test_unsaved_studio_draft_and_invalid_script_metadata_survive_backup(library, tmp_path):
    app, _ = library
    store = ProjectStore(app.root)
    draft_id = 'd' * 32
    draft = store.save_new_draft(draft_id, '@unknown unfinished directive',
        title='Unfinished title', language='uk', format='flac')
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    copies = ProjectStore(restored)
    assert copies.list_projects() == ()
    assert copies.new_drafts() == (draft,)
    assert copies.new_drafts()[0]['language'] == 'uk'
    assert copies.new_drafts()[0]['format'] == 'flac'
