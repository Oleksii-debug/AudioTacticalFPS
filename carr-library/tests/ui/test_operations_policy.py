"""User resource policy is parsed explicitly and persisted by the shared broker."""
import pytest

from carr_library.operations import ResourceBroker, ResourceRequest
from carr_library.ui.operations_dialog import policy_from_fields, MIB, GIB


def test_explicit_native_policy_persists_without_enabling_unlisted_gpu(tmp_path):
    value = policy_from_fields(cpu=2, memory_mib=1024, disk_gib=3, free_mib=0,
        gpu='cuda:0=512\ncuda:1=1024', max_leases=8)
    broker = ResourceBroker.for_app(tmp_path)
    broker.configure(value)
    reopened = ResourceBroker.for_app(tmp_path)
    assert reopened.limits == value
    assert value.memory_bytes == 1024*MIB and value.disk_bytes == 3*GIB
    assert value.gpu_devices == (('cuda:0', 512*MIB), ('cuda:1', 1024*MIB))
    with reopened.reserve(ResourceRequest(cpu_slots=1, gpu_device='cuda:1', gpu_memory_bytes=128*MIB),
                          operation_id='asr:synthetic', timeout=0):
        assert len(reopened.snapshot()['leases']) == 1
    assert not reopened.snapshot()['leases']
    disabled = policy_from_fields(cpu=2, memory_mib=1024, disk_gib=3, free_mib=0,
        gpu='', max_leases=8)
    assert disabled.gpu_devices == ()


@pytest.mark.parametrize('gpu', ['cuda:0', 'cuda:0=-1', 'cuda:0=NaN', 'cuda:0=1.5',
    'cuda:0=0', 'cuda:0=1\ncuda:0=2', '../device=5', 'CUDA:0=100', 'a'*4097])
def test_invalid_gpu_policy_is_rejected_without_persistence(gpu):
    with pytest.raises(ValueError):
        policy_from_fields(cpu=2, memory_mib=1024, disk_gib=2, free_mib=256,
            gpu=gpu, max_leases=8)


@pytest.mark.parametrize('alive,index', [(True, 0), (False, 0), (True, -1), (True, 1)],
                         ids=['current', 'closed', 'no-selection', 'stale-selection'])
def test_selected_operation_uses_readable_snapshot_without_refreshing_owners(alive, index):
    import ast
    import inspect
    from types import SimpleNamespace
    from carr_library.operations.facade import (ArtifactIdentityView, OperationDetails,
                                                OperationView, ResourcePlanView)
    from carr_library.ui import operations_dialog as module

    details = OperationDetails(handler_schema='studio-local-dag-v1',
        operation_plan_sha256='a' * 64, project_fingerprint='b' * 64,
        resource_plan=ResourcePlanView(2, 1024, True, 4096, (8192, 16384), 1, 3, 1.5),
        completed_artifacts=(ArtifactIdentityView(1, 'c' * 64, 44),),
        completed_artifact_count=2, omitted_artifact_count=1, restore_requires_new_output=True,
        note='Stored declarations only; current artifacts were not checked.')
    identity = '1' * 32
    row = OperationView('studio', identity, None, 'complete', 2, None,
        'Stored production checkpoint; accepted nodes', '1', (),
        'state/studio/jobs/' + identity + '/job.json', details=details)
    calls, texts = [], []
    def selection():
        calls.append('selection')
        return index
    environment = dict(alive=alive, jobs=(row,),
        listing=SimpleNamespace(GetSelection=selection),
        detail=SimpleNamespace(ChangeValue=texts.append),
        controls=lambda: calls.append('controls'),
        operation_details_text=module.operation_details_text)
    parsed = ast.parse(inspect.getsource(module.show_operations))
    callback, = [node for node in ast.walk(parsed)
                 if isinstance(node, ast.FunctionDef) and node.name == 'selected']
    compiled = ast.fix_missing_locations(ast.Module(body=[callback], type_ignores=[]))
    exec(compile(compiled, module.__file__, 'exec'), environment)
    environment['selected']()
    assert calls == (['selection', 'controls'] if alive else [])
    if alive and index == 0:
        text, = texts
        assert text == module.operation_details_text(row)
        assert 'Saved operation metadata; current output files have not been checked.' in text
        assert 'Current canonical handler: studio-local-dag-v1' in text
        assert 'Workspace: Unknown; not recorded' in text and 'Saved update time: 1' in text
        assert 'Available controls: None in the current context' in text
        assert 'Selected engine or model adapter: Unknown; not recorded' in text
        assert 'Saved operation-plan SHA-256: ' + 'a' * 64 in text
        assert 'Separate settings SHA-256: Unknown; not recorded' in text
        assert 'Declared CPU slots: 2' in text and 'Declared memory bytes: 1024' in text
        assert 'Declared disk reservations in bytes: 8192, 16384' in text
        assert 'Exclusive device count: 1' in text and 'Recorded input count: 3' in text
        assert 'Recorded completed artifact identities: showing 1 of 2' in text
        assert 'Artifact 1: 44 bytes; SHA-256 ' + 'c' * 64 in text
        assert 'Omitted artifact identities: 1' in text
        assert 'Saved restore marker requires a new output: Yes' in text
        assert '{' not in text and 'resource_plan' not in text
    else:
        assert texts == []


def test_historical_snapshot_shows_unknown_budgets_instead_of_zero_values():
    from carr_library.operations.facade import OperationView
    from carr_library.ui.operations_dialog import operation_details_text

    row = OperationView('studio', 'historical', None, 'paused', 0, None,
                        'Stored production checkpoint; accepted nodes', '', ('pause', 'cancel'),
                        'state/studio/jobs/historical/job.json')
    text = operation_details_text(row)
    assert 'Declared resource plan: Unknown; not recorded' in text
    assert 'Available controls: pause, cancel' in text
    assert 'Saved update time: Unknown; not recorded' in text
    assert 'Declared CPU slots: 0' not in text and 'Declared memory bytes: 0' not in text
    assert 'Completed artifact identities: Unavailable in this saved checkpoint' in text
    assert 'Saved operation-plan SHA-256: Unknown; not recorded' in text
    assert 'Saved restore marker requires a new output: Unknown; not recorded' in text
    assert 'Unknown values do not authorize execution or output reuse.' in text
