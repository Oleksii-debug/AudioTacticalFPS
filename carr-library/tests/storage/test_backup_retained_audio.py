"""Common-format4 closure for retained media and saved source-bound work.

Fixtures are generated PCM and actual synthetic child/gateway protocols.
No microphone, model inference, provider network, or old output is activated.
"""
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest

from carr_library import backup_sidecars
from carr_library.application import LibraryApp
from carr_library.assistant.providers import Gateway, UsageLedger
from carr_library.assistant.translation import TranslationService
from carr_library.library_backup import create_backup, restore_backup
from carr_library.studio.project import Cue, Project, ProjectStore, Scene


def fixture_file(relative):
    return runpy.run_path(str(Path(__file__).parents[1] / relative))


def reseal(folder, manifest):
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def changed_entry(folder, manifest, name):
    raw = (folder / name).read_bytes()
    row = next(row for row in manifest['sidecars'] if row['filename'] == name)
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())


@pytest.mark.parametrize('name', [
    'state/studio/.local-origin.json',
    'State/Studio/.LOCAL-ORIGIN.JSON',
    'state/studio/.local-origin.json/authority.json',
])
def test_common4_manifest_cannot_supply_local_output_authority(tmp_path, name):
    app = LibraryApp(tmp_path / 'library')
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    authority = b'{"schema":"carr-studio-origin/1","nonce":"' + b'a' * 64 + b'"}\n'
    target = backup / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(authority)
    manifest['sidecars'].append({'filename': name, 'size_bytes': len(authority),
                                 'sha256': sha256(authority).hexdigest()})
    digest = reseal(backup, manifest)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='origin authority'):
        restore_backup(backup, destination, digest)
    assert not destination.exists() and target.read_bytes() == authority


@pytest.mark.parametrize('name', ['state/studio/.local-origin.json', 'State/Studio/.LOCAL-ORIGIN.JSON'])
def test_current_sidecar_restore_refuses_existing_origin_before_copy(tmp_path, name):
    stage, source = tmp_path / 'stage', tmp_path / 'source'
    source.mkdir()
    origin = stage / name
    origin.parent.mkdir(parents=True)
    origin.write_bytes(b'Existing local authority must remain untouched.')
    with pytest.raises(ValueError, match='existing local Studio origin'):
        backup_sidecars.restore(None, source, stage, backup_sidecars.Inventory(()))
    assert {path.relative_to(stage).as_posix() for path in stage.rglob('*') if path.is_file()} == {name}
    assert origin.read_bytes() == b'Existing local authority must remain untouched.'


@pytest.mark.parametrize('operation', ['snapshot', 'restore'])
@pytest.mark.parametrize('damage', ['missing-receipt', 'changed-wave', 'changed-receipt'])
def test_common4_recording_requires_actual_pcm_and_matching_receipt(tmp_path, monkeypatch, operation, damage):
    helper = fixture_file('audio/test_recording_export.py')
    saved = helper['retained'].__wrapped__(tmp_path, monkeypatch)
    backup = tmp_path / 'backup'
    if operation == 'restore':
        create_backup(saved.app, backup)
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        root = backup
    else:
        root = saved.app.root
    wave_name = saved.source.relative_to(saved.app.root).as_posix()
    receipt_name = saved.receipt_path.relative_to(saved.app.root).as_posix()
    if damage == 'missing-receipt':
        (root / receipt_name).unlink()
        if operation == 'restore':
            manifest['sidecars'] = [row for row in manifest['sidecars'] if row['filename'] != receipt_name]
    elif damage == 'changed-wave':
        wave = root / wave_name
        raw = wave.read_bytes()
        wave.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        if operation == 'restore':
            changed_entry(backup, manifest, wave_name)
    else:
        receipt = root / receipt_name
        value = json.loads(receipt.read_bytes())
        value['audio']['sha256'] = '0' * 64
        receipt.write_text(json.dumps(value), encoding='utf-8')
        if operation == 'restore':
            changed_entry(backup, manifest, receipt_name)
    rejected = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='recording|receipt'):
        if operation == 'snapshot':
            create_backup(saved.app, rejected)
        else:
            restore_backup(backup, rejected, reseal(backup, manifest))
    assert not rejected.exists()


@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_common4_managed_clip_reference_checks_only_copied_local_input(tmp_path, damage):
    app = LibraryApp(tmp_path / 'library')
    raw = b'Synthetic retained input identity; no inference.'
    checksum = sha256(raw).hexdigest()
    relative = 'inputs/' + checksum + '.audio'
    studio = app.root / 'state/studio'
    target = studio / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    project = Project('portable', 'Portable', (), (Scene('scene', 'Scene', (),
        cues=(Cue('selected', relative),)),), schema=2)
    ProjectStore(studio / 'projects').save(project)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    selected = 'state/studio/' + relative
    if damage == 'missing':
        (backup / selected).unlink()
        (backup / selected).parent.rmdir()
        manifest['sidecars'] = [row for row in manifest['sidecars'] if row['filename'] != selected]
    else:
        (backup / selected).write_bytes(b'Changed copied original.')
        changed_entry(backup, manifest, selected)
    with pytest.raises(ValueError, match='managed Studio input'):
        restore_backup(backup, tmp_path / 'rejected', reseal(backup, manifest))
    assert not (tmp_path / 'rejected').exists()
    assert target.read_bytes() == raw


def test_common4_pinned_helper_pair_is_passive_and_retained(tmp_path, monkeypatch):
    helper = fixture_file('storage/test_engine_settings_backup.py')
    app = LibraryApp(tmp_path / 'library')
    value = helper['configuration']()
    value['voice_helpers'][0].update(script='/absent-reviewed-adapter/adapter.py', script_sha256='1' * 64)
    value['voice_helpers'][1].update(script='../absent-reviewed-adapter/adapter.py', script_sha256='2' * 64)
    value['voice_helpers'].append({'executable': 'absent-helper.exe', 'sha256': '3' * 64,
        'engine_name': 'Legacy executable-only helper', 'script': None, 'script_sha256': None})
    raw = helper['write_settings'](app.root, value)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Backup activated an engine.'))
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    target = restore_backup(backup, tmp_path / 'restored', digest)
    assert (target / helper['NAME']).read_bytes() == raw


@pytest.mark.parametrize('missing', ['script', 'script_sha256'])
def test_common4_rejects_unpaired_helper_identity(tmp_path, missing):
    helper = fixture_file('storage/test_engine_settings_backup.py')
    app = LibraryApp(tmp_path / 'library')
    value = helper['configuration']()
    value['voice_helpers'][0].update(script='/absent-reviewed-adapter/adapter.py', script_sha256='1' * 64)
    del value['voice_helpers'][0][missing]
    helper['write_settings'](app.root, value)
    with pytest.raises(ValueError, match='configured together'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('damage', ['missing-input', 'changed-output', 'changed-edit-source'])
def test_common4_transcription_rechecks_saved_receipts_without_engine(tmp_path, damage, monkeypatch):
    helper = fixture_file('transcription/test_supplied_worker_bridge.py')
    prepared = helper['prepared'].__wrapped__(tmp_path)
    app, _, service, _, _ = prepared
    identity, result = helper['completed'](prepared)
    revision = service.save_edit(identity, result['sources'][0]['filename'], 'An explicitly saved correction.')
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    prefix = service._path(identity).relative_to(app.root).as_posix()
    if damage == 'missing-input':
        name = prefix + '/input/' + result['sources'][0]['filename']
        (backup / name).unlink()
        manifest['sidecars'] = [row for row in manifest['sidecars'] if row['filename'] != name]
    elif damage == 'changed-output':
        name = prefix + '/results/' + result['artifacts'][0]['path']
        (backup / name).write_bytes(b'Changed accepted output.')
        changed_entry(backup, manifest, name)
    else:
        name = prefix + '/edits/' + revision + '.json'
        value = json.loads((backup / name).read_bytes())
        value['source_sha256'] = '0' * 64
        (backup / name).write_text(json.dumps(value), encoding='utf-8')
        changed_entry(backup, manifest, name)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Restore activated a worker.'))
    with pytest.raises(ValueError, match='transcription|retained|Transcript'):
        restore_backup(backup, tmp_path / 'rejected', reseal(backup, manifest))
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('damage', [None, 'ledger-text', 'missing-ledger'])
def test_advanced_source_translation_backup_keeps_offline_export_and_receipt_binding(tmp_path, monkeypatch, damage):
    helper = fixture_file('assistant/test_translation_advanced_source.py')
    case = helper['advanced_source'].__wrapped__(tmp_path, monkeypatch)
    identity, approval = helper['submit'](case, helper['GLOSSARY'])
    assert case.service.resume(identity)['status'] == 'complete'
    original_job = case.service.jobs.load(identity)
    calls = len(case.calls)
    backup = tmp_path / 'backup'
    with monkeypatch.context() as inactive:
        inactive.setattr(Gateway, '__init__', lambda *a, **k: pytest.fail('Backup constructed a gateway.'))
        inactive.setattr(Gateway, 'request', lambda *a, **k: pytest.fail('Backup called a provider.'))
        inactive.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Backup started a process.'))
        digest = create_backup(case.app, backup)
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        ledger = 'state/translation/provider/usage/' + approval.id + '.usage.json'
        if damage == 'missing-ledger':
            (backup / ledger).unlink()
            manifest['sidecars'] = [row for row in manifest['sidecars'] if row['filename'] != ledger]
            digest = reseal(backup, manifest)
        elif damage == 'ledger-text':
            value = json.loads((backup / ledger).read_bytes())
            value['operations'][identity + '_0']['usage']['translation_sha256'] = '0' * 64
            (backup / ledger).write_text(json.dumps(value), encoding='utf-8')
            changed_entry(backup, manifest, ledger)
            digest = reseal(backup, manifest)
        if damage:
            with pytest.raises(ValueError, match='translation|Translation'):
                restore_backup(backup, tmp_path / 'rejected', digest)
            assert not (tmp_path / 'rejected').exists()
            return
        restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
        directory = restored.root / 'state/translation'
        passive = SimpleNamespace(ledger=UsageLedger(directory / 'provider/usage'))
        service = TranslationService(directory, library=restored, gateway=passive)
        assert service.jobs.load(identity) == original_job
        exported = service.export(identity, tmp_path / 'offline-export')
        assert exported['status'] == 'complete'
        assert (tmp_path / 'offline-export/original.txt').read_text(encoding='utf-8') == case.source.text
    assert len(case.calls) == calls
