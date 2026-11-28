"""Real full-library recovery of reading, audio and durable import sidecars."""
from dataclasses import asdict, replace
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
from carr_library.audio.projects import ProjectStore, parse_script, read_json, atomic_json, digest
from carr_library.audio.director import ProductionDirector
from carr_library.reader.reading_state import ReadingLocation, ReadingState
from carr_library.assistant.providers import ConnectionStore, ProviderProfile
from carr_library.assistant.workflows import LocalAssistant, Scope, Step, WorkflowPlan
from carr_library.ingest.restore import RestoreError


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


def test_library_without_personal_sidecars_keeps_canonical_v4_journal_backup(library, tmp_path):
    app, _ = library
    assert (app.root / 'imports.sqlite').is_file()
    ProjectStore(app.root)  # Empty directories and locks are not personal state.
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert manifest['version'] == 4 and 'sidecars' not in manifest
    assert any(row['filename'] == 'imports.sqlite' for row in manifest['pending']['files'])
    assert not (backup / 'sidecars').exists()


def test_full_v5_roundtrip_preserves_reading_revisions_drafts_profiles_and_plan(library, tmp_path):
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
    assert manifest['version'] == 5
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
    files = {item['filename'] for item in manifest['pending']['files']}
    assert 'imports.sqlite' in files and f'originals/{asset}.txt' in files
    assert 'sidecars' not in manifest  # No duplicate owner for import recovery.
    original.unlink()  # Resume must use the retained original, not this path.
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = LibraryApp(root)
    copied_request = load_request(root, request['request_id'])
    assert copied_request['data_dir'] == str(root)
    retained_source = str(root / 'originals' / (asset + '.txt'))
    assert copied_request['source'] == retained_source
    assert copied_request['workspace'] == workspace
    assert copied_request['metadata']['edition_id'] == row['edition_id']
    copied_batch = restored.import_batch(workspace, batch['batch_id'])
    assert copied_batch['data_dir'] == str(root) and copied_batch['rows'][0]['source'] == retained_source
    assert restored.assets.resolve(asset).read_bytes() == payload
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete'
    hit, = restored.search(workspace, 'manuscript')
    assert hit.edition_id == row['edition_id']


@pytest.mark.parametrize('extension,payload', [
    ('.html', b'<!doctype html><html><body><p>Pending restored manuscript.</p></body></html>'),
    ('.md', b'# Pending restored manuscript.\r\nExact notation **kept**.\r\n'),
])
def test_pending_extended_source_resumes_from_backed_up_original(library, tmp_path, extension, payload):
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import prepare_request

    app, workspace = library
    original = tmp_path / ('queued' + extension)
    original.write_bytes(payload)
    batch = app.plan_import_batch(workspace, [original])
    row, = batch['rows']
    metadata = dict(title=row['title'], authors=[], language=None, edition=None,
        volume=None, collections=[], work_id=row['work_id'], edition_id=row['edition_id'])
    request = prepare_request(app.root, workspace, original, metadata, Config(),
        request_id=row['request_id'], expected_manifest=row['manifest'])
    with pytest.raises(InterruptedError, match='cancelled'):
        app._import_file(workspace, original, **metadata, _request_id=request['request_id'],
            _expected_manifest=row['manifest'], cancelled=lambda: True)
    digest = sha256(payload).hexdigest()
    assert app.assets.resolve(digest).read_bytes() == payload
    assert not app.search(workspace, 'manuscript')
    checksum = create_backup(app, tmp_path / 'backup')
    original.unlink()
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum))
    assert restored.assets.resolve(digest).read_bytes() == payload
    result = restored.resume_import(workspace, request['request_id'])
    assert result['state'] == 'complete' and result['isolated'] is True and result['verified'] is False
    hit, = restored.search(workspace, 'manuscript')
    assert hit.edition_id == row['edition_id']
    with pytest.raises(ValueError, match='unverified'):
        restored.copy(workspace, hit.ref, hit.revision, 'quote')


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
    digest = create_backup(app, backup, include_generated=True)
    root = restore_backup(backup, tmp_path / 'restored', digest)
    restored = ProjectStore(root).path(saved.id)
    assert (restored / 'assets' / (checksum + '.wav')).read_bytes() == payload
    assert (restored / 'cache' / (key + '.wav')).read_bytes() == payload
    (folder / 'cache' / (key + '.wav')).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='chunk digest'):
        create_backup(app, tmp_path / 'rejected-backup', include_generated=True)
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


def test_per_window_stale_drafts_keep_exact_bytes_and_legacy_recovery(library, tmp_path):
    app, _ = library
    store = ProjectStore(app.root)
    original = store.create(parse_script('Narrator: Original script.', ['local-voice']))
    store.save_draft(original.id, 'Legacy unfinished draft')
    stale = store.save_draft(original.id, '\0Unfinished first window',
        session_id='a' * 32, title='First window', language='uk', format='flac')
    newer = parse_script('Narrator: Saved by the other window.', ['local-voice'], previous=original)
    store.save(newer)
    current = store.save_draft(newer.id, '@unknown unfinished directive',
        session_id='b' * 32, title='Second window', language='sk', format='wav')
    before = store.project_drafts(original.id)
    raw = {row['id']: (store.studio / 'drafts' / (row['id'] + '.json')).read_bytes()
           for row in (stale, current)}
    checksum = create_backup(app, tmp_path / 'backup')
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    copies = ProjectStore(restored)
    assert copies.load(original.id) == newer
    assert copies.project_drafts(original.id) == before
    assert copies.new_drafts() == ()
    assert stale['base_revision'] == original.revision
    assert stale['base_fingerprint'] == original.fingerprint != newer.fingerprint
    for draft_id, expected in raw.items():
        assert (copies.studio / 'drafts' / (draft_id + '.json')).read_bytes() == expected
    assert read_json(copies.path(original.id) / 'draft.json')['script'] == 'Legacy unfinished draft'


@pytest.mark.parametrize('phase', ['snapshot', 'restore'])
def test_rehashed_per_window_draft_cannot_reference_a_missing_project(library, tmp_path, phase):
    app, _ = library
    store = ProjectStore(app.root)
    saved = store.create(parse_script('Narrator: Original.', ['local-voice']))
    draft = store.save_draft(saved.id, 'Unfinished recovery text', session_id='a' * 32)
    relative = 'studio/drafts/' + draft['id'] + '.json'
    backup = tmp_path / 'backup'
    if phase == 'restore':
        create_backup(app, backup)
        path = backup / 'sidecars' / relative
    else:
        path = app.root / relative
    draft['project_id'] = 'missing-project'
    draft['sha256'] = digest({key: value for key, value in draft.items() if key != 'sha256'})
    raw = json.dumps(draft).encode('utf-8')
    path.write_bytes(raw)
    if phase == 'restore':
        manifest_path = backup / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        row = next(row for row in manifest['sidecars']['files'] if row['path'] == relative)
        row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
        sealed = json.dumps(manifest).encode('utf-8')
        manifest_path.write_bytes(sealed)
        destination = tmp_path / 'restored'
        with pytest.raises(RestoreError, match='no matching saved project'):
            restore_backup(backup, destination, sha256(sealed).hexdigest())
    else:
        destination = backup
        with pytest.raises(RestoreError, match='no matching saved project'):
            create_backup(app, backup)
    assert not destination.exists()


def test_keyboard_and_ordered_ocr_presets_survive_full_restore(library, tmp_path):
    from carr_library.keybindings import Command, KeybindingStore
    from carr_library.extract.ocr_languages import store_preset, load_presets
    from carr_library.extract.page_ocr import Config

    app, _ = library
    KeybindingStore(app.root).save((Command('File/Save', 'Save', 'Ctrl+S'),),
                                   {'File/Save': 'Ctrl+Alt+S'})
    config = Config(languages='ukr+eng+script/Latin', orientation=True)
    store_preset(app.root, 'Manuscripts', config)
    checksum = create_backup(app, tmp_path / 'backup')
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    assert KeybindingStore(restored).load() == {'File/Save': 'Ctrl+Alt+S'}
    assert load_presets(restored) == load_presets(app.root)
    assert load_presets(restored)[0]['languages'] == 'ukr+eng+script/Latin'


def test_music_registration_restores_without_touching_external_assets(library, tmp_path):
    from carr_library.audio.music_settings import MusicSettingsStore, load_music_options

    app, _ = library
    store = MusicSettingsStore(app.root)
    record = store.read()
    record['fluidsynth'] = dict(soundfont=str(tmp_path / 'absent.sf2'), soundfont_sha256='a' * 64,
        soundfont_license='Synthetic registration fixture; no distributed SoundFont.',
        fluidsynth_library=str(tmp_path / 'absent.dll'), fluidsynth_sha256='b' * 64)
    # This fixture exercises state transfer, not engine or asset registration.
    saved = store.save(record, expected_revision=0, check_files=False)
    checksum = create_backup(app, tmp_path / 'backup')
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    assert MusicSettingsStore(restored).read() == saved
    with pytest.raises((ValueError, OSError)):
        load_music_options(restored)
    assert not (tmp_path / 'absent.sf2').exists()


@pytest.mark.parametrize('field,value', [('revision', 'missing'), ('edition', 'other-edition'),
    ('asset_sha256', 'b' * 64), ('text_sha256', 'b' * 64), ('index', 999)])
def test_forged_reading_binding_cannot_be_backed_up(library, tmp_path, field, value):
    app, workspace = library
    location, _ = reading(app, workspace)
    ReadingState(app.root).remember(replace(location, **{field: value}))
    with pytest.raises(RestoreError, match='exact saved source'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_rehashed_reading_sidecar_forgery_fails_before_restore_publication(library, tmp_path):
    app, workspace = library
    reading(app, workspace)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    path = backup / 'sidecars' / 'reading-state.json'
    value = json.loads(path.read_text())
    value['positions'][0]['location']['revision'] = 'missing-revision'
    raw = json.dumps(value).encode('utf-8')
    path.write_bytes(raw)
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    row = next(row for row in manifest['sidecars']['files'] if row['path'] == 'reading-state.json')
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    sealed = json.dumps(manifest).encode('utf-8')
    manifest_path.write_bytes(sealed)
    with pytest.raises(RestoreError, match='exact saved source'):
        restore_backup(backup, tmp_path / 'restored', sha256(sealed).hexdigest())
    assert not (tmp_path / 'restored').exists()


def test_historical_archived_reading_and_studio_sources_keep_exact_binding(library, tmp_path):
    app, workspace = library
    location, _ = reading(app, workspace)
    source = asdict(location)
    source.pop('asset_sha256')
    saved = ProjectStore(app.root).create(parse_script('Original railway quotation.',
        ['local-voice'], directives=False, source=source))
    current = app.update_metadata(workspace, location.edition, title='New metadata revision')
    assert current != location.revision
    app.archive_workspace(workspace)
    checksum = create_backup(app, tmp_path / 'backup')
    root = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    assert ReadingState(root).resume(workspace, location.edition) == location
    assert ProjectStore(root).load(saved.id).source == source


def test_studio_source_must_belong_to_its_exact_saved_revision(library, tmp_path):
    app, workspace = library
    location, _ = reading(app, workspace)
    source = asdict(location)
    source.pop('asset_sha256')
    source['workspace'] = app.create_workspace('Different library')
    ProjectStore(app.root).create(parse_script('Original railway quotation.',
        ['local-voice'], directives=False, source=source))
    with pytest.raises(RestoreError, match='exact saved source'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()



@pytest.mark.parametrize('maximum', ['1', True, -1, 1.5, sidecars.MAX_BYTES + 1])
def test_selected_sidecar_limit_can_only_lower_the_existing_cap(
        library, tmp_path, maximum):
    app, workspace = library
    reading(app, workspace)
    stage = tmp_path / 'invalid-cap'
    stage.mkdir()
    with app.read(workspace) as db:
        with pytest.raises(ValueError, match='resource limit'):
            sidecars.snapshot(app, stage, set(), db=db, include_pending=False,
                              max_bytes=maximum)
    assert not (stage / sidecars.DIRECTORY).exists()


def test_selected_sidecar_exact_cap_preserves_default_manifest_and_bytes(library, tmp_path):
    app, workspace = library
    reading(app, workspace)
    original = (app.root / 'reading-state.json').read_bytes()
    ordinary, selected = tmp_path / 'ordinary', tmp_path / 'selected'
    ordinary.mkdir()
    selected.mkdir()
    with app.read(workspace) as db:
        old = sidecars.snapshot(app, ordinary, set(), db=db, include_pending=False)
        bounded = sidecars.snapshot(app, selected, set(), db=db, include_pending=False,
                                   max_bytes=len(original))
    assert old == bounded
    assert [row['path'] for row in bounded['files']] == ['reading-state.json']
    assert sum(row['size_bytes'] for row in bounded['files']) == len(original)
    assert (selected / sidecars.DIRECTORY / 'reading-state.json').read_bytes() == original
    assert (ordinary / sidecars.DIRECTORY / 'reading-state.json').read_bytes() == original
    assert (app.root / 'reading-state.json').read_bytes() == original


def test_selected_sidecar_cap_refuses_the_next_file_before_copy(
        library, tmp_path, monkeypatch):
    app, workspace = library
    reading(app, workspace)
    ConnectionStore(app.root).save_profile(ProviderProfile(
        'resource-review', 'Resource review', 'openai-compatible',
        'https://api.example.com/v1', 'chosen-model', 'chat'))
    originals = {name: (app.root / name).read_bytes()
                 for name in ('connections.json', 'reading-state.json')}
    stage = tmp_path / 'one-file-cap'
    stage.mkdir()
    copied = []
    actual = sidecars._copy
    def observed(source, target, **options):
        result = actual(source, target, **options)
        if Path(target).is_relative_to(stage):
            copied.append(Path(target).relative_to(stage).as_posix())
        return result
    monkeypatch.setattr(sidecars, '_copy', observed)
    with app.read(workspace) as db:
        with pytest.raises(ValueError, match='resource limit'):
            sidecars.snapshot(app, stage, set(), db=db, include_pending=False,
                              max_bytes=len(originals['connections.json']))
    assert copied == ['sidecars/connections.json']
    assert not (stage / sidecars.DIRECTORY / 'reading-state.json').exists()
    assert (stage / sidecars.DIRECTORY / 'connections.json').read_bytes() == originals['connections.json']
    assert all((app.root / name).read_bytes() == raw for name, raw in originals.items())


@pytest.mark.parametrize('cap_kind', ['lowered', 'equal-current-maximum'])
def test_selected_sidecar_cap_rechecks_growth_before_opening_the_destination(
        library, tmp_path, monkeypatch, cap_kind):
    app, workspace = library
    reading(app, workspace)
    original_path = app.root / 'reading-state.json'
    original = original_path.read_bytes()
    if cap_kind == 'equal-current-maximum':
        monkeypatch.setattr(sidecars, 'MAX_BYTES', len(original))
    stage = tmp_path / 'changed-before-copy'
    stage.mkdir()
    actual = sidecars._copy
    observed = []
    def grow(source, target, **options):
        if Path(source) == original_path:
            observed.append(options['maximum_bytes'])
            original_path.write_bytes(original + b' ')
        return actual(source, target, **options)
    monkeypatch.setattr(sidecars, '_copy', grow)
    try:
        with app.read(workspace) as db:
            with pytest.raises(ValueError, match='resource limit'):
                sidecars.snapshot(app, stage, set(), db=db, include_pending=False,
                                  max_bytes=len(original))
        assert observed == [len(original)]
        assert not (stage / sidecars.DIRECTORY / 'reading-state.json').exists()
    finally:
        original_path.write_bytes(original)
    assert original_path.read_bytes() == original


def test_selected_sidecar_journal_cap_refuses_before_journal_copy(
        library, tmp_path, monkeypatch):
    app, workspace = library
    incoming = tmp_path / 'queued-resource.txt'
    incoming.write_bytes(b'An explicitly planned synthetic source.')
    app.plan_import_batch(workspace, [incoming])
    stage = tmp_path / 'journal-cap'
    stage.mkdir()
    # Use the actual canonical folder inventory; exclude the journal's bytes.
    selected = [(name + '/' + relative, path)
                for name in ('import-requests', 'import-batches')
                if (app.root / name).exists()
                for relative, path in sidecars._walk(
                    app.root / name, exclude_temporary=True, cancelled=lambda: False)]
    maximum = sum(path.stat().st_size for _, path in selected)
    assert selected and (app.root / 'imports.sqlite').stat().st_size > 0
    actual = sidecars._journal
    calls = []
    def observed(path):
        calls.append(Path(path))
        return actual(path)
    monkeypatch.setattr(sidecars, '_journal', observed)
    with app.read(workspace) as db:
        registered = {row[0] for row in db.execute('SELECT digest FROM catalog_asset')}
        with pytest.raises(ValueError, match='resource limit'):
            sidecars.snapshot(app, stage, registered, db=db, max_bytes=maximum)
    assert calls == []
    assert not (stage / sidecars.DIRECTORY / 'imports.sqlite').exists()
    assert sum(path.stat().st_size for path in (stage / sidecars.DIRECTORY).rglob('*')
               if path.is_file()) <= maximum
    assert incoming.read_bytes() == b'An explicitly planned synthetic source.'



@pytest.mark.parametrize('cap_kind', ['lowered', 'equal-current-maximum'])
def test_selected_sidecar_journal_accounts_for_real_wal_pages_before_destination_open(
        library, tmp_path, monkeypatch, cap_kind):
    import sqlite3
    app, workspace = library
    incoming = tmp_path / 'wal-source.txt'
    incoming.write_bytes(b'Original planned input is unchanged.')
    app.plan_import_batch(workspace, [incoming])
    selected = [(name + '/' + relative, path)
                for name in ('import-requests', 'import-batches')
                if (app.root / name).exists()
                for relative, path in sidecars._walk(
                    app.root / name, exclude_temporary=True, cancelled=lambda: False)]
    sidecar_bytes = sum(path.stat().st_size for _, path in selected)
    stage = tmp_path / 'wal-bounded-stage'
    stage.mkdir()
    journal = app.root / 'imports.sqlite'
    writer = sqlite3.connect(journal, isolation_level=None)
    try:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        # Resource admission is checked before the later catalog/job verifier.
        # The real canonical journal tables and FK remain valid; this synthetic
        # row exists only to keep genuinely committed pages in its live WAL.
        job = 'f' * 64
        writer.execute('INSERT INTO job VALUES(?,?,?,?,?,?,?,?,?)',
            (job, workspace, 'bounded-wal-fixture', 'e' * 64, '{}', 1, None, 'pending', None))
        payload = 'Synthetic checkpoint bytes. ' * 100_000
        writer.execute('INSERT INTO checkpoint VALUES(?,?,?,?)',
            (job, 1, payload, sha256(payload.encode('utf-8')).hexdigest()))
        page_size = writer.execute('PRAGMA page_size').fetchone()[0]
        page_count = writer.execute('PRAGMA page_count').fetchone()[0]
        physical = journal.stat().st_size
        assert page_size * page_count > physical
        maximum = sidecar_bytes + physical
        if cap_kind == 'equal-current-maximum':
            monkeypatch.setattr(sidecars, 'MAX_BYTES', maximum)
        target = stage / sidecars.DIRECTORY / 'imports.sqlite'
        actual_connect = sqlite3.connect
        def connect(path, *args, **options):
            if str(path) == str(target):
                pytest.fail('Known over-cap WAL snapshot opened its destination.')
            return actual_connect(path, *args, **options)
        with app.read(workspace) as db:
            registered = {row[0] for row in db.execute('SELECT digest FROM catalog_asset')}
            monkeypatch.setattr(sidecars.sqlite3, 'connect', connect)
            with pytest.raises(ValueError, match='resource limit'):
                sidecars.snapshot(app, stage, registered, db=db, max_bytes=maximum)
        assert not target.exists()
        assert sum(path.stat().st_size for path in (stage / sidecars.DIRECTORY).rglob('*')
                   if path.is_file()) <= maximum
        assert incoming.read_bytes() == b'Original planned input is unchanged.'
    finally:
        writer.close()


def test_omitted_sidecar_limit_reads_current_default_and_keeps_legacy_copy_call(
        library, tmp_path, monkeypatch):
    app, workspace = library
    reading(app, workspace)
    source = app.root / 'reading-state.json'
    original = source.read_bytes()
    fitting, too_small = tmp_path / 'current-default', tmp_path / 'lower-current-default'
    fitting.mkdir()
    too_small.mkdir()
    actual = sidecars._copy
    calls = []
    def observed(path, target, **options):
        if Path(path) == source:
            calls.append(dict(options))
            assert 'maximum_bytes' not in options
        return actual(path, target, **options)
    monkeypatch.setattr(sidecars, '_copy', observed)
    monkeypatch.setattr(sidecars, 'MAX_BYTES', len(original))
    with app.read(workspace) as db:
        result = sidecars.snapshot(app, fitting, set(), db=db, include_pending=False)
        assert len(calls) == 1
        assert sum(row['size_bytes'] for row in result['files']) == len(original)
        assert (fitting / sidecars.DIRECTORY / 'reading-state.json').read_bytes() == original
        calls.clear()
        monkeypatch.setattr(sidecars, 'MAX_BYTES', len(original) - 1)
        with pytest.raises(ValueError, match='resource limit'):
            sidecars.snapshot(app, too_small, set(), db=db, include_pending=False)
    assert calls == []
    assert not (too_small / sidecars.DIRECTORY / 'reading-state.json').exists()
    assert source.read_bytes() == original
