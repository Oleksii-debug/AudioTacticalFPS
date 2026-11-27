"""Regressions for actual current producer fields in complete common4 backups."""
from decimal import Decimal
from hashlib import sha256
import json
import subprocess
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.credentials import WindowsCredentialVault
from carr_library.assistant.profiles import ProviderProfile
from carr_library.assistant.providers import Authorization, Gateway, UsageLedger
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.studio.render import ProductionService


def ledger_data(app, root):
    profile = ProviderProfile('ledger-fixture', 'Synthetic retained translation', 'google-translation-v2',
        'https://translation.googleapis.com/language/translate/v2', 'nmt',
        capabilities=('translation.text',))
    approval = Authorization('a' * 32, 'historical-translation',
        ((profile.id, profile.endpoint),), ('translation.text',), ('synthetic-test',),
        '0', 1, 1, request_hashes=('b' * 64,))
    ledger = UsageLedger(app.root / 'state' / root)
    ledger.reserve(approval, 'translation_0', 'b' * 64, Decimal('0'), profile, 'translation.text')
    ledger.finish(approval.id, 'translation_0', status='complete', output_hash='c' * 64,
                  usage={'characters': 23, 'decoded_translation_sha256': 'd' * 64})
    return ledger, approval


@pytest.mark.parametrize('root', ('providers/usage', 'translation/provider/usage', 'gateway/usage'))
def test_actual_usage_ledgers_keep_decoded_evidence_without_reopening_remote_authority(
        tmp_path, monkeypatch, root):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Historical ledger')
    ledger, approval = ledger_data(app, root)
    path = ledger._path(approval.id)
    raw, value = path.read_bytes(), ledger.get(approval.id)

    def forbidden(*args, **kwargs):
        pytest.fail('A retained usage ledger must not open a gateway, credential or process.')

    monkeypatch.setattr(Gateway, '__init__', forbidden)
    monkeypatch.setattr(WindowsCredentialVault, 'get', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    name = path.relative_to(app.root).as_posix()
    entry, = [row for row in manifest['sidecars'] if row['filename'] == name]
    assert entry['sha256'] == sha256(raw).hexdigest() and entry['size_bytes'] == len(raw)
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    reopened = UsageLedger(restored / 'state' / root)
    assert reopened.get(approval.id) == value
    assert reopened._path(approval.id).read_bytes() == path.read_bytes() == raw
    assert value['operations']['translation_0']['usage']['decoded_translation_sha256'] == 'd' * 64


@pytest.mark.parametrize('damage', ('decoded-digest', 'usage-value', 'row-field', 'schema'))
def test_gateway_path_reuses_the_strict_existing_usage_schema(tmp_path, damage):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Invalid ledger')
    ledger, approval = ledger_data(app, 'gateway/usage')
    value = ledger.get(approval.id)
    row = value['operations']['translation_0']
    if damage == 'decoded-digest':
        row['usage']['decoded_translation_sha256'] = 'unverified'
    elif damage == 'usage-value':
        row['usage']['characters'] = True
    elif damage == 'row-field':
        row['credential_secret'] = 'not an accepted ledger field'
    else:
        value['schema'] = 'unknown'
    atomic_json(ledger._path(approval.id), value, replace=True)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists()


@pytest.mark.parametrize('disk_bytes', (True, 1.5, 1024**2 - 1, 2**63))
def test_studio_disk_budget_does_not_weaken_current_job_validation(tmp_path, disk_bytes):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Studio budget')
    voices = SimpleNamespace(voices=lambda: (SimpleNamespace(id='fixture-voice'),))
    service = ProductionService(app.root / 'state' / 'studio', voices=voices)
    project = service.create('Exact synthetic source', voice_ids=('fixture-voice',))
    output = tmp_path / 'output'
    output.mkdir()
    identity = service.submit(project, output, stems=False)
    state = service.get(identity)
    state['total_disk_bytes'] = disk_bytes
    service._save(state)
    path = service.jobs_root / identity / 'job.json'
    before = path.read_bytes()
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert path.read_bytes() == before
    # The retained JSON uses arrays; the immutable project model uses tuples.
    # Compare every field through the same JSON wire shape, not Python container types.
    expected_project = json.loads(json.dumps(project.to_dict(), ensure_ascii=False, allow_nan=False))
    assert read_json(path)['project'] == expected_project
    assert service.close() is True
