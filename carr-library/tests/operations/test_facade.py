"""Views reuse actual import/assistant/studio stores without starting workers."""
from pathlib import Path
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.durable_state import atomic_json, read_json
from carr_library.operations import OperationFacade, ResourceBroker, ResourceRequest
from carr_library.speech import VoiceRegistry
from carr_library.studio.render import ProductionService


def test_actual_existing_stores_share_view_and_controls_without_resubmission(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Shared operation view')
    other = app.create_workspace('Another library')
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir()
    outputs.mkdir()
    source = inputs / 'source.txt'
    source.write_text('Preserved original body that must not enter a dashboard.', encoding='utf-8')
    imported = app.plan_import_batch(workspace, [source])
    other_imported = app.plan_import_batch(other, [source])
    assistant = AssistantService(app)
    plan = assistant.preview('List books', Scope(workspace, (), outputs))
    assistant_id = assistant.submit(plan)
    studio = ProductionService(app.root / 'state' / 'studio', voices=VoiceRegistry([]))
    project = studio.create('[Pause: 1]', title='No engine is needed for this saved plan')
    studio_id = studio.submit(project, outputs)
    broker = ResourceBroker.for_app(app.root)
    facade = OperationFacade(app, broker=broker, studio=studio, assistant=assistant)
    before_source = source.read_bytes()
    with broker.reserve(ResourceRequest(), operation_id='studio:' + studio_id, timeout=0):
        views = facade.list(workspace=workspace)
        assert {view.key for view in views} == {
            'import:' + imported['batch_id'], 'assistant:' + assistant_id, 'studio:' + studio_id}
        assert next(view for view in views if view.kind == 'studio').live is True
        assert next(view for view in views if view.kind == 'assistant').live is None
        assert all('Preserved original' not in repr(view) for view in views)
        assert facade.get('studio', studio_id).status == 'pending'
        facade.control('studio', studio_id, 'pause')
        assert read_json(studio.jobs_root / studio_id / 'control.json') == {'action': 'pause'}
        assert facade.get('studio', studio_id).status == 'pending'  # requested != acknowledged
        facade.control('assistant', assistant_id, 'cancel')
        assert assistant.store.control(assistant_id) == 'cancel'
        with pytest.raises(PermissionError):
            facade.get('import', other_imported['batch_id'], workspace=workspace)
        with pytest.raises(ValueError, match='resume'):
            facade.control('studio', studio_id, 'resume')
        with pytest.raises(ValueError, match='does not expose'):
            facade.control('import', imported['batch_id'], 'cancel')
    assert source.read_bytes() == before_source
    assert len(assistant.jobs()) == 1 and len(studio.jobs()) == 1
    assert not list(outputs.iterdir())
    assert not tuple(studio.cache.iterdir())


def test_actual_completed_assistant_and_corruption_are_reported_honestly(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Read-only dashboard')
    service = AssistantService(app)
    identity = service.submit(service.preview('List books', Scope(workspace)))
    service.run(identity)
    facade = OperationFacade(app, assistant=service)
    view = facade.get('assistant', identity)
    assert (view.status, view.completed, view.total, view.live) == ('complete', 1, 1, None)
    assert view.controls == ()
    # A dashboard must neither execute injected text nor display diagnostic
    # bodies that could contain private document or credential material.
    service.store.path(identity).write_text('SECRET malformed original body', encoding='utf-8')
    broken = facade.list()[0]
    assert broken.status == 'attention' and broken.controls == ()
    assert 'SECRET' not in repr(broken)


def test_translation_and_asr_checkpoint_contracts_have_no_provider_side_effects(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    translation_id, asr_id = uuid4().hex, uuid4().hex
    translation = app.root / 'state' / 'translation' / 'jobs' / (translation_id + '.json')
    transcription = app.root / 'state' / 'transcription' / 'jobs' / asr_id / 'job.json'
    # These are schema fixtures for summary adaptation, not translation/ASR
    # inference evidence. Original bodies and provider data stay out of views.
    atomic_json(translation, {'schema': 'carr-assistant-job/1', 'kind': 'translation/1',
        'id': translation_id, 'status': 'paused', 'receipts': [{'text': 'private translation'}],
        'chunks': ['private original', 'unfinished'], 'updated': '2026-11-27T00:00:00Z',
        'profile': {'credential_ref': 'opaque-private-ref'}})
    atomic_json(transcription, {'schema': 'resonance-transcription-job/1', 'id': asr_id,
        'status': 'interrupted', 'done': 1, 'total': 2, 'updated': 1000,
        'results': [{'text': 'private transcript'}], 'sources': ['private source']})
    values = OperationFacade(app).list()
    assert {(value.kind, value.completed, value.total) for value in values} == {
        ('translation', 1, 2), ('asr', 1, 2)}
    assert all(value.live is None and value.controls == () for value in values)
    assert all('private' not in repr(value) for value in values)


def test_facade_rejects_traversal_and_unknown_workspace(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    facade = OperationFacade(app)
    with pytest.raises(ValueError):
        facade.get('assistant', '../../source')
    with pytest.raises(ValueError):
        facade.get('shell', 'tool')
    with pytest.raises((ValueError, PermissionError)):
        facade.list(workspace='not-an-existing-library')
    assert not (app.root / 'runtime').exists()  # read-only views create no broker or job store



def _detail_job(tmp_path):
    """Real saved producer envelope; no inference or live resource admission."""
    from types import SimpleNamespace
    from carr_library.audio import AudioBudget

    class InventoryOnly:
        def voices(self):
            return (SimpleNamespace(id='private:voice-sentinel'),)

    app = LibraryApp(tmp_path / 'detail-library')
    output = tmp_path / 'private-output-sentinel'
    output.mkdir()
    service = ProductionService(app.root / 'state/studio', voices=InventoryOnly())
    project = service.create('Narrator: PRIVATE_SOURCE_SENTINEL.',
        title='PRIVATE_TITLE_SENTINEL', voice_ids=('private:voice-sentinel',))
    identity = service.submit(project, output, format='flac', stems=True)
    saved = service.get(identity)
    _, plan = service._resource_plan(project, saved,
                                     SimpleNamespace(budget=AudioBudget(threads=2)))
    # This is a saved declaration fixture, not evidence of actual GPU admission.
    plan['request'].update(gpu_device='private-gpu-sentinel', gpu_memory_bytes=4096,
                           devices=['private-device-sentinel'])
    for key in ('cpu_basis', 'memory_basis', 'disk_basis', 'enforcement'):
        plan[key] = 'PRIVATE_BASIS_SENTINEL'
    saved.update(resource_plan=plan,
        runtime={'credential_ref': 'PRIVATE_CREDENTIAL_SENTINEL',
                 'installation': str(output / 'private-model-sentinel')},
        error='PRIVATE_ERROR_SENTINEL', progress='PRIVATE_PROGRESS_SENTINEL')
    service._save(saved)
    path = service.jobs_root / identity / 'job.json'
    return app, service, identity, path, saved


def test_studio_detail_projection_is_single_read_frozen_redacted_and_passive(tmp_path, monkeypatch):
    from dataclasses import FrozenInstanceError, asdict
    import json
    import os
    from carr_library import runtime_settings
    from carr_library.audio import AudioRuntime
    from carr_library.operations import facade as module
    from carr_library.studio.render import ENGINE_SCHEMA, _operation_sha256
    from carr_library.ui.operations_dialog import operation_details_text

    app, service, identity, path, saved = _detail_job(tmp_path)
    original = path.read_bytes()
    reads, real_read = [], module.read_json
    def observed_read(value):
        reads.append(value)
        return real_read(value)
    monkeypatch.setattr(module, 'read_json', observed_read)
    def forbidden(*args, **kwargs):
        pytest.fail('Reading saved details started a coordinator, provider or audio runtime.')
    monkeypatch.setattr(ResourceBroker, '__init__', forbidden)
    monkeypatch.setattr(ProductionService, '__init__', forbidden)
    monkeypatch.setattr(AudioRuntime, '__init__', forbidden)
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)

    private_roots = (Path(saved['destination']).parent, service.cache)
    def guarded(function):
        def call(value, *args, **kwargs):
            if isinstance(value, (str, Path)):
                candidate = Path(value)
                if any(candidate == root or candidate.is_relative_to(root)
                       for root in private_roots):
                    pytest.fail('Saved detail projection inspected a referenced external asset.')
            return function(value, *args, **kwargs)
        return call
    with monkeypatch.context() as guard:
        for owner, name in ((Path, 'open'), (Path, 'stat'), (Path, 'lstat'), (os, 'open')):
            guard.setattr(owner, name, guarded(getattr(owner, name)))
        view = OperationFacade(app, studio=service).get('studio', identity)
        rendered = operation_details_text(view)
    assert reads == [path]
    assert view.status == 'pending' and view.live is None
    assert view.details.handler_schema == ENGINE_SCHEMA and view.details.adapter is None
    assert view.details.operation_plan_sha256 == saved['operation_sha256'] == _operation_sha256(saved)
    assert view.details.settings_sha256 is None
    assert view.details.project_fingerprint == saved['fingerprint']
    resources = view.details.resource_plan
    request = saved['resource_plan']['request']
    assert resources.cpu_slots == 2 and resources.memory_bytes == request['memory_bytes']
    assert resources.gpu_requested is True and resources.gpu_memory_bytes == 4096
    assert resources.exclusive_device_count == 1
    assert resources.disk_reservation_bytes == (
        request['disk_bytes'], *(size for _, size in request['additional_disks']))
    assert resources.source_count == 0
    assert resources.estimated_duration_seconds == saved['resource_plan']['estimated_duration']
    assert view.details.completed_artifacts == ()
    assert view.details.completed_artifact_count is view.details.omitted_artifact_count is None
    serialized = json.dumps(asdict(view), ensure_ascii=False)
    for secret in ('PRIVATE_', 'private:voice', 'private-gpu', 'private-device',
                   str(app.root), str(Path(saved['destination']).parent), str(service.cache)):
        assert secret not in serialized and secret not in rendered
    assert 'current output files have not been checked' in rendered
    assert 'declarations, not measured use' in view.details.note
    with pytest.raises(FrozenInstanceError):
        view.details.settings_sha256 = '0' * 64
    with pytest.raises(FrozenInstanceError):
        resources.cpu_slots = 100
    assert path.read_bytes() == original


@pytest.mark.parametrize('change', [
    'operation-digest', 'format', 'stems', 'project-fingerprint', 'resource-schema',
    'resource-type', 'relative-disk', 'artifact-digest', 'artifact-path',
    'partial-envelope', 'unknown-field', 'runtime-type',
])
def test_invalid_canonical_details_are_redacted_attention_and_cannot_control(
    tmp_path, monkeypatch, change
):
    from copy import deepcopy
    from carr_library.operations import ResourceStateError

    app, service, identity, path, saved = _detail_job(tmp_path)
    damaged = deepcopy(saved)
    if change == 'operation-digest':
        damaged['operation_sha256'] = '0' * 64
    elif change == 'format':
        damaged['format'] = 'wav'
    elif change == 'stems':
        damaged['stems'] = False
    elif change == 'project-fingerprint':
        damaged['fingerprint'] = '0' * 64
    elif change == 'resource-schema':
        damaged['resource_plan']['schema'] = True
    elif change == 'resource-type':
        damaged['resource_plan']['request']['cpu_slots'] = True
    elif change == 'relative-disk':
        damaged['resource_plan']['request']['disk_path'] = 'PRIVATE_RELATIVE_SENTINEL'
    elif change.startswith('artifact'):
        damaged['artifacts'] = [dict(path=str(Path(saved['destination']) / 'private.wav'),
                                    sha256='a' * 64, size=44)]
        damaged['artifacts'][0]['sha256' if change == 'artifact-digest' else 'path'] = 'PRIVATE_INVALID_SENTINEL'
    elif change == 'partial-envelope':
        del damaged['project']
    elif change == 'unknown-field':
        damaged['private_unreviewed_setting'] = 'PRIVATE_UNKNOWN_SENTINEL'
    else:
        damaged['runtime'] = 'PRIVATE_RUNTIME_SENTINEL'
    atomic_json(path, damaged, replace=True)
    raw = path.read_bytes()
    monkeypatch.setattr(service, 'request_pause',
                        lambda *_: pytest.fail('Invalid metadata reached a control.'))
    facade = OperationFacade(app, studio=service)
    with pytest.raises(ResourceStateError, match='canonical checks') as caught:
        facade.get('studio', identity)
    assert 'PRIVATE' not in str(caught.value) and str(app.root) not in str(caught.value)
    with pytest.raises(ResourceStateError, match='canonical checks'):
        facade.control('studio', identity, 'pause')
    row, = facade.list()
    assert row.status == 'attention' and row.controls == ()
    assert row.details.adapter is None and row.details.operation_plan_sha256 is None
    assert row.details.settings_sha256 is None and row.details.resource_plan is None
    assert 'PRIVATE' not in repr(row)
    assert path.read_bytes() == raw


@pytest.mark.parametrize('minimal', [False, True], ids=['canonical-legacy', 'historical-summary'])
def test_legacy_studio_details_stay_unknown_without_changing_existing_controls(tmp_path, minimal):
    app, service, identity, path, saved = _detail_job(tmp_path)
    if minimal:
        saved = {key: saved[key] for key in ('schema', 'id', 'status', 'nodes', 'updated')}
    else:
        del saved['operation_sha256']
    atomic_json(path, saved, replace=True)
    raw = path.read_bytes()
    facade = OperationFacade(app, studio=service)
    view = facade.get('studio', identity)
    assert view.status == 'pending' and view.controls == ('pause', 'cancel')
    assert view.details.operation_plan_sha256 is view.details.settings_sha256 is None
    assert view.details.completed_artifact_count is view.details.omitted_artifact_count is None
    if minimal:
        assert view.details.adapter is None and view.details.resource_plan is None
        assert 'unavailable' in view.details.note
    else:
        assert 'no saved operation-plan digest' in view.details.note
    if not minimal:
        facade.control('studio', identity, 'pause')
        assert read_json(service.jobs_root / identity / 'control.json') == {'action': 'pause'}
    else:
        assert not (service.jobs_root / identity / 'control.json').exists()
    assert path.read_bytes() == raw


def test_recorded_artifact_projection_is_capped_and_never_claims_live_verification(tmp_path):
    from dataclasses import FrozenInstanceError, asdict
    from hashlib import sha256
    import json
    from carr_library.operations.facade import _MAX_DETAIL_ARTIFACTS

    app, service, identity, path, saved = _detail_job(tmp_path)
    # Deliberately nonexistent files: this validates only their stored identities.
    saved['status'] = 'complete'
    saved['artifacts'] = [
        dict(path=str(Path(saved['destination']) / f'PRIVATE_ARTIFACT_{index}.wav'),
             sha256=sha256(str(index).encode('ascii')).hexdigest(), size=44 + index)
        for index in range(_MAX_DETAIL_ARTIFACTS + 3)]
    atomic_json(path, saved, replace=True)
    raw = path.read_bytes()
    assert not Path(saved['destination']).exists()
    view = OperationFacade(app).get('studio', identity)
    details = view.details
    assert view.status == 'complete' and view.live is None and view.controls == ()
    assert len(details.completed_artifacts) == _MAX_DETAIL_ARTIFACTS == 64
    assert details.completed_artifact_count == 67 and details.omitted_artifact_count == 3
    for index, value in enumerate(details.completed_artifacts):
        row = saved['artifacts'][index]
        assert (value.ordinal, value.sha256, value.size) == (index + 1, row['sha256'], row['size'])
    with pytest.raises(FrozenInstanceError):
        details.completed_artifacts[0].sha256 = '0' * 64
    encoded = json.dumps(asdict(view)).encode('utf-8')
    assert len(encoded) < 32768 and b'PRIVATE' not in encoded
    assert 'artifact bytes and current assets were not checked' in details.note
    assert path.read_bytes() == raw and not Path(saved['destination']).exists()


def test_validator_failure_body_never_enters_the_shared_view(tmp_path, monkeypatch):
    from carr_library import backup_producers
    from carr_library.operations import ResourceStateError

    app, service, identity, path, saved = _detail_job(tmp_path)
    calls = []
    def refused(database, name, value):
        calls.append((database, name, value))
        raise ValueError('PRIVATE_VALIDATOR_SENTINEL ' + str(Path(saved['destination'])))
    monkeypatch.setattr(backup_producers, 'validate', refused)
    with pytest.raises(ResourceStateError, match='canonical checks') as caught:
        OperationFacade(app).get('studio', identity)
    assert caught.value.__suppress_context__
    assert 'PRIVATE' not in str(caught.value) and str(Path(saved['destination'])) not in str(caught.value)
    assert len(calls) == 1 and calls[0][0] is None
    assert calls[0][1] == path.relative_to(app.root).as_posix()
    assert calls[0][2] == saved


@pytest.mark.parametrize('status', [[], {}], ids=['array-state', 'object-state'])
def test_malformed_status_is_attention_without_hiding_other_saved_operations(
        tmp_path, monkeypatch, status):
    from carr_library.operations import ResourceStateError
    from carr_library.studio.project import Project

    app, service, identity, path, saved = _detail_job(tmp_path)
    other = service.submit(Project.from_dict(saved['project']),
                           Path(saved['destination']).parent,
                           format=saved['format'], stems=saved['stems'])
    atomic_json(path, {**saved, 'status': status}, replace=True)
    original = path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('A malformed saved state reached an owner control.')
    monkeypatch.setattr(service, 'cancel', forbidden)
    facade = OperationFacade(app, studio=service)
    with pytest.raises(ResourceStateError, match='Unknown saved operation state'):
        facade.get('studio', identity)
    with pytest.raises(ResourceStateError, match='Unknown saved operation state'):
        facade.control('studio', identity, 'cancel')
    rows = {row.id: row for row in facade.list()}
    assert set(rows) == {identity, other}
    assert rows[identity].status == 'attention' and rows[identity].controls == ()
    assert rows[identity].details.resource_plan is None
    assert rows[other].status == 'pending' and rows[other].controls == ('pause', 'cancel')
    assert 'PRIVATE' not in repr(tuple(rows.values()))
    assert path.read_bytes() == original
