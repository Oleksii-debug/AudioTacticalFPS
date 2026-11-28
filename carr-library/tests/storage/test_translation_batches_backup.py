"""Real common4 snapshots preserve translation grouping without remote activation."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.models import canonical
from carr_library.assistant.providers import Gateway, translation_receipt_sha256
from carr_library.assistant.translation import TranslationService
from carr_library.assistant.translation_batch import TranslationBatchService
from carr_library.library_backup import restore_backup

_fixture = runpy.run_path(str(Path(__file__).parents[1] / 'assistant' / 'translation_batch_fixtures.py'))
transport, source_library, batch_env = (_fixture[name] for name in ('transport', 'source_library', 'batch_env'))
TOKEN = _fixture['TOKEN']


def prepared(e):
    review = e.batches.preview(e.items)
    identity = e.batches.submit(e.items, review_sha256=review['review_sha256'], approve_remote=True)
    return identity, e.batches._load(identity)


def rehash_backup(folder, name, value):
    raw = (canonical(value) + '\n').encode('utf-8')
    (folder / name).write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    entry = next(row for row in manifest['sidecars'] if row['filename'] == name)
    entry.update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    raw = (canonical(manifest) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.mark.parametrize('phase', ['preparing', 'paused', 'complete'])
def test_actual_common4_restore_keeps_original_templates_receipts_and_forces_pause(
        batch_env, tmp_path, monkeypatch, transport, phase):
    e = batch_env
    identity, manifest = prepared(e)
    if phase == 'preparing':
        e.service.jobs.path(manifest['members'][1]['job']['id']).unlink()
    elif phase == 'paused':
        stopped = threading.Event()
        assert e.batches.resume(identity, cancelled=stopped.is_set,
            progress=lambda *args: stopped.set())['status'] == 'paused'
    else:
        assert e.batches.resume(identity)['status'] == 'complete'
    before = e.batches.get(identity)
    manifest_raw = e.batches.jobs.path(identity).read_bytes()
    children = {row['job']['id']: e.service.jobs.path(row['job']['id']).read_bytes()
                for row in manifest['members'] if e.service.jobs.path(row['job']['id']).exists()}
    folder = tmp_path / 'batch-backup'
    calls = len(transport['calls']), len(e.vault.calls)
    def forbidden(*args, **kwargs):
        pytest.fail('Passive backup/restore must not activate a provider, vault or worker.')
    with monkeypatch.context() as offline:
        offline.setattr(Gateway, '__init__', forbidden)
        offline.setattr(Gateway, 'request', forbidden)
        offline.setattr(subprocess, 'Popen', forbidden)
        checksum = e.app.backup(folder)
        inventory = json.loads((folder / 'manifest.json').read_bytes())
        assert inventory['version'] == 4
        assert any(row['filename'] == 'state/translation/batches/' + identity + '.json'
                   for row in inventory['sidecars'])
        shutil.rmtree(e.app.root)
        destination = restore_backup(folder, tmp_path / 'restored-library', checksum)
    assert (len(transport['calls']), len(e.vault.calls)) == calls
    app = LibraryApp(destination)
    service = TranslationService(app.root / 'state' / 'translation', library=app,
        gateway=Gateway(app.root / 'state' / 'translation' / 'provider', vault=e.vault, mode='APPROVED_REMOTE'))
    batches = TranslationBatchService(service)
    assert batches.jobs.path(identity).read_bytes() == manifest_raw
    assert {key: service.jobs.path(key).read_bytes() for key in children} == children
    assert batches.get(identity)['control'] == 'pause'
    assert {key: value for key, value in batches.get(identity).items() if key != 'control'} == {
        key: value for key, value in before.items() if key != 'control'}
    assert all(TOKEN not in path.read_text(encoding='utf-8')
               for path in (app.root / 'state' / 'translation').rglob('*.json'))
    requests = len(transport['calls'])
    if phase == 'complete':
        output = tmp_path / 'restored-complete.zip'
        assert batches.export(identity, output)['status'] == 'complete'
        assert output.is_file()
    else:
        # Exact old approval/root identity remains inert after relocation.
        with pytest.raises(PermissionError, match='moved or restored'):
            batches.resume(identity)
        assert not (tmp_path / 'implicit-output.zip').exists()
    assert len(transport['calls']) == requests


def test_expired_completed_batch_backups_remain_readable_without_renewal(batch_env, tmp_path, monkeypatch, transport):
    from carr_library.assistant import providers
    e = batch_env
    identity, _ = prepared(e)
    e.batches.resume(identity)
    expiry = max(row['job']['authorization']['expires_at'] for row in e.batches._load(identity)['members'])
    monkeypatch.setattr(providers, 'time', lambda: expiry + 1000)
    before = len(transport['calls'])
    folder = tmp_path / 'expired-complete'
    checksum = e.app.backup(folder)
    restored = restore_backup(folder, tmp_path / 'restored-expired', checksum)
    app = LibraryApp(restored)
    service = TranslationService(app.root / 'state' / 'translation', library=app)
    batches = TranslationBatchService(service)
    assert batches.get(identity)['status'] == 'complete'
    assert batches.export(identity, tmp_path / 'expired.zip')['status'] == 'complete'
    assert len(transport['calls']) == before


@pytest.mark.parametrize('change', ['child-title', 'parent-source-metadata', 'orphan-control', 'unknown-file'])
def test_batch_namespace_and_resigned_graph_tampering_fail_before_restore(batch_env, tmp_path, change):
    e = batch_env
    identity, manifest = prepared(e)
    folder = tmp_path / 'backup'
    checksum = e.app.backup(folder)
    if change == 'child-title':
        name = 'state/translation/jobs/' + manifest['members'][0]['job']['id'] + '.json'
        value = json.loads((folder / name).read_bytes())
        value['title'] = 'An individually valid but different child'
        checksum = rehash_backup(folder, name, value)
    elif change == 'parent-source-metadata':
        name = 'state/translation/batches/' + identity + '.json'
        value = json.loads((folder / name).read_bytes())
        value['members'][0]['source_metadata']['title'] = 'Not the copied source metadata'
        value['review']['items'][0]['source_metadata']['title'] = 'Not the copied source metadata'
        value['review_sha256'] = sha256(canonical(value['review']).encode()).hexdigest()
        value['manifest_sha256'] = sha256(canonical({key: item for key, item in value.items()
            if key not in ('updated', 'manifest_sha256')}).encode()).hexdigest()
        checksum = rehash_backup(folder, name, value)
    else:
        manifest_path = folder / 'manifest.json'
        bundle = json.loads(manifest_path.read_bytes())
        name = 'state/translation/batches/' + ('f' * 32 + '.control.json' if change == 'orphan-control' else 'opaque.txt')
        raw = b'{"command":"pause"}\n' if change == 'orphan-control' else b'Unrecognized state'
        (folder / name).write_bytes(raw)
        bundle['sidecars'].append({'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
        bundle['sidecars'].sort(key=lambda row: row['filename'])
        data = (canonical(bundle) + '\n').encode()
        manifest_path.write_bytes(data)
        checksum = sha256(data).hexdigest()
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, checksum)
    assert not destination.exists()


def test_recomputed_local_receipt_hash_cannot_override_restored_independent_usage_ledger(batch_env, tmp_path):
    e = batch_env
    identity, manifest = prepared(e)
    e.batches.resume(identity)
    folder = tmp_path / 'backup'
    e.app.backup(folder)
    child_id = manifest['members'][0]['job']['id']
    name = 'state/translation/jobs/' + child_id + '.json'
    value = json.loads((folder / name).read_bytes())
    row = value['receipts'][0]
    row['text'] = 'A forged translation with its own matching local hashes.'
    row['translation_sha256'] = sha256(row['text'].encode()).hexdigest()
    row['decoded_translation_sha256'] = translation_receipt_sha256(row['text'], row['detected_source'])
    checksum = rehash_backup(folder, name, value)
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    app = LibraryApp(restored)
    batches = TranslationBatchService(TranslationService(app.root / 'state' / 'translation', library=app))
    with pytest.raises(ValueError):
        batches.get(identity)
    with pytest.raises(ValueError):
        batches.export(identity, tmp_path / 'forged.zip')
    assert not (tmp_path / 'forged.zip').exists()


def test_passive_batch_requires_its_exact_copied_catalog_even_before_child_preparation(batch_env, tmp_path):
    e = batch_env
    identity, manifest = prepared(e)
    for member in manifest['members']:
        e.service.jobs.path(member['job']['id']).unlink()
    name = e.batches.jobs.path(identity)
    value = json.loads(name.read_bytes())
    value['members'][0]['source_metadata']['author'] = 'Invented author'
    value['review']['items'][0]['source_metadata']['author'] = 'Invented author'
    value['review_sha256'] = sha256(canonical(value['review']).encode()).hexdigest()
    value['manifest_sha256'] = sha256(canonical({key: item for key, item in value.items()
        if key not in ('updated', 'manifest_sha256')}).encode()).hexdigest()
    name.write_text(canonical(value) + '\n', encoding='utf-8')
    with pytest.raises(ValueError):
        e.app.backup(tmp_path / 'rejected-source-snapshot')
