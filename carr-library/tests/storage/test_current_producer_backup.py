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


def retained_studio_plan(tmp_path, plan):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Retained resource declaration')
    voices = SimpleNamespace(voices=lambda: (SimpleNamespace(id='fixture-voice'),))
    service = ProductionService(app.root / 'state' / 'studio', voices=voices)
    project = service.create('Exact historical source', voice_ids=('fixture-voice',))
    destination = tmp_path / 'old-output'
    destination.mkdir()
    identity = service.submit(project, destination, stems=False)
    value = service.get(identity)
    value['resource_plan'] = plan
    service._save(value)
    source = service.jobs_root / identity / 'job.json'
    assert service.close() is True
    return app, source, identity, destination


def inert_resource_plan():
    from carr_library.operations.models import ResourceRequest
    return {'schema': 1, 'request': ResourceRequest(cpu_slots=1).to_dict(),
            'cpu_basis': 'Historical explicit allowance',
            'memory_basis': 'Historical declared estimate',
            'disk_basis': 'Historical declared estimate',
            'enforcement': 'Cooperative declaration; not measured consumption'}


@pytest.mark.parametrize('source_bound', (False, True))
def test_known_resource_plan_shapes_roundtrip_as_inert_reviewable_metadata(
        tmp_path, monkeypatch, source_bound):
    from carr_library.operations import ResourceBroker
    plan = inert_resource_plan()
    if source_bound:
        plan.update(source_sha256={}, estimated_duration=1.25)
    app, source, identity, destination = retained_studio_plan(tmp_path, plan)
    original = source.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Backing up an inert resource plan must not acquire or start a worker.')
    monkeypatch.setattr(ResourceBroker, 'for_app', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    observed = read_json(restored / 'state' / 'studio' / 'jobs' / identity / 'job.json')
    assert observed['resource_plan'] == plan
    assert observed['status'] == 'waiting-input'
    assert observed['destination'] == ''
    assert observed['recovery']['requires_output_directory'] is True
    assert source.read_bytes() == original
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize('damage', ('source-only', 'duration-only', 'relative-disk', 'unknown-field', 'boolean-schema'))
def test_historical_plan_recognition_does_not_admit_partial_or_untyped_variants(tmp_path, damage):
    plan = inert_resource_plan()
    if damage == 'source-only':
        plan['source_sha256'] = {}
    elif damage == 'duration-only':
        plan['estimated_duration'] = 1
    elif damage == 'relative-disk':
        plan['request'].update(disk_bytes=1, disk_path='relative-folder')
    elif damage == 'unknown-field':
        plan['grant'] = 'not authority'
    else:
        plan['schema'] = True
    app, source, _identity, _destination = retained_studio_plan(tmp_path, plan)
    before = source.read_bytes()
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize('name', ('', 'Ukrainian'))
def test_retained_advanced_language_inventory_keeps_its_exact_provider_label(tmp_path, name):
    from carr_library.assistant.models import canonical
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Historical Advanced language inventory')
    rows = [{'language': 'uk', 'name': name, 'source': True, 'target': True}]
    value = {'schema': 'carr-translation-languages/1', 'profile': 'historical-inventory',
             'endpoint': 'https://translation.googleapis.com/v3/projects/123456/locations/global',
             'model': 'general/nmt', 'fetched_at': '2026-11-27T10:00:00Z',
             'languages': rows, 'languages_sha256': sha256(canonical(rows).encode()).hexdigest(),
             'qualification': 'provider-declared; not acoustic or translation-quality verification'}
    path = app.root / 'state' / 'translation' / 'languages-historical-inventory.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)
    before = path.read_bytes()
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    assert (restored / path.relative_to(app.root)).read_bytes() == before
    assert path.read_bytes() == before
