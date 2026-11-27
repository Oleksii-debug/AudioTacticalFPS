"""Portable policy identity without copying live resource authority."""
import json
import shutil

import pytest

from carr_library.operations import ResourceBroker, ResourceConflict, ResourceRequest
from carr_library.operations._state import atomic_json


def test_complete_library_copy_uses_its_own_policy_and_no_original_runtime(tmp_path):
    root = tmp_path / 'original'
    root.mkdir()
    original = ResourceBroker.for_app(root)
    copied = tmp_path / 'copied'
    shutil.copytree(root, copied)
    shutil.rmtree(root)
    restored = ResourceBroker.for_app(copied)
    assert restored.policy_path == copied / 'state/operations/resources.json'
    with restored.reserve(ResourceRequest(cpu_slots=1), operation_id='document:restored'):
        assert len(restored.snapshot()['leases']) == 1
    assert restored.snapshot()['leases'] == []


def test_legacy_absolute_application_binding_migrates_without_opening_old_root(tmp_path):
    root = tmp_path / 'copied'
    root.mkdir()
    broker = ResourceBroker.for_app(root)
    atomic_json(broker.root / '.policy-location.json', {
        'schema': 'carr-resource-policy-location/1',
        'path': str(tmp_path / 'unavailable-original/state/operations/resources.json'),
    })
    migrated = ResourceBroker.for_app(root)
    assert migrated.policy_path == broker.policy_path
    assert json.loads((broker.root / '.policy-location.json').read_bytes()) == {
        'schema': 'carr-application-resource-policy/1',
        'path': 'state/operations/resources.json',
    }


def test_legacy_binding_migration_waits_for_actual_native_reservation_owner(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    broker = ResourceBroker(root / 'runtime/resources',
        policy_path=root / 'state/operations/resources.json')
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='document:owned'):
        with pytest.raises(ResourceConflict):
            ResourceBroker.for_app(root)
        assert len(broker.snapshot()['leases']) == 1
    migrated = ResourceBroker.for_app(root)
    assert migrated.snapshot()['leases'] == []


@pytest.mark.parametrize('old_path', ['another-policy.json', '/elsewhere/custom.json'])
def test_application_migration_never_adopts_an_arbitrary_policy_binding(tmp_path, old_path):
    root = tmp_path / 'library'
    root.mkdir()
    broker = ResourceBroker.for_app(root)
    previous = {'schema': 'carr-resource-policy-location/1', 'path': old_path}
    atomic_json(broker.root / '.policy-location.json', previous)
    with pytest.raises(ResourceConflict):
        ResourceBroker.for_app(root)
    assert json.loads((broker.root / '.policy-location.json').read_bytes()) == previous


def test_generic_coordinator_cannot_rebind_an_application_policy(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    broker = ResourceBroker.for_app(root)
    with pytest.raises(ResourceConflict):
        ResourceBroker(broker.root, policy_path=root / 'alternate.json')
    assert not (root / 'alternate.json').exists()


@pytest.mark.parametrize('application', [False, True])
@pytest.mark.parametrize('active', [False, True])
def test_existing_null_binding_is_corruption_even_without_a_live_lease(tmp_path, application, active):
    from contextlib import nullcontext
    root = tmp_path / 'library'
    root.mkdir()
    create = (lambda: ResourceBroker.for_app(root)) if application else (
        lambda: ResourceBroker(root / 'coordinator'))
    broker = create()
    held = (broker.reserve(ResourceRequest(cpu_slots=1), operation_id='document:held')
            if active else nullcontext())
    with held:
        binding = broker.root / '.policy-location.json'
        binding.write_bytes(b'null')
        with pytest.raises(ResourceConflict):
            create()
        assert binding.read_bytes() == b'null'
        if active:
            assert len(broker.snapshot()['leases']) == 1
