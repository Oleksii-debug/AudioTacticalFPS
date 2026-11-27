"""Real restoration of the other accepted canonical backup envelopes.

The fixtures transpose an actual closed SQLite/original/pending snapshot into
the donor's exact root-relative inventory. They do not mock restoration, source
authorization, schema fingerprints or the producer validators.
"""
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
from time import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_compat, library_backup, runtime_settings
from carr_library.durable_state import atomic_json, read_json
from carr_library.evidence_drafts import EvidenceDrafts
from carr_library.ingest.restore import RestoreError
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.reader.reading_state import ReadingLocation, ReadingState
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION, Storage
from carr_library.storage.schema_identity import expected_schema_fingerprint


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _seal(folder, manifest):
    raw = _json(manifest)
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _entry(folder, name):
    raw = (folder / name).read_bytes()
    return {'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}


def _replace_entry(folder, manifest, name, raw):
    (folder / name).write_bytes(raw)
    manifest['sidecars'] = [_entry(folder, name) if row['filename'] == name else row
                           for row in manifest['sidecars']]
    return _seal(folder, manifest)


def _flat_backup(app, destination, *, generated=False):
    create_backup(app, destination, include_generated=generated)
    own = json.loads((destination / 'manifest.json').read_bytes())
    entries = []
    for row in own.get('sidecars', {}).get('files', []):
        name = row['path']
        target = destination / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(destination / 'sidecars' / name, target)
        entries.append({'filename': name, 'size_bytes': row['size_bytes'], 'sha256': row['sha256']})
    if (destination / 'sidecars').exists():
        shutil.rmtree(destination / 'sidecars')
    for row in own.get('state', []):
        entries.append({**row, 'filename': 'state/' + row['filename']})
    if not own.get('state') and (destination / 'state').exists():
        (destination / 'state').rmdir()
    base = {key: own[key] for key in ('kind', 'schema_version', 'schema_fingerprint', 'database', 'assets')}
    manifest = {**base, 'version': 4, 'sidecars': sorted(entries, key=lambda row: row['filename']),
                'generated_audio_included': generated}
    if 'pending' in own:
        manifest['pending'] = own['pending']
    return manifest, _seal(destination, manifest)


def _without_pending(folder, manifest):
    if 'pending' in manifest:
        manifest.pop('pending')
        shutil.rmtree(folder / 'pending')


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Canonical recovery')
    topic = app.create_topic(workspace, 'Exact source')
    app.add_text(workspace, 'Original text', 'Exact immutable railway source.')
    hit, = app.search(workspace, 'railway')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved baseline')
    row, = app.evidence(workspace, topic)
    with app.read(workspace) as db:
        anchor, loader, _ = app._anchor(db, workspace, hit.ref, hit.revision)
        _, _, raw, _ = loader(anchor.identity, anchor.locator)
    location = ReadingLocation(workspace, hit.edition_id, hit.revision, anchor.locator.kind,
        anchor.locator.index, sha256(raw.encode('utf-8')).hexdigest(), anchor.identity.asset_sha256)
    ReadingState(app.root).remember(location)
    return app, row, location


def test_flat_v4_restores_exact_reader_drafts_preferences_and_pending_source(library, tmp_path):
    app, row, location = library
    drafts = EvidenceDrafts(app.root)
    draft = drafts.checkpoint(row, 'Незбережена думка.\r\nExact author text.\x00')
    with app.evidence_transaction(location.workspace) as store:
        store.edit_note(row.topic_id, row.evidence_id, 'Saved in another window')
    old_keymap = _json({'version': 1, 'bindings': {'future.module': 'Ctrl+Alt+K'}})
    old_progress = _json({'version': 1, 'preferences': {'mode': 'beep_speech',
        'interval_seconds': 5.0, 'volume': 30, 'muted': False,
        'allow_background': False, 'quiet_during_media': True}})
    (app.root / 'keyboard-shortcuts.json').write_bytes(old_keymap)
    (app.root / 'progress-preferences.json').write_bytes(old_progress)
    pending_source = tmp_path / 'pending.txt'
    source_bytes = b'Unpublished canonical pending text.'
    pending_source.write_bytes(source_bytes)
    batch = app.plan_import_batch(location.workspace, [pending_source])
    backup = tmp_path / 'flat'
    manifest, checksum = _flat_backup(app, backup)
    before_manifest = (backup / 'manifest.json').read_bytes()
    assert manifest['schema_version'] == CURRENT_SCHEMA_VERSION == 6
    assert type(manifest['sidecars']) is list and not (backup / 'sidecars').exists()
    pending_originals = [r for r in manifest['pending']['files'] if r['filename'].startswith('originals/')]
    pending_digest = sha256(source_bytes).hexdigest()
    # The canonical journal retains both the completed text source and the
    # unpublished batch source. Bind every exact file instead of assuming only
    # the not-yet-registered request has an original in that producer.
    expected_originals = {
        'originals/' + location.asset_sha256 + '.txt': app.assets.resolve(location.asset_sha256).read_bytes(),
        'originals/' + pending_digest + '.txt': source_bytes,
    }
    assert len(pending_originals) == 2
    assert {r['filename']: (r['size_bytes'], r['sha256']) for r in pending_originals} == {
        name: (len(payload), sha256(payload).hexdigest()) for name, payload in expected_originals.items()
    }
    for name, payload in expected_originals.items():
        assert (backup / 'pending' / name).read_bytes() == payload
    pending_source.unlink()
    restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = LibraryApp(restored_root)
    assert ReadingState(restored_root).resume(location.workspace, location.edition) == location
    copied = EvidenceDrafts(restored_root).get(*draft.key)
    assert copied == draft
    saved, = restored.evidence(location.workspace, row.topic_id)
    assert saved.note == 'Saved in another window'
    assert copied.source_matches(saved) and not copied.matches(saved)
    assert (restored_root / 'keyboard-shortcuts.json').read_bytes() == old_keymap
    assert (restored_root / 'progress-preferences.json').read_bytes() == old_progress
    copied_batch = restored.import_batch(location.workspace, batch['batch_id'])
    selected, = copied_batch['rows']
    assert Path(selected['source']).is_relative_to(restored_root / 'originals')
    assert Path(selected['source']).read_bytes() == source_bytes
    assert restored.run_import_batch(location.workspace, batch['batch_id'])['state'] == 'complete'
    assert restored.search(location.workspace, 'Unpublished')
    assert not (restored_root / INCOMPLETE).exists()
    assert (backup / 'manifest.json').read_bytes() == before_manifest
    # Subsequent saves use our one existing composite writer, not a new store.
    next_backup = tmp_path / 'next-backup'
    create_backup(restored, next_backup)
    next_manifest = json.loads((next_backup / 'manifest.json').read_bytes())
    assert next_manifest['version'] == 5 and type(next_manifest['sidecars']) is dict
    names = {r['path'] for r in next_manifest['sidecars']['files']}
    assert {'evidence-drafts.json', 'keyboard-shortcuts.json', 'progress-preferences.json'} <= names


@pytest.mark.parametrize('shape', ['v3-list', 'v3-reading', 'v3-empty', 'v5-reading'], ids=str)
def test_exact_other_canonical_envelopes_remain_distinct(library, tmp_path, shape):
    app, _, location = library
    backup = tmp_path / shape
    manifest, _ = _flat_backup(app, backup)
    _without_pending(backup, manifest)
    reading, = manifest.pop('sidecars')
    manifest.pop('generated_audio_included')
    manifest['version'] = 3
    if shape == 'v3-list':
        manifest['sidecars'] = [reading]
    elif shape == 'v3-empty':
        manifest['reading_state'] = None
        (backup / 'reading-state.json').unlink()
    else:
        manifest['reading_state'] = reading
        if shape == 'v5-reading':
            manifest.update(version=5, state=[], generated_audio_included=False, pending=None)
            (backup / 'state').mkdir()
    root = restore_backup(backup, tmp_path / 'restored', _seal(backup, manifest))
    expected = None if shape == 'v3-empty' else location
    assert ReadingState(root).resume(location.workspace, location.edition) == expected
    assert LibraryApp(root).search(location.workspace, 'railway')


@pytest.mark.parametrize('schema', [3, 4, 5, 6])
def test_flat_reader_preserves_exact_schema_then_normal_open_migrates(tmp_path, schema):
    backup = tmp_path / 'backup'
    backup.mkdir()
    (backup / 'originals').mkdir()
    with closing(Storage(tmp_path / 'original.sqlite', schema_version=schema)) as storage:
        storage.backup(backup / 'library.sqlite')
    raw = _json({'version': 1, 'bindings': {'future.command': 'Ctrl+Alt+Y'}})
    (backup / 'keyboard-shortcuts.json').write_bytes(raw)
    manifest = {'version': 4, 'kind': 'carr-library', 'schema_version': schema,
        'schema_fingerprint': expected_schema_fingerprint(schema),
        'database': _entry(backup, 'library.sqlite'), 'assets': [],
        'sidecars': [_entry(backup, 'keyboard-shortcuts.json')], 'generated_audio_included': False}
    root = restore_backup(backup, tmp_path / 'restored', _seal(backup, manifest))
    with closing(sqlite3.connect(root / 'library.sqlite')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == schema
    LibraryApp(root)
    with closing(sqlite3.connect(root / 'library.sqlite')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert (root / 'keyboard-shortcuts.json').read_bytes() == raw


def test_flat_provider_usage_and_assistant_records_lose_old_execution_authority(library, tmp_path):
    from carr_library.assistant.models import Plan, Scope, Step
    from carr_library.assistant.profiles import ProfileStore, ProviderProfile
    from carr_library.assistant.providers import Authorization, UsageLedger
    from carr_library.assistant.service import AssistantService

    app, _, location = library
    profile = ProviderProfile('remote', 'Selected account', 'openai-compatible',
        'https://api.example.com/v1', 'model', credential_ref='carr/provider/remote/' + 'a' * 32,
        enabled=True)
    ProfileStore(app.root / 'state/providers').save(profile)
    approval = Authorization('approval', 'task', ((profile.id, profile.endpoint),),
        ('llm.chat',), ('synthetic-test',), '1', 2, time() + 3600)
    ledger = UsageLedger(app.root / 'state/providers/usage')
    ledger.reserve(approval, 'original', 'b' * 64, Decimal('0.1'), profile, 'llm.chat')
    # The donor's original ledger predates this host's additive restore marker.
    ledger_path = app.root / 'state/providers/usage/approval.usage.json'
    before_usage = read_json(ledger_path)
    before_usage.pop('restored', None)
    atomic_json(ledger_path, before_usage, replace=True)
    service = AssistantService(app)
    identity = service.submit(Plan(Scope(workspace_id=location.workspace), (Step('library.list'),), 'Review'))
    before_plan = read_json(app.root / 'state/assistant' / (identity + '.json'))
    backup = tmp_path / 'backup'
    _, checksum = _flat_backup(app, backup)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    assert ProfileStore(root / 'state/providers').get(profile.id).credential_ref is None
    assert ProfileStore(root / 'state/providers').get(profile.id).enabled is False
    assert read_json(root / 'state/providers/usage/approval.usage.json') == {**before_usage, 'restored': True}
    assert read_json(root / 'state/assistant' / (identity + '.json')) == {**before_plan, 'restore_requires_review': True}
    with pytest.raises(PermissionError, match='restored|Restored'):
        UsageLedger(root / 'state/providers/usage').reserve(
            approval, 'new', 'c' * 64, Decimal('0.1'), profile, 'llm.chat')
    restored = AssistantService(LibraryApp(root))
    assert restored.get(identity).id == identity
    with pytest.raises(PermissionError, match='restored|Restored'):
        restored.run(identity)


def _production_with_retained_budget(app, output, budget):
    """Persist an actual submitted job with its producer's optional allowance.

    No audio runtime is created: renderer acceptance separately checks that
    running jobs record this field. This fixture exercises the actual project,
    queue, backup, restore and restored-execution boundary.
    """
    from carr_library.speech import VoiceRegistry
    from carr_library.studio.render import ProductionService

    output.mkdir()
    service = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]))
    project = service.create('[Scene: Budget recovery]\n[Pause: 0.1]', mode='authored')
    identity = service.submit(project, output, stems=False)
    record = service.get(identity)
    record['total_disk_bytes'] = budget
    path = service.jobs_root / identity / 'job.json'
    atomic_json(path, record, replace=True)
    assert service.get(identity) == record
    service.close()
    return identity, path, record


@pytest.mark.parametrize('shape', ['composite', 'flat'], ids=str)
@pytest.mark.parametrize('budget', [1024**2, 2**63 - 1], ids=['minimum', 'maximum'])
def test_production_disk_budget_survives_backup_without_restoring_output_authority(
        library, tmp_path, monkeypatch, shape, budget):
    from carr_library.studio.render import ProductionService

    app, _, location = library
    identity, path, original = _production_with_retained_budget(app, tmp_path / 'output', budget)
    source_bytes = path.read_bytes()
    old_output = Path(original['destination'])
    old_output.mkdir()
    sentinel = old_output / 'unrelated.txt'
    sentinel.write_bytes(b'Existing independent destination bytes.')
    backup = tmp_path / 'backup'
    if shape == 'flat':
        _, checksum = _flat_backup(app, backup)
    else:
        create_backup(app, backup)
        checksum = sha256((backup / 'manifest.json').read_bytes()).hexdigest()
    stored_job = backup / 'state/studio/jobs' / identity / 'job.json'
    assert stored_job.read_bytes() == source_bytes
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    assert LibraryApp(root).search(location.workspace, 'railway')
    service = ProductionService(root / 'state/studio')
    expected = {**original, 'restore_requires_new_output': True}
    assert service.get(identity) == expected
    assert read_json(root / 'state/studio/jobs' / identity / 'job.json') == expected

    def forbidden():
        raise AssertionError('Restoring a disk allowance must not activate an audio runtime.')

    monkeypatch.setattr(service, '_runtime', forbidden)
    with pytest.raises(ValueError, match='restored.*new output'):
        service.run(identity)
    assert service.get(identity) == expected
    assert sentinel.read_bytes() == b'Existing independent destination bytes.'
    assert sorted(item.name for item in old_output.iterdir()) == ['unrelated.txt']
    assert path.read_bytes() == stored_job.read_bytes() == source_bytes
    service.close()


@pytest.mark.parametrize('invalid', [True, False, -1, 0, 1024**2 - 1, 2**63,
    float(1024**2), '1048576', None],
    ids=['true', 'false', 'negative', 'zero', 'below-minimum', 'above-maximum',
         'float', 'string', 'null'])
def test_flat_production_budget_rejects_invalid_type_or_range_before_publication(
        library, tmp_path, invalid):
    app, _, _ = library
    _, path, _ = _production_with_retained_budget(app, tmp_path / 'output', invalid)
    original = path.read_bytes()
    backup = tmp_path / 'backup'
    _, checksum = _flat_backup(app, backup)
    destination = tmp_path / 'rejected'
    with pytest.raises(RestoreError, match='checkpoint number'):
        restore_backup(backup, destination, checksum)
    assert not destination.exists()
    assert path.read_bytes() == original


def _engines():
    return {'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [], 'voice_helpers': [],
            'music_models': [], 'fluidsynth': None}


def test_canonical_dordiai_declaration_is_preserved_without_access_or_activation(library, tmp_path, monkeypatch):
    app, _, _ = library
    settings = {**_engines(), 'dordiai': {'enabled': True,
        'runtime_directory': r'C:\OwnerPrivate\Dordiai', 'python_executable': r'C:\OwnerPrivate\python.exe',
        'expected_worker_sha256': 'a' * 64}}
    atomic_json(app.root / 'state/audio/engines.json', settings)
    backup = tmp_path / 'backup'
    _, checksum = _flat_backup(app, backup)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    assert runtime_settings.load_settings(root / 'state') == {**settings, 'restore_requires_revalidation': True}
    def forbidden(*args, **kwargs):
        raise AssertionError('An inert declaration must not resolve or launch its assets.')
    monkeypatch.setattr(runtime_settings, 'verify_configured_audio_runtime', forbidden)
    with pytest.raises(ValueError, match='[Rr]estor'):
        runtime_settings.voice_registry(root / 'state')
    with pytest.raises(ValueError, match='migration'):
        runtime_settings.save_settings(root / 'state', {**settings, 'restore_requires_revalidation': False})
    assert read_json(root / 'state/audio/engines.json') == {**settings, 'restore_requires_revalidation': True}
    # Even a legacy unmarked file cannot silently fall back to another engine.
    atomic_json(root / 'state/audio/engines.json', settings, replace=True)
    with pytest.raises(ValueError, match='migration'):
        runtime_settings.voice_registry(root / 'state')


def test_generated_cache_requires_exact_completed_receipt_and_inclusion(library, tmp_path):
    app, _, _ = library
    key = 'e' * 64
    name = key + '-0123456789.wav'
    payload = io.BytesIO()
    with wave.open(payload, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\0\0' * 160)
    raw = payload.getvalue()
    folder = app.root / 'state/studio/cache'
    folder.mkdir(parents=True)
    (folder / name).write_bytes(raw)
    atomic_json(folder / (key + '.json'), {'schema': 1, 'key': key, 'file': name,
        'sha256': sha256(raw).hexdigest(), 'duration': 0.01, 'sample_rate': 16000, 'channels': 1})
    backup = tmp_path / 'backup'
    manifest, checksum = _flat_backup(app, backup, generated=True)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    assert (root / 'state/studio/cache' / name).read_bytes() == raw
    manifest['generated_audio_included'] = False
    with pytest.raises(RestoreError, match='explicit'):
        restore_backup(backup, tmp_path / 'no-consent', _seal(backup, manifest))
    manifest['generated_audio_included'] = True
    receipt_name = 'state/studio/cache/' + key + '.json'
    receipt = read_json(backup / receipt_name)
    receipt['sha256'] = 'a' * 64
    checksum = _replace_entry(backup, manifest, receipt_name, _json(receipt))
    with pytest.raises(RestoreError, match='receipt|disagree'):
        restore_backup(backup, tmp_path / 'wrong-receipt', checksum)
    assert not (tmp_path / 'no-consent').exists() and not (tmp_path / 'wrong-receipt').exists()


@pytest.mark.parametrize('damage', ['mixed-envelope', 'pending-null', 'bool-audio', 'duplicate',
    'traversal', 'oversize', 'changed-bytes', 'missing-file', 'unknown-file', 'empty-directory', 'hardlink'],
    ids=str)
def test_foreign_envelope_and_inventory_fail_closed_before_publication(library, tmp_path, damage):
    app, _, _ = library
    backup = tmp_path / 'backup'
    manifest, checksum = _flat_backup(app, backup)
    path = backup / 'reading-state.json'
    if damage == 'mixed-envelope':
        manifest['state'] = []
    elif damage == 'pending-null':
        manifest['pending'] = None
    elif damage == 'bool-audio':
        manifest['generated_audio_included'] = 1
    elif damage == 'duplicate':
        manifest['sidecars'].append(dict(manifest['sidecars'][0]))
    elif damage == 'traversal':
        manifest['sidecars'][0]['filename'] = '../outside.json'
    elif damage == 'oversize':
        manifest['sidecars'][0]['size_bytes'] = 2**40
    elif damage == 'changed-bytes':
        raw = path.read_bytes()
        path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    elif damage == 'missing-file':
        path.unlink()
    elif damage == 'unknown-file':
        (backup / 'surprise.json').write_bytes(b'{}')
    elif damage == 'empty-directory':
        (backup / 'unlisted-directory').mkdir()
    elif damage == 'hardlink':
        outside = tmp_path / 'other-copy'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        os.link(outside, path)
    if damage in {'mixed-envelope', 'pending-null', 'bool-audio', 'duplicate', 'traversal', 'oversize'}:
        checksum = _seal(backup, manifest)
    destination = tmp_path / 'rejected'
    with pytest.raises((RestoreError, OSError, ValueError)):
        restore_backup(backup, destination, checksum)
    assert not destination.exists()


def test_foreign_draft_identity_is_rejected_but_stale_baselines_are_not_retargeted(library, tmp_path):
    app, row, _ = library
    EvidenceDrafts(app.root).checkpoint(row, 'Private pending note')
    backup = tmp_path / 'backup'
    manifest, _ = _flat_backup(app, backup)
    name = 'evidence-drafts.json'
    value = read_json(backup / name)
    value['drafts'][0]['workspace'] = 'another-workspace'
    checksum = _replace_entry(backup, manifest, name, _json(value))
    with pytest.raises(RestoreError, match='matching exact saved evidence'):
        restore_backup(backup, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()


def test_current_writer_also_binds_evidence_and_keeps_the_previous_draft(library, tmp_path):
    from dataclasses import replace
    app, row, _ = library
    store = EvidenceDrafts(app.root)
    store.checkpoint(replace(row, evidence_id='not-saved'), 'Foreign draft')
    before = store.path.read_bytes()
    with pytest.raises(RestoreError, match='matching exact saved evidence'):
        create_backup(app, tmp_path / 'rejected')
    assert store.path.read_bytes() == before and not (tmp_path / 'rejected').exists()


def test_root_preferences_cannot_hide_an_oversize_raw_document(library, tmp_path):
    app, _, _ = library
    path = app.root / 'progress-preferences.json'
    raw = _json({'version': 1, 'preferences': {'mode': 'off', 'interval_seconds': 5,
        'volume': 30, 'muted': False, 'allow_background': False, 'quiet_during_media': True}})
    path.write_bytes(raw + b' ' * 8192)
    with pytest.raises(RestoreError, match='size limit'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists() and path.read_bytes() == raw + b' ' * 8192


def test_unreadable_root_draft_is_not_silently_omitted(library, tmp_path, monkeypatch):
    app, row, _ = library
    EvidenceDrafts(app.root).checkpoint(row, 'Must not disappear')
    target = app.root / 'evidence-drafts.json'
    original = Path.lstat
    def guarded(path, *args, **kwargs):
        if path == target:
            raise PermissionError('synthetic unreadable recovery file')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', guarded)
    with pytest.raises(PermissionError, match='unreadable'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_unknown_namespace_entry_is_rejected_before_consuming_unbounded_iterator(library, tmp_path, monkeypatch):
    app, _, _ = library
    backup = tmp_path / 'backup'
    _, checksum = _flat_backup(app, backup)
    original = Path.iterdir
    def guarded(path):
        if path == backup:
            yield path / 'not-in-manifest'
            raise AssertionError('The inventory eagerly consumed unknown extra entries.')
        else:
            yield from original(path)
    monkeypatch.setattr(Path, 'iterdir', guarded)
    with pytest.raises(RestoreError, match='unexpected'):
        restore_backup(backup, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('phase', ['copy', 'publication'], ids=str)
def test_flat_restore_cancellation_never_yields_a_usable_partial_root(library, tmp_path, monkeypatch, phase):
    app, _, _ = library
    backup = tmp_path / 'backup'
    _, checksum = _flat_backup(app, backup)
    destination = tmp_path / 'cancelled'
    stopped = False
    if phase == 'copy':
        original = backup_compat._stream_file
        def copied(*args, **kwargs):
            nonlocal stopped
            result = original(*args, **kwargs)
            stopped = True
            return result
        monkeypatch.setattr(backup_compat, '_stream_file', copied)
    else:
        original = os.link
        def linked(source, target, *args, **kwargs):
            nonlocal stopped
            result = original(source, target, *args, **kwargs)
            if Path(target) == destination / 'reading-state.json':
                stopped = True
            return result
        monkeypatch.setattr(os, 'link', linked)
    with pytest.raises(InterruptedError, match='[Cc]ancel'):
        restore_backup(backup, destination, checksum, cancelled=lambda: stopped)
    assert stopped
    if phase == 'copy':
        assert not destination.exists()
    else:
        assert (destination / INCOMPLETE).is_file()
        with pytest.raises((ValueError, OSError), match='incomplete|[Rr]estor'):
            LibraryApp(destination)


@pytest.mark.parametrize('invalid', [True, {'enabled': 1}, {'enabled': True},
    {'enabled': False, 'expected_worker_sha256': 'G' * 64},
    {'enabled': False, 'unknown': 'not-a-schema-field'}], ids=['bool', 'int-enabled', 'missing-path', 'bad-hash', 'extra'])
def test_retained_dordiai_configuration_rejects_invalid_schema_without_io(invalid):
    with pytest.raises(ValueError):
        runtime_settings._settings_document({**_engines(), 'dordiai': invalid})
