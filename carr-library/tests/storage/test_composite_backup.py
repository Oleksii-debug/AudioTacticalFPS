"""Real SQLite recovery across both accepted V3 state layouts and canonical V4."""
from contextlib import closing

from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import shutil
from time import time
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.projects import ProjectStore, parse_script
from carr_library.assistant.models import Plan, Scope, Step
from carr_library.assistant.profiles import ProfileStore, ProviderProfile
from carr_library.assistant.providers import Authorization, UsageLedger
from carr_library.assistant.service import AssistantService
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume


@pytest.fixture
def mixed(tmp_path):
    app = LibraryApp(tmp_path / 'source-library')
    workspace = app.create_workspace('Both accepted state families')
    app.add_text(workspace, 'Original', 'Immutable original railway quotation.')
    project = ProjectStore(app.root).create(parse_script('Narrator: Private derived script.', ['local-voice']))
    ReaderDraftStore(app.root).save(ReaderResume(ReadingDocument('Exact original text.')))
    atomic_json(app.root / 'state/ui/legacy.json', {'opaque': 'preserved byte-for-byte'})
    pending = tmp_path / 'pending.txt'
    pending.write_bytes(b'Pinned pending source, not yet registered.')
    batch = app.plan_import_batch(workspace, [pending])
    return app, workspace, project, pending, batch


def seal(folder, manifest):
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def remove_component(folder, manifest, name):
    manifest.pop(name, None)
    if (folder / name).exists():
        shutil.rmtree(folder / name)


def test_v5_preserves_both_stores_and_only_one_pending_component(mixed, tmp_path):
    app, workspace, project, original, batch = mixed
    original_bytes = original.read_bytes()
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert manifest['version'] == 5
    assert set(manifest) == {'version', 'kind', 'schema_version', 'schema_fingerprint',
        'database', 'assets', 'state', 'generated_audio_included', 'pending', 'sidecars'}
    assert all(not row['path'].startswith(('import-', 'pending-originals/'))
               and row['path'] != 'imports.sqlite' for row in manifest['sidecars']['files'])
    assert any(row['filename'].startswith('import-batches/') for row in manifest['pending']['files'])
    original.unlink()
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = LibraryApp(root)
    assert ProjectStore(root).load(project.id) == project
    assert ReaderDraftStore(root).load().document.source_text == 'Exact original text.'
    assert (root / 'state/ui/legacy.json').read_bytes() == (app.root / 'state/ui/legacy.json').read_bytes()
    copied = restored.import_batch(workspace, batch['batch_id'])
    source = Path(copied['rows'][0]['source'])
    assert source.is_relative_to(root / 'originals') and source.read_bytes() == original_bytes
    assert restored.run_import_batch(workspace, batch['batch_id'])['state'] == 'complete'
    assert restored.search(workspace, 'railway')[0].quote == 'railway'
    assert not (root / INCOMPLETE).exists()


@pytest.mark.parametrize('shape', ['v1', 'v2', 'v3-state', 'v3-sidecars', 'v4', 'v5-without-pending'])
def test_strict_legacy_shapes_still_restore(mixed, tmp_path, shape):
    app, workspace, project, _, _ = mixed
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    if shape != 'v5-without-pending':
        manifest['version'] = int(shape[1])
    if shape in ('v1', 'v2', 'v3-state', 'v4'):
        remove_component(backup, manifest, 'sidecars')
    if shape in ('v1', 'v2', 'v3-state', 'v3-sidecars', 'v5-without-pending'):
        remove_component(backup, manifest, 'pending')
    if shape in ('v1', 'v2', 'v3-sidecars'):
        remove_component(backup, manifest, 'state')
        manifest.pop('generated_audio_included')
    if shape == 'v1':
        manifest.pop('schema_fingerprint')
    root = restore_backup(backup, tmp_path / 'restored', seal(backup, manifest))
    assert LibraryApp(root).search(workspace, 'railway')[0].quote == 'railway'
    assert (root / 'state').exists() == ('state' in manifest)
    assert (root / 'studio/projects' / project.id / 'project.json').exists() == ('sidecars' in manifest)
    assert not (root / INCOMPLETE).exists()


@pytest.mark.parametrize('damage', ['mixed-v3', 'missing-sidecars', 'missing-state',
                                    'pending-in-sidecars', 'unknown-component', 'corrupt-state',
                                    'corrupt-sidecar', 'corrupt-pending'])
def test_mixed_or_corrupt_components_fail_before_publication(mixed, tmp_path, damage):
    app, *_ = mixed
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    if damage == 'mixed-v3':
        manifest['version'] = 3
    elif damage == 'missing-sidecars':
        manifest.pop('sidecars')
    elif damage == 'missing-state':
        manifest.pop('state')
    elif damage == 'pending-in-sidecars':
        payload = (backup / 'pending/imports.sqlite').read_bytes()
        (backup / 'sidecars/imports.sqlite').write_bytes(payload)
        manifest['sidecars']['files'].append({'path': 'imports.sqlite', 'size_bytes': len(payload),
                                             'sha256': sha256(payload).hexdigest()})
        manifest['sidecars']['files'].sort(key=lambda row: row['path'])
    elif damage == 'unknown-component':
        (backup / 'extra').mkdir()
    else:
        key = damage.removeprefix('corrupt-')
        component = {'state': 'state', 'sidecar': 'sidecars', 'pending': 'pending'}[key]
        entries = manifest[component] if component == 'state' else manifest[component]['files']
        name = entries[0]['filename' if component != 'sidecars' else 'path']
        path = backup / component / name
        path.write_bytes(path.read_bytes() + b'corruption')
    if not damage.startswith('corrupt-'):
        checksum = seal(backup, manifest)
    destination = tmp_path / 'rejected'
    with pytest.raises((ValueError, OSError)):
        restore_backup(backup, destination, checksum)
    assert not destination.exists()


def test_canonical_profile_and_usage_restore_revokes_authority(mixed, tmp_path):
    app, *_ = mixed
    profile = ProviderProfile('remote', 'Chosen account', 'openai-compatible',
        'https://api.example.com/v1', 'chosen-model', credential_ref='carr/provider/remote/' + 'a' * 32,
        enabled=True)
    ProfileStore(app.root / 'state/providers').save(profile)
    authorization = Authorization('approval', 'task', ((profile.id, profile.endpoint),),
        ('llm.chat',), ('synthetic-test',), '1', 5, time() + 3600)
    directory = app.root / 'state/provider/usage'
    ledger = UsageLedger(directory)
    ledger.reserve(authorization, 'original', 'b' * 64, Decimal('0.1'), profile, 'llm.chat')
    before = ledger.get(authorization.id)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    copied = ProfileStore(root / 'state/providers').get(profile.id)
    assert copied.credential_ref is None and copied.enabled is False
    restored_ledger = UsageLedger(root / 'state/provider/usage')
    after = restored_ledger.get(authorization.id)
    assert after == {**before, 'restored': True}
    with pytest.raises(PermissionError, match='restored|Restored'):
        restored_ledger.reserve(authorization, 'never-sent', 'c' * 64, Decimal('0.1'), profile, 'llm.chat')
    assert ProfileStore(app.root / 'state/providers').get(profile.id) == profile
    assert ledger.get(authorization.id) == before


def test_canonical_assistant_paths_are_preserved_without_being_reopened(mixed, tmp_path):
    app, workspace, *_ = mixed
    external = tmp_path / 'external-authorized-scope'
    external.mkdir()
    plan = Plan(Scope(workspace_id=workspace, input_roots=(external,), output_root=external),
                (Step('library.list'),), 'Keep this reviewable after moving machines')
    service = AssistantService(app)
    identity = service.submit(plan)
    before = read_json(app.root / 'state/assistant' / (identity + '.json'))
    external.rmdir()
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    after = read_json(root / 'state/assistant' / (identity + '.json'))
    assert after == {**before, 'restore_requires_review': True}
    restored = AssistantService(LibraryApp(root))
    assert restored.get(identity).id == identity
    with pytest.raises(PermissionError, match='restored|Restored'):
        restored.run(identity)
    assert not external.exists()


def test_translation_restore_keeps_text_receipts_and_requires_new_approval(mixed, tmp_path):
    from carr_library.assistant.translation import TranslationService, ENDPOINT
    from carr_library.ui.translation_dialog import translation_approval

    app, *_ = mixed
    profile = ProviderProfile('google', 'Translation account', 'google-translation-v2', ENDPOINT, 'nmt',
        capabilities=('translation.text', 'translation.languages'), enabled=True,
        credential_ref='carr/provider/google/' + 'a' * 32)
    calls = []
    def discover(*args, **kwargs):
        calls.append((args, kwargs))
        return {'languages': [{'language': 'en', 'name': 'English'}, {'language': 'uk', 'name': 'Ukrainian'}]}
    service = TranslationService(app.root / 'state/translation', gateway=SimpleNamespace(request=discover))
    language_approval = Authorization('languages', 'languages', ((profile.id, profile.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, time() + 3600)
    service.refresh_languages(profile, authorization=language_approval)
    approval, per_call = translation_approval(profile, 'Original selected text.', 'en', 'uk', '1')
    identity = service.submit(profile, 'Original selected text.', 'en', 'uk',
        authorization=approval, per_call_cost=per_call)
    before = service.jobs.load(identity)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    after = read_json(root / 'state/translation/jobs' / (identity + '.json'))
    assert after['source_text'] == before['source_text'] and after['receipts'] == before['receipts']
    assert after['authorization'] == before['authorization']
    assert after['profile'] == {**before['profile'], 'credential_ref': None, 'enabled': False}
    assert after['restore_requires_review'] is True
    restored = TranslationService(root / 'state/translation', gateway=SimpleNamespace(request=discover))
    with pytest.raises(PermissionError, match='restored|Restored'):
        restored.resume(identity)
    assert len(calls) == 1  # Only the explicit local fixture discovery, no resumed call.


def test_canonical_production_keeps_evidence_and_cannot_reuse_external_output(mixed, tmp_path):
    from carr_library.studio.render import ProductionService

    app, *_ = mixed
    voices = SimpleNamespace(voices=lambda: (SimpleNamespace(id='local-voice'),))
    external = tmp_path / 'external-output'
    external.mkdir()
    service = ProductionService(app.root / 'state/studio', voices=voices)
    project = service.create('Narrator: Original narration.', voice_ids=('local-voice',))
    identity = service.submit(project, external)
    before = service.get(identity)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    def forbidden(*args, **kwargs):
        raise AssertionError('A restored job must stop before runtime or output access.')
    restored = ProductionService(root / 'state/studio', voices=voices, runtime_factory=forbidden)
    assert restored.get(identity) == {**before, 'restore_requires_new_output': True}
    with pytest.raises((ValueError, PermissionError), match='restored|Restored|new output'):
        restored.run(identity)
    assert list(external.iterdir()) == []


def test_opaque_legacy_state_and_cancelled_restore_are_explicit(mixed, tmp_path):
    app, *_ = mixed
    opaque = app.root / 'state/ui/opaque.json'
    opaque.write_bytes(b'Historical diagnostic data, not an executable JSON job.')
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    rejected = tmp_path / 'cancelled'
    with pytest.raises(InterruptedError, match='cancelled'):
        restore_backup(backup, rejected, checksum, cancelled=lambda: True)
    assert not rejected.exists()
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    assert (root / 'state/ui/opaque.json').read_bytes() == opaque.read_bytes()


@pytest.mark.parametrize('generated', [False, True])
def test_generated_cache_choice_applies_to_both_state_families(mixed, tmp_path, generated):
    app, _, project, *_ = mixed
    folder = ProjectStore(app.root).path(project.id)
    payload = b'bounded checksum-only waveform fixture'
    digest = sha256(payload).hexdigest()
    key = 'e' * 64
    (folder / 'assets' / (digest + '.wav')).write_bytes(payload)
    (folder / 'cache' / (key + '.wav')).write_bytes(payload)
    atomic_json(folder / 'cache' / (key + '.json'), {'key': key, 'sha256': digest})
    canonical_cache = app.root / 'state/studio/cache'
    canonical_cache.mkdir(parents=True)
    (canonical_cache / 'chunk.wav').write_bytes(payload)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup, include_generated=generated)
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert manifest['generated_audio_included'] is generated
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = ProjectStore(root).path(project.id)
    assert (restored / 'assets' / (digest + '.wav')).read_bytes() == payload
    assert (restored / 'cache' / (key + '.wav')).exists() is generated
    assert (root / 'state/studio/cache/chunk.wav').exists() is generated


def test_historical_sidecar_v3_pending_source_still_resumes(mixed, tmp_path):
    from carr_library import backup_sidecars
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import prepare_request, load_request
    from carr_library.storage.revisions import Storage

    app, workspace, _, source, batch = mixed
    row, = batch['rows']
    metadata = {'title': row['title'], 'authors': [], 'language': None, 'edition': None,
        'volume': None, 'collections': [], 'work_id': row['work_id'], 'edition_id': row['edition_id']}
    request = prepare_request(app.root, workspace, source, metadata, Config(),
        request_id=row['request_id'], expected_manifest=row['manifest'])
    app._import_file(workspace, source, **metadata, _request_id=request['request_id'],
                     _expected_manifest=row['manifest'], cancelled=lambda: True)
    backup = tmp_path / 'legacy-sidecar-backup'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    for component in ('state', 'pending', 'sidecars'):
        remove_component(backup, manifest, component)
    manifest.pop('generated_audio_included')
    manifest['version'] = 3
    with closing(Storage(backup / 'library.sqlite', schema_version=4)) as storage:
        manifest['sidecars'] = backup_sidecars.snapshot(app, backup,
            {asset['sha256'] for asset in manifest['assets']}, db=storage.db)
    assert any(row['path'].startswith('pending-originals/') for row in manifest['sidecars']['files'])
    source.unlink()
    root = restore_backup(backup, tmp_path / 'restored', seal(backup, manifest))
    assert load_request(root, request['request_id'])['source'] == str(source)
    assert LibraryApp(root).resume_import(workspace, request['request_id'])['state'] == 'complete'


def test_cancellation_during_publication_keeps_incomplete_marker(mixed, tmp_path):
    app, *_ = mixed
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    target = tmp_path / 'interrupted'
    with pytest.raises(InterruptedError, match='cancelled'):
        restore_backup(backup, target, checksum, cancelled=lambda: target.exists())
    assert (target / INCOMPLETE).is_file()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(target)


def test_canonical_engine_registration_restore_requires_revalidation_without_path_access(mixed, tmp_path):
    from carr_library.runtime_settings import voice_registry, audio_runtime

    app, *_ = mixed
    settings = {'schema': 1, 'ffmpeg': 'Z:\\Previous machine\\ffmpeg.exe',
        'ffprobe': 'Z:\\Previous machine\\ffprobe.exe', 'piper': [], 'voice_helpers': [],
        'music_models': [], 'fluidsynth': None}
    atomic_json(app.root / 'state/audio/engines.json', settings)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    assert read_json(root / 'state/audio/engines.json') == {**settings, 'restore_requires_revalidation': True}
    assert read_json(app.root / 'state/audio/engines.json') == settings
    with pytest.raises((ValueError, PermissionError), match='revalidat|Restored|restored'):
        voice_registry(root / 'state')
    with pytest.raises((ValueError, PermissionError), match='revalidat|Restored|restored'):
        audio_runtime(tmp_path / 'no-runtime-created', state_root=root / 'state')
    assert not (tmp_path / 'no-runtime-created').exists()


def test_boolean_production_schema_cannot_escape_restore_guard(mixed, tmp_path):
    from carr_library.studio.project import parse_script as canonical_script

    app, *_ = mixed
    project = canonical_script('Narrator: Exact script.', voice_ids=('local-voice',))
    atomic_json(app.root / 'state/studio/jobs/job/job.json',
        {'schema': True, 'id': 'job', 'project': project.to_dict(),
         'fingerprint': project.fingerprint, 'destination': str(tmp_path / 'external')})
    with pytest.raises(ValueError, match='schema type'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
